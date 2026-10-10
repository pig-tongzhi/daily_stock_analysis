#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据一致性验收：股票身份的跨表契约。

这个脚本是"很多人可以用"的前提检查 —— 身份一旦分裂，资讯、K线、基本面、
建议就 join 不上，而这类问题不会报错，只会静默地少给数据。

检查项
------
1. **无未统一形态**：同一只股票不应在库里有多个 canonical_id。
2. **code 列无名称**：中文名混进代码列会让行情/盈亏解析失败（曾发生：'唐人神'）。
3. **canonical_id 覆盖率**：所有持有股票标识的表都要填满。
4. **跨表可 join**：以 K 线表为基准，验证各表能通过 canonical_id 关联上。
5. **无孤儿 canonical_id**：各表的 canonical_id 都应能在 K 线或档案里找到对应。

退出码非 0 表示验收失败，可直接用于 CI。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# (表名, 标识列, 说明)
IDENTITY_COLUMNS: List[Tuple[str, str, str]] = [
    ("stock_daily", "code", "K线"),
    ("analysis_history", "code", "分析"),
    ("decision_signals", "stock_code", "AI建议"),
    ("fundamental_snapshot", "code", "基本面快照"),
    ("intelligence_items", "scope_value", "资讯(symbol级)"),
    ("news_intel", "code", "搜索新闻"),
    ("company_profile", "code", "公司档案"),
    ("portfolio_positions", "symbol", "持仓"),
    ("portfolio_position_lots", "symbol", "持仓批次"),
    ("portfolio_trades", "symbol", "交易"),
]

# intelligence_items 只有 symbol 级才是"某只股票"
SCOPE_FILTER = {"intelligence_items": "scope_type = 'symbol'"}

# 非股票标识：大盘复盘用 code='MARKET'，它不是一只股票，本就不该有 canonical_id。
# 把它当作"覆盖不足"是误报 —— 验收脚本必须能区分"该有的没填"和"本来就没有"。
NON_STOCK_CODES = frozenset({"MARKET", "GLOBAL", "ALL", "CN", "HK", "US"})

# 合法代码形态：数字/字母/点/连字符，可带市场前缀或后缀
CODE_LIKE = re.compile(r"^[A-Za-z0-9.\-]{1,16}$")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


class Acceptance:
    def __init__(self) -> None:
        self.failures: List[str] = []
        self.notes: List[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        print(f"  ❌ {msg}")

    def ok(self, msg: str) -> None:
        print(f"  ✅ {msg}")

    def note(self, msg: str) -> None:
        self.notes.append(msg)
        print(f"  ⚠️  {msg}")


def check_no_name_in_code_column(conn: sqlite3.Connection, acc: Acceptance) -> None:
    print("\n[1] code 列不得混入股票名称")
    for table, col, label in IDENTITY_COLUMNS:
        if not _table_exists(conn, table) or col not in _columns(conn, table):
            continue
        where = SCOPE_FILTER.get(table)
        clause = f" AND {where}" if where else ""
        bad = [
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT {col} FROM {table} "
                f"WHERE {col} IS NOT NULL AND {col} != ''{clause}"
            )
            if not CODE_LIKE.match(str(r[0]))
        ]
        if bad:
            acc.fail(f"{table}.{col} 含非代码值（{label}）: {bad[:6]}")
        else:
            acc.ok(f"{table}.{col} 全是代码形态")


def check_canonical_id_coverage(conn: sqlite3.Connection, acc: Acceptance) -> None:
    print("\n[2] canonical_id 覆盖率")
    for table, col, label in IDENTITY_COLUMNS:
        if not _table_exists(conn, table):
            continue
        cols = _columns(conn, table)
        if "canonical_id" not in cols:
            acc.note(f"{table} 没有 canonical_id 列（{label}）")
            continue
        where = SCOPE_FILTER.get(table)
        clause = f" WHERE {where}" if where else ""
        # 排除 MARKET 之类的非股票标识，否则会把正确的 NULL 当成覆盖不足
        stock_clause = (
            f"{clause} AND {col} NOT IN ({','.join('?' * len(NON_STOCK_CODES))})"
            if clause
            else f" WHERE {col} NOT IN ({','.join('?' * len(NON_STOCK_CODES))})"
        )
        params = tuple(sorted(NON_STOCK_CODES))
        total, filled = conn.execute(
            f"SELECT COUNT(*), COUNT(canonical_id) FROM {table}{stock_clause}", params
        ).fetchone()
        if total == 0:
            continue
        if filled < total:
            acc.fail(f"{table} 覆盖不足: {filled}/{total}（{label}）")
        else:
            acc.ok(f"{table} 覆盖 {filled}/{total}（{label}）")


def check_one_identity_per_stock(conn: sqlite3.Connection, acc: Acceptance) -> None:
    """同一只股票不应映射到多个 canonical_id —— 那正是"形态分裂"。"""
    print("\n[3] 一只股票只能有一个 canonical_id")
    for table, col, label in IDENTITY_COLUMNS:
        if not _table_exists(conn, table):
            continue
        cols = _columns(conn, table)
        if "canonical_id" not in cols:
            continue
        where = SCOPE_FILTER.get(table)
        clause = f" AND {where}" if where else ""
        splits = list(
            conn.execute(
                f"""
                SELECT canonical_id, GROUP_CONCAT(DISTINCT {col}) AS aliases, COUNT(DISTINCT {col}) AS n
                FROM {table}
                WHERE canonical_id IS NOT NULL{clause}
                GROUP BY canonical_id
                HAVING n > 1
                """
            )
        )
        if splits:
            # stock_daily 是历史遗留的已知情况（'000063' 与 '000063.SZ' 并存），
            # canonical_id 已经把它们统一，读取走 canonical_id 就没事。
            detail = ", ".join(f"{r[0]}=[{r[1]}]" for r in splits[:3])
            if table == "stock_daily":
                acc.note(
                    f"{table} 存在 {len(splits)} 组历史别名（{detail}）—— "
                    "canonical_id 已统一，按 canonical_id 读取不受影响"
                )
            else:
                acc.fail(f"{table} 有 {len(splits)} 组形态分裂: {detail}")
        else:
            acc.ok(f"{table} 每个 canonical_id 对应单一形态")


def check_cross_table_join(conn: sqlite3.Connection, acc: Acceptance) -> None:
    print("\n[4] 跨表可通过 canonical_id join")
    base = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT canonical_id FROM stock_daily WHERE canonical_id IS NOT NULL"
        )
    }
    if not base:
        acc.note("stock_daily 没有 canonical_id，跳过 join 检查")
        return
    print(f"  基准（K线）: {len(base)} 只")
    for table, _col, label in IDENTITY_COLUMNS:
        if table == "stock_daily" or not _table_exists(conn, table):
            continue
        cols = _columns(conn, table)
        if "canonical_id" not in cols:
            continue
        ids = {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT canonical_id FROM {table} WHERE canonical_id IS NOT NULL"
            )
        }
        overlap = base & ids
        if ids and not overlap:
            acc.fail(f"{label} ({table}) 与 K线 零交集 —— join 不上")
        else:
            acc.ok(f"{label:<12} {len(ids):>2} 只，与 K线 交集 {len(overlap)}")


def check_orphan_canonical_ids(conn: sqlite3.Connection, acc: Acceptance) -> None:
    print("\n[5] 无孤儿 canonical_id")
    known = set()
    for table in ("stock_daily", "company_profile"):
        if _table_exists(conn, table) and "canonical_id" in _columns(conn, table):
            known |= {
                r[0]
                for r in conn.execute(
                    f"SELECT DISTINCT canonical_id FROM {table} WHERE canonical_id IS NOT NULL"
                )
            }
    if not known:
        acc.note("没有可用的已知身份集合，跳过")
        return
    for table, _col, label in IDENTITY_COLUMNS:
        if table in ("stock_daily", "company_profile") or not _table_exists(conn, table):
            continue
        cols = _columns(conn, table)
        if "canonical_id" not in cols:
            continue
        ids = {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT canonical_id FROM {table} WHERE canonical_id IS NOT NULL"
            )
        }
        orphans = ids - known
        if orphans:
            acc.fail(f"{label} ({table}) 有 {len(orphans)} 个孤儿: {sorted(orphans)[:5]}")
        else:
            acc.ok(f"{label:<12} 无孤儿")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db",
        default=str(Path(__file__).resolve().parents[1] / "data" / "stock_analysis.db"),
    )
    args = ap.parse_args()

    print(f"  数据一致性验收: {args.db}")
    conn = sqlite3.connect(args.db)
    acc = Acceptance()
    try:
        check_no_name_in_code_column(conn, acc)
        check_canonical_id_coverage(conn, acc)
        check_one_identity_per_stock(conn, acc)
        check_cross_table_join(conn, acc)
        check_orphan_canonical_ids(conn, acc)
    finally:
        conn.close()

    print()
    print("═" * 52)
    if acc.failures:
        print(f"  验收失败：{len(acc.failures)} 项")
        for f in acc.failures:
            print(f"    • {f}")
        print(f"  （另有 {len(acc.notes)} 条提示）")
        return 1
    print(f"  ✅ 验收通过（{len(acc.notes)} 条提示，非阻塞）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
