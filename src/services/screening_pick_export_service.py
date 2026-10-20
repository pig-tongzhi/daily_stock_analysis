# -*- coding: utf-8 -*-
"""选股入选明细的规范化落库 + 每日快照导出。

为什么需要这一层：``screening_runs.result_json`` 是"每次运行的原始记录"，
同一策略同一天可能跑多次、同一只票会重复出现（实测 133 条候选里 **36 条**
``(入选日期, 策略, 代码)`` 重复）。下游按条统计会把胜率算歪；而且快照价不是
能成交的价格。

这里做两件事：

1. **物化进 ``screening_picks``**：按唯一键 ``(pick_date, strategy, code)``
   收敛成一行，并补齐「买入价(实际)」= **入选日之后第一个交易日的开盘价**
   （开盘缺失时退该根 K 线的收盘价，并记 ``buy_basis``）。
2. **导出每日快照**：``screening-YYYYMMDD.csv``，只写当天那一份，
   绝不触碰其它日期的文件。

口径与 ``screening_tracking_service`` 完全一致（复用它的取数与前瞻 K 线），
避免两套口径漂移。
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.services.screening_tracking_service import (
    FIXED_ENTRY_BASIS,
    load_pick_rows,
)
from src.services.screening_tracking_service import _fetch_forward_bars
from src.storage import DatabaseManager, ScreeningPick, local_naive_now

logger = logging.getLogger(__name__)

# 快照目录默认值（相对项目根）。data/ 已在 .gitignore 内，历史快照不进版本库。
DEFAULT_SNAPSHOT_DIR = os.path.join("data", "screening-snapshots")

# CSV 列顺序即下游读数的字段顺序，改动等于改契约。
SNAPSHOT_COLUMNS: Tuple[str, ...] = (
    "pick_date",
    "strategy",
    "market",
    "code",
    "name",
    "pick_price",
    "buy_price",
    "buy_date",
    "buy_basis",
    "score",
    "run_id",
    "created_at",
    "updated_at",
)

# 同一份当天快照被重写时，旧版本归档成这个名字（只留一份，避免无限堆积）。
PREVIOUS_SNAPSHOT_SUFFIX = ".previous.csv"


def resolve_snapshot_dir(config: Any = None, out_dir: Optional[str] = None) -> str:
    """解析快照目录的绝对路径。"""
    if out_dir:
        return os.path.abspath(out_dir)
    configured = getattr(config, "screening_snapshot_dir", None) if config is not None else None
    return os.path.abspath(str(configured or DEFAULT_SNAPSHOT_DIR))


def _run_marker(pick: Dict[str, Any]) -> Optional[datetime]:
    """把 ``run_created_at`` 归一成可比较的 naive datetime。

    为什么需要它：``load_pick_rows`` 用裸 SQL 读 ``created_at``，SQLAlchemy 不带
    类型信息，SQLite 会把值原样返回成**字符串**（'2026-10-07 19:30:00+00:00' 或
    '... .123456'）而不是 datetime。直接 isinstance 判断会在真实数据上静默失效，
    让去重退化成"最后出现者胜" —— 而那是 created_at DESC 顺序下的**最旧**一次运行。
    """
    value = pick.get("run_created_at")
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    return None


def dedupe_picks(picks: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """按 ``(入选日期, 策略, 代码)`` 去重，保留**最近一次运行**的那条。

    返回 ``(去重后的行, 被折叠的重复条数)``。折叠数要如实返回并落日志：
    "151 条里 30 条重复"是下游最容易踩的坑，不能悄悄吃掉。

    注意不能用"后出现者胜"：``load_pick_rows`` 是 ``created_at DESC`` 返回的，
    按出现顺序取最后一条会留下**最旧**的那次运行。这里比较归一后的运行时间；
    时间都拿不到时才退回"后出现者胜"（并保持行为确定）。
    """
    best: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    duplicates = 0
    for pick in picks:
        key = (
            str(pick.get("pick_date") or ""),
            str(pick.get("strategy") or ""),
            str(pick.get("code") or ""),
        )
        current = best.get(key)
        if current is None:
            best[key] = pick
            continue
        duplicates += 1
        newer = _run_marker(pick)
        older = _run_marker(current)
        if newer is not None and older is not None:
            # 严格 > ：时间相同时保留"先出现的"。load_pick_rows 是
            # (created_at DESC, id DESC) 返回的，先出现 = id 更大 = 更新。
            if newer > older:
                best[key] = pick
        else:
            best[key] = pick
    return list(best.values()), duplicates


def _buy_price_for(
    pick: Dict[str, Any],
    bars: Optional[List[Dict[str, Any]]],
) -> Tuple[Optional[float], Optional[date], Optional[str]]:
    """入选日之后第一根 K 线的开盘价；开盘缺失时退收盘价并标明。"""
    if not bars:
        return None, None, None
    forward = [bar for bar in bars if bar["date"] > pick["pick_date"]]
    if not forward:
        return None, None, None
    entry_bar = forward[0]
    price = entry_bar.get("open")
    basis = FIXED_ENTRY_BASIS
    if price is None or price <= 0:
        price = entry_bar.get("close")
        basis = "next_close"
    if price is None or price <= 0:
        return None, None, None
    return float(price), entry_bar["date"], basis


def materialize_screening_picks(
    *,
    config: Any = None,
    db_manager: Any = None,
    days: Optional[int] = None,
) -> Dict[str, Any]:
    """把窗口内的选股入选物化进 ``screening_picks``（幂等 upsert）。

    - 唯一键冲突时**就地更新**（买入价可能要到下一个交易日才补得齐）；
    - ``created_at`` 只在首次插入时写，更新不动它 —— 否则下游按它做增量取数
      会重复读到旧行；要看"后补"的更新请用 ``updated_at``。
    """
    from src.services.screening_tracking_service import _resolve_database_manager

    db = _resolve_database_manager(db_manager)
    picks, malformed = load_pick_rows(config=config, db_manager=db, days=days)
    result: Dict[str, Any] = {
        "source_picks": len(picks),
        "malformed_runs": malformed,
        "deduped": 0,
        "duplicates": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "with_buy_price": 0,
        "missing_buy_price": 0,
    }
    if not picks:
        return result

    deduped, duplicates = dedupe_picks(picks)
    result["deduped"] = len(deduped)
    result["duplicates"] = duplicates
    # 本次物化涉及的 pick_date（升序）。下游导出快照必须按这个日期写文件，
    # 不能用 date.today()：跨零点补跑时 today 已经是第二天，
    # 会把当天的入选写到第二天的文件名里，当天快照永远是空表。
    result["dates"] = sorted(
        {str(p.get("pick_date")) for p in deduped if p.get("pick_date")}
    )
    if duplicates:
        # 这是下游最容易算歪的地方，必须每次都说话。
        logger.info(
            "[ScreeningPicks] 折叠重复入选 %s 条（同 (日期,策略,代码) 多次运行）→ 去重后 %s 条",
            duplicates,
            len(deduped),
        )

    canonical_by_code: Dict[str, Optional[str]] = {}
    for pick in deduped:
        code = str(pick["code"])
        if code not in canonical_by_code:
            try:
                canonical_by_code[code] = db._derive_canonical_id(code)
            except Exception:
                logger.warning("[ScreeningPicks] canonical_id 推导失败 code=%s", code, exc_info=True)
                canonical_by_code[code] = None

    bars_by_canonical = _fetch_forward_bars(
        db, [value for value in canonical_by_code.values() if value]
    )

    now = local_naive_now()
    with db.get_session() as session:
        for pick in deduped:
            canonical_id = canonical_by_code.get(str(pick["code"]))
            buy_price, buy_date, buy_basis = _buy_price_for(
                pick, bars_by_canonical.get(canonical_id) if canonical_id else None
            )

            desired: Dict[str, Any] = {
                "name": str(pick.get("name") or "") or None,
                "market": str(pick.get("market") or "cn"),
                "run_id": str(pick.get("run_id") or "") or None,
                "pick_price": pick.get("pick_price"),
                "score": pick.get("score"),
            }
            if buy_price is not None:
                # 买入价一旦补齐就不再清空：拿不到前瞻 K 线时保留既有值，
                # 免得行情刷新抖动一次就把已核对过的买入价抹掉。
                desired["buy_price"] = buy_price
                desired["buy_date"] = buy_date
                desired["buy_basis"] = buy_basis

            row = (
                session.query(ScreeningPick)
                .filter(
                    ScreeningPick.pick_date == pick["pick_date"],
                    ScreeningPick.strategy == pick["strategy"],
                    ScreeningPick.code == pick["code"],
                )
                .one_or_none()
            )
            if row is None:
                row = ScreeningPick(
                    pick_date=pick["pick_date"],
                    strategy=pick["strategy"],
                    code=pick["code"],
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                for key, value in desired.items():
                    setattr(row, key, value)
                result["inserted"] += 1
            else:
                # 只有字段真的变了才动 updated_at：否则每次物化都会刷新时间戳，
                # 当天快照会每次都"内容不同"，下游也没法靠 updated_at 判断变化。
                changed = False
                for key, value in desired.items():
                    if getattr(row, key) != value:
                        setattr(row, key, value)
                        changed = True
                if changed:
                    row.updated_at = now
                    result["updated"] += 1
                else:
                    result["unchanged"] += 1

            if buy_price is not None:
                result["with_buy_price"] += 1
            else:
                result["missing_buy_price"] += 1
        session.commit()

    logger.info(
        "[ScreeningPicks] inserted=%s updated=%s with_buy_price=%s missing_buy_price=%s duplicates=%s",
        result["inserted"],
        result["updated"],
        result["with_buy_price"],
        result["missing_buy_price"],
        result["duplicates"],
    )
    return result


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, float):
        # 价格与分数保留 4 位，去掉多余尾零，方便人工核对。
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def export_daily_snapshot(
    *,
    config: Any = None,
    db_manager: Any = None,
    on_date: Optional[date] = None,
    out_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """把某一天的入选写成 ``screening-YYYYMMDD.csv``。

    只写当天那一份：**不删除、不改写任何其它日期的文件**（这就是"不覆盖历史"
    的实际含义）。当天文件是幂等重写 —— 买入价要等下一个交易日才能补齐；
    若内容与磁盘上已有的不同，先把旧版本归档成 ``*.previous.csv``。
    """
    from src.services.screening_tracking_service import _resolve_database_manager

    db = _resolve_database_manager(db_manager)
    directory = resolve_snapshot_dir(config, out_dir)
    os.makedirs(directory, exist_ok=True)
    target = on_date or date.today()
    filename = f"screening-{target.strftime('%Y%m%d')}.csv"
    path = os.path.join(directory, filename)

    with db.get_session() as session:
        rows = (
            session.query(ScreeningPick)
            .filter(ScreeningPick.pick_date == target)
            .order_by(ScreeningPick.strategy, ScreeningPick.code)
            .all()
        )

    import io

    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(SNAPSHOT_COLUMNS)
    for row in rows:
        writer.writerow([_format_cell(getattr(row, column)) for column in SNAPSHOT_COLUMNS])
    payload = stream.getvalue()

    previous_path: Optional[str] = None
    rewritten = False

    # 空结果绝不覆盖已有的非空快照：DATABASE_PATH 指错、物化没跑、时区错位时，
    # 一个只剩表头的文件会让下游把"这天没选股"读成"这天没入选"。
    if not rows and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                existing = handle.read()
        except OSError:
            existing = ""
        if len(existing.splitlines()) > 1:
            logger.warning(
                "[ScreeningPicks] 拒绝用空快照覆盖非空文件（rows=0）path=%s", path
            )
            return {
                "date": target.isoformat(),
                "path": path,
                "directory": directory,
                "rows": 0,
                "bytes": 0,
                "rewritten": False,
                "skipped_empty": True,
                "previous_path": None,
                "columns": list(SNAPSHOT_COLUMNS),
            }

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                existing = handle.read()
        except OSError:
            existing = None
        if existing != payload:
            rewritten = True
            previous_path = path[: -len(".csv")] + PREVIOUS_SNAPSHOT_SUFFIX
            try:
                os.replace(path, previous_path)
            except OSError:
                logger.warning("[ScreeningPicks] 归档旧快照失败 path=%s", path, exc_info=True)
                previous_path = None

    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(payload)

    return {
        "date": target.isoformat(),
        "path": path,
        "directory": directory,
        "rows": len(rows),
        "bytes": len(payload.encode("utf-8")),
        "rewritten": rewritten,
        "skipped_empty": False,
        "previous_path": previous_path,
        "columns": list(SNAPSHOT_COLUMNS),
    }
