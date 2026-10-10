# -*- coding: utf-8 -*-
"""Tests for the local news-pool agent tool."""

from __future__ import annotations

from datetime import datetime

import pytest

from src.agent.tools.intelligence_tools import (
    ALL_LOCAL_INTELLIGENCE_TOOLS,
    _handle_get_local_intelligence,
)
from src.storage import DatabaseManager, IntelligenceItem


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    import os

    from src.config import Config

    old = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(tmp_path / "local_intel.db")
    Config.reset_instance()
    DatabaseManager.reset_instance()
    db = DatabaseManager.get_instance()
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if old is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = old


def _seed(db, *, code="600519", title="贵州茅台获机构上调评级", scope="symbol", name=None):
    with db.session_scope() as session:
        session.add(
            IntelligenceItem(
                source_name=name or "测试源",
                source_type="rss",
                title=title,
                summary="摘要",
                url=f"https://example.com/{title}",
                published_at=datetime(2026, 10, 9, 10, 0, 0),
                scope_type=scope,
                scope_value=code,
                market="cn",
            )
        )


def test_tool_is_registered() -> None:
    assert [t.name for t in ALL_LOCAL_INTELLIGENCE_TOOLS] == ["get_local_intelligence"]


def test_tool_is_read_only_and_needs_no_network() -> None:
    tool = ALL_LOCAL_INTELLIGENCE_TOOLS[0]
    assert tool.policy.read_only is True
    assert "network_read" not in (tool.policy.side_effects or [])
    assert tool.policy.side_effects == ["db_read"]


def test_returns_symbol_scoped_items(isolated_db) -> None:
    _seed(isolated_db, code="600519")
    result = _handle_get_local_intelligence(stock_code="600519", stock_name="贵州茅台")
    assert result["success"] is True
    assert result["results_count"] == 1
    assert "贵州茅台" in result["results"][0]["title"]


def test_empty_result_is_success_not_error(isolated_db) -> None:
    """池里没有该标的必须是成功+0 条，而不是报错。

    否则模型会把「本地池没覆盖」误读成「工具不可用」，这正是此前
    「所有个股工具都返回空」那类误判的来源。
    """
    result = _handle_get_local_intelligence(stock_code="588200")
    assert result["success"] is True
    assert result["results_count"] == 0
    assert "error" not in result
    assert "search_stock_news" in result["note"]


def test_missing_both_code_and_name_fails_closed(isolated_db) -> None:
    result = _handle_get_local_intelligence(stock_code="", stock_name="")
    assert result["success"] is False
    assert "error" in result


def test_invalid_days_and_limit_fall_back(isolated_db) -> None:
    _seed(isolated_db, code="600519")
    for bad in ("x", None, 0, -1):
        result = _handle_get_local_intelligence(
            stock_code="600519", days=bad, limit=bad
        )
        assert result["success"] is True
        assert result["query"]["days"] >= 1


def test_limit_is_capped(isolated_db) -> None:
    for index in range(40):
        _seed(isolated_db, code="600519", title=f"标题 {index}")
    result = _handle_get_local_intelligence(stock_code="600519", limit=999)
    assert result["success"] is True
    assert result["results_count"] <= 30
