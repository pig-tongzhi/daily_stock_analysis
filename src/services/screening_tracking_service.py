# -*- coding: utf-8 -*-
"""选股结果复盘：把"上次选的股票后来怎么样了"变成可回答的问题。

为什么需要这个模块：入选记录（``screening_runs.result_json.candidates[]``，含选股
时点价格）与前瞻价格（``stock_daily``）此前从未被连起来。结果是 24 次选股选中 49
只不同的股票，其中只有 1 只有 K 线 —— 命中率、平均收益、最大回撤这些复盘问题
全都无法回答。这个模块只读既有数据、**不新增任何表**：入选来自 result_json，
前瞻价格来自 stock_daily，用 canonical_id（``sz002567`` 形式）对齐。

诚实优先：拿不到前瞻价格的入选必须显式计入 ``untrackable`` 并单列原因，
绝不能当成 0% 收益、也不能悄悄从分母里丢掉 —— "48/49 不可见"正是当前要治的病。

收益本身也不够：「+0.47%」在同期大盘 +2% 时是跑输。因此本模块还会拿
``stock_daily`` 里的基准指数序列（沪深300 等，由 ``benchmark_index_service``
落库）对**每一只入选按其自己的窗口**算超额收益。入选发生在不同日期，用同一个
全局区间去比基准会同时算错个股答案与汇总数字；拿不到基准时一律 ``null`` +
机器原因码，绝不填 0、也绝不换窗口。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 入选拿不到前瞻收益的原因码。前端据此本地化，后端不返回人话文案。
UNTRACKABLE_MISSING_PICK_PRICE = "missing_pick_price"
UNTRACKABLE_UNKNOWN_IDENTITY = "unknown_identity"
UNTRACKABLE_NO_DAILY_ROWS = "no_daily_rows"
UNTRACKABLE_NO_FORWARD_BARS = "no_forward_bars"

# 基准对比拿不到的原因码。同样只给机器码，人话由前端本地化。
# 这三种必须分开：处理动作不同（补抓指数 / 补抓更早或更新的指数 / 先让入选可复盘）。
BENCHMARK_DISABLED = "benchmark_disabled"
BENCHMARK_SERIES_MISSING = "benchmark_series_missing"
BENCHMARK_WINDOW_NOT_COVERED = "benchmark_window_not_covered"
BENCHMARK_PICK_UNTRACKABLE = "pick_untrackable"

# 胜出率（跑赢基准的比例）的分母口径说明。
BEAT_BASIS_TRACKABLE = "benchmark_comparable_trackable_picks"
BEAT_BASIS_NONE = "no_comparable_picks"

DEFAULT_TRACKING_DAYS = 60
DEFAULT_MAX_RUNS = 500
# SQLite 的绑定变量上限（旧版本 999）；分批查询留出余量。
_QUERY_CHUNK_SIZE = 400
# stock_daily 里看起来像指数的 canonical_id：A 股指数走 sh000xxx / sz399xxx / sh950xxx。
_INDEX_CANONICAL_PATTERN = re.compile(r"^(sh000\d{3}|sz399\d{3}|sh950\d{3})$")
# 单次查询最多回看多少次选股运行。窗口过滤在 Python 侧做（见 _load_pick_rows）。
_MAX_RUNS_SCAN = 2000


def _coerce_float(value: Any) -> Optional[float]:
    """把价格类字段转成 float；空值/占位符一律 None，不塞 0。

    塞 0 会让"没有价格"看起来像"0 元买入"，收益率会被算成无穷大或假 100%。
    NULL/None 才是诚实的表达。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            text = value.strip().replace(",", "")
            if not text or text in {"-", "--", "N/A", "nan", "None", "null"}:
                return None
            value = text
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    if number in (float("inf"), float("-inf")):
        return None
    return number


def _coerce_date(value: Any) -> Optional[date]:
    """把 created_at / date 列转成 date。字符串与 datetime 都要能处理。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # 兼容 'YYYY-MM-DD HH:MM:SS.ffffff' / 'YYYY-MM-DDTHH:MM:SS+00:00' / 'YYYY-MM-DD'
    normalized = text.replace(" ", "T", 1) if " " in text else text
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_database_manager(db_manager: Any = None) -> Any:
    if db_manager is not None:
        return db_manager
    from src.storage import DatabaseManager

    return DatabaseManager.get_instance()


def _resolve_tracking_window(
    config: Any = None,
    *,
    days: Optional[int] = None,
    max_codes: Optional[int] = None,
) -> Tuple[int, int, bool]:
    """解析回看窗口 / 追加上限 / 是否启用。显式入参优先于配置。"""
    enabled = True
    configured_days = DEFAULT_TRACKING_DAYS
    configured_max = 200
    if config is not None:
        enabled = bool(getattr(config, "screening_pick_tracking_enabled", True))
        configured_days = getattr(config, "screening_pick_tracking_days", DEFAULT_TRACKING_DAYS)
        configured_max = getattr(config, "screening_pick_tracking_max_codes", 200)
    resolved_days = days if days is not None else configured_days
    resolved_max = max_codes if max_codes is not None else configured_max
    try:
        resolved_days = int(resolved_days)
    except (TypeError, ValueError):
        resolved_days = DEFAULT_TRACKING_DAYS
    try:
        resolved_max = int(resolved_max)
    except (TypeError, ValueError):
        resolved_max = 200
    if resolved_days <= 0:
        resolved_days = DEFAULT_TRACKING_DAYS
    if resolved_max <= 0:
        resolved_max = 200
    return resolved_days, resolved_max, enabled


def _decode_result_json(raw: Any) -> Optional[Dict[str, Any]]:
    """解析 result_json。畸形 JSON 返回 None —— 不能因为一次坏记录炸掉整次复盘。"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except Exception:
            return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _iter_candidate_dicts(result: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [item for item in candidates if isinstance(item, dict)]


def _fetch_recent_run_rows(
    db_manager: Any,
    *,
    days: int,
    runs_limit: int = _MAX_RUNS_SCAN,
) -> List[Dict[str, Any]]:
    """读取窗口内的选股运行（含 result_json）。

    窗口过滤放在 Python 侧：created_at 是 DATETIME 列，字符串比较在
    "同一天不同时刻" 上会踩到空格 vs 'T' 的字典序陷阱，解析成 date 再比较
    才是确定性的。
    """
    from sqlalchemy import text

    cutoff = (_utc_now() - timedelta(days=days)).date()
    statement = text(
        "SELECT run_id, strategy, market, created_at, result_json "
        "FROM screening_runs ORDER BY created_at DESC, id DESC LIMIT :limit"
    )
    with db_manager.get_session() as session:
        rows = session.execute(statement, {"limit": int(runs_limit)}).fetchall()

    selected: List[Dict[str, Any]] = []
    for row in rows:
        mapping = row._mapping if hasattr(row, "_mapping") else row
        run_date = _coerce_date(mapping["created_at"])
        if run_date is None or run_date < cutoff:
            continue
        selected.append(
            {
                "run_id": str(mapping["run_id"] or ""),
                "strategy": str(mapping["strategy"] or ""),
                "market": str(mapping["market"] or ""),
                "created_at": mapping["created_at"],
                "run_date": run_date,
                "result_json": mapping["result_json"],
            }
        )
    return selected


def load_pick_rows(
    *,
    config: Any = None,
    db_manager: Any = None,
    days: Optional[int] = None,
    runs_limit: int = _MAX_RUNS_SCAN,
) -> Tuple[List[Dict[str, Any]], int]:
    """把窗口内的选股运行摊平成入选行。

    返回 ``(picks, malformed_run_count)``。畸形 result_json 只跳过该次运行并计数，
    不抛异常、不污染其它运行的结果。
    """
    resolved_days, _max_codes, enabled = _resolve_tracking_window(config, days=days)
    if not enabled:
        return [], 0

    db = _resolve_database_manager(db_manager)
    picks: List[Dict[str, Any]] = []
    malformed = 0
    for run in _fetch_recent_run_rows(db, days=resolved_days, runs_limit=runs_limit):
        result = _decode_result_json(run["result_json"])
        if result is None:
            malformed += 1
            continue
        for candidate in _iter_candidate_dicts(result):
            code = str(candidate.get("code") or "").strip()
            if not code:
                continue
            picks.append(
                {
                    "run_id": run["run_id"],
                    "strategy": run["strategy"],
                    "market": run["market"],
                    "pick_date": run["run_date"],
                    "code": code,
                    "name": str(candidate.get("name") or "").strip(),
                    "pick_price": _coerce_float(candidate.get("price")),
                    "score": _coerce_float(candidate.get("score")),
                }
            )
    return picks, malformed


def list_recent_pick_codes(
    *,
    config: Any = None,
    db_manager: Any = None,
    days: Optional[int] = None,
    max_codes: Optional[int] = None,
) -> List[str]:
    """最近 N 天选股入选的代码（按时间倒序去重，带上限）。

    fail-open：任何解析/查询失败都返回空列表，让调用方退回原清单 ——
    行情刷新不该因为复盘数据坏掉而整体失败。
    """
    try:
        resolved_days, resolved_max, enabled = _resolve_tracking_window(
            config, days=days, max_codes=max_codes
        )
        if not enabled:
            return []
        picks, _malformed = load_pick_rows(
            config=config, db_manager=db_manager, days=resolved_days
        )
        ordered: List[str] = []
        seen: set[str] = set()
        for pick in picks:
            code = pick["code"]
            if code in seen:
                continue
            seen.add(code)
            ordered.append(code)
            if len(ordered) >= resolved_max:
                break
        return ordered
    except Exception:
        logger.warning(
            "[ScreeningTracking] 解析选股代码失败；本次行情刷新将退回原清单",
            exc_info=True,
        )
        return []


def _fetch_forward_bars(
    db_manager: Any,
    canonical_ids: Sequence[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """按 canonical_id 读取 stock_daily 日线。

    注意 stock_daily 的唯一约束是 (code, date)，不是 (canonical_id, date)：
    同一个 canonical_id 可能有多条历史别名行（如 000063 与 000063.SZ）。
    这里按 (canonical_id, date) 去重，保留 id 最大的一行（最近写入）。
    """
    from sqlalchemy import text

    unique_ids = [value for value in dict.fromkeys(canonical_ids) if value]
    bars: Dict[str, List[Dict[str, Any]]] = {}
    if not unique_ids:
        return bars

    for start in range(0, len(unique_ids), _QUERY_CHUNK_SIZE):
        chunk = unique_ids[start:start + _QUERY_CHUNK_SIZE]
        with db_manager.get_session() as session:
            rows = session.execute(
                text(
                    "SELECT id, canonical_id, date, close, high, low FROM stock_daily "
                    "WHERE canonical_id IN ("
                    + ",".join(f":id{index}" for index in range(len(chunk)))
                    + ")"
                ),
                {f"id{index}": value for index, value in enumerate(chunk)},
            ).fetchall()
        for row in rows:
            mapping = row._mapping if hasattr(row, "_mapping") else row
            canonical = str(mapping["canonical_id"] or "")
            bar_date = _coerce_date(mapping["date"])
            if not canonical or bar_date is None:
                continue
            bucket = bars.setdefault(canonical, [])
            bucket.append(
                {
                    "id": int(mapping["id"] or 0),
                    "date": bar_date,
                    "close": _coerce_float(mapping["close"]),
                    "high": _coerce_float(mapping["high"]),
                    "low": _coerce_float(mapping["low"]),
                }
            )
    # 去重 + 按日期升序，保证 max_gain / max_drawdown 的遍历是确定性的。
    deduped: Dict[str, List[Dict[str, Any]]] = {}
    for canonical, rows in bars.items():
        by_date: Dict[date, Dict[str, Any]] = {}
        for row in rows:
            existing = by_date.get(row["date"])
            if existing is None or row["id"] > existing["id"]:
                by_date[row["date"]] = row
        deduped[canonical] = [by_date[key] for key in sorted(by_date)]
    return deduped


def _compute_pick_performance(
    pick: Dict[str, Any],
    *,
    canonical_id: Optional[str],
    bars: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """算单只入选的前瞻表现；拿不到前瞻价格时返回 untrackable 结果。

    前瞻窗口 = 严格晚于入选日期的 K 线。选股价格是选股时点快照，
    同一天的收盘价不算前瞻收益（否则会凭空造出一个 ~0% 的"收益"）。
    """
    base: Dict[str, Any] = {
        "run_id": pick["run_id"],
        "strategy": pick["strategy"],
        "market": pick["market"],
        "code": pick["code"],
        "name": pick["name"],
        "pick_date": pick["pick_date"].isoformat(),
        "pick_price": _round(pick["pick_price"], 4),
        "score": pick["score"],
        "canonical_id": canonical_id,
        "trackable": False,
        "reason": None,
        "latest_close": None,
        "latest_date": None,
        "return_pct": None,
        "max_gain_pct": None,
        "max_drawdown_pct": None,
        "days_held": None,
        "forward_bars": 0,
    }

    if canonical_id is None:
        base["reason"] = UNTRACKABLE_UNKNOWN_IDENTITY
        return base
    if pick["pick_price"] is None or pick["pick_price"] <= 0:
        # 没有选股价格就没有收益率的分母；即使有 K 线也不能算。
        base["reason"] = UNTRACKABLE_MISSING_PICK_PRICE
        return base
    if not bars:
        base["reason"] = UNTRACKABLE_NO_DAILY_ROWS
        return base

    forward = [bar for bar in bars if bar["date"] > pick["pick_date"]]
    if not forward:
        # 有 K 线但都早于（或等于）入选日期：还需要新的行情才能复盘。
        base["reason"] = UNTRACKABLE_NO_FORWARD_BARS
        return base

    closes = [bar["close"] for bar in forward if bar["close"] is not None]
    if not closes:
        base["reason"] = UNTRACKABLE_NO_FORWARD_BARS
        return base

    pick_price = float(pick["pick_price"])
    latest_bar = forward[-1]
    latest_close = latest_bar["close"] if latest_bar["close"] is not None else closes[-1]
    highs = [bar["high"] for bar in forward if bar["high"] is not None] or closes
    lows = [bar["low"] for bar in forward if bar["low"] is not None] or closes

    base.update(
        {
            "trackable": True,
            "latest_close": _round(latest_close, 4),
            "latest_date": latest_bar["date"].isoformat(),
            "return_pct": _round((latest_close - pick_price) / pick_price * 100.0),
            "max_gain_pct": _round((max(highs) - pick_price) / pick_price * 100.0),
            "max_drawdown_pct": _round((min(lows) - pick_price) / pick_price * 100.0),
            "days_held": (latest_bar["date"] - pick["pick_date"]).days,
            "forward_bars": len(forward),
        }
    )
    return base


def _compute_benchmark_comparison(
    row: Dict[str, Any],
    *,
    enabled: bool,
    benchmark_code: Optional[str],
    index_bars: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """按入选**自己的窗口**算基准收益与超额收益。

    这是本模块最容易做错的地方：入选发生在不同日期，用同一个全局区间去比基准，
    等于拿"今天的行情"去评判"三周前的选股"。因此基准区间严格对齐该入选自己的
    窗口 —— 起点是入选日或之前最近的指数收盘（选股价就是入选日的快照价），
    终点是该入选最新前瞻 K 线的日期或之前最近的指数收盘。

    拿不到就如实报不可用并给出机器原因码：**绝不填 0**（0 会让"没数据"看起来像
    "与大盘持平"），也**绝不悄悄换一个窗口**（换窗口得到的超额收益不再是这个
    入选的）。
    """
    result: Dict[str, Any] = {
        "benchmark_code": benchmark_code,
        "benchmark_available": False,
        "benchmark_reason": None,
        "benchmark_start_date": None,
        "benchmark_start_close": None,
        "benchmark_end_date": None,
        "benchmark_end_close": None,
        "benchmark_return_pct": None,
        "excess_return_pct": None,
    }
    if not enabled or not benchmark_code:
        result["benchmark_reason"] = BENCHMARK_DISABLED if not enabled else BENCHMARK_SERIES_MISSING
        return result
    if not index_bars:
        result["benchmark_reason"] = BENCHMARK_SERIES_MISSING
        return result
    if not row.get("trackable") or not row.get("latest_date"):
        # 入选本身不可复盘 → 没有窗口可对齐，基准自然也无从谈起。
        result["benchmark_reason"] = BENCHMARK_PICK_UNTRACKABLE
        return result

    pick_date = _coerce_date(row.get("pick_date"))
    latest_date = _coerce_date(row.get("latest_date"))
    if pick_date is None or latest_date is None:
        result["benchmark_reason"] = BENCHMARK_WINDOW_NOT_COVERED
        return result

    start_bar = None
    for bar in index_bars:  # index_bars 已按日期升序
        if bar["date"] <= pick_date and bar.get("close") is not None:
            start_bar = bar
        elif bar["date"] > pick_date:
            break
    end_bar = None
    for bar in index_bars:
        if bar["date"] > latest_date:
            break
        if start_bar is not None and start_bar["date"] < bar["date"] and bar.get("close") is not None:
            end_bar = bar
    if start_bar is None or end_bar is None:
        # 起点或终点落在指数序列覆盖范围之外：说清楚是哪一段缺，而不是退回 0。
        result["benchmark_reason"] = BENCHMARK_WINDOW_NOT_COVERED
        return result

    start_close = float(start_bar["close"])
    end_close = float(end_bar["close"])
    if start_close <= 0:
        result["benchmark_reason"] = BENCHMARK_WINDOW_NOT_COVERED
        return result

    benchmark_return_pct = (end_close - start_close) / start_close * 100.0
    pick_return_pct = _coerce_float(row.get("return_pct"))
    result.update(
        {
            "benchmark_available": True,
            "benchmark_start_date": start_bar["date"].isoformat(),
            "benchmark_start_close": _round(start_close, 4),
            "benchmark_end_date": end_bar["date"].isoformat(),
            "benchmark_end_close": _round(end_close, 4),
            "benchmark_return_pct": _round(benchmark_return_pct),
            "excess_return_pct": (
                _round(pick_return_pct - benchmark_return_pct)
                if pick_return_pct is not None
                else None
            ),
        }
    )
    return result


def _aggregate(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """聚合一组入选。

    hit rate 的分母**只有 trackable**：把不可复盘的入选按 0% 计进分母会稀释
    命中率，凭空得出一个看起来更"稳"的假数字。untrackable 单独计数。
    """
    trackable = [row for row in rows if row.get("trackable")]
    untrackable = [row for row in rows if not row.get("trackable")]
    returns = [row["return_pct"] for row in trackable if row.get("return_pct") is not None]
    days = [row["days_held"] for row in trackable if row.get("days_held") is not None]
    hits = [value for value in returns if value > 0]

    # 基准对比只能统计"有基准且窗口被覆盖"的那些入选。分母单独给出，
    # 因为"跑赢比例 80%"在 5 个样本和 50 个样本上完全不是一回事。
    comparable = [
        row for row in trackable if row.get("excess_return_pct") is not None
    ]
    benchmark_returns = [
        row["benchmark_return_pct"] for row in comparable if row.get("benchmark_return_pct") is not None
    ]
    excess = [row["excess_return_pct"] for row in comparable]
    beats = [value for value in excess if value > 0]
    benchmark_reason_counts: Dict[str, int] = {}
    for row in trackable:
        reason = row.get("benchmark_reason")
        if reason:
            benchmark_reason_counts[reason] = benchmark_reason_counts.get(reason, 0) + 1

    aggregate: Dict[str, Any] = {
        "picks": len(rows),
        "trackable": len(trackable),
        "untrackable": len(untrackable),
        "hit_rate": _round(len(hits) / len(returns), 4) if returns else None,
        "hit_rate_basis": "trackable_only" if returns else "no_trackable_picks",
        "avg_return_pct": _round(sum(returns) / len(returns)) if returns else None,
        "median_return_pct": _round(median(returns)) if returns else None,
        "best_return_pct": _round(max(returns)) if returns else None,
        "worst_return_pct": _round(min(returns)) if returns else None,
        "avg_days_held": _round(sum(days) / len(days), 1) if days else None,
        # === 同期基准对比（每只入选用自己的窗口）===
        "benchmark_code": next(
            (row.get("benchmark_code") for row in rows if row.get("benchmark_code")), None
        ),
        "benchmark_comparable": len(comparable),
        "beat_benchmark_rate": _round(len(beats) / len(excess), 4) if excess else None,
        # 与 hit_rate_basis 同一套写法：分母口径是可读契约，必须显式给出。
        "beat_benchmark_basis": BEAT_BASIS_TRACKABLE if excess else BEAT_BASIS_NONE,
        "beat_benchmark_denominator": len(excess),
        "avg_benchmark_return_pct": (
            _round(sum(benchmark_returns) / len(benchmark_returns)) if benchmark_returns else None
        ),
        "median_benchmark_return_pct": _round(median(benchmark_returns)) if benchmark_returns else None,
        "avg_excess_return_pct": _round(sum(excess) / len(excess)) if excess else None,
        "median_excess_return_pct": _round(median(excess)) if excess else None,
        "benchmark_unavailable_reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(
                benchmark_reason_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
    }
    return aggregate


def _probe_benchmark(db_manager: Any, *, config: Any = None) -> Dict[str, Any]:
    """检查是否真的存在可用的基准序列，并确定用哪一条指数。

    复盘需要"同期基准"才能说清收益是选股能力还是大盘 beta。这里先查证再决定：
    不查就编一个"跑赢大盘 x%"正是本仓库要治的病。

    ``code`` 是**实际**被用作基准的指数 canonical_id（配置里排第一且有本地 K 线的
    那条），必须回传 —— 用户有权知道自己是拿沪深300 还是中证500 在比。
    """
    from sqlalchemy import text

    probe: Dict[str, Any] = {
        "available": False,
        "reason": "no_benchmark_series",
        "code": None,
        "codes_configured": [],
        "window_basis": "per_pick_window",
        "index_series": [],
        "stock_daily_series_count": 0,
        "stock_daily_latest_date": None,
        "market_snapshot_dates": [],
    }
    try:
        from src.services.benchmark_index_service import resolve_benchmark_index_codes

        probe["codes_configured"] = resolve_benchmark_index_codes(config)
    except Exception:
        logger.warning("[ScreeningTracking] 解析基准指数配置失败", exc_info=True)
    try:
        with db_manager.get_session() as session:
            canonical_rows = session.execute(
                text("SELECT DISTINCT canonical_id FROM stock_daily WHERE canonical_id IS NOT NULL")
            ).fetchall()
            latest_row = session.execute(text("SELECT MAX(date) AS latest FROM stock_daily")).fetchone()
            as_of_rows = session.execute(
                text("SELECT DISTINCT as_of FROM market_snapshot ORDER BY as_of")
            ).fetchall()
    except Exception as exc:
        probe["reason"] = "benchmark_probe_failed"
        probe["error"] = str(exc)
        return probe

    series = sorted(
        str((row._mapping if hasattr(row, "_mapping") else row)["canonical_id"])
        for row in canonical_rows
    )
    probe["stock_daily_series_count"] = len(series)
    probe["index_series"] = [value for value in series if _INDEX_CANONICAL_PATTERN.match(value)]
    # 先按配置顺序挑；配置的指数一条都没有时退回本地实际存在的指数（并在 code
    # 里如实写明用的是哪条），而不是干脆不算 —— 有可比数据就比，用哪条必须说清。
    available_series = set(probe["index_series"])
    for candidate in probe["codes_configured"]:
        if candidate in available_series:
            probe["code"] = candidate
            break
    if probe["code"] is None and probe["index_series"]:
        probe["code"] = probe["index_series"][0]
    if latest_row is not None:
        latest_value = (latest_row._mapping if hasattr(latest_row, "_mapping") else latest_row)["latest"]
        latest_date = _coerce_date(latest_value)
        probe["stock_daily_latest_date"] = latest_date.isoformat() if latest_date else None
    probe["market_snapshot_dates"] = [
        str((row._mapping if hasattr(row, "_mapping") else row)["as_of"]) for row in as_of_rows
    ]
    if probe["index_series"]:
        probe["available"] = True
        probe["reason"] = "index_series_found"
    elif len(probe["market_snapshot_dates"]) <= 1:
        # market_snapshot 是按选股运行写入的单日横截面，没有时间序列，
        # 因此不能当基准用 —— 这一点必须说清楚，而不是拿一天的涨跌幅冒充区间基准。
        probe["reason"] = "market_snapshot_is_single_date_cross_section"
    else:
        probe["reason"] = "no_index_and_no_multi_date_snapshot"
    return probe


class ScreeningTrackingService:
    """读取选股入选与前瞻价格，输出可复盘的收益表现。"""

    def __init__(self, config: Any = None, db_manager: Any = None):
        self.config = config
        self.db_manager = db_manager

    def tracking(
        self,
        *,
        days: Optional[int] = None,
        limit: Optional[int] = None,
        strategy: str = "",
        runs_limit: int = _MAX_RUNS_SCAN,
    ) -> Dict[str, Any]:
        resolved_days, _max_codes, enabled = _resolve_tracking_window(self.config, days=days)
        db = _resolve_database_manager(self.db_manager)
        picks, malformed_runs = load_pick_rows(
            config=self.config,
            db_manager=db,
            days=resolved_days,
            runs_limit=runs_limit,
        )
        strategy_filter = str(strategy or "").strip()
        if strategy_filter:
            picks = [pick for pick in picks if pick["strategy"] == strategy_filter]

        canonical_by_code: Dict[str, Optional[str]] = {}
        for pick in picks:
            code = pick["code"]
            if code not in canonical_by_code:
                try:
                    canonical_by_code[code] = db._derive_canonical_id(code)
                except Exception:
                    logger.warning(
                        "[ScreeningTracking] canonical_id 推导失败 code=%s", code, exc_info=True
                    )
                    canonical_by_code[code] = None

        bars_by_canonical = _fetch_forward_bars(
            db, [value for value in canonical_by_code.values() if value]
        )

        rows: List[Dict[str, Any]] = []
        for pick in picks:
            canonical_id = canonical_by_code.get(pick["code"])
            rows.append(
                _compute_pick_performance(
                    pick,
                    canonical_id=canonical_id,
                    bars=bars_by_canonical.get(canonical_id) if canonical_id else None,
                )
            )

        # === 同期基准对比 ===
        # 先探测本地到底有哪条指数序列，再按每只入选**自己的窗口**取指数区间。
        # 指数序列缺失时不补 0、不换窗口：每只入选都会带上 benchmark_reason。
        benchmark_probe = _probe_benchmark(db, config=self.config)
        benchmark_code = benchmark_probe.get("code")
        benchmark_bars: Optional[List[Dict[str, Any]]] = None
        if benchmark_code:
            benchmark_bars = _fetch_forward_bars(db, [benchmark_code]).get(benchmark_code)
        benchmark_enabled = bool(benchmark_probe.get("codes_configured"))
        for row in rows:
            row.update(
                _compute_benchmark_comparison(
                    row,
                    enabled=benchmark_enabled,
                    benchmark_code=benchmark_code,
                    index_bars=benchmark_bars,
                )
            )

        # 列表按可复盘优先 + 收益从低到高（先看亏得最多的），不可复盘的按日期倒序垫底。
        trackable_rows = sorted(
            (row for row in rows if row["trackable"]),
            key=lambda row: (row["return_pct"] if row["return_pct"] is not None else 0.0),
        )
        untrackable_rows = sorted(
            (row for row in rows if not row["trackable"]),
            key=lambda row: (row["pick_date"], row["code"]),
            reverse=True,
        )
        ordered_rows = trackable_rows + untrackable_rows
        if limit is not None:
            try:
                ordered_rows = ordered_rows[: max(0, int(limit))]
            except (TypeError, ValueError):
                pass

        reason_counts: Dict[str, int] = {}
        for row in untrackable_rows:
            key = str(row.get("reason") or "unknown")
            reason_counts[key] = reason_counts.get(key, 0) + 1
        # 用列表而不是 dict：前端 toCamelCase 会深度改写【对象键】，把
        # no_daily_rows 变成 noDailyRows —— 原因码是契约，不能被静默改名。
        untrackable_reasons = [
            {"reason": reason, "count": count}
            for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))
        ]

        strategies: List[Dict[str, Any]] = []
        strategy_names = sorted({row["strategy"] for row in rows if row["strategy"]})
        for name in strategy_names:
            group = [row for row in rows if row["strategy"] == name]
            strategies.append({"strategy": name, **_aggregate(group)})
        strategies.sort(key=lambda item: (item["trackable"], item["picks"]), reverse=True)

        latest_dates = sorted(
            row["latest_date"] for row in rows if row.get("latest_date")
        )
        total = _aggregate(rows)
        # 把"用哪条指数、有多少只可比"补进 benchmark 对象：原有字段语义一个不动，
        # 只是不再停在 available=false 的占位状态。
        benchmark_probe["comparable_picks"] = total["benchmark_comparable"]
        benchmark_probe["avg_benchmark_return_pct"] = total["avg_benchmark_return_pct"]
        benchmark_probe["median_benchmark_return_pct"] = total["median_benchmark_return_pct"]
        benchmark_probe["avg_excess_return_pct"] = total["avg_excess_return_pct"]
        benchmark_probe["median_excess_return_pct"] = total["median_excess_return_pct"]
        benchmark_probe["beat_benchmark_rate"] = total["beat_benchmark_rate"]
        benchmark_probe["beat_benchmark_basis"] = total["beat_benchmark_basis"]
        benchmark_probe["beat_benchmark_denominator"] = total["beat_benchmark_denominator"]
        benchmark_probe["unavailable_reasons"] = total["benchmark_unavailable_reasons"]
        return {
            "enabled": enabled,
            "window_days": resolved_days,
            "generated_at": _utc_now().isoformat(timespec="seconds"),
            "run_window": {
                "days": resolved_days,
                "malformed_runs": malformed_runs,
            },
            "total": total,
            "strategies": strategies,
            "picks": ordered_rows,
            "untrackable": untrackable_rows,
            "untrackable_reasons": untrackable_reasons,
            "data_freshness": {
                # 本地 K 线整体最新日期（不是"可复盘入选里的最新日期"）：
                # 即使没有任何可复盘入选，也要如实显示行情到底更到哪一天了。
                "stock_daily_latest_date": benchmark_probe.get("stock_daily_latest_date"),
                "stock_daily_series_count": benchmark_probe.get("stock_daily_series_count"),
                "latest_trackable_bar_date": max(latest_dates) if latest_dates else None,
                "pick_forward_bars_available": bool(latest_dates),
            },
            "benchmark": benchmark_probe,
            "notes": [
                "return_pct = 最新收盘价相对选股时点价格的涨跌幅；取不到前瞻 K 线的入选计入 untrackable，不计入命中率分母。",
                "同一交易日的 K 线不算前瞻收益（选股价格已是当日快照价）。",
                "stock_daily 需要先刷新：STOCK_DAILY_REFRESH_ENABLED 打开后，刷新清单会覆盖最近选股入选。",
                "excess_return_pct = 该入选自己的 return_pct 减去同期基准指数收益；基准区间随每只入选的窗口走（入选日 → 该入选最新前瞻 K 线日），不是全局统一的一个区间。",
                "跑赢比例的分母只有 benchmark_comparable（既有可复盘收益、窗口又被指数覆盖的入选）；拿不到基准的入选单列 benchmark_reason，绝不按 0 超额计入。",
            ],
        }


def build_screening_tracking(
    config: Any = None,
    db_manager: Any = None,
    *,
    days: Optional[int] = None,
    limit: Optional[int] = None,
    strategy: str = "",
) -> Dict[str, Any]:
    """便捷入口：默认服务 + 配置。"""
    return ScreeningTrackingService(config=config, db_manager=db_manager).tracking(
        days=days,
        limit=limit,
        strategy=strategy,
    )
