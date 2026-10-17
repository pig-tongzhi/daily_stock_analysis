#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""物化选股入选明细，并导出「每天一份」的 CSV 快照。

为什么需要
----------
``screening_runs.result_json`` 是"每次运行的原始记录"：同一策略同一天可能跑
多次，同一只票会重复出现（实测 151 条候选里 **30 条** ``(日期,策略,代码)``
重复）。下游按条统计会把胜率算歪，而且快照价不是能成交的价格。

本脚本做两件事：

1. 把窗口内的入选**物化**进 ``screening_picks``（唯一键
   ``(pick_date, strategy, code)``），并补齐「买入价(实际)」=
   **入选日之后第一个交易日的开盘价**（开盘缺失时退该根 K 线的收盘价，
   记 ``buy_basis=next_close``）；拿不到前瞻 K 线的留空，**绝不填快照价**。
2. 导出 ``screening-YYYYMMDD.csv``。**只写当天那一份**，不删除、不改写其它
   日期的文件；当天文件幂等重写（买入价要等下一个交易日才补得齐），重写前
   把旧版本归档成 ``*.previous.csv``。

用法
----
    # 物化 + 导出「今天」的快照
    .venv/bin/python scripts/export_screening_picks.py

    # 导出指定日期的快照（仍会先物化窗口内的数据）
    .venv/bin/python scripts/export_screening_picks.py --date 2026-10-16

    # 只物化，不写快照
    .venv/bin/python scripts/export_screening_picks.py --no-export

    # 覆盖输出目录（默认 data/screening-snapshots）
    .venv/bin/python scripts/export_screening_picks.py --out-dir /tmp/snap
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import get_config, setup_env  # noqa: E402
from src.services.screening_pick_export_service import (  # noqa: E402
    export_daily_snapshot,
    materialize_screening_picks,
    resolve_snapshot_dir,
)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD，收到 {value!r}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="物化选股入选明细并导出每日快照")
    parser.add_argument("--days", type=int, default=None, help="只看最近 N 天的选股运行（默认走配置，60）")
    parser.add_argument("--date", type=_parse_date, default=None, help="导出哪一天的快照（默认今天）")
    parser.add_argument("--out-dir", default=None, help="快照输出目录（默认 data/screening-snapshots）")
    parser.add_argument("--no-export", action="store_true", help="只物化，不写快照")
    args = parser.parse_args()

    setup_env()
    config = get_config()

    print("[1/2] 物化 screening_picks ...")
    materialized = materialize_screening_picks(config=config, days=args.days)
    print(
        f"  源入选 {materialized['source_picks']} 条 → 去重 {materialized['deduped']} 条"
        f"（折叠重复 {materialized['duplicates']} 条）"
    )
    print(
        f"  新增 {materialized['inserted']} · 更新 {materialized['updated']} · "
        f"无变化 {materialized['unchanged']} · "
        f"有买入价 {materialized['with_buy_price']} · 缺买入价 {materialized['missing_buy_price']}"
    )
    if materialized["malformed_runs"]:
        print(f"  ⚠️ 畸形 result_json 运行 {materialized['malformed_runs']} 次（已跳过）")

    if args.no_export:
        print("[2/2] 跳过快照导出（--no-export）")
        return 0

    print("[2/2] 导出每日快照 ...")
    result = export_daily_snapshot(config=config, on_date=args.date, out_dir=args.out_dir)
    print(f"  目录: {result['directory']}")
    print(f"  文件: {result['path']}")
    print(f"  行数: {result['rows']} · 字节: {result['bytes']} · 重写: {result['rewritten']}")
    if result["previous_path"]:
        print(f"  旧版本已归档: {result['previous_path']}")
    print(f"  默认快照目录 = {resolve_snapshot_dir(config)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
