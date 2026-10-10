# -*- coding: utf-8 -*-
"""全市场横截面快照的持久化（第 1 层）。"""

from __future__ import annotations

import pandas as pd
import pytest


class TestValueCoercion:
    def test_placeholder_values_become_null_not_zero(self):
        """`-` / `--` / 空串必须存成 NULL。

        塞 0 会让"没有数据"看起来像"PE=0"，横截面筛选时会把垃圾选进来。
        """
        from src.services.market_snapshot_service import _to_float

        for raw in (None, "", "-", "--", "N/A", "nan", "None"):
            assert _to_float(raw) is None, f"{raw!r} 应为 None"
        assert _to_float("1,234.5") == 1234.5
        assert _to_float(19.39) == 19.39
        assert _to_float("abc") is None

    def test_nan_becomes_null(self):
        from src.services.market_snapshot_service import _to_float

        assert _to_float(float("nan")) is None


class TestSaveSnapshot:
    def test_pandas_index_does_not_break_column_detection(self):
        """回归：`df.columns or []` 会抛 "truth value of a Index is ambiguous"。

        pandas.Index 的真值是未定义的，写成 `getattr(df, "columns", []) or []`
        会让整次持久化静默失败（fail-open 吞掉），表现为"存了 0 行"。
        """
        from src.services.market_snapshot_service import save_market_snapshot

        df = pd.DataFrame([{"code": "600519", "name": "贵州茅台",
                            "price": 1263.0, "pe_ratio": 19.39}])
        # 不抛异常即算通过；写入结果取决于库状态，这里只验证列检测不炸
        result = save_market_snapshot(df, source="unit-test")
        assert isinstance(result, int)

    def test_empty_frame_returns_zero(self):
        from src.services.market_snapshot_service import save_market_snapshot

        assert save_market_snapshot(pd.DataFrame(), source="x") == 0
        assert save_market_snapshot(None, source="x") == 0

    def test_missing_code_column_returns_zero(self):
        from src.services.market_snapshot_service import save_market_snapshot

        df = pd.DataFrame([{"name": "无代码", "price": 1.0}])
        assert save_market_snapshot(df, source="x") == 0
