# -*- coding: utf-8 -*-
"""数据盘点：把库里实际有什么、新不新鲜、缺什么，如实报出来。

为什么需要：这一路反复出现同一类问题 —— 表建了没数据（公司档案只有脚本写过）、
数据存了但覆盖率不足（cnadidate 缺 canonical_id）、字段抓取一直失败（基本面 6 个
维度全超时）。这些都不会报错，只会静默地少给数据。

一个能一眼看到"哪张表多少行、覆盖多少标的、最新一条是什么时候、哪些列为空"的
观测面，比事后逐个排查有用得多。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# (表名, 中文名, 用途说明, 标识列, 股票数怎么数, 新鲜度参照列, 新鲜阈值小时)
TABLE_SPECS: List[Dict[str, Any]] = [
    {
        "table": "market_snapshot", "label": "全市场横截面", "group": "行情 / 个股",
        "note": "每次选股落库一份，只存可筛选的数值",
        "stock_col": "canonical_id", "time_col": "as_of", "stale_hours": 48,
    },
    {
        "table": "stock_daily", "label": "日线 K 线", "group": "行情 / 个股",
        "note": "自选股的日线，含均线与量比",
        "stock_col": "canonical_id", "time_col": "date", "stale_hours": 72,
    },
    {
        "table": "company_profile", "label": "公司档案", "group": "行情 / 个股",
        "note": "行业层级 / 地域 / 业务概念（慢变）",
        "stock_col": "canonical_id", "time_col": "updated_at", "stale_hours": 720,
    },
    {
        "table": "company_metrics", "label": "按日估值", "group": "行情 / 个股",
        "note": "PE / PB / 市值，按日累积永不覆盖",
        "stock_col": "canonical_id", "time_col": "as_of", "stale_hours": 48,
    },
    {
        "table": "fundamental_snapshot", "label": "基本面快照", "group": "行情 / 个股",
        "note": "原始多维度抓取结果（含失败原因）",
        "stock_col": "canonical_id", "time_col": "created_at", "stale_hours": 48,
    },
    {
        "table": "intelligence_items", "label": "资讯池", "group": "资讯",
        "note": "RSSHub / NewsNow 抓取的本地资讯",
        "stock_col": "canonical_id", "time_col": "fetched_at", "stale_hours": 6,
    },
    {
        "table": "intelligence_sources", "label": "资讯源", "group": "资讯",
        "note": "配置的抓取源（含启用状态）",
        "stock_col": None, "time_col": None, "stale_hours": None,
    },
    {
        "table": "news_intel", "label": "搜索新闻", "group": "资讯",
        "note": "外部搜索（Bocha）抓到并落库的新闻",
        "stock_col": "canonical_id", "time_col": "created_at", "stale_hours": 48,
    },
    {
        "table": "analysis_history", "label": "分析报告", "group": "分析 / 建议",
        "note": "每次分析的报告与结论",
        "stock_col": "canonical_id", "time_col": "created_at", "stale_hours": 48,
    },
    {
        "table": "decision_signals", "label": "AI 建议", "group": "分析 / 建议",
        "note": "结构化的操作建议（含有效期）",
        "stock_col": "canonical_id", "time_col": "created_at", "stale_hours": 48,
    },
    {
        "table": "decision_signal_outcomes", "label": "到期验证", "group": "分析 / 建议",
        "note": "建议到期后的实际结果与命中判定",
        "stock_col": None, "time_col": "created_at", "stale_hours": 48,
    },
    {
        "table": "screening_runs", "label": "选股运行", "group": "选股",
        "note": "每次选股的条件、候选与完整上下文",
        "stock_col": None, "time_col": "created_at", "stale_hours": 168,
    },
    {
        "table": "portfolio_positions", "label": "持仓", "group": "持仓 / 账户",
        "note": "当前持仓与浮动盈亏",
        "stock_col": "canonical_id", "time_col": "updated_at", "stale_hours": 48,
    },
    {
        "table": "portfolio_trades", "label": "交易记录", "group": "持仓 / 账户",
        "note": "买卖流水",
        "stock_col": "canonical_id", "time_col": "created_at", "stale_hours": None,
    },
    {
        "table": "llm_usage", "label": "Token 用量", "group": "系统",
        "note": "每次模型调用的用量记账",
        "stock_col": "stock_code", "time_col": "created_at", "stale_hours": None,
    },
]

# 期望"应该有数据却没有"的表 —— 空着就是缺口，值得显式提示
EXPECTED_NON_EMPTY = {
    "stock_daily", "intelligence_items", "market_snapshot",
}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _staleness(conn: sqlite3.Connection, table: str, time_col: Optional[str], hours: Optional[float]) -> Dict[str, Any]:
    if not time_col or time_col not in _columns(conn, table):
        return {"latest": None, "status": "unknown"}
    try:
        latest = conn.execute(f"SELECT MAX({time_col}) FROM {table}").fetchone()[0]
    except Exception:
        return {"latest": None, "status": "unknown"}
    if latest is None:
        return {"latest": None, "status": "empty"}

    text = str(latest)
    parsed: Optional[datetime] = None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text[:26] if "." in text else text[:19], fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return {"latest": text, "status": "unknown"}

    age_hours = (datetime.now() - parsed).total_seconds() / 3600.0
    if hours is None:
        status = "ok"
    elif age_hours <= hours:
        status = "fresh"
    elif age_hours <= hours * 3:
        status = "aging"
    else:
        status = "stale"
    return {"latest": text, "age_hours": round(age_hours, 1), "status": status}


def _column_fill(conn: sqlite3.Connection, table: str, columns: List[str], total: int) -> List[Dict[str, Any]]:
    """每个列有多少行非空 —— 用来一眼看出哪一列其实一直是空的。"""
    if total == 0:
        return []
    out = []
    for col in columns:
        try:
            n = conn.execute(f"SELECT COUNT({col}) FROM {table}").fetchone()[0]
        except Exception:
            continue
        pct = round(n * 100.0 / total, 1)
        out.append({"column": col, "filled": n, "pct": pct, "empty": n == 0})
    return out


def build_data_inventory(db_path: Optional[str] = None) -> Dict[str, Any]:
    """盘点库内容。纯只读，失败时逐表降级而不是整体报错。"""
    from src.config import get_config

    path = Path(db_path or getattr(get_config(), "database_path", "data/stock_analysis.db"))
    if not path.is_absolute():
        path = Path.cwd() / path

    result: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": {
            "path": str(path),
            "exists": path.exists(),
            "size_mb": round(path.stat().st_size / 1024 / 1024, 2) if path.exists() else 0,
        },
        "groups": [],
        "gaps": [],
    }
    if not path.exists():
        result["gaps"].append({
            "table": "-", "issue": "database_missing", "detail": f"找不到数据库文件 {path}",
        })
        return result

    conn = sqlite3.connect(str(path))
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    try:
        for spec in TABLE_SPECS:
            table = spec["table"]
            if not _table_exists(conn, table):
                grouped.setdefault(spec["group"], []).append({
                    "table": table, "label": spec["label"], "note": spec["note"],
                    "rows": 0, "stocks": 0, "exists": False,
                    "latest": None, "freshness": "missing", "columns": [], "fill": [],
                })
                result["gaps"].append({
                    "table": table, "issue": "table_missing",
                    "detail": f"{spec['label']} 表不存在（新装应自动创建）",
                })
                continue

            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            stock_col = spec.get("stock_col")
            cols = _columns(conn, table)
            stocks = 0
            if stock_col and stock_col in cols:
                stocks = conn.execute(
                    f"SELECT COUNT(DISTINCT {stock_col}) FROM {table} WHERE {stock_col} IS NOT NULL"
                ).fetchone()[0]

            stale = _staleness(conn, table, spec.get("time_col"), spec.get("stale_hours"))
            entries = {
                "table": table, "label": spec["label"], "note": spec["note"],
                "rows": total, "stocks": stocks, "exists": True,
                "latest": stale.get("latest"), "freshness": stale.get("status"),
                "age_hours": stale.get("age_hours"),
                "columns": cols,
                "fill": _column_fill(conn, table, cols, total),
            }
            grouped.setdefault(spec["group"], []).append(entries)

            if total == 0 and table in EXPECTED_NON_EMPTY:
                result["gaps"].append({
                    "table": table, "issue": "empty_table",
                    "detail": f"{spec['label']} 一行都没有",
                })
            elif total and any(c["empty"] for c in entries["fill"]):
                empty_cols = [c["column"] for c in entries["fill"] if c["empty"]]
                result["gaps"].append({
                    "table": table, "issue": "empty_columns",
                    "detail": f"{spec['label']} 有整列为空: {', '.join(empty_cols[:6])}",
                })
    finally:
        conn.close()

    result["groups"] = [
        {"name": name, "tables": tables} for name, tables in grouped.items()
    ]
    return result
