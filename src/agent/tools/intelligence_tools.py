# -*- coding: utf-8 -*-
"""本地资讯池的只读工具。

问股此前只有 ``search_stock_news`` 一条资讯来源，而它走外部搜索 API ——
没有配置 key 时直接报错。用户自己搭的 RSSHub/NewsNow 资讯池（intelligence_items）
则完全没被问股用上：分析、大盘复盘、股票档案都在读它，只有问股不读。

这个工具把那条路接上。它是纯 DB 读取：不联网、不消耗搜索配额、不受
provider 可用性影响。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.agent.tools.registry import ToolDefinition, ToolParameter, ToolPolicy

logger = logging.getLogger(__name__)

# 纯本地读取：没有 network_read，也没有任何写入。
_LOCAL_INTEL_READ_POLICY = ToolPolicy.declared(
    read_only=True,
    side_effects=["db_read"],
    permissions=["intel:read"],
    scope_dimensions=["stock"],
)

DEFAULT_DAYS = 7
DEFAULT_LIMIT = 10
MAX_LIMIT = 30


def _handle_get_local_intelligence(
    stock_code: str,
    stock_name: str = "",
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_LIMIT,
) -> Dict[str, Any]:
    """Read the local news pool for one stock."""
    try:
        from src.repositories.intelligence_repo import IntelligenceRepository
    except Exception as exc:  # pragma: no cover - import guard
        return {"success": False, "error": f"资讯仓储不可用: {exc}"}

    code = str(stock_code or "").strip()
    name = str(stock_name or "").strip()
    if not code and not name:
        return {"success": False, "error": "至少需要 stock_code 或 stock_name"}

    try:
        window_days = max(1, int(days))
    except (TypeError, ValueError):
        window_days = DEFAULT_DAYS
    try:
        row_limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        row_limit = DEFAULT_LIMIT

    try:
        rows = IntelligenceRepository().list_candidate_items(
            code=code or None,
            name=name or None,
            days=window_days,
            limit=row_limit,
            market="cn",
        )
    except Exception as exc:
        logger.warning("[LocalIntelligence] 读取资讯池失败: %s", exc, exc_info=True)
        return {"success": False, "error": f"读取资讯池失败: {exc}"}

    results = []
    for row in rows:
        published = getattr(row, "published_at", None) or getattr(row, "fetched_at", None)
        results.append({
            "title": getattr(row, "title", "") or "",
            "summary": (getattr(row, "summary", "") or "")[:300],
            "source": getattr(row, "source_name", "") or "",
            "url": getattr(row, "url", "") or "",
            "published_at": str(published) if published else "",
            "scope": getattr(row, "scope_type", "") or "",
        })

    return {
        "success": True,
        "query": {"stock_code": code, "stock_name": name, "days": window_days},
        "results_count": len(results),
        # 空结果本身是有效信息：说明这只票的资讯源没覆盖到，而不是查询出错。
        # 明确写出来，避免模型把「池里没有」误当成「工具不可用」。
        "note": (
            "本地资讯池为只读快照，覆盖范围取决于已配置的资讯源；"
            "为空表示该标的当前没有入库资讯，可改用 search_stock_news 联网检索。"
        ),
        "results": results,
    }


get_local_intelligence_tool = ToolDefinition(
    name="get_local_intelligence",
    description=(
        "Read the locally stored news pool for one stock (no network, no API key "
        "needed). Covers items that the RSSHub/NewsNow sources have already "
        "ingested. Returns titles, summaries, sources, URLs and timestamps. "
        "An empty result means the pool has no coverage for that stock yet — it "
        "is not an error; fall back to search_stock_news for live search."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519'",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Stock name in Chinese; improves matching inside market-level news, e.g., '贵州茅台'",
            required=False,
        ),
        ToolParameter(
            name="days",
            type="integer",
            description="Look back this many days (default 7)",
            required=False,
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description="Maximum items to return (default 10, max 30)",
            required=False,
        ),
    ],
    handler=_handle_get_local_intelligence,
    category="search",
    policy=_LOCAL_INTEL_READ_POLICY,
)


ALL_LOCAL_INTELLIGENCE_TOOLS = [
    get_local_intelligence_tool,
]
