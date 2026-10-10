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
from datetime import date, datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 快照 DataFrame 的列 → 表字段。缺列时该字段留空，不影响其余列入库。
_COLUMN_MAP = {
    "price": "price",
    "change_pct": "change_pct",
    "amount": "amount",
    "total_mv": "total_mv",
    "pe_ratio": "pe_ratio",
    "pb_ratio": "pb_ratio",
    "volume_ratio": "volume_ratio",
    "turnover_rate": "turnover_rate",
}


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
    day = as_of or datetime.now(timezone.utc).date()
    now = datetime.now(timezone.utc).isoformat()

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
        }
        for src_col, field in _COLUMN_MAP.items():
            row[field] = _to_float(record.get(src_col)) if src_col in columns else None
        rows.append(row)

    if not rows:
        return 0

    sql = text(
        """
        INSERT INTO market_snapshot
            (canonical_id, as_of, code, name, industry, price, change_pct, amount,
             total_mv, pe_ratio, pb_ratio, volume_ratio, turnover_rate,
             snapshot_source, created_at)
        VALUES
            (:canonical_id, :as_of, :code, :name, :industry, :price, :change_pct, :amount,
             :total_mv, :pe_ratio, :pb_ratio, :volume_ratio, :turnover_rate,
             :snapshot_source, :created_at)
        ON CONFLICT(canonical_id, as_of) DO UPDATE SET
            code=excluded.code, name=excluded.name, industry=excluded.industry,
            price=excluded.price, change_pct=excluded.change_pct, amount=excluded.amount,
            total_mv=excluded.total_mv, pe_ratio=excluded.pe_ratio, pb_ratio=excluded.pb_ratio,
            volume_ratio=excluded.volume_ratio, turnover_rate=excluded.turnover_rate,
            snapshot_source=excluded.snapshot_source
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
