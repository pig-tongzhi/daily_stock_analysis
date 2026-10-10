# -*- coding: utf-8 -*-
"""Incident replay: a session pinned to the non-existent code ``HK10000``.

Origin (2026-10-08): the user asked about buying a stock with 10,000 yuan
(``本金10000元``). The bare 5-digit amount was read as a Hong Kong code, so the
session's scope became ``allowed={HK10000}`` and every individual-stock tool
call for a real stock (恒瑞医药 600276, 药明康德 603259, …) was blocked with
``stock_scope_violation`` / ``retriable=False``. Naming a real code explicitly
did not help either, because the turn stayed ``maintain``.

The tests below pin the three fixes:

* BUG-1 — a bare 5-digit run is an HK code only with a real HK signal;
* BUG-2 — a context ``stock_code`` that does not exist is not a valid anchor;
* BUG-3 — an invalid anchor cannot trap the session: an explicitly named valid
  stock takes over without a switch verb, and a turn that names no stock fails
  open instead of blocking every tool call.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.agent.stock_scope import (
    _is_unknown_stock_code,
    extract_stock_codes,
    extract_stock_mentions,
    resolve_stock_scope,
)

INCIDENT_MESSAGE = "本金10000元，买哪只医药股"
POLLUTED_CONTEXT = {"stock_code": "HK10000", "stock_name": ""}


class TestAmountIsNotAHongKongCode(unittest.TestCase):
    """BUG-1: ordinary 5-digit numbers must not become HK codes."""

    def test_yuan_amounts_are_not_hk_codes(self):
        for message in (
            "本金10000元",
            "10000元本金",
            "本金10000元，买哪只医药股",
            "我有50000元，怎么配置",
        ):
            with self.subTest(message=message):
                self.assertEqual(extract_stock_codes(message), [])
                self.assertEqual(extract_stock_mentions(message), [])

    def test_arbitrary_five_digit_numbers_are_not_hk_codes(self):
        for message in ("99999", "12345", "60051"):
            with self.subTest(message=message):
                self.assertEqual(extract_stock_codes(message), [])

    def test_incident_message_does_not_pin_a_scope(self):
        strict = resolve_stock_scope(
            INCIDENT_MESSAGE, None, strict_initial_scope=True
        )

        self.assertIsNone(strict.stock_scope)
        self.assertNotIn("stock_code", strict.effective_context)

    def test_incident_message_against_polluted_context_is_free(self):
        resolution = resolve_stock_scope(INCIDENT_MESSAGE, dict(POLLUTED_CONTEXT))

        self.assertNotIn("HK10000", resolution.effective_context.get("stock_code", ""))
        if resolution.stock_scope is not None:
            self.assertNotEqual(
                resolution.stock_scope.allowed_stock_codes, {"HK10000"}
            )


class TestExtractStockCodesStaysPure(unittest.TestCase):
    """BUG-1 lives in text only; web_intent_tokenizer's no-DB contract holds."""

    def test_no_name_index_lookup_for_amounts_or_hk_context(self):
        with patch(
            "src.agent.stock_scope._stock_name_index",
            side_effect=AssertionError(
                "extract_stock_codes must stay a pure format check"
            ),
        ):
            self.assertEqual(extract_stock_codes("本金10000元"), [])
            self.assertEqual(extract_stock_codes("港股 00700"), ["HK00700"])

    def test_tokenizer_does_not_share_the_five_digit_hk_assumption(self):
        from src.agent.web_intent_tokenizer import _split_by_codes
        from src.agent.web_intent_types import TAG_UNKNOWN_NUMBER

        tokens = _split_by_codes("本金10000元")

        self.assertEqual(
            [(token.text, token.tag) for token in tokens if token.tag],
            [("10000", TAG_UNKNOWN_NUMBER)],
        )


class TestLegitimateHongKongForms(unittest.TestCase):
    """BUG-1 must not break the HK forms users actually type."""

    def test_explicit_and_contextual_hk_forms_resolve(self):
        cases = {
            "hk00700": "HK00700",
            "00700.HK": "HK00700",
            "HK00700": "HK00700",
            "港股 00700": "HK00700",
            "港交所 00700": "HK00700",
            "港股00700": "HK00700",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertIn(expected, extract_stock_codes(message))
                resolution = resolve_stock_scope(
                    message, None, strict_initial_scope=True
                )
                self.assertIsNotNone(resolution.stock_scope)
                self.assertEqual(
                    resolution.stock_scope.expected_stock_code, expected
                )

    def test_bare_five_digits_need_hk_context(self):
        self.assertEqual(extract_stock_codes("00700"), [])
        self.assertEqual(extract_stock_codes("港股10000"), ["HK10000"])

    def test_bare_five_digits_survive_an_explicit_comparison(self):
        self.assertIn("HK01810", extract_stock_codes("比较 01810 和 AAPL"))
        self.assertIn("HK01810", extract_stock_codes("01810 HK 和 AAPL"))

    def test_amount_reading_wins_even_with_nearby_hk_word(self):
        self.assertEqual(extract_stock_codes("港股通买入本金10000元"), [])
        self.assertEqual(extract_stock_codes("比较 600519 和 10000元"), ["600519"])


class TestUnknownContextCodeIsNotAnAnchor(unittest.TestCase):
    """BUG-2: current_code must exist locally before it may pin the session."""

    def test_existence_check(self):
        self.assertTrue(_is_unknown_stock_code("HK10000"))
        for known in (
            "600276",
            "300122",
            "HK00700",
            "HK01810",
            "sh000016",
            "csi930955",
            "AAPL",
            "005930.KS",
        ):
            with self.subTest(code=known):
                self.assertFalse(_is_unknown_stock_code(known))

    def test_unknown_code_is_cleared_from_context(self):
        resolution = resolve_stock_scope("继续看", dict(POLLUTED_CONTEXT))

        self.assertNotIn("stock_code", resolution.effective_context)
        self.assertNotIn("stock_name", resolution.effective_context)

    def test_check_fails_open_when_the_universe_is_unavailable(self):
        with patch(
            "src.agent.stock_scope._known_stock_code_universe",
            return_value=frozenset(),
        ):
            self.assertFalse(_is_unknown_stock_code("HK10000"))
            self.assertFalse(_is_unknown_stock_code("600276"))

    def test_unadjudicable_shapes_fail_open(self):
        # A foreign/unknown shape the local pool does not index must never be
        # rejected just because it is absent.
        self.assertFalse(_is_unknown_stock_code("2330"))
        self.assertFalse(_is_unknown_stock_code("MARKET"))


class TestPollutedSessionRecovers(unittest.TestCase):
    """BUG-3: an invalid anchor must not be able to trap the session."""

    def setUp(self):
        self.context = dict(POLLUTED_CONTEXT)

    def test_named_real_stock_takes_over(self):
        cases = {
            "分析恒瑞医药": {"600276"},
            "看看药明康德": {"603259"},
            # No switch verb: the invalid anchor is what makes this a takeover.
            "600276怎么样": {"600276"},
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                resolution = resolve_stock_scope(message, dict(self.context))

                self.assertIsNotNone(resolution.stock_scope)
                self.assertEqual(
                    resolution.stock_scope.allowed_stock_codes, expected
                )
                self.assertEqual(
                    resolution.stock_scope.expected_stock_code,
                    next(iter(expected)),
                )
                self.assertEqual(
                    resolution.effective_context.get("stock_code"),
                    next(iter(expected)),
                )

    def test_explicit_code_recovers_without_a_switch_verb(self):
        for message in ("600276怎么样", "600276", "600276 能买吗"):
            with self.subTest(message=message):
                resolution = resolve_stock_scope(message, dict(self.context))

                self.assertEqual(
                    resolution.stock_scope.allowed_stock_codes, {"600276"}
                )

    def test_attribute_tail_name_takes_over(self):
        resolution = resolve_stock_scope(
            "分析宁德时代的基本面", dict(self.context)
        )

        self.assertEqual(
            resolution.stock_scope.allowed_stock_codes, {"300750"}
        )

    def test_message_without_a_stock_fails_open(self):
        for message in ("帮我找医药股", "帮我看看步步高升的概率"):
            with self.subTest(message=message):
                resolution = resolve_stock_scope(message, dict(self.context))

                self.assertIsNone(resolution.stock_scope)
                self.assertNotIn("stock_code", resolution.effective_context)

    def test_valid_anchor_keeps_the_switch_guard(self):
        # The escape hatch is only for an invalid anchor: with a valid current
        # stock, a bare code and no switch verb still stays in scope.
        resolution = resolve_stock_scope(
            "600276怎么样", {"stock_code": "600519", "stock_name": "贵州茅台"}
        )

        self.assertEqual(resolution.stock_scope.mode, "maintain")
        self.assertEqual(
            resolution.stock_scope.allowed_stock_codes, {"600519"}
        )


class TestToolGuardUnblocks(unittest.TestCase):
    """The incident itself was the tool guard refusing every real code."""

    @staticmethod
    def _registry():
        from src.agent.tools.registry import (
            ToolDefinition,
            ToolParameter,
            ToolRegistry,
        )

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="get_realtime_quote",
                description="quote",
                parameters=[
                    ToolParameter(
                        name="stock_code", type="string", description="code"
                    )
                ],
                handler=lambda stock_code: {"stock_code": stock_code},
            )
        )
        return registry

    def test_polluted_scope_would_block_the_tool_call(self):
        from src.agent.stock_scope import StockScope
        from src.agent.tools.execution import _guard_tool_stock_scope

        polluted = StockScope(
            expected_stock_code="HK10000", allowed_stock_codes={"HK10000"}
        )
        blocked = _guard_tool_stock_scope(
            self._registry(),
            "get_realtime_quote",
            {"stock_code": "600276"},
            polluted,
        )

        self.assertEqual(blocked["error"], "stock_scope_violation")
        self.assertEqual(blocked["allowed_stock_codes"], ["HK10000"])

    def test_recovered_session_reaches_the_real_stock(self):
        from src.agent.tools.execution import _guard_tool_stock_scope

        resolution = resolve_stock_scope("600276怎么样", dict(POLLUTED_CONTEXT))
        allowed = _guard_tool_stock_scope(
            self._registry(),
            "get_realtime_quote",
            {"stock_code": "600276"},
            resolution.stock_scope,
        )

        self.assertIsNone(allowed)

    def test_stockless_turn_is_not_restricted(self):
        from src.agent.tools.execution import _guard_tool_stock_scope

        resolution = resolve_stock_scope("帮我找医药股", dict(POLLUTED_CONTEXT))

        self.assertIsNone(
            _guard_tool_stock_scope(
                self._registry(),
                "get_realtime_quote",
                {"stock_code": "600276"},
                resolution.stock_scope,
            )
        )


class TestPreviousRoundContract(unittest.TestCase):
    """The mid-session switch / prose-trap contract must not regress."""

    def _resolve(self, message: str):
        return resolve_stock_scope(
            message, {"stock_code": "600519", "stock_name": "贵州茅台"}
        )

    def test_mid_session_switch_still_works(self):
        cases = {
            "看看宁德时代": "300750",
            "分析宁德时代的基本面": "300750",
            "看看宁德时代的走势": "300750",
            "换成宁德时代": "300750",
            "分析万科A": "000002",
            "看看值得买": "300785",
            "分析一下宁德时代": "300750",
            "看看这只比亚迪": "002594",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                resolution = self._resolve(message)

                self.assertEqual(resolution.stock_scope.mode, "switch")
                self.assertEqual(
                    resolution.stock_scope.expected_stock_code, expected
                )

    def test_prose_traps_stay_in_scope(self):
        for message in (
            "帮我看看步步高升的概率",
            "祝你步步高升",
            "好想你",
            "三人行，必有我师",
            "这个人很有大智慧",
        ):
            with self.subTest(message=message):
                resolution = self._resolve(message)

                self.assertEqual(resolution.stock_scope.mode, "maintain")
                self.assertEqual(
                    resolution.stock_scope.allowed_stock_codes, {"600519"}
                )

    def test_compare_mode_still_widens(self):
        resolution = self._resolve("宁德时代和比亚迪哪个好")

        self.assertEqual(resolution.stock_scope.mode, "compare")
        self.assertEqual(
            resolution.stock_scope.allowed_stock_codes,
            {"600519", "300750", "002594"},
        )


if __name__ == "__main__":
    unittest.main()
