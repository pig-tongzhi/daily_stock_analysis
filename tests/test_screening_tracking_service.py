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


# ---------------------------------------------------------------------------
# 基准对比：每只入选用**自己的窗口**，拿不到就报原因码而不是 0
# ---------------------------------------------------------------------------

BENCHMARK_CODE = "sh000300"


def _benchmark_config(**overrides):
    from src.config import Config

    params = {
        "stock_list": [],
        "screening_benchmark_index_enabled": True,
        "screening_benchmark_index_codes": [BENCHMARK_CODE],
    }
    params.update(overrides)
    return Config(**params)


def _insert_index_bar(db, *, day: date, close: float) -> None:
    """写一条指数 K 线；code/canonical_id/data_source 与落库时的口径一致。"""
    with db._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO stock_daily (code, canonical_id, date, close, high, low, data_source) "
                "VALUES (:code, :canonical_id, :date, :close, :high, :low, :source)"
            ),
            {
                "code": BENCHMARK_CODE,
                "canonical_id": BENCHMARK_CODE,
                "date": day,
                "close": close,
                "high": close,
                "low": close,
                "source": "akshare_index",
            },
        )


class TestBenchmarkComparison:
    """选股收益必须能对照同期基准，而基准区间必须随每只入选自己的窗口走。"""

    def test_index_up_10_pick_up_5_is_underperformance_not_beat(self, tracking_db):
        """指数涨 10%、个股涨 5% → 超额 -5，是跑输，不是"跑赢"。

        反例对照：只看 return_pct=+5 会得出"赚钱了"的结论；同期大盘 +10%，
        真实结论是跑输 5 个百分点。
        """
        from src.services.screening_tracking_service import ScreeningTrackingService

        picked_at = datetime(2026, 10, 6, 15, 30, tzinfo=timezone.utc)
        _insert_run(tracking_db, run_id="run-lag", created_at=picked_at,
                    result_json=_candidate_json("601166", "兴业银行", 100.0))
        _insert_bar(tracking_db, code="601166", canonical_id="sh601166",
                    day=date(2026, 10, 6), close=100.0)
        _insert_bar(tracking_db, code="601166", canonical_id="sh601166",
                    day=date(2026, 10, 7), close=105.0)
        # 指数：入选日 100 → 次日 110
        _insert_index_bar(tracking_db, day=date(2026, 10, 5), close=100.0)
        _insert_index_bar(tracking_db, day=date(2026, 10, 6), close=100.0)
        _insert_index_bar(tracking_db, day=date(2026, 10, 7), close=110.0)

        out = ScreeningTrackingService(config=_benchmark_config(), db_manager=tracking_db).tracking()

        pick = out["picks"][0]
        assert pick["return_pct"] == pytest.approx(5.0)
        assert pick["benchmark_available"] is True
        assert pick["benchmark_code"] == BENCHMARK_CODE
        # 基准用入选日收盘 100 → 入选最新前瞻 K 线日 110
        assert pick["benchmark_start_date"] == "2026-10-06"
        assert pick["benchmark_end_date"] == "2026-10-07"
        assert pick["benchmark_return_pct"] == pytest.approx(10.0)
        assert pick["excess_return_pct"] == pytest.approx(-5.0)

        assert out["total"]["benchmark_comparable"] == 1
        assert out["total"]["beat_benchmark_rate"] == pytest.approx(0.0)
        assert out["total"]["avg_excess_return_pct"] == pytest.approx(-5.0)
        assert out["benchmark"]["available"] is True
        assert out["benchmark"]["code"] == BENCHMARK_CODE
        assert out["benchmark"]["comparable_picks"] == 1

    def test_each_pick_uses_its_own_window_not_one_global_window(self, tracking_db):
        """两只不同日期的入选，必须各用自己的窗口比基准。

        指数走势刻意做成"前段横盘、后段跳涨"：
            10-05..10-08 = 100（横盘），10-09 = 110（+10%）
        入选 A（10-06 买入，10-07 收盘 +3%）真实同期基准是 100→100 = 0% → 超额 +3。
        入选 B（10-08 买入，10-09 收盘 +4%）真实同期基准是 100→110 = +10% → 超额 -6。

        如果偷懒用一个全局窗口（最早入选日 10-06 → 最新前瞻日 10-09），基准对两只
        都是 +10%，A 的超额会被算成 -7、平均超额 -6.5、跑赢比例 0/2 —— 三个数字
        全错，而且错得看不出来。这里逐一断言每只自己的答案。
        """
        from src.services.screening_tracking_service import ScreeningTrackingService

        _insert_run(tracking_db, run_id="run-a",
                    created_at=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
                    result_json=_candidate_json("601166", "兴业银行", 100.0))
        _insert_run(tracking_db, run_id="run-b",
                    created_at=datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc),
                    result_json=_candidate_json("600036", "招商银行", 100.0))
        # A：只有到 10-07 的前瞻 K 线 → 窗口止于 10-07
        _insert_bar(tracking_db, code="601166", canonical_id="sh601166",
                    day=date(2026, 10, 6), close=100.0)
        _insert_bar(tracking_db, code="601166", canonical_id="sh601166",
                    day=date(2026, 10, 7), close=103.0)
        # B：窗口止于 10-09
        _insert_bar(tracking_db, code="600036", canonical_id="sh600036",
                    day=date(2026, 10, 8), close=100.0)
        _insert_bar(tracking_db, code="600036", canonical_id="sh600036",
                    day=date(2026, 10, 9), close=104.0)

        for day, close in [
            (date(2026, 10, 5), 100.0),
            (date(2026, 10, 6), 100.0),
            (date(2026, 10, 7), 100.0),
            (date(2026, 10, 8), 100.0),
            (date(2026, 10, 9), 110.0),
        ]:
            _insert_index_bar(tracking_db, day=day, close=close)

        out = ScreeningTrackingService(config=_benchmark_config(), db_manager=tracking_db).tracking()
        by_code = {row["code"]: row for row in out["picks"]}

        a, b = by_code["601166"], by_code["600036"]
        # A：10-06 → 10-07，指数横盘不动
        assert a["benchmark_start_date"] == "2026-10-06"
        assert a["benchmark_end_date"] == "2026-10-07"
        assert a["benchmark_return_pct"] == pytest.approx(0.0)
        assert a["excess_return_pct"] == pytest.approx(3.0)
        # B：10-08 → 10-09，指数跳涨 10%
        assert b["benchmark_start_date"] == "2026-10-08"
        assert b["benchmark_end_date"] == "2026-10-09"
        assert b["benchmark_return_pct"] == pytest.approx(10.0)
        assert b["excess_return_pct"] == pytest.approx(-6.0)

        # 汇总：平均超额 (3 + -6)/2 = -1.5，跑赢 1/2
        assert out["total"]["avg_benchmark_return_pct"] == pytest.approx(5.0)
        assert out["total"]["avg_excess_return_pct"] == pytest.approx(-1.5)
        assert out["total"]["median_excess_return_pct"] == pytest.approx(-1.5)
        assert out["total"]["beat_benchmark_rate"] == pytest.approx(0.5)
        assert out["total"]["beat_benchmark_denominator"] == 2
        # 全局窗口会给出 -6.5 / 0.0，这里显式钉住"不是那个答案"
        assert out["total"]["avg_excess_return_pct"] != pytest.approx(-6.5)
        assert out["total"]["beat_benchmark_rate"] != pytest.approx(0.0)

    def test_missing_index_series_is_unavailable_with_reason_not_zero(self, tracking_db):
        """本地没有指数序列 → 基准不可用 + 原因码；超额必须是 None，不是 0。"""
        from src.services.screening_tracking_service import ScreeningTrackingService

        _insert_run(tracking_db, run_id="run-noidx",
                    created_at=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
                    result_json=_candidate_json("601166", "兴业银行", 100.0))
        _insert_bar(tracking_db, code="601166", canonical_id="sh601166",
                    day=date(2026, 10, 7), close=105.0)

        out = ScreeningTrackingService(config=_benchmark_config(), db_manager=tracking_db).tracking()

        pick = out["picks"][0]
        assert pick["trackable"] is True
        assert pick["return_pct"] == pytest.approx(5.0)
        assert pick["benchmark_available"] is False
        assert pick["benchmark_reason"] == "benchmark_series_missing"
        # "没有基准" ≠ "与基准持平"
        assert pick["benchmark_return_pct"] is None
        assert pick["excess_return_pct"] is None

        assert out["benchmark"]["available"] is False
        assert out["benchmark"]["code"] is None
        total = out["total"]
        assert total["benchmark_comparable"] == 0
        assert total["beat_benchmark_rate"] is None
        assert total["beat_benchmark_basis"] == "no_comparable_picks"
        assert total["beat_benchmark_denominator"] == 0
        assert total["avg_excess_return_pct"] is None
        assert total["benchmark_unavailable_reasons"] == [
            {"reason": "benchmark_series_missing", "count": 1}
        ]

    def test_pick_window_outside_index_coverage_drops_out_of_denominator(self, tracking_db):
        """入选窗口落在指数覆盖范围之外 → 该只不可比，分母同步缩小并说明口径。

        指数从 10-07 才开始有数据，因此 10-06 入选的 A 找不到起点，只能报
        window_not_covered；B 的窗口完整覆盖，分母是 1 而不是"可复盘 2 只"。
        """
        from src.services.screening_tracking_service import ScreeningTrackingService

        _insert_run(tracking_db, run_id="run-early",
                    created_at=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
                    result_json=_candidate_json("601166", "兴业银行", 100.0))
        _insert_run(tracking_db, run_id="run-covered",
                    created_at=datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc),
                    result_json=_candidate_json("600036", "招商银行", 100.0))
        _insert_bar(tracking_db, code="601166", canonical_id="sh601166",
                    day=date(2026, 10, 7), close=102.0)
        _insert_bar(tracking_db, code="600036", canonical_id="sh600036",
                    day=date(2026, 10, 9), close=101.0)
        # 指数最早只有 10-07
        _insert_index_bar(tracking_db, day=date(2026, 10, 7), close=100.0)
        _insert_index_bar(tracking_db, day=date(2026, 10, 9), close=110.0)

        out = ScreeningTrackingService(config=_benchmark_config(), db_manager=tracking_db).tracking()
        by_code = {row["code"]: row for row in out["picks"]}

        early = by_code["601166"]
        assert early["trackable"] is True
        assert early["benchmark_available"] is False
        assert early["benchmark_reason"] == "benchmark_window_not_covered"
        assert early["excess_return_pct"] is None

        covered = by_code["600036"]
        assert covered["benchmark_start_date"] == "2026-10-07"
        assert covered["benchmark_end_date"] == "2026-10-09"
        assert covered["excess_return_pct"] == pytest.approx(-9.0)

        total = out["total"]
        assert total["trackable"] == 2
        assert total["benchmark_comparable"] == 1
        # 分母被调整过，而且口径写明是可比的 trackable
        assert total["beat_benchmark_denominator"] == 1
        assert total["beat_benchmark_basis"] == "benchmark_comparable_trackable_picks"
        assert total["avg_excess_return_pct"] == pytest.approx(-9.0)
        assert total["benchmark_unavailable_reasons"] == [
            {"reason": "benchmark_window_not_covered", "count": 1}
        ]

    def test_beat_benchmark_when_pick_outruns_flat_index(self, tracking_db):
        """指数不动、个股上涨 → 跑赢比例 100%，超额为正。"""
        from src.services.screening_tracking_service import ScreeningTrackingService

        _insert_run(tracking_db, run_id="run-beat",
                    created_at=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
                    result_json=_candidate_json("601166", "兴业银行", 100.0))
        _insert_bar(tracking_db, code="601166", canonical_id="sh601166",
                    day=date(2026, 10, 6), close=100.0)
        _insert_bar(tracking_db, code="601166", canonical_id="sh601166",
                    day=date(2026, 10, 7), close=104.0)
        _insert_index_bar(tracking_db, day=date(2026, 10, 6), close=100.0)
        _insert_index_bar(tracking_db, day=date(2026, 10, 7), close=100.0)

        out = ScreeningTrackingService(config=_benchmark_config(), db_manager=tracking_db).tracking()

        assert out["total"]["beat_benchmark_rate"] == pytest.approx(1.0)
        assert out["total"]["avg_excess_return_pct"] == pytest.approx(4.0)
        assert out["picks"][0]["excess_return_pct"] == pytest.approx(4.0)

    def test_per_strategy_aggregate_carries_benchmark_fields(self, tracking_db):
        """分策略聚合同样带基准字段，且只用该策略自己的入选。"""
        from src.services.screening_tracking_service import ScreeningTrackingService

        _insert_run(tracking_db, run_id="run-dl", strategy="dual_low",
                    created_at=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
                    result_json=_candidate_json("601166", "兴业银行", 100.0))
        _insert_run(tracking_db, run_id="run-mq", strategy="momentum_quality",
                    created_at=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
                    result_json=_candidate_json("600036", "招商银行", 100.0))
        _insert_bar(tracking_db, code="601166", canonical_id="sh601166",
                    day=date(2026, 10, 6), close=100.0)
        _insert_bar(tracking_db, code="601166", canonical_id="sh601166",
                    day=date(2026, 10, 7), close=105.0)
        _insert_bar(tracking_db, code="600036", canonical_id="sh600036",
                    day=date(2026, 10, 6), close=100.0)
        _insert_bar(tracking_db, code="600036", canonical_id="sh600036",
                    day=date(2026, 10, 7), close=90.0)
        _insert_index_bar(tracking_db, day=date(2026, 10, 6), close=100.0)
        _insert_index_bar(tracking_db, day=date(2026, 10, 7), close=110.0)

        out = ScreeningTrackingService(config=_benchmark_config(), db_manager=tracking_db).tracking()
        by_strategy = {row["strategy"]: row for row in out["strategies"]}

        assert by_strategy["dual_low"]["beat_benchmark_rate"] == pytest.approx(0.0)
        assert by_strategy["dual_low"]["avg_excess_return_pct"] == pytest.approx(-5.0)
        assert by_strategy["dual_low"]["benchmark_comparable"] == 1
        assert by_strategy["momentum_quality"]["beat_benchmark_rate"] == pytest.approx(0.0)
        assert by_strategy["momentum_quality"]["avg_excess_return_pct"] == pytest.approx(-20.0)

    def test_disabled_configuration_reports_disabled_reason(self, tracking_db):
        """关掉基准功能 → 即使本地有指数序列也不比，并给出 benchmark_disabled。"""
        from src.services.screening_tracking_service import ScreeningTrackingService

        _insert_run(tracking_db, run_id="run-off",
                    created_at=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
                    result_json=_candidate_json("601166", "兴业银行", 100.0))
        _insert_bar(tracking_db, code="601166", canonical_id="sh601166",
                    day=date(2026, 10, 7), close=105.0)
        _insert_index_bar(tracking_db, day=date(2026, 10, 6), close=100.0)
        _insert_index_bar(tracking_db, day=date(2026, 10, 7), close=110.0)

        out = ScreeningTrackingService(
            config=_benchmark_config(screening_benchmark_index_enabled=False),
            db_manager=tracking_db,
        ).tracking()

        assert out["picks"][0]["benchmark_available"] is False
        assert out["picks"][0]["benchmark_reason"] == "benchmark_disabled"
        assert out["picks"][0]["excess_return_pct"] is None

    def test_untrackable_pick_reports_pick_untrackable_not_zero(self, tracking_db):
        """入选自己不可复盘 → 基准原因是 pick_untrackable，而不是"与大盘持平"。"""
        from src.services.screening_tracking_service import ScreeningTrackingService

        _insert_run(tracking_db, run_id="run-untrackable",
                    created_at=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
                    result_json=_candidate_json("601166", "兴业银行", 100.0))
        _insert_index_bar(tracking_db, day=date(2026, 10, 6), close=100.0)
        _insert_index_bar(tracking_db, day=date(2026, 10, 7), close=110.0)

        out = ScreeningTrackingService(config=_benchmark_config(), db_manager=tracking_db).tracking()

        assert out["picks"][0]["trackable"] is False
        assert out["picks"][0]["benchmark_reason"] == "pick_untrackable"
        assert out["picks"][0]["excess_return_pct"] is None
        # 不可复盘的入选不进可比分母
        assert out["total"]["beat_benchmark_denominator"] == 0

    def test_configured_index_missing_falls_back_to_local_series_and_names_it(self, tracking_db):
        """配置的指数本地没有、但存在别的指数序列 → 用它，并在 code 里写明是哪条。"""
        from src.services.screening_tracking_service import ScreeningTrackingService

        _insert_run(tracking_db, run_id="run-fallback",
                    created_at=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
                    result_json=_candidate_json("601166", "兴业银行", 100.0))
        _insert_bar(tracking_db, code="601166", canonical_id="sh601166",
                    day=date(2026, 10, 7), close=105.0)
        for day, close in [
            (date(2026, 10, 6), 100.0),
            (date(2026, 10, 7), 120.0),
        ]:
            with tracking_db._engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO stock_daily (code, canonical_id, date, close, high, low, data_source) "
                        "VALUES ('sh000905', 'sh000905', :date, :close, :close, :close, 'akshare_index')"
                    ),
                    {"date": day, "close": close},
                )

        out = ScreeningTrackingService(config=_benchmark_config(), db_manager=tracking_db).tracking()

        assert out["benchmark"]["code"] == "sh000905"
        assert out["picks"][0]["benchmark_code"] == "sh000905"
        assert out["picks"][0]["excess_return_pct"] == pytest.approx(-15.0)


class TestBenchmarkIndexConfig:
    """基准指数配置解析：list / 逗号串 / 中文逗号 / 关闭。"""

    @staticmethod
    def _namespace(**kwargs):
        from types import SimpleNamespace

        params = {
            "screening_benchmark_index_enabled": True,
            "screening_benchmark_index_codes": ["sh000300", "sh000905"],
        }
        params.update(kwargs)
        return SimpleNamespace(**params)

    def test_list_value_is_used_as_is(self):
        from src.services.benchmark_index_service import resolve_benchmark_index_codes

        assert resolve_benchmark_index_codes(self._namespace()) == ["sh000300", "sh000905"]

    def test_comma_string_is_split_and_deduped(self):
        """字符串值不能被当成 list 的 repr 处理，中文逗号也要能分。"""
        from src.services.benchmark_index_service import resolve_benchmark_index_codes

        config = self._namespace(screening_benchmark_index_codes="sh000300，sh000905,SH000300")

        assert resolve_benchmark_index_codes(config) == ["sh000300", "sh000905"]

    def test_disabled_returns_empty(self):
        from src.services.benchmark_index_service import resolve_benchmark_index_codes

        assert resolve_benchmark_index_codes(
            self._namespace(screening_benchmark_index_enabled=False)
        ) == []

    def test_missing_config_falls_back_to_defaults(self):
        from src.services.benchmark_index_service import (
            DEFAULT_BENCHMARK_INDEX_CODES,
            resolve_benchmark_index_codes,
        )

        assert resolve_benchmark_index_codes(None) == list(DEFAULT_BENCHMARK_INDEX_CODES)


class TestBenchmarkIndexIngestion:
    """指数落库：标记、日期格式、以及与既有 stock_daily 自愈迁移的相互作用。"""

    @staticmethod
    def _frame():
        import pandas as pd

        return pd.DataFrame(
            [
                {"date": date(2026, 10, 8), "open": 4300.0, "high": 4390.0,
                 "low": 4290.0, "close": 4310.0, "volume": 19155271100},
                {"date": date(2026, 10, 9), "open": 4297.0, "high": 4331.0,
                 "low": 4240.0, "close": 4317.255, "volume": 19918741600},
            ]
        )

    def test_saved_rows_are_marked_as_index_and_use_explicit_code(self, tracking_db):
        """code 用显式指数形式、canonical_id 由 storage 推导、data_source 固定标记。"""
        from src.services.benchmark_index_service import INDEX_DATA_SOURCE, save_index_bars

        written = save_index_bars(tracking_db, "sh000300", self._frame())

        assert written == 2
        with tracking_db._engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT code, canonical_id, data_source, date, close FROM stock_daily "
                    "ORDER BY date"
                )
            ).fetchall()
        assert [row[0] for row in rows] == ["sh000300", "sh000300"]
        assert [row[1] for row in rows] == ["sh000300", "sh000300"]
        assert {row[2] for row in rows} == {INDEX_DATA_SOURCE}
        # 日期列与既有行同格式：SQLite DATE 存成 'YYYY-MM-DD'
        assert [str(row[3]) for row in rows] == ["2026-10-08", "2026-10-09"]
        assert rows[1][4] == pytest.approx(4317.255)

    def test_existing_self_healing_migrations_leave_index_rows_alone(self, tracking_db):
        """storage 的两个 canonical_id 自愈迁移都不能改写指数行。

        _backfill_stock_daily_canonical_id 只处理 canonical_id IS NULL；
        _backfill_canonical_ids 只修"裸六位数字码"。显式 sh 前缀两边都不沾，
        这正是选择显式指数的原因。
        """
        from src.services.benchmark_index_service import save_index_bars

        save_index_bars(tracking_db, "sh000300", self._frame())
        tracking_db._backfill_stock_daily_canonical_id()
        tracking_db._backfill_canonical_ids()

        with tracking_db._engine.begin() as conn:
            rows = conn.execute(
                text("SELECT canonical_id FROM stock_daily WHERE code = 'sh000300'")
            ).fetchall()
        assert [row[0] for row in rows] == ["sh000300", "sh000300"]

    def test_unregistered_index_code_is_not_written(self, tracking_db):
        """推导不出 canonical_id 的代码不写库 —— 否则只会留下对不上号的孤儿行。"""
        from src.services.benchmark_index_service import save_index_bars

        assert save_index_bars(tracking_db, "csi999999", self._frame()) == 0
        with tracking_db._engine.begin() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM stock_daily")).scalar()
        assert count == 0

    def test_sync_is_fail_open_per_index(self, tracking_db, monkeypatch):
        """单个指数抓取失败只影响它自己，不影响其它指数、也不抛异常。"""
        from src.services import benchmark_index_service as service

        def fake_fetch(code, *, history_days=service.DEFAULT_HISTORY_DAYS):
            if code == "sh000905":
                raise RuntimeError("proxy exploded")
            return TestBenchmarkIndexIngestion._frame()

        monkeypatch.setattr(service, "fetch_index_daily", fake_fetch)

        results = service.sync_benchmark_index_bars(
            codes=["sh000300", "sh000905"], db_manager=tracking_db
        )

        assert [item["code"] for item in results] == ["sh000300", "sh000905"]
        assert results[0]["ok"] is True and results[0]["rows"] == 2
        assert results[1]["ok"] is False and results[1]["rows"] == 0
        assert "proxy exploded" in results[1]["error"]

    def test_index_codes_are_not_fed_to_the_stock_refresh_loop(self, tracking_db, monkeypatch):
        """指数代码不能混进个股刷新清单（会被当个股抓、并打上个股的 data_source）。

        这里断言两件事：
        1. _tracked_daily_codes 不会返回 sh000300；
        2. 行情刷新任务仍然会同步基准 —— 通过独立的指数路径。
        """
        from src.services import benchmark_index_service as service
        from src.services import runtime_scheduler

        _insert_run(tracking_db, run_id="r-idx",
                    created_at=datetime.now(timezone.utc),
                    result_json=_candidate_json("300457", "赢合科技", 22.72))

        config = _benchmark_config(**{
            "stock_daily_refresh_enabled": True,
            "stock_daily_refresh_interval_minutes": 60,
        })
        assert "sh000300" not in runtime_scheduler._tracked_daily_codes(config)

        calls = []
        monkeypatch.setattr(
            service, "sync_benchmark_index_bars", lambda **kwargs: calls.append(kwargs) or []
        )
        tasks = runtime_scheduler.build_stock_daily_refresh_background_tasks(
            config, config_provider=lambda: config
        )
        # 清空自选股与入选，让个股循环在一开始就返回（避免真实网络请求），
        # 从而单独验证 finally 里的基准同步一定被执行。
        monkeypatch.setattr(runtime_scheduler, "_tracked_daily_codes", lambda _config: [])
        tasks[0]["task"]()

        assert len(calls) == 1

