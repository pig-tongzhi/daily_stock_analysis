# -*- coding: utf-8 -*-
"""选股明细物化 + 每日快照导出。

全部跑在内存库上；快照写到 pytest 的 ``tmp_path``，不碰生产库、也不写真实快照目录。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from src.services.screening_pick_export_service import (
    SNAPSHOT_COLUMNS,
    dedupe_picks,
    export_daily_snapshot,
    materialize_screening_picks,
)
from src.storage import DatabaseManager, ScreeningPick

PICK_DATE = date(2026, 10, 7)


@pytest.fixture()
def memory_db():
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url="sqlite:///:memory:")
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()


def _insert_run(db, *, run_id, created_at, candidates, strategy="dual_low", market="cn"):
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
                "candidate_count": len(candidates),
                "result_json": json.dumps({"candidates": candidates}, ensure_ascii=False),
                "created_at": created_at,
            },
        )


def _insert_bar(db, *, code, canonical_id, day, close, open_price=None):
    with db._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO stock_daily (code, canonical_id, date, open, close, high, low) "
                "VALUES (:code, :canonical_id, :date, :open, :close, :high, :low)"
            ),
            {
                "code": code,
                "canonical_id": canonical_id,
                "date": day,
                "open": open_price if open_price is not None else close,
                "close": close,
                "high": close,
                "low": close,
            },
        )


def _cand(code, name, price, score=80.0):
    return {"rank": 1, "code": code, "name": name, "price": price, "score": score}


def _picked_at(day=PICK_DATE):
    return datetime(day.year, day.month, day.day, 23, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 去重：下游最容易踩的坑
# ---------------------------------------------------------------------------


def test_dedupe_folds_same_date_strategy_code():
    """同 (日期, 策略, 代码) 只保留最后一条，并如实报告折叠了多少条。"""
    picks = [
        {"pick_date": date(2026, 10, 7), "strategy": "dual_low", "code": "600016", "pick_price": 3.50},
        {"pick_date": date(2026, 10, 7), "strategy": "dual_low", "code": "600016", "pick_price": 3.52},
        {"pick_date": date(2026, 10, 7), "strategy": "quality_value", "code": "600016", "pick_price": 3.51},
        {"pick_date": date(2026, 10, 8), "strategy": "dual_low", "code": "600016", "pick_price": 3.55},
    ]

    deduped, duplicates = dedupe_picks(picks)

    assert duplicates == 1
    assert len(deduped) == 3
    kept = [row for row in deduped if row["strategy"] == "dual_low" and row["pick_date"] == date(2026, 10, 7)]
    assert kept[0]["pick_price"] == 3.52  # 后出现的覆盖先出现的


# ---------------------------------------------------------------------------
# 买入价(实际)：入选日之后第一个交易日的开盘价
# ---------------------------------------------------------------------------


def test_materialize_uses_next_open_as_buy_price(memory_db):
    _insert_run(
        memory_db,
        run_id="run-a",
        created_at=_picked_at(),
        candidates=[_cand("600016", "民生银行", 3.50)],
    )
    _insert_bar(memory_db, code="600016", canonical_id="sh600016", day=PICK_DATE, close=3.50)
    _insert_bar(
        memory_db, code="600016", canonical_id="sh600016",
        day=date(2026, 10, 8), close=3.60, open_price=3.55,
    )

    result = materialize_screening_picks(db_manager=memory_db)

    assert result["inserted"] == 1
    assert result["with_buy_price"] == 1
    assert result["missing_buy_price"] == 0
    row = memory_db.get_session().query(ScreeningPick).one()
    assert row.buy_price == pytest.approx(3.55)  # 开盘价，不是快照价 3.50
    assert row.buy_date == date(2026, 10, 8)
    assert row.buy_basis == "next_open"
    assert row.pick_price == pytest.approx(3.50)  # 快照价仍在，作为对照


def test_buy_price_falls_back_to_close_when_open_missing(memory_db):
    _insert_run(
        memory_db, run_id="run-no-open", created_at=_picked_at(),
        candidates=[_cand("600519", "贵州茅台", 1200.0)],
    )
    with memory_db._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO stock_daily (code, canonical_id, date, open, close, high, low) "
                "VALUES ('600519', 'sh600519', '2026-10-08', NULL, 1250.00, 1260.00, 1240.00)"
            )
        )

    materialize_screening_picks(db_manager=memory_db)

    row = memory_db.get_session().query(ScreeningPick).one()
    assert row.buy_price == pytest.approx(1250.00)
    assert row.buy_basis == "next_close"


def test_materialize_keeps_null_when_no_forward_bar(memory_db):
    """入选之后还没有 K 线：买入价必须是 NULL，绝不拿快照价顶上。"""
    _insert_run(
        memory_db, run_id="run-latest", created_at=_picked_at(),
        candidates=[_cand("601328", "交通银行", 8.09)],
    )
    _insert_bar(memory_db, code="601328", canonical_id="sh601328", day=PICK_DATE, close=8.09)

    result = materialize_screening_picks(db_manager=memory_db)

    row = memory_db.get_session().query(ScreeningPick).one()
    assert row.buy_price is None
    assert row.buy_basis is None
    assert result["missing_buy_price"] == 1


# ---------------------------------------------------------------------------
# 唯一键 / 幂等 / 时间戳语义
# ---------------------------------------------------------------------------


def test_rerun_collapses_into_one_row_and_advances_run_id(memory_db):
    """同键被两次运行命中 → 只有一行，run_id 指向较新的那次运行。

    load_pick_rows 是 (created_at DESC, id DESC) 返回的，所以"保留最后出现的一条"
    会留下最旧那次运行 —— 这里刻意给两次运行不同的 created_at 来锁死语义。
    """
    _insert_run(
        memory_db, run_id="run-old",
        created_at=datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc),
        candidates=[_cand("600016", "民生银行", 3.50)],
    )
    _insert_run(
        memory_db, run_id="run-new",
        created_at=datetime(2026, 10, 7, 19, 30, tzinfo=timezone.utc),
        candidates=[_cand("600016", "民生银行", 3.52)],
    )
    _insert_bar(memory_db, code="600016", canonical_id="sh600016", day=PICK_DATE, close=3.50)
    _insert_bar(
        memory_db, code="600016", canonical_id="sh600016",
        day=date(2026, 10, 8), close=3.60, open_price=3.55,
    )

    result = materialize_screening_picks(db_manager=memory_db)

    assert result["deduped"] == 1
    assert result["duplicates"] == 1
    session = memory_db.get_session()
    assert session.query(ScreeningPick).count() == 1
    row = session.query(ScreeningPick).one()
    assert row.run_id == "run-new"
    assert row.pick_price == pytest.approx(3.52)  # 较新那次运行的快照价


def test_materialize_is_idempotent_and_keeps_created_at(memory_db):
    """重复物化不刷新时间戳：否则当天快照每次都"内容不同"。"""
    _insert_run(
        memory_db, run_id="run-a", created_at=_picked_at(),
        candidates=[_cand("600016", "民生银行", 3.50)],
    )
    _insert_bar(memory_db, code="600016", canonical_id="sh600016", day=PICK_DATE, close=3.50)
    _insert_bar(
        memory_db, code="600016", canonical_id="sh600016",
        day=date(2026, 10, 8), close=3.60, open_price=3.55,
    )

    first = materialize_screening_picks(db_manager=memory_db)
    row_after_first = memory_db.get_session().query(ScreeningPick).one()
    created_at = row_after_first.created_at
    updated_at = row_after_first.updated_at

    second = materialize_screening_picks(db_manager=memory_db)
    row_after_second = memory_db.get_session().query(ScreeningPick).one()

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["updated"] == 0
    assert second["unchanged"] == 1
    assert row_after_second.created_at == created_at
    assert row_after_second.updated_at == updated_at


def test_unique_constraint_rejects_manual_duplicate(memory_db):
    session = memory_db.get_session()
    session.add(ScreeningPick(pick_date=PICK_DATE, strategy="dual_low", code="600016"))
    session.commit()
    session.add(ScreeningPick(pick_date=PICK_DATE, strategy="dual_low", code="600016"))
    with pytest.raises(Exception):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# 快照导出：append-only / 不覆盖历史 / 幂等
# ---------------------------------------------------------------------------


def test_export_writes_expected_columns_and_is_idempotent(tmp_path, memory_db):
    _insert_run(
        memory_db, run_id="run-a", created_at=_picked_at(),
        candidates=[_cand("600016", "民生银行", 3.50)],
    )
    _insert_bar(memory_db, code="600016", canonical_id="sh600016", day=PICK_DATE, close=3.50)
    _insert_bar(
        memory_db, code="600016", canonical_id="sh600016",
        day=date(2026, 10, 8), close=3.60, open_price=3.55,
    )
    materialize_screening_picks(db_manager=memory_db)

    first = export_daily_snapshot(db_manager=memory_db, on_date=PICK_DATE, out_dir=str(tmp_path))
    second = export_daily_snapshot(db_manager=memory_db, on_date=PICK_DATE, out_dir=str(tmp_path))

    assert first["path"].endswith("screening-20261007.csv")
    assert first["rows"] == 1
    assert first["rewritten"] is False
    # 同样数据重复导出：内容一致，不重写、不产生归档
    assert second["rewritten"] is False
    assert second["previous_path"] is None

    content = (tmp_path / "screening-20261007.csv").read_text(encoding="utf-8")
    header = content.splitlines()[0].split(",")
    assert header == list(SNAPSHOT_COLUMNS)
    row = content.splitlines()[1].split(",")
    assert row[SNAPSHOT_COLUMNS.index("code")] == "600016"
    assert row[SNAPSHOT_COLUMNS.index("pick_price")] == "3.5"
    assert row[SNAPSHOT_COLUMNS.index("buy_price")] == "3.55"


def test_export_rewrites_same_day_and_archives_previous(tmp_path, memory_db):
    """当天文件允许重写（买入价次日才补齐），但旧版本要归档、不丢。"""
    _insert_run(
        memory_db, run_id="run-a", created_at=_picked_at(),
        candidates=[_cand("600016", "民生银行", 3.50)],
    )
    _insert_bar(memory_db, code="600016", canonical_id="sh600016", day=PICK_DATE, close=3.50)
    materialize_screening_picks(db_manager=memory_db)
    export_daily_snapshot(db_manager=memory_db, on_date=PICK_DATE, out_dir=str(tmp_path))

    # 次日行情到了 → 买入价补齐 → 内容变化
    _insert_bar(
        memory_db, code="600016", canonical_id="sh600016",
        day=date(2026, 10, 8), close=3.60, open_price=3.55,
    )
    materialize_screening_picks(db_manager=memory_db)
    second = export_daily_snapshot(db_manager=memory_db, on_date=PICK_DATE, out_dir=str(tmp_path))

    assert second["rewritten"] is True
    assert second["previous_path"].endswith("screening-20261007.previous.csv")
    # 新文件里买入价已补齐
    content = (tmp_path / "screening-20261007.csv").read_text(encoding="utf-8")
    assert "3.55" in content


def test_export_never_touches_other_dates(tmp_path, memory_db):
    """导出某一天绝不改动其它日期的文件（"不覆盖历史"的实际含义）。"""
    other = tmp_path / "screening-20261001.csv"
    other.write_text("pick_date,strategy\n2026-10-01,seed\n", encoding="utf-8")
    before = other.read_text(encoding="utf-8")

    _insert_run(
        memory_db, run_id="run-a", created_at=_picked_at(),
        candidates=[_cand("600016", "民生银行", 3.50)],
    )
    materialize_screening_picks(db_manager=memory_db)
    export_daily_snapshot(db_manager=memory_db, on_date=PICK_DATE, out_dir=str(tmp_path))

    assert other.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "screening-20261001.csv",
        "screening-20261007.csv",
    ]


def test_export_refuses_to_overwrite_non_empty_snapshot_with_empty(tmp_path, memory_db):
    """空结果绝不覆盖已有的非空快照。

    真实场景：DATABASE_PATH 指错 / 物化没跑 / 时区错位时，一个只剩表头的文件
    会让下游把"这天没选股"读成"这天没入选"，把当天数据整份丢掉。
    """
    _insert_run(
        memory_db, run_id="run-a", created_at=_picked_at(),
        candidates=[_cand("600016", "民生银行", 3.50)],
    )
    _insert_bar(memory_db, code="600016", canonical_id="sh600016", day=PICK_DATE, close=3.50)
    materialize_screening_picks(db_manager=memory_db)
    first = export_daily_snapshot(db_manager=memory_db, on_date=PICK_DATE, out_dir=str(tmp_path))
    assert first["rows"] == 1

    # 模拟"查不到任何入选"：清空表后再导出
    with memory_db._engine.begin() as conn:
        conn.execute(text("DELETE FROM screening_picks"))

    second = export_daily_snapshot(db_manager=memory_db, on_date=PICK_DATE, out_dir=str(tmp_path))

    assert second["rows"] == 0
    assert second["skipped_empty"] is True
    assert second["rewritten"] is False
    # 磁盘上的原快照必须原封不动
    content = (tmp_path / "screening-20261007.csv").read_text(encoding="utf-8")
    assert "600016" in content
    assert not (tmp_path / "screening-20261007.previous.csv").exists()
