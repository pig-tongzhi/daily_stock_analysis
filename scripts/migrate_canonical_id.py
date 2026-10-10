#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一次性迁移：给持有股票标识的表补上 canonical_id，并统一存量。

背景
----
同一个「股票身份」在库里被写成了至少三种形态：

    stock_daily.code          '000063' 与 '000063.SZ' 同时存在（同一只股票两份）
    analysis_history.code     '000063.SZ'
    decision_signals.stock_code '000063'
    fundamental_snapshot.code '000063.SZ'，且混进过股票名称 '唐人神'

于是资讯、K 线、基本面、分析、建议彼此 join 不上 —— 这正是"把股票信息存进库
再让 AI 判断"这件事的阻塞点：每加一张表就多一种形态。

做法（照抄 storage.py 里 stock_daily 已有的 Expand-Contract 六步模式）
------------------------------------------------------------------
1. 仅 SQLite。
2. inspect() 表是否存在。
3. inspect() canonical_id 列是否已存在 —— 幂等，可重复执行。
4. ALTER TABLE ADD COLUMN canonical_id VARCHAR(32)（可空）。
5. 用 DatabaseManager._derive_canonical_id(code) 分批回填，
   条件恒为 `WHERE canonical_id IS NULL`，所以重跑安全。
6. 建普通索引（非唯一）—— 历史别名行共享同一个 canonical_id 是合法的。

**不动 code 列。** 读取路径继续用 code（storage.py 的 AC 4 明确要求），
canonical_id 只作为新增的、可用于跨表 join 的权威键。

不做的事
--------
- 不删任何行。
- 不改任何 code 值。
- 名称形态（如 fundamental_snapshot 里的 '唐人神'）无法凭解析器得到代码，
  因此回填为 NULL 并单独报告，不猜。
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger("migrate_canonical_id")

# (表名, 存股票标识的列, 附加过滤条件)
# intelligence_items 只有 scope_type='symbol' 的那部分才是"某只股票"，
# market 级的 scope_value 是市场名，不该有 canonical_id。
TARGETS: List[Tuple[str, str, Optional[str]]] = [
    ("analysis_history", "code", None),
    ("decision_signals", "stock_code", None),
    ("fundamental_snapshot", "code", None),
    ("news_intel", "code", None),
    ("intelligence_items", "scope_value", "scope_type = 'symbol'"),
    ("portfolio_positions", "symbol", None),
    ("portfolio_position_lots", "symbol", None),
    ("portfolio_trades", "symbol", None),
]

BATCH = 5000


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def add_column(conn: sqlite3.Connection, table: str, *, dry_run: bool) -> bool:
    """Add canonical_id if missing. Returns True when it had to be added."""
    if "canonical_id" in _columns(conn, table):
        return False
    if dry_run:
        print(f"  [dry-run] ALTER TABLE {table} ADD COLUMN canonical_id VARCHAR(32)")
        return True
    conn.execute(f"ALTER TABLE {table} ADD COLUMN canonical_id VARCHAR(32)")
    print(f"  ✅ 加列 canonical_id → {table}")
    return True


def backfill(
    conn: sqlite3.Connection,
    table: str,
    code_col: str,
    extra_where: Optional[str],
    derive,
    *,
    dry_run: bool,
) -> Dict[str, int]:
    """Backfill canonical_id for rows where it is NULL. Idempotent."""
    has_column = "canonical_id" in _columns(conn, table)
    where = [f"{code_col} IS NOT NULL", f"{code_col} != ''"]
    if extra_where:
        where.append(extra_where)
    base_clause = " AND ".join(where)
    if not has_column:
        # dry-run：列还没建，只能报"将要处理多少行"
        total = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {base_clause}"
        ).fetchone()[0]
        print(f"  [dry-run] 将回填 {table}.canonical_id ← {code_col}（{total} 行）")
        return {"pending": total, "resolved": 0, "unresolved": 0, "batches": 0}

    clause = " AND ".join(where + ["canonical_id IS NULL"])
    total = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {clause}").fetchone()[0]
    stats = {"pending": total, "resolved": 0, "unresolved": 0, "batches": 0}
    if total == 0:
        return stats

    unresolved: List[str] = []
    cursor_id = 0
    while True:
        rows = conn.execute(
            f"SELECT id, {code_col} FROM {table} "
            f"WHERE {clause} AND id > ? ORDER BY id LIMIT ?",
            (cursor_id, BATCH),
        ).fetchall()
        if not rows:
            break
        stats["batches"] += 1
        updates = []
        for row_id, raw in rows:
            cursor_id = row_id
            try:
                cid = derive(str(raw))
            except Exception:
                cid = None
            # 解析器对名称等非代码输入会原样返回（'唐人神' → '唐人神'）。
            # 那不是 canonical_id，不能写进去污染这一列。
            if not cid or cid == str(raw) and not str(raw).isalnum():
                unresolved.append(str(raw))
                continue
            if cid == str(raw) and str(raw).isdigit() is False and not any(
                ch.isdigit() for ch in str(raw)
            ):
                unresolved.append(str(raw))
                continue
            updates.append((cid, row_id))
            stats["resolved"] += 1

        if updates and not dry_run:
            conn.executemany(
                f"UPDATE {table} SET canonical_id = ? WHERE id = ?", updates
            )
            conn.commit()

    stats["unresolved"] = len(set(unresolved))
    if unresolved:
        print(f"     ⚠️ {table}: {len(set(unresolved))} 个值无法解析 → {sorted(set(unresolved))[:6]}")
    return stats


def ensure_index(conn: sqlite3.Connection, table: str, *, dry_run: bool) -> None:
    name = f"ix_{table}_canonical_id"
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    if exists:
        return
    if dry_run:
        print(f"  [dry-run] CREATE INDEX {name}")
        return
    conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}(canonical_id)")
    print(f"  ✅ 建索引 {name}")


def report(conn: sqlite3.Connection) -> None:
    print()
    print("═══ 迁移后：各表 canonical_id 覆盖 ═══")
    print("  %-26s %-14s %8s %8s %8s" % ("表", "标识列", "总行", "已填", "不同ID"))
    for table, code_col, _ in TARGETS:
        if not _table_exists(conn, table):
            continue
        cols = _columns(conn, table)
        if "canonical_id" not in cols:
            print("  %-26s %-14s %8s %8s %8s" % (table, code_col, "-", "❌未加列", "-"))
            continue
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        filled = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE canonical_id IS NOT NULL"
        ).fetchone()[0]
        distinct = conn.execute(
            f"SELECT COUNT(DISTINCT canonical_id) FROM {table} WHERE canonical_id IS NOT NULL"
        ).fetchone()[0]
        print("  %-26s %-14s %8d %8d %8d" % (table, code_col, total, filled, distinct))


def cross_table_check(conn: sqlite3.Connection) -> None:
    """证明跨表 join 现在对得上：同一只股票在各表的 canonical_id 一致。"""
    print()
    print("═══ 跨表一致性验证 ═══")
    pairs = [
        ("stock_daily", "canonical_id", "K线"),
        ("analysis_history", "canonical_id", "分析"),
        ("decision_signals", "canonical_id", "建议"),
        ("fundamental_snapshot", "canonical_id", "基本面"),
    ]
    ids: Dict[str, set] = {}
    for table, col, label in pairs:
        if not _table_exists(conn, table) or col not in _columns(conn, table):
            continue
        ids[label] = {
            r[0] for r in conn.execute(
                f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL"
            )
        }
        print(f"  {label:<8} {len(ids[label]):>3} 个 canonical_id")

    if len(ids) >= 2:
        common = set.intersection(*ids.values())
        print(f"  → 所有表都有的：{len(common)} 个 {sorted(common)[:10]}")
        overlap_an = ids.get("分析", set()) & ids.get("K线", set())
        print(f"  → 分析 ∩ K线：{len(overlap_an)} 个  ← 此前 join 不上")
        sig = ids.get("建议", set()) & ids.get("K线", set())
        print(f"  → 建议 ∩ K线：{len(sig)} 个")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "data" / "stock_analysis.db"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)
    print(f"  数据库: {args.db}")
    print(f"  模式  : {'DRY-RUN（不改数据）' if args.dry_run else '实际执行'}")
    print()

    from src.storage import DatabaseManager

    manager = DatabaseManager.get_instance()
    derive = manager._derive_canonical_id

    conn = sqlite3.connect(args.db)
    try:
        for table, code_col, extra in TARGETS:
            if not _table_exists(conn, table):
                print(f"  ⏭  跳过 {table}（表不存在）")
                continue
            added = add_column(conn, table, dry_run=args.dry_run)
            if not args.dry_run and added:
                conn.commit()
            stats = backfill(
                conn, table, code_col, extra, derive, dry_run=args.dry_run
            )
            if stats["pending"]:
                print(
                    f"  {table}: 待回填 {stats['pending']} → 解析成功 {stats['resolved']}，"
                    f"无法解析 {stats['unresolved']}（{stats['batches']} 批）"
                )
            ensure_index(conn, table, dry_run=args.dry_run)

        if not args.dry_run:
            report(conn)
            cross_table_check(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
