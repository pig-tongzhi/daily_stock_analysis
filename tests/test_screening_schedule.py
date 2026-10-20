# -*- coding: utf-8 -*-
"""选股定时：墙钟时间判断、当日去重、周末跳过、补跑窗口。

全部跑在内存库上（``sqlite:///:memory:``），并用假的 ``ScreeningService``
拦截真实选股：本文件不触网、不调 LLM、不碰生产库。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.services import runtime_scheduler
from src.storage import DatabaseManager


@pytest.fixture(autouse=True)
def _isolate_snapshot_dir(tmp_path, monkeypatch):
    """把快照目录引到 tmp。

    定时 tick 跑完会物化 + 导当日快照；不隔离的话，测试会拿内存库的空结果去覆盖
    真实快照目录里的当天文件（曾把 3KB 的快照压成只剩表头的 111 字节）。
    """
    from src.services import screening_pick_export_service as export_service

    monkeypatch.setattr(
        export_service, "DEFAULT_SNAPSHOT_DIR", str(tmp_path / "screening-snapshots")
    )
    return tmp_path


@pytest.fixture()
def memory_db():
    """每个用例一个干净的内存库，结束后清掉单例。"""
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url="sqlite:///:memory:")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()


def _config(**overrides):
    base = dict(
        screening_schedule_enabled=True,
        screening_schedule_times=["15:20"],
        screening_schedule_strategies=["dual_low", "quality_value"],
        screening_schedule_market="cn",
        screening_schedule_max_results=5,
        screening_schedule_weekdays_only=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _freeze(monkeypatch, moment: datetime) -> None:
    """把模块内的 datetime.now() 冻结到指定时刻。"""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102 - 与被替换对象保持同签名
            return moment

    monkeypatch.setattr(runtime_scheduler, "datetime", _Frozen)


def _capture_screen(monkeypatch) -> list:
    """替换 ScreeningService，记录被调用的 (strategy, market, max_results)。"""
    calls: list = []

    class _FakeService:
        def __init__(self, config=None, db_manager=None):  # noqa: D107
            self.config = config

        def screen(self, *, strategy, market, max_results, selection_seed=None):
            calls.append((strategy, market, max_results))
            return {"run_id": f"run-{strategy}", "candidate_count": 3}

    monkeypatch.setattr(
        "src.services.screening_service.ScreeningService", _FakeService, raising=True
    )
    return calls


def test_builder_requires_enabled_times_and_strategies():
    """开关/时间点/策略列表缺一不可：宁可什么都不跑，也不要"开了一半"就跑满全策略。"""
    provider = lambda: _config()  # noqa: E731

    assert runtime_scheduler.build_screening_schedule_background_tasks(
        _config(screening_schedule_enabled=False), config_provider=provider
    ) == []
    assert runtime_scheduler.build_screening_schedule_background_tasks(
        _config(screening_schedule_times=[]), config_provider=provider
    ) == []
    assert runtime_scheduler.build_screening_schedule_background_tasks(
        _config(screening_schedule_strategies=[]), config_provider=provider
    ) == []

    entries = runtime_scheduler.build_screening_schedule_background_tasks(
        _config(), config_provider=provider
    )
    assert len(entries) == 1
    assert entries[0]["name"] == "screening_schedule"
    assert entries[0]["interval_seconds"] == 60
    assert entries[0]["run_immediately"] is False


def test_tick_does_nothing_before_scheduled_time(monkeypatch, memory_db):
    """到点之前一次都不能跑。"""
    calls = _capture_screen(monkeypatch)
    _freeze(monkeypatch, datetime(2026, 10, 16, 14, 0, 0))  # 周五 14:00

    runtime_scheduler._run_screening_schedule_tick(_config())

    assert calls == []


def test_tick_runs_each_strategy_once_when_due(monkeypatch, memory_db):
    """到点后按配置顺序把每个策略跑一次。"""
    calls = _capture_screen(monkeypatch)
    _freeze(monkeypatch, datetime(2026, 10, 16, 15, 20, 30))

    runtime_scheduler._run_screening_schedule_tick(_config())

    assert calls == [("dual_low", "cn", 5), ("quality_value", "cn", 5)]


def test_tick_injects_db_manager_into_screening_service(monkeypatch, memory_db):
    """回归：调用点必须把 db_manager 传进 ScreeningService。

    历史 bug：``ScreeningService(config=config)`` 漏传 db_manager →
    ``screen()`` 末尾 ``if self.db_manager is not None`` 静默跳过
    ``save_screening_run`` → ``screening_runs`` 长期没有当日行 →
    去重判断失效（每轮重复跑）→ 当日快照永远只有表头。

    这条测试不看"调了哪些策略"，只看"有没有把库接上" ——
    正是原先缺失的那一环（storage 单测 + fake service 都覆盖不到）。
    """
    captured = []

    class _ProbeService:
        def __init__(self, config=None, db_manager=None):  # noqa: D107
            captured.append(db_manager)

        def screen(self, *, strategy, market, max_results):  # noqa: D102
            return {"run_id": f"probe-{strategy}", "candidate_count": 1}

    monkeypatch.setattr(
        "src.services.screening_service.ScreeningService",
        _ProbeService,
        raising=True,
    )
    _freeze(monkeypatch, datetime(2026, 10, 16, 15, 20, 30))

    runtime_scheduler._run_screening_schedule_tick(_config())

    assert len(captured) == 2, "两个策略都应被执行"
    assert all(db is not None for db in captured), (
        "db_manager 必须注入，否则选股运行结果不会落库（screening_runs 空 → 去重失效）"
    )


def test_tick_skips_strategy_already_run_today(monkeypatch, memory_db):
    """当天已经跑过的策略不重复跑 —— 去重看的是库里的 screening_runs。"""
    calls = _capture_screen(monkeypatch)
    with memory_db._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO screening_runs "
                "(run_id, strategy, market, candidate_count, result_json, created_at) "
                "VALUES ('run-manual', 'dual_low', 'cn', 1, '{}', '2026-10-16 15:21:00')"
            )
        )
    _freeze(monkeypatch, datetime(2026, 10, 16, 15, 22, 0))

    runtime_scheduler._run_screening_schedule_tick(_config())

    # dual_low 被去重跳过，只剩 quality_value
    assert calls == [("quality_value", "cn", 5)]


def test_tick_skips_weekend_unless_overridden(monkeypatch, memory_db):
    """默认跳过周末；显式关掉 weekdays_only 后周末照跑。"""
    calls = _capture_screen(monkeypatch)
    _freeze(monkeypatch, datetime(2026, 10, 17, 15, 30, 0))  # 周六

    runtime_scheduler._run_screening_schedule_tick(_config())
    assert calls == []

    runtime_scheduler._run_screening_schedule_tick(
        _config(screening_schedule_weekdays_only=False)
    )
    assert calls == [("dual_low", "cn", 5), ("quality_value", "cn", 5)]


def test_tick_skips_when_past_catch_up_window(monkeypatch, memory_db):
    """超过补跑窗口（到点后 120 分钟）就不再补，避免深夜跑出一份过期结果。"""
    calls = _capture_screen(monkeypatch)
    _freeze(monkeypatch, datetime(2026, 10, 16, 18, 0, 0))  # 15:20 + 160 分钟

    runtime_scheduler._run_screening_schedule_tick(_config())

    assert calls == []


def test_tick_runs_all_configured_times_independently(monkeypatch, memory_db):
    """多个时间点：只有已经到点的那些才算到期，且用最晚的一个做去重基准。"""
    calls = _capture_screen(monkeypatch)
    _freeze(monkeypatch, datetime(2026, 10, 16, 15, 21, 0))

    runtime_scheduler._run_screening_schedule_tick(
        _config(screening_schedule_times=["09:05", "15:20", "21:00"])
    )

    # 21:00 还没到，不该触发；但因为去重基准是 15:20，本轮仍会补齐一次。
    assert calls == [("dual_low", "cn", 5), ("quality_value", "cn", 5)]


def test_tick_isolates_single_strategy_failure(monkeypatch, memory_db):
    """单个策略抛异常不能拖垮其余策略。"""
    calls: list = []

    class _FlakyService:
        def __init__(self, config=None, db_manager=None):  # noqa: D107
            self.config = config

        def screen(self, *, strategy, market, max_results, selection_seed=None):
            calls.append(strategy)
            if strategy == "dual_low":
                raise RuntimeError("boom")
            return {"run_id": "ok", "candidate_count": 1}

    monkeypatch.setattr(
        "src.services.screening_service.ScreeningService", _FlakyService, raising=True
    )
    _freeze(monkeypatch, datetime(2026, 10, 16, 15, 20, 30))

    runtime_scheduler._run_screening_schedule_tick(_config())

    assert calls == ["dual_low", "quality_value"]


def test_screening_ran_since_uses_database(memory_db):
    """去重查库：created_at 是 UTC-naive，since 是本地时间，必须换算后再比。

    回归背景：两者混用会让 UTC 的 07:32 永远小于本地的 15:30，
    于是每轮都判"今天还没跑"，同一策略重复跑（2026-10-20 实测 4 轮）。
    这里用 +08:00 的本地时刻做断言，钉住"本地 15:30 == UTC 07:30"这条换算。
    """
    from datetime import datetime as dt, timezone

    LOCAL_TZ = timezone(timedelta(hours=8))
    with memory_db._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO screening_runs "
                "(run_id, strategy, market, candidate_count, result_json, created_at) "
                "VALUES ('run-early', 'dual_low', 'cn', 1, '{}', '2026-10-16 07:30:00')"
            )
        )

    # 本地 07:00（= UTC 前一日 23:00）→ 早于那条 UTC 07:30 的记录 → 算"跑过"
    assert (
        runtime_scheduler._screening_ran_since(
            "dual_low", dt(2026, 10, 16, 7, 0, 0, tzinfo=LOCAL_TZ)
        )
        is True
    )
    # 本地 16:00（= UTC 08:00）→ 晚于那条记录 → 算"没跑过"
    assert (
        runtime_scheduler._screening_ran_since(
            "dual_low", dt(2026, 10, 16, 16, 0, 0, tzinfo=LOCAL_TZ)
        )
        is False
    )
    # 别的策略从未写过 → 永远算"没跑过"
    assert (
        runtime_scheduler._screening_ran_since(
            "quality_value", dt(2026, 10, 16, 7, 0, 0, tzinfo=LOCAL_TZ)
        )
        is False
    )


def test_screening_ran_since_naive_since_is_treated_as_local(memory_db):
    """调度器传进来的是 naive 本地时间（datetime.now()），必须等价于带 +08:00。

    这条钉住"naive 必须按本地解释"，否则 astimezone() 在 UTC 容器里会算错。
    """
    from datetime import datetime as dt, timezone

    LOCAL_TZ = timezone(timedelta(hours=8))
    with memory_db._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO screening_runs "
                "(run_id, strategy, market, candidate_count, result_json, created_at) "
                "VALUES ('run-x', 'dual_low', 'cn', 1, '{}', '2026-10-16 07:30:00')"
            )
        )

    naive = dt(2026, 10, 16, 16, 0, 0)          # 本地 16:00，不带 tzinfo
    aware = naive.replace(tzinfo=LOCAL_TZ)
    assert runtime_scheduler._screening_ran_since(
        "dual_low", naive
    ) == runtime_scheduler._screening_ran_since("dual_low", aware)
