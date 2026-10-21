# -*- coding: utf-8 -*-
"""本地事实层工具（get_company_profile / get_market_context）与 get_stock_info 本地优先。

测试的重点不是"能读出来"，而是**读不出来时说的是什么**：
NULL 不参与分位、样本不足不给分位、没有档案要明确失败、本地过期要回退实时。
这些都是 OVERVIEW 第 5 节"读本地优先"要成立的前提 —— 假精确比没有数据更糟。
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from src.agent.tools import data_tools
from src.agent.tools.local_data_tools import (
    ALL_LOCAL_DATA_TOOLS,
    MIN_USABLE_ROWS,
    _handle_get_company_profile,
    _handle_get_market_context,
)
from src.storage import CompanyMetrics, CompanyProfile, DatabaseManager, MarketSnapshot

SNAPSHOT_DAY = date(2026, 10, 10)


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """把工具指向一个临时库，避免测试污染（或被）真实库的状态。"""
    from src.config import Config

    old = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(tmp_path / "local_data.db")
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


def _seed_profile(
    db,
    canonical_id: str,
    *,
    code: str = "600519",
    name: str = "贵州茅台",
    market: str = "cn",
    industry_l1="食品饮料",
    industry_l2="白酒",
    industry_l3="白酒",
    region="贵州板块",
    concepts=("电商概念", "白酒"),
    updated_at=None,
):
    with db.session_scope() as session:
        session.add(
            CompanyProfile(
                canonical_id=canonical_id,
                code=code,
                name=name,
                market=market,
                industry_l1=industry_l1,
                industry_l2=industry_l2,
                industry_l3=industry_l3,
                region=region,
                concepts_json=json.dumps(list(concepts), ensure_ascii=False),
                boards_json="[]",
                updated_at=updated_at or datetime.now(timezone.utc).replace(tzinfo=None),
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )


def _seed_metrics(db, canonical_id: str, *, as_of: date, pe_ttm=None, pb=None,
                  total_mv=None, circ_mv=None):
    with db.session_scope() as session:
        session.add(
            CompanyMetrics(
                canonical_id=canonical_id,
                as_of=as_of,
                pe_ttm=pe_ttm,
                pb=pb,
                total_mv=total_mv,
                circ_mv=circ_mv,
                payload="{}",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )


def _seed_snapshot_rows(db, rows, *, as_of=SNAPSHOT_DAY):
    """rows: list of (canonical_id, code, name, pe_ratio) — 其余列留空。"""
    with db.session_scope() as session:
        for canonical_id, code, name, pe_ratio in rows:
            session.add(
                MarketSnapshot(
                    canonical_id=canonical_id,
                    as_of=as_of,
                    code=code,
                    name=name,
                    pe_ratio=pe_ratio,
                    snapshot_source="test",
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
            )


class _FakeFetcherManager:
    """实时抓取的替身：记录是否被调用，返回一份形状正确的基本面上下文。"""

    def __init__(self):
        self.calls = []

    def get_fundamental_context(self, stock_code):
        self.calls.append(("get_fundamental_context", stock_code))
        return {
            "market": "cn",
            "status": "ok",
            "coverage": {},
            "valuation": {
                "status": "ok",
                "data": {"pe_ratio": 11.1, "pb_ratio": 2.2, "total_mv": 333.0, "circ_mv": 222.0},
            },
            "boards": {"status": "ok", "data": {"rank": 1}},
        }

    def build_failed_fundamental_context(self, stock_code, message):
        return {"market": "cn", "status": "failed", "coverage": {}}

    def get_belong_boards(self, stock_code):
        return [{"name": "白酒"}]

    def get_stock_name(self, stock_code):
        return "实时名字"


# ============================================================
# 工具本身
# ============================================================

class TestToolDefinitions:
    def test_both_tools_are_registered(self):
        assert [t.name for t in ALL_LOCAL_DATA_TOOLS] == [
            "get_company_profile",
            "get_market_context",
        ]

    def test_tools_are_read_only_and_never_touch_network(self):
        for tool in ALL_LOCAL_DATA_TOOLS:
            assert tool.policy.read_only is True
            assert tool.policy.side_effects == ["db_read"]
            assert "network_read" not in (tool.policy.side_effects or [])
            assert "stock" in tool.policy.scope_dimensions

    def test_market_metric_parameter_is_whitelisted_in_schema(self):
        market_tool = next(t for t in ALL_LOCAL_DATA_TOOLS if t.name == "get_market_context")
        metric_param = next(p for p in market_tool.parameters if p.name == "metric")
        assert metric_param.enum is not None
        assert "pe_ratio" in metric_param.enum
        # volume_ratio 实测整列为 NULL，故意不在白名单里
        assert "volume_ratio" not in metric_param.enum


# ============================================================
# get_company_profile
# ============================================================

class TestCompanyProfileTool:
    def test_returns_industry_and_flags_each_missing_field(self, isolated_db):
        """行业层级可以为 NULL，估值抓取失败也是 NULL —— 必须逐字段说清楚缺什么。"""
        _seed_profile(
            isolated_db,
            "sh600519",
            industry_l2=None,
            industry_l3=None,
            region=None,
        )
        _seed_metrics(isolated_db, "sh600519", as_of=date(2026, 10, 9), pe_ttm=None, pb=6.28)

        result = _handle_get_company_profile("600519")

        assert result["success"] is True
        assert result["canonical_id"] == "sh600519"
        assert result["industry"] == {"l1": "食品饮料", "l2": None, "l3": None}
        assert result["name"] == "贵州茅台"
        assert result["valuation"]["as_of"] == "2026-10-09"
        assert result["valuation"]["pe_ttm"] is None
        assert result["valuation"]["pb"] == 6.28
        # 每个缺失字段都要点名，不能把 NULL 当值展示
        assert "industry_l2" in result["missing_fields"]
        assert "industry_l3" in result["missing_fields"]
        assert "region" in result["missing_fields"]
        assert "valuation.pe_ttm" in result["missing_fields"]
        assert "valuation.pb" not in result["missing_fields"]
        assert "NULL" in result["note"]

    def test_missing_profile_is_explicit_failure_not_empty_success(self, isolated_db):
        result = _handle_get_company_profile("999999")
        assert result["success"] is False
        assert result["reason"] == "no_local_profile"
        assert result["canonical_id"]  # 999999 解析为北交所 bj999999，只要解析成功即可
        assert result["canonical_id"].endswith("999999")
        assert "error" in result

    def test_blank_code_fails_closed(self, isolated_db):
        result = _handle_get_company_profile("   ")
        assert result["success"] is False
        assert result["reason"] == "missing_stock_code"

    def test_latest_metrics_row_wins(self, isolated_db):
        """company_metrics 按日累积，必须取 as_of 最新一行，而不是最早一行。"""
        _seed_profile(isolated_db, "sh600519")
        _seed_metrics(isolated_db, "sh600519", as_of=date(2026, 10, 2), pe_ttm=None, pb=None)
        _seed_metrics(isolated_db, "sh600519", as_of=date(2026, 10, 9), pe_ttm=19.32, pb=6.26)

        result = _handle_get_company_profile("600519")
        assert result["valuation"]["as_of"] == "2026-10-09"
        assert result["valuation"]["pe_ttm"] == 19.32

    def test_no_metrics_row_is_reported(self, isolated_db):
        _seed_profile(isolated_db, "sh600519")
        result = _handle_get_company_profile("600519")
        assert result["success"] is True
        assert result["valuation"] is None
        assert "valuation" in result["missing_fields"]
        assert result["limitations"] == []

    def test_real_db_has_maotai_profile(self):
        """真实库里 600519 的档案（与既有 stock_profile 测试同一假设：库即应用库）。"""
        result = _handle_get_company_profile("600519")
        assert result["success"] is True, result
        assert result["canonical_id"] == "sh600519"
        assert result["name"] == "贵州茅台"
        assert result["industry"]["l1"] == "食品饮料"
        assert result["valuation_as_of"]


# ============================================================
# get_market_context
# ============================================================

class TestMarketContextTool:
    def test_percentile_excludes_nulls(self, isolated_db):
        """把 NULL 当 0 会把分位整体拉偏，这个夹具专门让两种算法给出不同答案。

        可用样本 = 1..100（100 条），目标的 pe = 50：
          正确（排除 NULL）: <=50 的 50 条 / 100 = 50.00，跑赢 49 只
          错误（NULL 当 0）: (50 + 40) / 140 = 64.29，跑赢 89 只
        """
        rows = [("sh600519", "600519", "贵州茅台", 50)]
        remaining = [v for v in range(1, 101) if v != 50]
        for index, value in enumerate(remaining, start=1):
            rows.append((f"sz{index:06d}", f"{index:06d}", f"票{index}", value))
        for index in range(1, 41):
            rows.append((f"bj{index:06d}", f"9{index:05d}", f"空{index}", None))
        _seed_snapshot_rows(isolated_db, rows)

        result = _handle_get_market_context("600519", metric="pe_ratio")

        assert result["success"] is True
        assert result["market"]["usable_count"] == 100
        assert result["market"]["excluded_null_count"] == 40
        assert result["market"]["median"] == pytest.approx(50.5)
        assert result["target"]["found"] is True
        assert result["target"]["value"] == 50
        assert result["position"]["peers_total"] == 100
        assert result["position"]["peers_beaten"] == 49
        assert result["position"]["percentile_rank"] == pytest.approx(50.0)

    def test_below_min_rows_on_every_date_fails_explicitly(self, isolated_db):
        """每一天的样本都不足时，明确失败并列出被拒绝的日期，而不是算一个假精确的数。"""
        rows = [("sh600519", "600519", "贵州茅台", 15)]
        for index, value in enumerate([v for v in range(1, 31) if v != 15], start=1):
            rows.append((f"sz{index:06d}", f"{index:06d}", f"票{index}", value))
        _seed_snapshot_rows(isolated_db, rows)

        result = _handle_get_market_context("600519", metric="pe_ratio")

        assert result["success"] is False
        assert result["reason"] == "no_usable_snapshot"
        assert result["rejected_date_count"] == 1
        rejected = result["rejected_dates"][0]
        assert rejected["as_of"] == str(SNAPSHOT_DAY)
        assert rejected["usable_count_for_metric"] == 30
        assert rejected["reason"] == "below_min_usable_rows"
        assert str(MIN_USABLE_ROWS) in result["error"]

    def test_skips_newer_partial_snapshot_and_uses_latest_usable_one(self, isolated_db):
        """回归：最新的一天可能是跑了一半的残缺快照，不能用它盖住前一天的完整横截面。

        夹具：2026-10-11 只有 3 行（2 行 pe 可用），2026-10-10 有 100 行可用。
        正确行为必须选 10-10 并给出分位，同时把 10-11 列进 rejected_dates。
        """
        newer = date(2026, 10, 11)
        partial = [
            ("sh600519", "600519", "贵州茅台", 7),
            ("sz000002", "000002", "票2", 8),
            ("bj000001", "900001", "残1", None),
        ]
        _seed_snapshot_rows(isolated_db, partial, as_of=newer)

        full = [("sh600519", "600519", "贵州茅台", 50)]
        full += [
            (f"sz{i:06d}", f"{i:06d}", f"票{i}", v)
            for i, v in enumerate([v for v in range(1, 101) if v != 50], start=1)
        ]
        _seed_snapshot_rows(isolated_db, full, as_of=SNAPSHOT_DAY)

        result = _handle_get_market_context("600519", metric="pe_ratio")

        assert result["success"] is True
        assert result["as_of"] == str(SNAPSHOT_DAY)
        assert result["market"]["usable_count"] == 100
        assert result["position"] is not None
        assert result["position"]["percentile_rank"] == pytest.approx(50.0)
        assert "skipped_recent_snapshot_dates" in result["limitations"]
        assert [item["as_of"] for item in result["rejected_dates"]] == [str(newer)]
        assert result["rejected_dates"][0]["usable_count_for_metric"] == 2

    def test_non_whitelisted_metric_is_rejected(self, isolated_db):
        for bad in ("roe", "pe_ratio FROM market_snapshot; --", "1=1", "", "volume_ratio"):
            result = _handle_get_market_context("600519", metric=bad)
            assert result["success"] is False, bad
            assert result["reason"] == "metric_not_allowed", bad
            assert "pe_ratio" in result["allowed_metrics"]
            assert "volume_ratio" not in result["allowed_metrics"]

    def test_injection_attempt_leaves_table_intact(self, isolated_db):
        """入参永远不拼进 SQL：恶意 metric 只应被拒绝，表必须原封不动。"""
        _seed_snapshot_rows(isolated_db, [("sh600519", "600519", "贵州茅台", 10)])
        hostile = "pe_ratio); DROP TABLE market_snapshot; --"
        result = _handle_get_market_context("600519", metric=hostile)
        assert result["success"] is False
        assert result["reason"] == "metric_not_allowed"

        from sqlalchemy import text

        with isolated_db._engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM market_snapshot")).scalar()
        assert count == 1

    def test_industry_scope_is_unavailable_with_reason(self, isolated_db):
        """market_snapshot.industry 全为 NULL —— 不提供行业分位，并说明为什么。"""
        _seed_snapshot_rows(isolated_db, [("sh600519", "600519", "贵州茅台", 10)])
        result = _handle_get_market_context("600519", scope="industry")
        assert result["success"] is False
        assert result["reason"] == "industry_context_unavailable"
        assert "industry" in result["error"]

    def test_target_metric_null_is_not_treated_as_zero(self, isolated_db):
        _seed_snapshot_rows(
            isolated_db,
            [("sh600519", "600519", "贵州茅台", None)]
            + [(f"sz{i:06d}", f"{i:06d}", f"票{i}", i) for i in range(1, 121)],
        )
        result = _handle_get_market_context("600519", metric="pe_ratio")
        assert result["success"] is True
        assert result["target"]["present"] is True
        assert result["target"]["found"] is False
        assert result["target"]["value"] is None
        assert result["position"] is None
        assert "target_metric_missing" in result["limitations"]
        # 市场分布仍然可用（目标的 NULL 只影响目标自己的位置）
        assert result["market"]["usable_count"] == 120
        assert result["market"]["excluded_null_count"] == 1

    def test_target_absent_from_snapshot_is_reported_distinctly(self, isolated_db):
        """"这只票不在快照里"与"在快照里但该指标为 NULL"是两回事，limitations 要分开。"""
        _seed_snapshot_rows(
            isolated_db,
            [(f"sz{i:06d}", f"{i:06d}", f"票{i}", i) for i in range(1, 121)],
        )
        result = _handle_get_market_context("600519", metric="pe_ratio")
        assert result["success"] is True
        assert result["target"]["present"] is False
        assert result["position"] is None
        assert "target_not_in_snapshot" in result["limitations"]
        assert "target_metric_missing" not in result["limitations"]

    def test_empty_snapshot_fails_closed(self, isolated_db):
        result = _handle_get_market_context("600519")
        assert result["success"] is False
        assert result["reason"] == "no_market_snapshot"

    def test_real_db_uses_latest_usable_snapshot_not_latest_date(self):
        """真实库：必须选中"最近一份可用快照"，即使更新的日期上只有零散假行。

        注：本仓库的既有测试会把选股假快照写进库（现已由 conftest 的临时副本拦住），
        所以这里只断言"选中的那一天样本 >= 阈值"，不写死日期。
        """
        result = _handle_get_market_context("600519", metric="pe_ratio")
        assert result["success"] is True, result
        assert result["canonical_id"] == "sh600519"
        assert result["market"] is not None
        assert result["market"]["usable_count"] >= MIN_USABLE_ROWS

        # 这里原来写死断言「茅台不在这份快照里（真实数据如此）」。
        # 2026-10-22 数据源换成 Wind 之后快照变成完整的 5,567 只，茅台自然在里面，
        # 断言就红了 —— 那不是回归，是测试赌了当时那份残缺数据。
        # 改成两种结果都必须自洽：在或不在都可以，但说明必须给对。
        target = result["target"]
        if target["present"]:
            assert "target_not_in_snapshot" not in result["limitations"]
            assert result["position"] is not None
        else:
            assert "target_not_in_snapshot" in result["limitations"]
            assert result["position"] is None

    def test_real_db_computes_a_real_percentile_for_a_present_stock(self):
        """真实库：对确实在快照里的个股，必须算出 0-100 的真实分位。"""
        from sqlalchemy import text

        from src.storage import DatabaseManager

        probe = _handle_get_market_context("600519", metric="pe_ratio")
        assert probe["success"] is True, probe
        chosen_day = probe["as_of"]

        with DatabaseManager.get_instance()._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT canonical_id, code FROM market_snapshot "
                    "WHERE as_of = :as_of AND pe_ratio IS NOT NULL "
                    "ORDER BY canonical_id LIMIT 1"
                ),
                {"as_of": chosen_day},
            ).fetchone()
        assert row is not None, "所选日期必须有可用的 pe_ratio 行"

        result = _handle_get_market_context(row[1], metric="pe_ratio")
        assert result["success"] is True, result
        assert result["as_of"] == chosen_day
        assert result["target"]["present"] is True
        assert result["position"] is not None
        assert 0.0 <= result["position"]["percentile_rank"] <= 100.0
        assert result["position"]["peers_total"] == result["market"]["usable_count"]


# ============================================================
# get_stock_info：本地优先 + 回退
# ============================================================

class TestStockInfoLocalFirst:
    def _patch_fetcher(self, monkeypatch):
        fake = _FakeFetcherManager()
        monkeypatch.setattr(data_tools, "_get_fetcher_manager", lambda: fake)
        return fake

    def test_uses_local_when_fresh_and_never_calls_live(self, isolated_db, monkeypatch):
        fake = self._patch_fetcher(monkeypatch)
        _seed_profile(isolated_db, "sh600519")
        _seed_metrics(
            isolated_db,
            "sh600519",
            as_of=datetime.now(timezone.utc).date(),
            pe_ttm=19.39,
            pb=6.28,
            total_mv=1.57e12,
            circ_mv=1.57e12,
        )

        result = data_tools._handle_get_stock_info("600519")

        assert result["data_source"] == "local"
        assert fake.calls == []
        assert result["pe_ratio"] == 19.39
        assert result["pb_ratio"] == 6.28
        assert result["name"] == "贵州茅台"
        assert result["local_profile"]["industry"]["l1"] == "食品饮料"
        # 原有顶层键必须还在（形状不变，只加不改）
        for key in (
            "code", "name", "pe_ratio", "pb_ratio", "total_mv", "circ_mv",
            "fundamental_context", "belong_boards", "boards", "sector_rankings",
        ):
            assert key in result, key

    def test_falls_back_to_live_when_no_local_profile(self, isolated_db, monkeypatch):
        fake = self._patch_fetcher(monkeypatch)

        result = data_tools._handle_get_stock_info("600519")

        assert result["data_source"] == "live"
        assert result["data_source_reason"] == "no_local_profile"
        assert fake.calls == [("get_fundamental_context", "600519")]
        assert result["pe_ratio"] == 11.1
        assert result["belong_boards"] == [{"name": "白酒"}]
        assert result["local_snapshot"] is None

    def test_stale_local_falls_back_to_live_with_reason(self, isolated_db, monkeypatch):
        fake = self._patch_fetcher(monkeypatch)
        _seed_profile(
            isolated_db,
            "sh600519",
            updated_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60),
        )
        _seed_metrics(
            isolated_db,
            "sh600519",
            as_of=datetime.now(timezone.utc).date() - timedelta(days=20),
            pe_ttm=1.0,
            pb=1.0,
        )

        result = data_tools._handle_get_stock_info("600519")

        assert result["data_source"] == "local_stale_fallback_live"
        assert fake.calls == [("get_fundamental_context", "600519")]
        assert any("profile_stale" in r for r in result["local_stale_reasons"])
        assert any("metrics_stale" in r for r in result["local_stale_reasons"])
        # 回退时要把本地那份一起带出来，说明"本地有什么、为什么没用"
        assert result["local_snapshot"]["name"] == "贵州茅台"

    def test_local_read_error_degrades_to_live(self, isolated_db, monkeypatch):
        """本地读取抛异常绝不能变成问股失败 —— 降级为无本地数据，继续实时。"""
        fake = self._patch_fetcher(monkeypatch)
        # 库里其实有新鲜档案：只有本地读取炸了，才应该回退
        _seed_profile(isolated_db, "sh600519")
        _seed_metrics(
            isolated_db,
            "sh600519",
            as_of=datetime.now(timezone.utc).date(),
            pe_ttm=19.39,
            pb=6.28,
        )

        def _boom(_stock_code):
            raise RuntimeError("db exploded")

        # data_tools 在函数内部按名导入，因此必须 patch 源模块上的属性。
        monkeypatch.setattr(
            "src.agent.tools.local_data_tools._handle_get_company_profile",
            _boom,
        )

        result = data_tools._handle_get_stock_info("600519")

        assert result["data_source"] == "live"
        assert result["data_source_reason"] == "local_lookup_failed"
        assert fake.calls == [("get_fundamental_context", "600519")]
