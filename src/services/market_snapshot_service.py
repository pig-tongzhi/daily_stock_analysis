# -*- coding: utf-8 -*-
"""全市场横截面快照的持久化（第 1 层）。

选股每次现场拉 5211 只、用完即弃，于是回测、横截面查询、避免重复抓取这三件
事永远做不到。这个模块把选股【本来就要拉的】那份快照顺手存下来 —— 不额外抓数据，
只是不再丢掉。

只存可筛选的数值。描述性文本（主营、概念列表）留给 company_profile：
存全市场 × 完整文本约 50 MB/天，而选股只要 5 只、问股只问 1 只，
那属于负债而不是能力。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 快照 DataFrame 的列 → 表字段。缺列时该字段留空，不影响其余列入库。
# 标准列 → market_snapshot 列。统一用 _normalize() 之后的标准名；
# 每加一个源字段就在这里加一行，避免"源给了但没落库"（2026-10-21 审计发现
# 东财给了 19 个字段只落了 11 个）。
_COLUMN_MAP = {
    # 价格
    "prev_close": "prev_close",
    "open": "open",
    "high": "high",
    "low": "low",
    "price": "price",
    "change_amount": "change_amount",
    "change_pct": "change_pct",
    "amplitude": "amplitude",
    # 成交
    "volume": "volume",
    "amount": "amount",
    "turnover_rate": "turnover_rate",
    "volume_ratio": "volume_ratio",
    # 估值
    "pe_ttm": "pe_ttm",
    "pe_lyr": "pe_lyr",
    "pb_ratio": "pb_ratio",
    "dividend_yield": "dividend_yield",
    # 规模
    "total_mv": "total_mv",
    "circ_mv": "circ_mv",
    "total_shares": "total_shares",
    "free_shares": "free_shares",
    # 位置与动量
    "high_52w": "high_52w",
    "low_52w": "low_52w",
    "change_5d": "change_5d",
    "change_20d": "change_20d",
    "change_60d": "change_60d",
    "change_ytd": "change_ytd",
    # 交易状态
    "limit_up": "limit_up",
    "limit_down": "limit_down",
}

# 向后兼容：老代码/老数据源仍用 pe_ratio 这个名字代表 TTM 市盈率。
_COLUMN_ALIASES = {"pe_ratio": "pe_ttm"}

# 文本类字段
_TEXT_MAP = {
    "board_name": "board_name",
    "concepts": "concepts",
    "trade_status": "trade_status",
}


def _to_date(value: Any) -> Optional[date]:
    """把各种日期表示统一成 date。支持 20261020 / 2026-10-20 / 2026/10/20。"""
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text or text in ("-", "--", "None"):
        return None
    digits = text.replace("-", "").replace("/", "").replace(" ", "")[:8]
    if len(digits) == 8 and digits.isdigit():
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            return None
    return None


def _to_datetime(value: Any) -> Optional[datetime]:
    """解析行情时刻。Wind 给的是 2026-10-20T15:30:31.000+08:00。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # 统一存成"北京时间 naive"，与全库约定一致
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    return parsed


def _to_payload(record: Any) -> Optional[str]:
    """把原始记录整条序列化留档，防止"源给了但没落库"。"""
    if not record:
        return None
    try:
        import json as _json

        def _default(obj):
            if isinstance(obj, (datetime, date)):
                return obj.isoformat()
            return str(obj)

        return _json.dumps(dict(record), ensure_ascii=False, default=_default)
    except Exception:
        return None


def _to_float(value: Any) -> Optional[float]:
    """把快照里的值转成 float。空值/`-`/`--` 一律 None，不塞 0。

    塞 0 会让"没有数据"看起来像"PE=0"，横截面筛选时会把垃圾选进来 ——
    NULL 才是诚实的表达。
    """
    if value is None:
        return None
    try:
        if isinstance(value, str):
            text = value.strip().replace(",", "")
            if not text or text in {"-", "--", "N/A", "nan", "None"}:
                return None
            value = text
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def save_market_snapshot(
    snapshot_df: Any,
    *,
    as_of: Optional[date] = None,
    source: str = "",
) -> int:
    """把全市场快照写入 market_snapshot。幂等：同一 (canonical_id, as_of) 覆盖。

    返回写入行数。失败返回 0 —— 存快照不该影响选股本身。
    """
    if snapshot_df is None or getattr(snapshot_df, "empty", True):
        return 0

    try:
        from sqlalchemy import text

        from src.storage import DatabaseManager
    except Exception as exc:
        logger.warning("[MarketSnapshot] 初始化失败: %s", exc, exc_info=True)
        return 0

    manager = DatabaseManager.get_instance()
    # 优先用数据源自带的「数据日」；只有在源没给时才退回北京日期。
    # 历史 bug：一律用 datetime.now().date() → 凌晨/周末补跑会把前一天的行情
    # 标成当天（2026-10-20 的 10-17 周六快照就是这么来的）。
    day = as_of
    if day is None:
        try:
            from src.services.screening.snapshot import snapshot_trade_date

            day = snapshot_trade_date(snapshot_df)
        except Exception:
            day = None
    if day is None:
        day = datetime.now().date()
        logger.warning(
            "[MarketSnapshot] 数据源未提供数据日，退回运行日 %s（来源=%s）", day, source
        )
    # 全库时间基准是"北京本地 naive"。这里曾用 UTC ISO（带 +00:00 后缀），
    # 导致 market_snapshot.created_at 是全库唯一一个 UTC 时间戳，
    # 和其它表没法直接比较（2026-10-21 审计发现）。
    now = datetime.now().isoformat()

    # 注意：不能用 `getattr(df, "columns", []) or []` —— pandas.Index 的真值
    # 是未定义的，会抛 "truth value of a Index is ambiguous"。直接 set() 即可。
    columns = set(getattr(snapshot_df, "columns", []))
    if "code" not in columns:
        logger.warning("[MarketSnapshot] 快照缺 code 列，跳过持久化")
        return 0

    rows = []
    for record in snapshot_df.to_dict("records"):
        code = str(record.get("code") or "").strip()
        if not code:
            continue
        canonical_id = manager._derive_canonical_id(code)
        if not canonical_id:
            continue
        row = {
            "canonical_id": canonical_id,
            "as_of": day,
            "code": code,
            "name": (str(record.get("name")).strip() if record.get("name") else None),
            "industry": (str(record.get("industry")).strip() if record.get("industry") else None),
            "snapshot_source": source or None,
            "created_at": now,
            "listing_date": _to_date(record.get("listing_date")),
            "trade_time": _to_datetime(record.get("trade_time")),
            # 原始返回整条留档：将来发现漏存字段可直接补齐，不必重拉。
            "raw_payload": _to_payload(record),
        }
        for src_col, field in _COLUMN_MAP.items():
            row[field] = _to_float(record.get(src_col)) if src_col in columns else None
        # 兼容老数据源的 pe_ratio / 新数据源的 pe_ttm
        if row.get("pe_ttm") is None:
            row["pe_ttm"] = _to_float(record.get("pe_ratio"))
        for src_col, field in _TEXT_MAP.items():
            value = record.get(src_col)
            if value is None or value == "":
                row[field] = None
            elif isinstance(value, (list, tuple)):
                import json as _json
                row[field] = _json.dumps(list(value), ensure_ascii=False)
            else:
                row[field] = str(value).strip()
        rows.append(row)

    if not rows:
        return 0

    sql = text(
        """
        INSERT INTO market_snapshot
            (canonical_id, as_of, code, name, industry,
             board_name, concepts, prev_close, open, high,
             low, price, change_amount, change_pct, amplitude,
             volume, amount, turnover_rate, volume_ratio, pe_ttm,
             pe_lyr, pb_ratio, dividend_yield, total_mv, circ_mv,
             total_shares, free_shares, high_52w, low_52w, change_5d,
             change_20d, change_60d, change_ytd, limit_up, limit_down,
             trade_status, listing_date, trade_time, snapshot_source, raw_payload,
             created_at)
        VALUES
            (:canonical_id, :as_of, :code, :name, :industry,
             :board_name, :concepts, :prev_close, :open, :high,
             :low, :price, :change_amount, :change_pct, :amplitude,
             :volume, :amount, :turnover_rate, :volume_ratio, :pe_ttm,
             :pe_lyr, :pb_ratio, :dividend_yield, :total_mv, :circ_mv,
             :total_shares, :free_shares, :high_52w, :low_52w, :change_5d,
             :change_20d, :change_60d, :change_ytd, :limit_up, :limit_down,
             :trade_status, :listing_date, :trade_time, :snapshot_source, :raw_payload,
             :created_at)
        ON CONFLICT(canonical_id, as_of) DO UPDATE SET
            code=COALESCE(market_snapshot.code, excluded.code),
            name=COALESCE(market_snapshot.name, excluded.name),
            industry=COALESCE(market_snapshot.industry, excluded.industry),
            board_name=COALESCE(market_snapshot.board_name, excluded.board_name),
            concepts=COALESCE(market_snapshot.concepts, excluded.concepts),
            prev_close=COALESCE(market_snapshot.prev_close, excluded.prev_close),
            open=COALESCE(market_snapshot.open, excluded.open),
            high=COALESCE(market_snapshot.high, excluded.high),
            low=COALESCE(market_snapshot.low, excluded.low),
            price=COALESCE(market_snapshot.price, excluded.price),
            change_amount=COALESCE(market_snapshot.change_amount, excluded.change_amount),
            change_pct=COALESCE(market_snapshot.change_pct, excluded.change_pct),
            amplitude=COALESCE(market_snapshot.amplitude, excluded.amplitude),
            volume=COALESCE(market_snapshot.volume, excluded.volume),
            amount=COALESCE(market_snapshot.amount, excluded.amount),
            turnover_rate=COALESCE(market_snapshot.turnover_rate, excluded.turnover_rate),
            volume_ratio=COALESCE(market_snapshot.volume_ratio, excluded.volume_ratio),
            pe_ttm=COALESCE(market_snapshot.pe_ttm, excluded.pe_ttm),
            pe_lyr=COALESCE(market_snapshot.pe_lyr, excluded.pe_lyr),
            pb_ratio=COALESCE(market_snapshot.pb_ratio, excluded.pb_ratio),
            dividend_yield=COALESCE(market_snapshot.dividend_yield, excluded.dividend_yield),
            total_mv=COALESCE(market_snapshot.total_mv, excluded.total_mv),
            circ_mv=COALESCE(market_snapshot.circ_mv, excluded.circ_mv),
            total_shares=COALESCE(market_snapshot.total_shares, excluded.total_shares),
            free_shares=COALESCE(market_snapshot.free_shares, excluded.free_shares),
            high_52w=COALESCE(market_snapshot.high_52w, excluded.high_52w),
            low_52w=COALESCE(market_snapshot.low_52w, excluded.low_52w),
            change_5d=COALESCE(market_snapshot.change_5d, excluded.change_5d),
            change_20d=COALESCE(market_snapshot.change_20d, excluded.change_20d),
            change_60d=COALESCE(market_snapshot.change_60d, excluded.change_60d),
            change_ytd=COALESCE(market_snapshot.change_ytd, excluded.change_ytd),
            limit_up=COALESCE(market_snapshot.limit_up, excluded.limit_up),
            limit_down=COALESCE(market_snapshot.limit_down, excluded.limit_down),
            trade_status=COALESCE(market_snapshot.trade_status, excluded.trade_status),
            listing_date=COALESCE(market_snapshot.listing_date, excluded.listing_date),
            trade_time=COALESCE(market_snapshot.trade_time, excluded.trade_time),
            snapshot_source=COALESCE(market_snapshot.snapshot_source, excluded.snapshot_source),
            raw_payload=COALESCE(market_snapshot.raw_payload, excluded.raw_payload),
            created_at=COALESCE(market_snapshot.created_at, excluded.created_at)
        """
    )

    written = 0
    try:
        with manager._engine.begin() as conn:
            # 分批：5211 行一次 executemany 在 SQLite 上会撑大事务，分成 1000 一批
            for start in range(0, len(rows), 1000):
                chunk = rows[start:start + 1000]
                conn.execute(sql, chunk)
                written += len(chunk)
    except Exception as exc:
        logger.warning("[MarketSnapshot] 写入失败: %s", exc, exc_info=True)
        return 0

    return written
