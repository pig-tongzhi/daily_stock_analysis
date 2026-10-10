# -*- coding: utf-8 -*-
"""个股档案的本地库块（company / track_record）。

这两块是 StockProfileService 里仅有的纯 DB 读取，也是"让 AI 判断有据可依"的落点：
AI 需要知道这只票属于什么行业、以及自己过去在这只票上准不准。
"""

from __future__ import annotations

import pytest

from src.services.stock_profile_service import StockProfileService


@pytest.fixture()
def svc():
    # 只测纯 DB 块，跳过联网依赖的注入
    return StockProfileService.__new__(StockProfileService)


class TestCompanyBlock:
    def test_returns_industry_for_known_stock(self, svc):
        block = svc._company_block("600519")
        assert block["status"] == "ok"
        data = block["data"]
        assert data["industry_l1"] == "食品饮料"
        assert data["industry_l2"] == "白酒"
        assert data["name"] == "贵州茅台"

    def test_unknown_stock_is_unavailable_not_error(self, svc):
        # 档案是按需补的，没抓到的票是正常状态，不是故障
        block = svc._company_block("999999")
        assert block["status"] == "unavailable"
        assert "no_company_profile" in block["limitations"]

    def test_metrics_carried_with_as_of(self, svc):
        data = svc._company_block("600519")["data"]
        assert "metrics" in data
        assert data["metrics"]["as_of"]
        assert data["metrics"]["pe_ttm"] is not None

    def test_null_metrics_are_flagged_not_hidden(self, svc):
        """抓取失败的估值存的是 NULL，必须标明原因，否则会被读成"PE 就是空的"。"""
        block = svc._company_block("002567")
        data = block["data"]
        if data and data.get("metrics") and data["metrics"]["pe_ttm"] is None:
            assert "metrics_fetch_failed_for_latest_as_of" in block["limitations"]


class TestTrackRecordBlock:
    def test_hit_count_is_not_always_zero(self, svc):
        """回归：原始 SQL 读回的布尔是 SQLite 的 0/1，用 `is True` 判断会永远为假，
        命中数恒为 0 —— 表面上像"没命中过"，实际是把命中全丢了。"""
        data = svc._track_record_block("600519")["data"]
        assert data["completed"] > 0
        assert data["hit"] + data["miss"] == data["completed"]
        assert data["hit"] > 0

    def test_directional_and_range_are_separated(self, svc):
        """方向判断与区间判断（观望）必须分开，混合命中率会奖励从不下方向判断的策略。"""
        data = svc._track_record_block("600519")["data"]
        assert "directional_completed" in data
        assert "range_completed" in data
        assert data["directional_completed"] + data["range_completed"] == data["completed"]

    def test_no_directional_sample_blocks_calibration(self, svc):
        block = svc._track_record_block("600519")
        data = block["data"]
        if data["directional_completed"] == 0:
            assert "no_directional_evaluations" in block["limitations"]
            assert data["directional_hit_rate_pct"] is None

    def test_stock_never_analysed_reports_unavailable(self, svc):
        block = svc._track_record_block("002567")
        assert block["status"] == "unavailable"
        assert "no_signal_history" in block["limitations"]


class TestEvidenceQualityCoversNewBlocks:
    def test_new_blocks_appear_in_evidence_quality(self, svc):
        blocks = {
            "quote": {"status": "fresh"},
            "history": {"status": "fresh"},
            "research": {"status": "fresh"},
            "intelligence": {"status": "unavailable"},
            "portfolio": {"status": "fresh"},
            "monitors": {"status": "fresh"},
            "company": {"status": "ok"},
            "track_record": {"status": "ok"},
        }
        eq = svc._evidence_quality(blocks)
        assert set(eq["blocks"]) == set(blocks)
        assert eq["blocks"]["company"] == "ok"
        # ok 与 fresh 都算齐备，不能被判成"不新鲜"
        assert eq["status"] in {"partial", "fresh"}

    def test_missing_block_does_not_raise(self, svc):
        """块缺席要算 unavailable，不能让整次查询抛 KeyError。"""
        eq = svc._evidence_quality({"quote": {"status": "fresh"}})
        assert eq["blocks"]["track_record"] == "unavailable"
