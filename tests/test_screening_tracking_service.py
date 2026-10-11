# -*- coding: utf-8 -*-
"""选股结果复盘：入选记录 + 前瞻 K 线 → 收益 / 命中率 / 不可复盘原因。

这些用例全部跑在内存库上（``sqlite:///:memory:``），通过 ``DatabaseManager``
单例注入，不碰生产库 ``data/stock_analysis.db``。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.storage import DatabaseManager

PICK_DATE = date(2026, 10, 7)


def _insert_run(
    db,
    *,
    run_id: str,
    strategy: str = "dual_low",
    created_at: datetime,
    result_json,
    market: str = "cn",
) -> None:
    with db._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO screening_runs "
                "(run_id, strategy, market, candidate_count, result_json, created_at) "
                "VALUES (:run_id, :strategy, :market, :candidate_count, :result_json, :created_at)"
            ),
            {
                "run_id": run_id,
                "strategy": strategy,
                "market": market,
                "candidate_count": 1,
                "result_json": result_json,
                "created_at": created_at,
            },
        )


def _insert_bar(db, *, code: str, canonical_id: str, day: date, close: float, high=None, low=None) -> None:
    with db._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO stock_daily (code, canonical_id, date, close, high, low) "
                "VALUES (:code, :canonical_id, :date, :close, :high, :low)"
            ),
            {
                "code": code,
                "canonical_id": canonical_id,
                "date": day,
                "close": close,
                "high": high if high is not None else close,
                "low": low if low is not None else close,
            },
        )


def _candidate_json(code: str, name: str, price) -> str:
    import json

    payload = {
        "enabled": True,
        "candidates": [{"rank": 1, "code": code, "name": name, "price": price, "score": 80.0}],
    }
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture()
def tracking_db():
    """每个用例一个干净的内存库，并在结束后把单例清掉。"""
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url="sqlite:///:memory:")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()


def test_return_uses_pick_price_and_later_close(tracking_db):
    """收益率 = 选股时点价格 → 之后某日收盘价；同日 K 线不算前瞻收益。"""
    from src.services.screening_tracking_service import ScreeningTrackingService

    picked_at = datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc)
    _insert_run(
        tracking_db,
        run_id="run-return",
        created_at=picked_at,
        result_json=_candidate_json("601166", "兴业银行", 18.00),
    )
    # 同日（入选日）的 K 线：选股价格已经是当日快照，不能被算成 0% 收益。
    _insert_bar(tracking_db, code="601166", canonical_id="sh601166", day=PICK_DATE,
                close=18.00, high=18.10, low=17.90)
    _insert_bar(tracking_db, code="601166", canonical_id="sh601166", day=date(2026, 10, 8),
                close=18.24, high=18.50, low=18.00)
    _insert_bar(tracking_db, code="601166", canonical_id="sh601166", day=date(2026, 10, 9),
                close=19.80, high=20.00, low=17.50)

    out = ScreeningTrackingService(config=None, db_manager=tracking_db).tracking()

    assert out["total"]["picks"] == 1
    assert out["total"]["trackable"] == 1
    pick = out["picks"][0]
    assert pick["trackable"] is True
    assert pick["canonical_id"] == "sh601166"
    assert pick["latest_close"] == 19.80
    assert pick["latest_date"] == "2026-10-09"
    # (19.80 - 18.00) / 18.00 * 100
    assert pick["return_pct"] == pytest.approx(10.0)
    # 最大浮盈用区间最高价 20.00；(20.00 - 18.00) / 18.00 * 100
    assert pick["max_gain_pct"] == pytest.approx(11.11, abs=0.01)
    # 最大回撤用区间最低价 17.50
    assert pick["max_drawdown_pct"] == pytest.approx(-2.78, abs=0.01)
    assert pick["days_held"] == 2
    assert pick["forward_bars"] == 2


def test_untrackable_pick_is_excluded_from_hit_rate_denominator(tracking_db):
    """不可复盘的入选必须单列，不能当 0% 计入命中率分母。

    反例对照：命中 1 次、可复盘 1 只时命中率是 100%；若把不可复盘的
    那只按 0% 计入分母，会变成 50% —— 这正是要防的假数字。
    """
    from src.services.screening_tracking_service import ScreeningTrackingService

    picked_at = datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc)
    _insert_run(
        tracking_db,
        run_id="run-a",
        created_at=picked_at,
        result_json=_candidate_json("601166", "兴业银行", 18.00),
    )
    _insert_run(
        tracking_db,
        run_id="run-b",
        created_at=picked_at,
        result_json=_candidate_json("600036", "招商银行", 41.00),
    )
    _insert_bar(tracking_db, code="601166", canonical_id="sh601166", day=date(2026, 10, 8),
                close=19.80)

    out = ScreeningTrackingService(config=None, db_manager=tracking_db).tracking()

    assert out["total"]["picks"] == 2
    assert out["total"]["trackable"] == 1
    assert out["total"]["untrackable"] == 1
    # 分母只有 1 只可复盘的 → 命中率 100%，而不是 50%
    assert out["total"]["hit_rate"] == pytest.approx(1.0)
    assert out["total"]["hit_rate_basis"] == "trackable_only"
    assert out["total"]["avg_return_pct"] == pytest.approx(10.0)

    untrackable = out["untrackable"]
    assert len(untrackable) == 1
    assert untrackable[0]["code"] == "600036"
    assert untrackable[0]["reason"] == "no_daily_rows"
    # 不可复盘 ≠ 0% 收益：这里必须是 None
    assert untrackable[0]["return_pct"] is None
    assert untrackable[0]["trackable"] is False
    assert out["untrackable_reasons"] == [{"reason": "no_daily_rows", "count": 1}]


def test_pick_with_only_stale_bars_is_untrackable_with_specific_reason(tracking_db):
    """有 K 线但都早于入选日：原因必须是 no_forward_bars，而不是 no_daily_rows。

    这个区分决定用户下一步该做什么：补刷新 vs 等新行情。
    """
    from src.services.screening_tracking_service import ScreeningTrackingService

    picked_at = datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc)
    _insert_run(
        tracking_db,
        run_id="run-stale",
        created_at=picked_at,
        result_json=_candidate_json("601166", "兴业银行", 18.00),
    )
    _insert_bar(tracking_db, code="601166", canonical_id="sh601166", day=date(2026, 9, 30),
                close=17.00)

    out = ScreeningTrackingService(config=None, db_manager=tracking_db).tracking()

    assert out["total"]["trackable"] == 0
    assert out["untrackable_reasons"] == [{"reason": "no_forward_bars", "count": 1}]
    assert out["picks"][0]["reason"] == "no_forward_bars"
    # 即使一只都不可复盘，行情新鲜度也要如实报告本地 K 线到哪一天，
    # 而不是因为"没有可复盘入选"就显示成没有行情。
    assert out["data_freshness"]["stock_daily_latest_date"] == "2026-09-30"
    assert out["data_freshness"]["pick_forward_bars_available"] is False


def test_missing_pick_price_is_untrackable_not_zero(tracking_db):
    """没有选股价格就没有分母，不能被当成 0 元买入。"""
    import json

    from src.services.screening_tracking_service import ScreeningTrackingService

    picked_at = datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc)
    _insert_run(
        tracking_db,
        run_id="run-noprice",
        created_at=picked_at,
        result_json=json.dumps({"candidates": [{"code": "601166", "name": "兴业银行"}]}),
    )
    _insert_bar(tracking_db, code="601166", canonical_id="sh601166", day=date(2026, 10, 8),
                close=19.80)

    out = ScreeningTrackingService(config=None, db_manager=tracking_db).tracking()

    assert out["total"]["trackable"] == 0
    assert out["untrackable_reasons"] == [{"reason": "missing_pick_price", "count": 1}]


def test_malformed_result_json_does_not_raise_and_is_counted(tracking_db):
    """畸形 result_json 只跳过该次运行并计数，不影响其它运行。"""
    from src.services.screening_tracking_service import ScreeningTrackingService

    picked_at = datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc)
    _insert_run(tracking_db, run_id="run-broken", created_at=picked_at, result_json="{not json")
    _insert_run(tracking_db, run_id="run-ok", created_at=picked_at,
                result_json=_candidate_json("601166", "兴业银行", 18.00))
    _insert_bar(tracking_db, code="601166", canonical_id="sh601166", day=date(2026, 10, 8),
                close=19.80)

    out = ScreeningTrackingService(config=None, db_manager=tracking_db).tracking()

    assert out["run_window"]["malformed_runs"] == 1
    assert out["total"]["picks"] == 1
    assert out["picks"][0]["code"] == "601166"


def test_strategy_filter_and_aggregates(tracking_db):
    """按策略过滤时只统计该策略的入选。"""
    from src.services.screening_tracking_service import ScreeningTrackingService

    picked_at = datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc)
    _insert_run(tracking_db, run_id="run-dl", strategy="dual_low", created_at=picked_at,
                result_json=_candidate_json("601166", "兴业银行", 18.00))
    _insert_run(tracking_db, run_id="run-mq", strategy="momentum_quality", created_at=picked_at,
                result_json=_candidate_json("600036", "招商银行", 41.00))
    _insert_bar(tracking_db, code="601166", canonical_id="sh601166", day=date(2026, 10, 8),
                close=19.80)

    service = ScreeningTrackingService(config=None, db_manager=tracking_db)
    everything = service.tracking()
    only_dual_low = service.tracking(strategy="dual_low")

    assert everything["total"]["picks"] == 2
    assert only_dual_low["total"]["picks"] == 1
    assert [item["strategy"] for item in only_dual_low["strategies"]] == ["dual_low"]


def test_benchmark_is_reported_unavailable_with_evidence(tracking_db):
    """单日横截面 + 无指数序列 → 明确报告不可用，而不是编一个基准。"""
    from src.services.screening_tracking_service import ScreeningTrackingService

    out = ScreeningTrackingService(config=None, db_manager=tracking_db).tracking()

    benchmark = out["benchmark"]
    assert benchmark["available"] is False
    assert benchmark["index_series"] == []
    assert benchmark["reason"] in {
        "market_snapshot_is_single_date_cross_section",
        "no_index_and_no_multi_date_snapshot",
    }


class TestTrackedDailyCodesBridge:
    """行情刷新清单必须带上选股入选 —— 否则 K 线永远补不上。"""

    @staticmethod
    def _config(**overrides):
        from src.config import Config

        params = {
            "stock_list": ["600519"],
            "screening_pick_tracking_enabled": True,
            "screening_pick_tracking_days": 60,
            "screening_pick_tracking_max_codes": 200,
        }
        params.update(overrides)
        return Config(**params)

    def test_pick_codes_are_tracked_and_deduped(self, tracking_db):
        from src.services.runtime_scheduler import _tracked_daily_codes

        now = datetime.now(timezone.utc)
        _insert_run(tracking_db, run_id="r1", created_at=now,
                    result_json=_candidate_json("300457", "赢合科技", 22.72))
        # 与自选股重复的入选：去重后只能出现一次
        _insert_run(tracking_db, run_id="r2", created_at=now,
                    result_json=_candidate_json("600519", "贵州茅台", 1500.0))

        codes = _tracked_daily_codes(self._config())

        assert "600519" in codes
        assert "300457" in codes
        assert codes.count("600519") == 1
        assert len(codes) == len(set(codes))

    def test_cap_limits_added_pick_codes(self, tracking_db):
        from src.services.screening_tracking_service import list_recent_pick_codes

        now = datetime.now(timezone.utc)
        for index, code in enumerate(["300457", "600015", "601818", "601390", "000001"]):
            _insert_run(tracking_db, run_id=f"cap-{index}", created_at=now - timedelta(minutes=index),
                        result_json=_candidate_json(code, code, 10.0))

        picked = list_recent_pick_codes(config=self._config(), max_codes=2)

        assert len(picked) == 2
        # 最新一次运行排在最前
        assert picked[0] == "300457"

    def test_out_of_window_runs_are_ignored(self, tracking_db):
        from src.services.runtime_scheduler import _tracked_daily_codes

        now = datetime.now(timezone.utc)
        _insert_run(tracking_db, run_id="recent", created_at=now,
                    result_json=_candidate_json("300457", "赢合科技", 22.72))
        _insert_run(tracking_db, run_id="old", created_at=now - timedelta(days=90),
                    result_json=_candidate_json("601818", "光大银行", 3.04))

        codes = _tracked_daily_codes(self._config(screening_pick_tracking_days=60))

        assert "300457" in codes
        assert "601818" not in codes

    def test_disabled_tracking_adds_nothing(self, tracking_db):
        from src.services.runtime_scheduler import _tracked_daily_codes

        now = datetime.now(timezone.utc)
        _insert_run(tracking_db, run_id="r1", created_at=now,
                    result_json=_candidate_json("300457", "赢合科技", 22.72))

        codes = _tracked_daily_codes(
            self._config(screening_pick_tracking_enabled=False)
        )

        assert "300457" not in codes
        assert "600519" in codes

    def test_unparseable_result_json_fails_open(self, tracking_db):
        """解析失败必须退回原清单，而不是让整个行情刷新挂掉。"""
        from src.services.runtime_scheduler import _tracked_daily_codes

        _insert_run(tracking_db, run_id="broken", created_at=datetime.now(timezone.utc),
                    result_json="{{{" )

        codes = _tracked_daily_codes(self._config())

        assert codes == ["600519"]

    def test_list_recent_pick_codes_fails_open_on_query_error(self):
        """查询本身炸掉时返回空列表（fail-open）。"""

        class _BrokenDb:
            def get_session(self):
                raise RuntimeError("db is gone")

        from src.services.screening_tracking_service import list_recent_pick_codes

        assert list_recent_pick_codes(config=self._config(), db_manager=_BrokenDb()) == []
