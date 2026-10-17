#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""手动触发一次选股（不需要 Web 服务在跑）。

用途：独立验证选股链路 —— 云端/本机没有起 8010 时也能跑，
或在别人占着端口时避开 HTTP 直接调用同一套服务。

用法
----
    # 跑一次默认策略（dual_low）
    .venv/bin/python scripts/run_screening_once.py

    # 指定策略与条数
    .venv/bin/python scripts/run_screening_once.py --strategy quality_value --max-results 5

    # 连跑多个策略（逗号分隔），会串行执行
    .venv/bin/python scripts/run_screening_once.py --strategy dual_low,quality_value

    # 跑完顺手物化 + 导出当日快照（等价于再跑一次 export_screening_picks.py）
    .venv/bin/python scripts/run_screening_once.py --with-export

注意：选股会抓全市场快照并富集 K 线，**单策略实测约 7~13 分钟**；
若配置了 `POST_ANALYZERS=dsa`，还会对排名靠前的候选逐个做个股深度分析
（每只约 2 分钟，并产生 LLM 调用）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import get_config, setup_env  # noqa: E402
from src.services.screening_service import ScreeningService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="手动触发一次选股")
    parser.add_argument("--strategy", default="dual_low", help="策略 id，可逗号分隔（默认 dual_low）")
    parser.add_argument("--market", default="cn", help="市场（默认 cn）")
    parser.add_argument("--max-results", type=int, default=5, help="最多返回几只（默认 5）")
    parser.add_argument("--seed", default="", help="结果轮换种子（默认空）")
    parser.add_argument("--with-export", action="store_true", help="跑完物化并导出当日快照")
    args = parser.parse_args()

    setup_env()
    config = get_config()
    strategies = [item.strip() for item in str(args.strategy).split(",") if item.strip()]
    if not strategies:
        print("✗ --strategy 不能为空")
        return 2

    failed = 0
    for strategy in strategies:
        print(f"▶ 选股 strategy={strategy} market={args.market} max_results={args.max_results}")
        try:
            result = ScreeningService(config=config).screen(
                strategy=strategy,
                market=args.market,
                max_results=args.max_results,
                selection_seed=args.seed,
            )
        except Exception as exc:  # noqa: BLE001 - 单个策略失败不影响其余策略
            failed += 1
            print(f"  ✗ 失败: {type(exc).__name__}: {exc}")
            continue
        print(
            f"  ✓ run_id={result.get('run_id')} 候选={result.get('candidate_count')} "
            f"llm_ranked={result.get('llm_ranked')} post_analyzers={result.get('post_analyzers')}"
        )
        for candidate in (result.get("candidates") or [])[:5]:
            print(
                f"    {candidate.get('code')} {candidate.get('name')} "
                f"deep={candidate.get('deep_analysis_status')}"
            )

    if args.with_export:
        from src.services.screening_pick_export_service import (
            export_daily_snapshot,
            materialize_screening_picks,
        )

        print("▶ 物化 + 导出快照")
        materialized = materialize_screening_picks(config=config)
        print(
            f"  去重 {materialized['deduped']} 条（折叠 {materialized['duplicates']}）· "
            f"有买入价 {materialized['with_buy_price']} · 缺 {materialized['missing_buy_price']}"
        )
        snapshot = export_daily_snapshot(config=config)
        print(f"  快照: {snapshot['path']}（{snapshot['rows']} 行）")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
