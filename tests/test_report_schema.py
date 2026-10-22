# -*- coding: utf-8 -*-
"""
===================================
Report Engine - Schema parsing and fallback tests
===================================

Tests for AnalysisReportSchema validation and analyzer fallback behavior.
"""

import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Mock litellm before importing analyzer (optional runtime dep)
try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.schemas.report_schema import AnalysisReportSchema
from src.analyzer import GeminiAnalyzer, AnalysisResult


class TestAnalysisReportSchema(unittest.TestCase):
    """Schema parsing tests."""

    def test_valid_dashboard_parses(self) -> None:
        """Valid LLM-like JSON parses successfully."""
        data = {
            "stock_name": "贵州茅台",
            "sentiment_score": 75,
            "trend_prediction": "看多",
            "operation_advice": "持有",
            "decision_type": "hold",
            "confidence_level": "中",
            "dashboard": {
                "core_conclusion": {"one_sentence": "持有观望"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {"sniper_points": {"stop_loss": "110元"}},
            },
            "analysis_summary": "基本面稳健",
        }
        schema = AnalysisReportSchema.model_validate(data)
        self.assertEqual(schema.stock_name, "贵州茅台")
        self.assertEqual(schema.sentiment_score, 75)
        self.assertIsNotNone(schema.dashboard)

    def test_schema_allows_optional_fields_missing(self) -> None:
        """Schema accepts minimal valid structure."""
        data = {
            "stock_name": "测试",
            "sentiment_score": 50,
            "trend_prediction": "震荡",
            "operation_advice": "观望",
        }
        schema = AnalysisReportSchema.model_validate(data)
        self.assertIsNone(schema.dashboard)
        self.assertIsNone(schema.analysis_summary)

    def test_schema_accepts_phase_decision_and_defaults_lists(self) -> None:
        """Dashboard accepts the optional phase_decision contract."""
        data = {
            "stock_name": "贵州茅台",
            "sentiment_score": 70,
            "trend_prediction": "震荡",
            "operation_advice": "持有",
            "dashboard": {
                "core_conclusion": {"one_sentence": "等待确认"},
                "phase_decision": {
                    "phase_context": {"phase": "intraday", "market": "cn"},
                    "action_window": "盘中跟踪",
                    "immediate_action": "等待确认",
                    "next_check_time": "14:30",
                    "confidence_reason": "数据质量可用",
                },
            },
        }

        schema = AnalysisReportSchema.model_validate(data)

        self.assertIsNotNone(schema.dashboard)
        phase_decision = schema.dashboard and schema.dashboard.phase_decision
        self.assertIsNotNone(phase_decision)
        if phase_decision:
            self.assertEqual(phase_decision.watch_conditions, [])
            self.assertEqual(phase_decision.data_limitations, [])
            self.assertEqual(phase_decision.phase_context["phase"], "intraday")

    def test_schema_allows_numeric_strings(self) -> None:
        """Schema accepts string values for numeric fields (LLM may return N/A)."""
        data = {
            "stock_name": "测试",
            "sentiment_score": 60,
            "trend_prediction": "看多",
            "operation_advice": "买入",
            "dashboard": {
                "data_perspective": {
                    "price_position": {
                        "current_price": "N/A",
                        "bias_ma5": "2.5",
                    }
                }
            },
        }
        schema = AnalysisReportSchema.model_validate(data)
        self.assertIsNotNone(schema.dashboard)
        pp = schema.dashboard and schema.dashboard.data_perspective and schema.dashboard.data_perspective.price_position
        self.assertIsNotNone(pp)
        if pp:
            self.assertEqual(pp.current_price, "N/A")
            self.assertEqual(pp.bias_ma5, "2.5")

    def test_schema_fails_on_invalid_sentiment_score(self) -> None:
        """Schema validation fails when sentiment_score out of range."""
        data = {
            "stock_name": "测试",
            "sentiment_score": 150,  # out of 0-100
            "trend_prediction": "看多",
            "operation_advice": "买入",
        }
        with self.assertRaises(Exception):
            AnalysisReportSchema.model_validate(data)


class TestAnalyzerSchemaFallback(unittest.TestCase):
    """Analyzer fallback when schema validation fails."""

    def test_parse_response_continues_when_schema_fails(self) -> None:
        """When schema validation fails, analyzer continues with raw dict."""
        analyzer = GeminiAnalyzer()
        response = json.dumps({
            "stock_name": "贵州茅台",
            "sentiment_score": 150,  # invalid for schema
            "trend_prediction": "看多",
            "operation_advice": "持有",
            "analysis_summary": "测试摘要",
        })
        result = analyzer._parse_response(response, "600519", "贵州茅台")
        self.assertIsInstance(result, AnalysisResult)
        self.assertEqual(result.code, "600519")
        self.assertEqual(result.sentiment_score, 150)  # from raw dict
        self.assertTrue(result.success)

    def test_parse_response_valid_json_succeeds(self) -> None:
        """Valid JSON produces correct AnalysisResult."""
        analyzer = GeminiAnalyzer()
        response = json.dumps({
            "stock_name": "贵州茅台",
            "sentiment_score": 72,
            "trend_prediction": "看多",
            "operation_advice": "持有",
            "decision_type": "hold",
            "confidence_level": "高",
            "analysis_summary": "技术面向好",
        })
        result = analyzer._parse_response(response, "600519", "股票600519")
        self.assertIsInstance(result, AnalysisResult)
        self.assertEqual(result.name, "贵州茅台")
        self.assertEqual(result.sentiment_score, 72)
        self.assertEqual(result.analysis_summary, "技术面向好")
        self.assertEqual(result.action, "hold")
        self.assertEqual(result.action_label, "持有")

    def test_parse_response_preserves_explicit_action_in_raw_result(self) -> None:
        analyzer = GeminiAnalyzer()
        response = json.dumps({
            "stock_name": "贵州茅台",
            "sentiment_score": 58,
            "trend_prediction": "震荡",
            "operation_advice": "持有观察",
            "decision_type": "hold",
            "action": "watch",
            "analysis_summary": "等待确认",
        })

        result = analyzer._parse_response(response, "600519", "股票600519")
        raw_result = result.to_dict()

        self.assertEqual(result.action, "watch")
        self.assertEqual(result.action_label, "观望")
        self.assertEqual(result.decision_type, "hold")
        self.assertEqual(raw_result["action"], "watch")
        self.assertEqual(raw_result["action_label"], "观望")

    def test_parse_response_keeps_unknown_dashboard_fields(self) -> None:
        analyzer = GeminiAnalyzer()
        response = json.dumps({
            "stock_name": "贵州茅台",
            "sentiment_score": 72,
            "trend_prediction": "看多",
            "operation_advice": "持有",
            "decision_type": "hold",
            "analysis_summary": "技术面向好",
            "dashboard": {
                "core_conclusion": {
                    "one_sentence": "先观察",
                    "signal_type": "🟡持有观望",
                },
                "decision_stability": {
                    "applied": True,
                    "reason": "回测验证",
                },
            },
        })
        result = analyzer._parse_response(response, "600519", "股票600519")
        self.assertEqual(result.dashboard["decision_stability"]["applied"], True)
        self.assertEqual(result.dashboard["decision_stability"]["reason"], "回测验证")

    def test_parse_response_repairs_single_json_candidate(self) -> None:
        analyzer = GeminiAnalyzer()
        response = """```json
{
  "stock_name": "贵州茅台",
  "sentiment_score": 68,
  "trend_prediction": "看多",
  "operation_advice": "持有",
}
```"""

        result = analyzer._parse_response(response, "600519", "股票600519")

        self.assertTrue(result.success)
        self.assertEqual(result.name, "贵州茅台")
        self.assertEqual(result.sentiment_score, 68)

    def test_parse_response_accepts_single_generic_json_fence(self) -> None:
        analyzer = GeminiAnalyzer()
        response = """```
{
  "stock_name": "贵州茅台",
  "sentiment_score": 67,
  "trend_prediction": "看多",
  "operation_advice": "持有",
  "analysis_summary": "技术面向好"
}
```"""

        result = analyzer._parse_response(response, "600519", "股票600519")

        self.assertTrue(result.success)
        self.assertEqual(result.name, "贵州茅台")
        self.assertEqual(result.sentiment_score, 67)

    def test_parse_response_repairs_nested_single_json_candidate(self) -> None:
        analyzer = GeminiAnalyzer()
        response = """```json
{
  "stock_name": "贵州茅台",
  "sentiment_score": 69,
  "trend_prediction": "看多",
  "operation_advice": "持有",
  "dashboard": {"core_conclusion": {"one_sentence": "继续观察",},},
}
```"""

        result = analyzer._parse_response(response, "600519", "股票600519")

        self.assertTrue(result.success)
        self.assertEqual(result.sentiment_score, 69)
        self.assertEqual(result.dashboard["core_conclusion"]["one_sentence"], "继续观察")

    def test_validate_json_response_accepts_single_generic_json_fence(self) -> None:
        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        analyzer._config_override = SimpleNamespace(generation_backend="litellm")

        analyzer._validate_json_response("""```
{
  "stock_name": "贵州茅台",
  "sentiment_score": 66,
  "trend_prediction": "看多",
  "operation_advice": "持有",
  "analysis_summary": "技术面向好"
}
```""")

    def test_validate_json_response_accepts_single_json_fence(self) -> None:
        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        analyzer._config_override = SimpleNamespace(generation_backend="litellm")

        analyzer._validate_json_response("""```json
{
  "stock_name": "贵州茅台",
  "sentiment_score": 65,
  "trend_prediction": "看多",
  "operation_advice": "持有",
  "analysis_summary": "技术面向好"
}
```""")

    def test_validate_json_response_rejects_ambiguous_json_before_repair(self) -> None:
        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        analyzer._config_override = SimpleNamespace(generation_backend="litellm")

        with self.assertRaises(Exception) as context:
            analyzer._validate_json_response('{"sentiment_score": 70} {"sentiment_score": 80}')

        self.assertEqual(getattr(context.exception, "details", {}).get("reason"), "ambiguous_json")

    def test_validate_json_response_allows_short_prose_around_fence(self) -> None:
        """代码块外面有一点散文是可以接受的。

        契约在 2026-10-23 变了。原来只要 fence 外面有字就判 ambiguous_json，
        实测代价太大：LLM 习惯说"以下是分析结果："，于是正常输出被当成歧义，
        整只票的分析结果被丢弃（2026-10-22 中国海油 600938 就是这样失败的，
        数据全拿到了，只因格式被判死）。

        现在允许 ≤200 字符的散文；超过 200 才继续当歧义拒绝
        （长散文更可能是模型给了两套互相矛盾的分析）。
        """
        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        analyzer._config_override = SimpleNamespace(generation_backend="litellm")

        # _validate_json_response 的契约是「不抛异常即通过」（返回 None），
        # 它被当作 response_validator 用，异常才会触发换模型。
        analyzer._validate_json_response("""Here is the JSON:
```
{"sentiment_score": 70, "trend_prediction": "看多"}
```""")

    def test_validate_json_response_rejects_fence_with_long_outside_prose(self) -> None:
        """外面散文太长仍然要拒 —— 那更可能是两套矛盾的分析。"""
        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        analyzer._config_override = SimpleNamespace(generation_backend="litellm")

        long_prose = "这是一段很长的说明文字。" * 30  # 远超 200 字符
        with self.assertRaises(Exception) as context:
            analyzer._validate_json_response(f"""{long_prose}
```
{{"sentiment_score": 70, "trend_prediction": "看多"}}
```""")

        self.assertEqual(getattr(context.exception, "details", {}).get("reason"), "ambiguous_json")

    def test_validate_json_response_rejects_multiple_json_fences(self) -> None:
        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        analyzer._config_override = SimpleNamespace(generation_backend="litellm")

        with self.assertRaises(Exception) as context:
            analyzer._validate_json_response("""```json
{"sentiment_score": 70}
```
```json
{"sentiment_score": 80}
```""")

        self.assertEqual(getattr(context.exception, "details", {}).get("reason"), "ambiguous_json")

    def test_validate_json_response_rejects_non_json_language_fence(self) -> None:
        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        analyzer._config_override = SimpleNamespace(generation_backend="litellm")

        with self.assertRaises(Exception) as context:
            analyzer._validate_json_response("""```text
{"sentiment_score": 70, "trend_prediction": "看多"}
```""")

        self.assertEqual(getattr(context.exception, "details", {}).get("reason"), "ambiguous_json")

    def test_validate_json_response_rejects_missing_minimal_contract(self) -> None:
        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        analyzer._config_override = SimpleNamespace(generation_backend="litellm")

        with self.assertRaises(Exception) as context:
            analyzer._validate_json_response('{"stock_name": "贵州茅台"}')

        self.assertEqual(getattr(context.exception, "details", {}).get("reason"), "minimal_contract_failed")

    def test_validate_json_response_rejects_parser_unconstructable_sentiment(self) -> None:
        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        analyzer._config_override = SimpleNamespace(generation_backend="litellm")

        with self.assertRaises(Exception) as context:
            analyzer._validate_json_response(json.dumps({
                "stock_name": "贵州茅台",
                "sentiment_score": "not-a-number",
                "trend_prediction": "看多",
                "operation_advice": "持有",
                "analysis_summary": "测试摘要",
            }))

        self.assertEqual(getattr(context.exception, "details", {}).get("reason"), "parser_contract_failed")

    def test_parse_response_falls_back_when_parser_contract_fails(self) -> None:
        analyzer = GeminiAnalyzer()
        response = json.dumps({
            "stock_name": "贵州茅台",
            "sentiment_score": "not-a-number",
            "trend_prediction": "看多",
            "operation_advice": "持有",
            "analysis_summary": "测试摘要",
        })

        result = analyzer._parse_response(response, "600519", "股票600519")

        self.assertFalse(result.success)
        self.assertEqual(result.sentiment_score, 50)
        self.assertIn("JSON", result.error_message)

    def test_parse_text_response_honors_injected_runtime_report_language(self) -> None:
        """Fallback text parsing should use the analyzer's injected config, not the global singleton."""
        with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
            analyzer = GeminiAnalyzer(config=SimpleNamespace(report_language="en"))

        result = analyzer._parse_text_response("bullish buy setup", "AAPL", "Apple")

        self.assertEqual(result.report_language, "en")
        self.assertEqual(result.trend_prediction, "Bullish")
        self.assertEqual(result.operation_advice, "Buy")
        self.assertEqual(result.confidence_level, "Low")
