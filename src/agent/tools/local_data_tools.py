# -*- coding: utf-8 -*-
"""本地事实层（L2）的只读工具：公司档案与全市场横截面。

为什么需要这两个工具（见 docs/architecture/OVERVIEW.md 第 5 节与第 7 节第一步）：

问股此前 20 个工具里只有 1 个读本地库，`get_stock_info` 直接
``manager.get_fundamental_context()`` 实时抓取。后果是三层：

1. 每次问股都重新抓一遍（慢），且受数据源限流影响 —— 实测东方财富已拒绝连接；
2. 库里积累的 5565 条全市场横截面、company_profile / company_metrics 对问股完全不可见；
3. 数据源一抖，问股就"失败"，而本地明明有可用副本。

这两个工具把 L2 接上。它们是**纯 DB 读取**：没有 network_read，不消耗任何配额，
断网时依然可用。数据缺失时如实说缺什么，而不是把 NULL 当作 0 或空字符串展示 ——
这正是"AI 得知道自己没有数据"的落点。
"""

from __future__ import annotations

import json
import logging
import math
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.agent.tools.registry import ToolDefinition, ToolParameter, ToolPolicy

logger = logging.getLogger(__name__)

# 纯本地读取：只有 db_read，没有 network_read，也没有任何写入。
_LOCAL_DATA_READ_POLICY = ToolPolicy.declared(
    read_only=True,
    side_effects=["db_read"],
    permissions=["local_data:read"],
    scope_dimensions=["stock"],
)

# ---- 新鲜度阈值（get_stock_info 本地优先用，也在此定义以便单点维护）----
#
# 为什么是 30 / 7 天：
# * 公司档案（行业层级、地域、概念）是**慢变量** —— 申万行业分类与业务概念按季度
#   调整，30 天足以覆盖一次正常的按需/定时同步，又不至于让一份半年没更新的行业
#   分类被当成"现在的行业"。
# * 估值（PE/PB/市值）是**快变量** —— 价格每天都在动，7 天是"最近一周内有一次成功
#   抓取"的底线。更宽会让 agent 拿一周前的市值当今天的市值讲，更严则大量票因为
#   上游偶发失败而全部回退联网，失去本地优先的意义。
#
# 注意：阈值判定用的是**数据里的时间戳**（profile.updated_at / metrics.as_of），
# 不是文件时间，也不是当前时间 ± 随机量。
PROFILE_MAX_AGE_DAYS = 30
METRICS_MAX_AGE_DAYS = 7

# 少于这么多可用（非 NULL）样本时，分位数没有统计意义，不予返回。
# 实测全市场横截面 5565 行，100 只是"样本明显不完整"的下限：低于它说明快照没抓全，
# 此时给出一个看似精确的百分位反而会误导判断。
MIN_USABLE_ROWS = 100

# metric 白名单：**只允许真实列名**，用户输入不做任何 SQL 拼接。
# 之所以用"metric -> 固定列名"的映射，而不是把入参格式化进 SQL：问股的入参来自
# 模型，属于不可信输入，一旦拼接就是注入面。映射表是唯一入口，未命中直接拒绝。
# 注意 volume_ratio 故意不在表内：实测该列 5565 行全为 NULL（上游没给），
# 放进来只会让调用方拿到"没有数据"的分位。
_METRIC_COLUMNS = {
    "pe_ratio": "pe_ttm",
    "pb_ratio": "pb_ratio",
    "total_mv": "total_mv",
    "amount": "amount",
    "change_pct": "change_pct",
    "price": "price",
    "turnover_rate": "turnover_rate",
}

# get_market_context 支持的 scope。industry 恒不可用（见 _handle_get_market_context）。
_ALLOWED_SCOPES = ("market", "industry")


# ============================================================
# 共用小工具
# ============================================================

def _resolve_canonical_id(code: str) -> Optional[str]:
    """裸码 → 库里的 canonical_id（sz002567 形式）。

    复用 ``DatabaseManager._derive_canonical_id`` 这个既定契约，而不是自己拼
    市场前缀 —— 它已经能正确处理 600519 / 000063.SZ / SZ000063 / sh600519 /
    指数代码 等各种输入，自己再写一份必然与写入路径分叉。
    """
    try:
        from src.storage import DatabaseManager

        return DatabaseManager.get_instance()._derive_canonical_id(str(code or "").strip())
    except Exception as exc:  # pragma: no cover - import/DB guard
        logger.warning("[LocalData] canonical_id 推导失败 code=%r: %s", code, exc)
        return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """把库里读到的时间戳解析成 datetime（失败返回 None，不抛）。

    company_profile.updated_at 是**原始 SQL 写入的 ISO 字符串**（带 T 与 +00:00），
    company_metrics.as_of 是 DATE。两者形状不同，且前者经 ORM 读取会被 SQLite 的
    DateTime 结果处理器拒绝，所以统一按字符串解析。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text_value = str(value).strip()
    if not text_value:
        return None
    if text_value.endswith("Z"):
        text_value = text_value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text_value)
    except ValueError:
        try:
            return datetime.strptime(text_value[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _age_in_days(value: Any) -> Optional[int]:
    """时间戳距今多少天（只按 UTC 日期算）。解析不出来返回 None。"""
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    parsed_date = parsed.date() if isinstance(parsed, datetime) else parsed
    # 用北京日期算"几天前"：UTC 日期在北京 16:00 后已跨天。
    return (datetime.now().date() - parsed_date).days


def evaluate_local_freshness(local_profile: Dict[str, Any]) -> List[str]:
    """判断一份本地档案是否"够新"，返回**过期原因列表**（空 = 新鲜）。

    被 get_stock_info 的本地优先逻辑复用。判不出来（时间戳缺失/不可解析）时按
    **过期**处理：宁可多走一次联网，也不要把一份来历不明的时间戳当新鲜数据用。
    返回的是可读原因，便于写进响应的 ``data_source_reason``，让调用方知道为什么
    明明有本地数据却走了实时抓取。
    """
    reasons: List[str] = []

    profile_age = _age_in_days(local_profile.get("profile_updated_at"))
    if profile_age is None:
        reasons.append("profile_updated_at_missing_or_unparsable")
    elif profile_age > PROFILE_MAX_AGE_DAYS:
        reasons.append(
            f"profile_stale:{profile_age}d>{PROFILE_MAX_AGE_DAYS}d"
        )

    valuation = local_profile.get("valuation") or {}
    if not valuation:
        reasons.append("no_local_valuation")
    else:
        metrics_age = _age_in_days(valuation.get("as_of"))
        if metrics_age is None:
            reasons.append("metrics_as_of_missing_or_unparsable")
        elif metrics_age > METRICS_MAX_AGE_DAYS:
            reasons.append(
                f"metrics_stale:{metrics_age}d>{METRICS_MAX_AGE_DAYS}d"
            )
    return reasons


# ============================================================
# get_company_profile
# ============================================================

def _handle_get_company_profile(stock_code: str) -> Dict[str, Any]:
    """读 company_profile + 最新一期 company_metrics（纯本地）。

    返回行业层级、地域、业务概念、最新估值。没有档案行时返回
    ``success=False`` + ``reason="no_local_profile"`` —— 空成功会让模型以为
    "这只票没有行业"，而事实是"我们没抓过这只票的档案"，两者需要区分。

    缺失的字段单独列在 ``missing_fields`` 里：industry_l1/l2/l3 允许为 NULL，
    company_metrics.pe_ttm/pb 也常常因为上游抓取失败而为 NULL。把它们当 0 或
    空字符串展示，等于把"没有数据"伪装成"数据是空的"。
    """
    code = str(stock_code or "").strip()
    if not code:
        return {
            "success": False,
            "reason": "missing_stock_code",
            "error": "stock_code 不能为空",
        }

    try:
        from sqlalchemy import text

        from src.storage import DatabaseManager
    except Exception as exc:  # pragma: no cover - import guard
        return {"success": False, "reason": "db_unavailable", "error": f"数据库不可用: {exc}"}

    canonical_id = _resolve_canonical_id(code)
    if not canonical_id:
        # 连身份都定不下来（如未登记的代码），不可能有档案行。
        return {
            "success": False,
            "stock_code": code,
            "reason": "unresolved_stock_identity",
            "error": (
                f"无法把 {code!r} 解析成库内 canonical_id（sz002567 形式），"
                "因此不可能有本地档案"
            ),
        }

    try:
        manager = DatabaseManager.get_instance()
        with manager._engine.connect() as conn:
            profile = conn.execute(
                text(
                    "SELECT canonical_id, code, name, market, industry_l1, industry_l2, "
                    "industry_l3, region, concepts_json, updated_at "
                    "FROM company_profile WHERE canonical_id = :cid"
                ),
                {"cid": canonical_id},
            ).fetchone()

            if profile is None:
                # 档案按需/定时补写，没抓过的票落在这是**正常状态**，不是故障。
                return {
                    "success": False,
                    "stock_code": code,
                    "canonical_id": canonical_id,
                    "reason": "no_local_profile",
                    "error": (
                        f"本地 company_profile 没有 {canonical_id} 的档案行；"
                        "这只票尚未被抓取过档案（正常状态，不是查询错误）"
                    ),
                }

            metrics = conn.execute(
                text(
                    "SELECT as_of, pe_ttm, pb, total_mv, circ_mv FROM company_metrics "
                    "WHERE canonical_id = :cid ORDER BY as_of DESC LIMIT 1"
                ),
                {"cid": canonical_id},
            ).fetchone()
    except Exception as exc:
        logger.warning("[LocalData] 读取公司档案失败 code=%r: %s", code, exc, exc_info=True)
        return {"success": False, "reason": "db_query_failed", "error": f"读取公司档案失败: {exc}"}

    row = profile._mapping
    industry_l1 = row["industry_l1"]
    industry_l2 = row["industry_l2"]
    industry_l3 = row["industry_l3"]
    region = row["region"]

    concepts: List[str] = []
    if row["concepts_json"]:
        try:
            parsed = json.loads(row["concepts_json"])
            if isinstance(parsed, list):
                concepts = [str(item) for item in parsed if str(item or "").strip()]
        except (TypeError, ValueError):
            # concepts_json 损坏时不当成"没有概念"，而是如实标注解析失败。
            concepts = []

    valuation: Optional[Dict[str, Any]] = None
    metrics_as_of: Optional[str] = None
    if metrics is not None:
        metrics_row = metrics._mapping
        metrics_as_of = str(metrics_row["as_of"]) if metrics_row["as_of"] is not None else None
        valuation = {
            "as_of": metrics_as_of,
            "pe_ttm": metrics_row["pe_ttm"],
            "pb": metrics_row["pb"],
            "total_mv": metrics_row["total_mv"],
            "circ_mv": metrics_row["circ_mv"],
        }

    # 逐个字段说清楚"缺什么"，而不是让调用方自己从 null 里猜。
    missing_fields: List[str] = []
    for field_name, field_value in (
        ("industry_l1", industry_l1),
        ("industry_l2", industry_l2),
        ("industry_l3", industry_l3),
        ("region", region),
    ):
        if field_value is None or str(field_value).strip() == "":
            missing_fields.append(field_name)
    if not concepts:
        missing_fields.append("concepts")
    if valuation is None:
        missing_fields.append("valuation")
    else:
        for field_name in ("pe_ttm", "pb", "total_mv", "circ_mv"):
            if valuation.get(field_name) is None:
                missing_fields.append(f"valuation.{field_name}")

    limitations: List[str] = []
    if row["concepts_json"] and not concepts:
        limitations.append("concepts_json_unparsable")
    if valuation is not None and valuation.get("pe_ttm") is None and valuation.get("pb") is None:
        # 与 stock_profile_service 的既有语义保持一致：最新一期估值全空 =
        # 那次上游抓取失败，NULL 是可解释的，不是"PE 就是空的"。
        limitations.append("metrics_fetch_failed_for_latest_as_of")

    return {
        "success": True,
        "stock_code": code,
        "canonical_id": canonical_id,
        "code": row["code"],
        "name": row["name"],
        "market": row["market"],
        "industry": {"l1": industry_l1, "l2": industry_l2, "l3": industry_l3},
        "region": region,
        "concepts": concepts,
        "concepts_count": len(concepts),
        "valuation": valuation,
        "valuation_as_of": metrics_as_of,
        "profile_updated_at": str(row["updated_at"]) if row["updated_at"] else None,
        "missing_fields": missing_fields,
        "limitations": limitations,
        # 明确写出 NULL 的含义：这是上游那次抓取失败留下的空，不是 0，也不是空行业。
        "note": (
            "本地只读快照。missing_fields 里的字段是**确实没有数据**（NULL），"
            "不是 0 或空字符串；company_metrics 取 as_of 最新的一行，"
            "该行 NULL 表示那一次估值抓取失败。"
        ),
    }


get_company_profile_tool = ToolDefinition(
    name="get_company_profile",
    description=(
        "Read the locally stored company profile (industry levels l1/l2/l3, region, "
        "business concepts) and the latest locally stored valuation (pe_ttm, pb, "
        "total_mv, circ_mv with its as_of date). Pure local DB read: no network, no "
        "API key. Returns success=false with reason='no_local_profile' when this "
        "stock has never been profiled. Fields that are genuinely missing are listed "
        "in 'missing_fields' — treat them as absent data, never as zero."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519' or 'sz002567'",
        ),
    ],
    handler=_handle_get_company_profile,
    category="data",
    policy=_LOCAL_DATA_READ_POLICY,
)


# ============================================================
# get_market_context
# ============================================================

def _quantile(sorted_values: List[float], q: float) -> Optional[float]:
    """线性插值分位数（纯 Python，避免为几行统计引入依赖）。"""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * weight


def _handle_get_market_context(
    stock_code: str,
    metric: str = "pe_ratio",
    scope: str = "market",
) -> Dict[str, Any]:
    """回答"这只票在全市场什么位置"（读 market_snapshot 最近一份**可用**快照）。

    四条硬规则，都是为了不给出"看起来精确、其实没有意义"的数字：

    1. **NULL 不参与分位**。metric 为 NULL 的行直接排除；把 NULL 当 0 会让
       分位整体失真。分母是"可用行数"，不是总行数。
    2. **选日期不能只看 MAX(as_of)**。最新的一天可能是一次跑了一半/降级数据源写出的
       残缺快照。按 as_of 从新到旧找第一份该 metric 可用行数 >= 100 的快照；被跳过的
       日期连同可用行数与原因一起返回。若没有任何一天达标，明确失败并列出所有被拒绝
       的日期，而不是拿垃圾日期算分位、也不是谎称"没有数据"。
    3. **可用行数 < 100 时不给分位**。快照没抓全时，百分位是没有统计意义的假精确。
    4. **metric 走白名单**。入参来自模型，不做任何 SQL 拼接；未命中白名单直接拒绝。

    ``scope="industry"`` 恒不可用：实测 market_snapshot.industry 全为 NULL
    （上游没下发行业）。不提供行业相对分位，而不是给一个"全行业 0 只"的空结果。
    """
    code = str(stock_code or "").strip()
    metric_key = str(metric or "").strip()
    scope_key = str(scope or "market").strip().lower()

    if scope_key not in _ALLOWED_SCOPES:
        return {
            "success": False,
            "reason": "invalid_scope",
            "error": f"scope {scope!r} 不受支持",
            "allowed_scopes": list(_ALLOWED_SCOPES),
        }
    if scope_key == "industry":
        return {
            "success": False,
            "reason": "industry_context_unavailable",
            "error": (
                "不做行业相对分位：market_snapshot.industry 目前 5565 行全为 NULL"
                "（上游未提供行业字段），按行业分组会得到空集，任何行业分位都是假的。"
                "需要行业信息请用 get_company_profile。"
            ),
            "metric": metric_key,
        }

    if not code:
        return {"success": False, "reason": "missing_stock_code", "error": "stock_code 不能为空"}

    column = _METRIC_COLUMNS.get(metric_key)
    if column is None:
        return {
            "success": False,
            "reason": "metric_not_allowed",
            "error": (
                f"metric {metric!r} 不在白名单，拒绝执行；"
                "只接受真实存在的横截面数值列"
            ),
            "allowed_metrics": sorted(_METRIC_COLUMNS),
        }

    try:
        from sqlalchemy import text

        from src.storage import DatabaseManager
    except Exception as exc:  # pragma: no cover - import guard
        return {"success": False, "reason": "db_unavailable", "error": f"数据库不可用: {exc}"}

    canonical_id = _resolve_canonical_id(code)

    try:
        manager = DatabaseManager.get_instance()
        with manager._engine.connect() as conn:
            # 固定 SELECT：metric 列名永远来自模块级白名单，绝不与入参拼接。
            # 一次取全量（实测 5565 行、10 列，内存开销可忽略），按 as_of 在 Python 里
            # 分组，这样既能算每天每个 metric 的可用行数，又不需要把列名拼进 SQL。
            all_rows = conn.execute(
                text(
                    # 2026-10-21：pe_ratio 列已改名 pe_ttm（语义就是 TTM 市盈率）。
                    # 下游统一用 pe_ttm，避免"列名对不上"导致 db_query_failed。
                    "SELECT as_of, canonical_id, code, name, pe_ttm, pb_ratio, total_mv, "
                    "amount, change_pct, price, turnover_rate FROM market_snapshot"
                )
            ).fetchall()
    except Exception as exc:
        logger.warning("[LocalData] 读取全市场横截面失败 code=%r: %s", code, exc, exc_info=True)
        return {"success": False, "reason": "db_query_failed", "error": f"读取全市场横截面失败: {exc}"}

    if not all_rows:
        return {
            "success": False,
            "stock_code": code,
            "reason": "no_market_snapshot",
            "error": "本地 market_snapshot 为空，没有任何横截面数据",
        }

    rows_by_date: Dict[str, List[Any]] = {}
    for row in all_rows:
        rows_by_date.setdefault(str(row._mapping["as_of"]), []).append(row)

    # 规则 2（选日期）：**"最新的一天"不等于"最新可用的一天"**。
    #
    # market_snapshot 每次选股运行都会写一份。一次跑了一半、或者降级到某个残缺的
    # 数据源，就会给当天写出一份很小甚至指标全空的快照。如果只看 MAX(as_of)，这个
    # 垃圾日期会把前一天 5565 行的真实横截面整个盖住，工具就会回答"没有数据" ——
    # 而这正是"假精确/假缺失"里更糟的一种。
    #
    # 因此按 as_of 从新到旧找第一份【该 metric 可用行数 >= MIN_USABLE_ROWS】的快照；
    # 被跳过的日期连同各自的可用行数与原因一并返回，而不是悄悄换一天。
    chosen_as_of: Optional[str] = None
    chosen_rows: List[Any] = []
    rejected_dates: List[Dict[str, Any]] = []
    for as_of_key in sorted(rows_by_date.keys(), reverse=True):
        date_rows = rows_by_date[as_of_key]
        usable_for_metric = sum(
            1 for row in date_rows if row._mapping[column] is not None
        )
        if usable_for_metric >= MIN_USABLE_ROWS:
            chosen_as_of = as_of_key
            chosen_rows = date_rows
            break
        rejected_dates.append({
            "as_of": as_of_key,
            "row_count": len(date_rows),
            "usable_count_for_metric": usable_for_metric,
            "reason": "below_min_usable_rows",
        })

    if chosen_as_of is None:
        return {
            "success": False,
            "stock_code": code,
            "canonical_id": _resolve_canonical_id(code),
            "metric": metric_key,
            "reason": "no_usable_snapshot",
            "error": (
                f"没有任何一天的 {metric_key} 可用行数达到 {MIN_USABLE_ROWS}，"
                "分位数没有统计意义，因此不返回；rejected_dates 列出了每一天被拒绝的原因。"
            ),
            "rejected_dates": rejected_dates[:10],
            "rejected_date_count": len(rejected_dates),
        }

    rows = chosen_rows
    latest_as_of = chosen_as_of

    values: List[float] = []
    target_value: Optional[float] = None
    target_meta: Optional[Dict[str, Any]] = None
    target_present = False
    for row in rows:
        mapping = row._mapping
        raw_value = mapping[column]
        is_target = bool(canonical_id) and mapping["canonical_id"] == canonical_id
        if is_target:
            target_present = True
            target_meta = {"code": mapping["code"], "name": mapping["name"]}
        if raw_value is None:
            # 规则 1：NULL 既不进分母，也不当 0。
            continue
        try:
            numeric = float(raw_value)
        except (TypeError, ValueError):
            continue
        values.append(numeric)
        if is_target:
            target_value = numeric

    total_rows = len(rows)
    usable_count = len(values)
    excluded_null_count = total_rows - usable_count
    values.sort()

    limitations: List[str] = []
    if usable_count == 0:
        limitations.append("no_usable_metric_rows")
    if canonical_id is None:
        limitations.append("unresolved_stock_identity")
    elif not target_present:
        # 这两种"没有值"性质不同，必须分开说：
        # 缺席 = 这只票根本不在当天快照里；NULL = 在快照里但那一次没取到该指标。
        limitations.append("target_not_in_snapshot")
    elif target_value is None:
        limitations.append("target_metric_missing")
    if rejected_dates:
        # 有更新的一天但因为样本不足被跳过 —— 必须让调用方知道用的是哪一天、跳过了哪天。
        limitations.append("skipped_recent_snapshot_dates")

    # 分布永远返回字典（即使 0 条可用），这样调用方能看见"总行数 vs 可用行数"，
    # 而不是拿到一个 None 只能猜是"没数据"还是"查询失败"。
    distribution: Dict[str, Any] = {
        "usable_count": usable_count,
        "excluded_null_count": excluded_null_count,
        "total_rows": total_rows,
        "min": values[0] if values else None,
        "q1": _quantile(values, 0.25),
        "median": _quantile(values, 0.5),
        "q3": _quantile(values, 0.75),
        "max": values[-1] if values else None,
    }

    position: Optional[Dict[str, Any]] = None
    note = ""
    if target_value is None:
        note = (
            f"{canonical_id or code} 在 {latest_as_of} 的 {metric_key} 没有值（NULL）"
            "或在当天快照中缺席，因此无法给出它在市场中的位置；上方的市场分布仍然可用。"
        )
    elif usable_count < MIN_USABLE_ROWS:
        # 防御性分支：选日期时已经保证 >= MIN_USABLE_ROWS，正常不会走到这里。
        limitations.append("insufficient_market_rows")
        note = (
            f"{latest_as_of} 只有 {usable_count} 条 {metric_key} 可用（阈值 {MIN_USABLE_ROWS}），"
            "样本明显不完整，百分位没有统计意义，故不予返回。"
        )
    elif usable_count:
        # 严格小于目标值的行数 = "跑赢了几个同行"；<= 目标值的行数用于标准百分位秩。
        peers_beaten = bisect_left(values, target_value)
        less_or_equal = bisect_right(values, target_value)
        position = {
            "percentile_rank": round(less_or_equal / usable_count * 100, 2),
            "peers_beaten": peers_beaten,
            "peers_total": usable_count,
            "definition": "percentile_rank = (metric <= 本股的行数) / 可用行数 × 100；NULL 不参与",
        }

    if rejected_dates and not note:
        skipped = ", ".join(
            f"{item['as_of']}({item['usable_count_for_metric']}条)"
            for item in rejected_dates[:3]
        )
        note = (
            f"使用 {latest_as_of}：更新的快照因 {metric_key} 可用行数不足 "
            f"{MIN_USABLE_ROWS} 被跳过（{skipped}）。industry 字段全为 NULL，"
            "因此不提供行业相对分位。"
        )

    return {
        "success": True,
        "stock_code": code,
        "canonical_id": canonical_id,
        "metric": metric_key,
        "scope": scope_key,
        "as_of": str(latest_as_of),
        "target": {
            "present": target_present,
            "found": target_value is not None,
            "value": target_value,
            **(target_meta or {}),
        },
        "market": distribution,
        "position": position,
        "rejected_dates": rejected_dates[:10],
        "rejected_date_count": len(rejected_dates),
        "limitations": limitations,
        "note": note or (
            "market_snapshot 的最近一份可用快照（只读）。industry 字段全为 NULL，"
            "因此不提供行业相对分位。"
        ),
    }


get_market_context_tool = ToolDefinition(
    name="get_market_context",
    description=(
        "Answer 'where does this stock sit in the whole market' from the locally "
        "stored market cross-section. Uses the most recent snapshot date that has at "
        "least 100 non-null rows for the requested metric, so a partial/degraded "
        "snapshot written for today cannot hide yesterday's full one; skipped dates "
        "are listed in rejected_dates with the reason. Returns the stock's percentile "
        "rank and how many peers it beats for one metric, plus the market's median "
        "and quartiles. Pure local DB read: no network. NULL rows are excluded from "
        "the percentile. metric must be one of: pe_ratio, pb_ratio, total_mv, amount, "
        "change_pct, price, turnover_rate. Industry-relative percentile is NOT offered "
        "because market_snapshot.industry is entirely empty."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519' or 'sz002567'",
        ),
        ToolParameter(
            name="metric",
            type="string",
            description="Metric to rank by (default pe_ratio)",
            required=False,
            default="pe_ratio",
            enum=sorted(_METRIC_COLUMNS),
        ),
        ToolParameter(
            name="scope",
            type="string",
            description=(
                "Ranking universe. 'market' (default) is available; 'industry' is "
                "currently unavailable (market_snapshot.industry is empty)."
            ),
            required=False,
            default="market",
            enum=list(_ALLOWED_SCOPES),
        ),
    ],
    handler=_handle_get_market_context,
    category="market",
    policy=_LOCAL_DATA_READ_POLICY,
)


ALL_LOCAL_DATA_TOOLS = [
    get_company_profile_tool,
    get_market_context_tool,
]
