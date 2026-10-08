# -*- coding: utf-8 -*-
"""Regression tests for screening LiteLLM ranking request compatibility."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

from src.llm.generation_params import clear_litellm_generation_param_recovery_cache
from src.services.screening.models import Pick
from src.services.screening.ranker import (
    _call_llm,
    _looks_like_truncated_json,
    _salvage_ranking_objects,
    rank_candidates_with_metadata,
)


def _response(content: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _ranking_response(*codes: str) -> str:
    ranked = [
        {
            "code": code,
            "llm_score": 90 - index,
            "confidence": 0.8,
            "reason": f"reason-{code}",
            "risk": "risk",
        }
        for index, code in enumerate(codes)
    ]
    import json

    return json.dumps({"ranked": ranked}, ensure_ascii=False)


def test_screening_ranker_direct_call_omits_temperature_for_gpt5() -> None:
    clear_litellm_generation_param_recovery_cache()
    completion_calls: list[dict[str, object]] = []

    def completion(**kwargs):
        completion_calls.append(dict(kwargs))
        return _response()

    fake_litellm = SimpleNamespace(completion=completion)

    with patch.dict(sys.modules, {"litellm": fake_litellm}, clear=False):
        result = _call_llm(
            "rank candidates",
            api_key="test-key",
            model="openai/gpt-5-mini",
            base_url="",
            temperature=0.2,
            json_mode=False,
        )

    assert result == "ok"
    assert "temperature" not in completion_calls[0]


def test_screening_ranker_direct_call_uses_responses_wire_model_for_matching_channel() -> None:
    completion_calls: list[dict[str, object]] = []

    def completion(**kwargs):
        completion_calls.append(dict(kwargs))
        return _response()

    fake_litellm = SimpleNamespace(completion=completion)

    with patch.dict(sys.modules, {"litellm": fake_litellm}, clear=False):
        result = _call_llm(
            "rank candidates",
            api_key="test-key",
            model="openai/gpt-5.6-sol",
            base_url="",
            json_mode=False,
            channels=[
                {
                    "name": "draft",
                    "protocol": "openai",
                    "api_surface": "responses",
                    "api_keys": ["sk-draft"],
                    "base_url": "https://api.example.com/v1",
                    "models": ["openai/gpt-5.6-sol"],
                }
            ],
        )

    assert result == "ok"
    assert len(completion_calls) == 1
    assert completion_calls[0]["model"] == "openai/responses/gpt-5.6-sol"
    assert completion_calls[0]["api_key"] == "sk-draft"
    assert completion_calls[0]["api_base"] == "https://api.example.com/v1"


def test_screening_ranker_does_not_retry_public_alias_after_responses_attempt_failure() -> None:
    completion_calls: list[dict[str, object]] = []

    def completion(**kwargs):
        completion_calls.append(dict(kwargs))
        raise RuntimeError("responses endpoint rejected request")

    fake_litellm = SimpleNamespace(completion=completion)

    with patch.dict(sys.modules, {"litellm": fake_litellm}, clear=False):
        try:
            _call_llm(
                "rank candidates",
                api_key="test-key",
                model="openai/gpt-5.6-sol",
                base_url="https://fallback.example.com/v1",
                json_mode=False,
                channels=[
                    {
                        "name": "draft",
                        "protocol": "openai",
                        "api_surface": "responses",
                        "api_keys": ["sk-draft"],
                        "base_url": "https://api.example.com/v1",
                        "models": ["openai/gpt-5.6-sol"],
                    }
                ],
            )
        except RuntimeError as exc:
            assert "responses endpoint rejected request" in str(exc)
        else:
            raise AssertionError("expected _call_llm to raise")

    assert len(completion_calls) == 1
    assert completion_calls[0]["model"] == "openai/responses/gpt-5.6-sol"
    assert completion_calls[0]["api_base"] == "https://api.example.com/v1"


def test_screening_ranker_rejects_invalid_responses_wire_route_before_call() -> None:
    completion_calls: list[dict[str, object]] = []

    def completion(**kwargs):
        completion_calls.append(dict(kwargs))
        return _response()

    fake_litellm = SimpleNamespace(completion=completion)

    with patch.dict(sys.modules, {"litellm": fake_litellm}, clear=False):
        try:
            _call_llm(
                "rank candidates",
                api_key="test-key",
                model="anthropic/claude-sonnet-4-6",
                base_url="",
                json_mode=False,
                channels=[
                    {
                        "name": "draft",
                        "protocol": "openai",
                        "api_surface": "responses",
                        "api_keys": ["sk-draft"],
                        "models": ["anthropic/claude-sonnet-4-6"],
                    }
                ],
            )
        except ValueError as exc:
            assert "normalized openai" in str(exc)
        else:
            raise AssertionError("expected invalid Responses route to raise")

    assert completion_calls == []


def test_screening_ranker_direct_call_retries_temperature_with_param_recovery() -> None:
    clear_litellm_generation_param_recovery_cache()
    completion_calls: list[dict[str, object]] = []

    def completion(**kwargs):
        completion_calls.append(dict(kwargs))
        if len(completion_calls) == 1:
            raise RuntimeError("Unsupported parameter: temperature is not supported")
        return _response()

    fake_litellm = SimpleNamespace(completion=completion)

    with patch.dict(sys.modules, {"litellm": fake_litellm}, clear=False):
        result = _call_llm(
            "rank candidates",
            api_key="test-key",
            model="openai/custom-temp-locked",
            base_url="",
            temperature=0.7,
            json_mode=False,
        )

    assert result == "ok"
    assert completion_calls[0]["temperature"] == 0.7
    assert "temperature" not in completion_calls[1]


def test_screening_ranker_does_not_read_reasoning_content_when_content_is_empty() -> None:
    completion_calls: list[dict[str, object]] = []

    def completion(**kwargs):
        completion_calls.append(dict(kwargs))
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        reasoning_content='{"ranked": []}',
                    )
                )
            ]
        )

    fake_litellm = SimpleNamespace(completion=completion)
    with patch.dict(sys.modules, {"litellm": fake_litellm}, clear=False):
        result = _call_llm(
            "rank candidates",
            api_key="test-key",
            model="deepseek/deepseek-reasoner",
            base_url="",
            json_mode=True,
        )

    # Do not treat internal reasoning_content as final model output; allow higher
    # level fallback logic to handle it instead.
    assert result == ''
    assert len(completion_calls) == 1


def test_screening_ranker_reads_choice_content_blocks_without_changing_json() -> None:
    expected = '{"ranked":[{"code":"600519"}]}'

    def completion(**_kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=""),
                    content_blocks=[
                        {"type": "output_text", "text": '{"ranked":[{"code":"600'},
                        {"type": "output_text", "text": '519"}]}'},
                    ],
                )
            ]
        )

    with patch.dict(sys.modules, {"litellm": SimpleNamespace(completion=completion)}, clear=False):
        result = _call_llm(
            "rank candidates",
            api_key="test-key",
            model="openai/gpt-5-mini",
            base_url="",
            json_mode=True,
        )

    assert result == expected


def test_screening_ranker_ignores_thinking_blocks_in_message_content() -> None:
    final = _ranking_response("600519")
    draft = _ranking_response("000001")

    def completion(**_kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=[
                            {"type": "thinking", "text": draft},
                            {"type": "output_text", "text": final},
                        ],
                    )
                )
            ]
        )

    with patch.dict(sys.modules, {"litellm": SimpleNamespace(completion=completion)}, clear=False):
        result = _call_llm(
            "rank candidates",
            api_key="test-key",
            model="openai/gpt-5-mini",
            base_url="",
            json_mode=True,
        )

    assert result == final


def test_screening_ranker_router_call_applies_kimi_temperature_and_recovery(tmp_path) -> None:
    clear_litellm_generation_param_recovery_cache()
    router_calls: list[dict[str, object]] = []

    class FakeRouter:
        def __init__(self, *, model_list):
            self.model_list = model_list

        def completion(self, **kwargs):
            router_calls.append(dict(kwargs))
            if len(router_calls) == 1:
                raise RuntimeError("Unsupported parameter: temperature is not supported")
            return _response()

    fake_litellm = SimpleNamespace(Router=FakeRouter, completion=lambda **_: _response())
    config_path = tmp_path / "litellm.yaml"
    config_path.write_text(
        """
model_list:
  - model_name: moonshot/kimi-k2.6
    litellm_params:
      model: moonshot/kimi-k2.6
        """.strip(),
        encoding="utf-8",
    )

    with patch.dict(sys.modules, {"litellm": fake_litellm}, clear=False):
        result = _call_llm(
            "rank candidates",
            api_key="test-key",
            model="moonshot/kimi-k2.6",
            base_url="",
            temperature=0.2,
            json_mode=False,
            config_path=str(config_path),
        )

    assert result == "ok"
    assert router_calls[0]["temperature"] == 1.0
    assert "temperature" not in router_calls[1]


def test_rank_candidates_with_metadata_does_not_mutate_candidates_when_coverage_is_low() -> None:
    candidates = [
        Pick(rank=1, code="600519", name="贵州茅台", final_score=90.0, screen_score=90.0),
        Pick(rank=2, code="000001", name="平安银行", final_score=80.0, screen_score=80.0),
    ]
    response = """
    {
      "ranked": [
        {
          "code": "600519",
          "reason": "partial coverage",
          "risk": "watch valuation",
          "llm_score": 95,
          "sector": "Baijiu"
        }
      ]
    }
    """.strip()

    with patch("src.services.screening.ranker._call_llm", return_value=response):
        result = rank_candidates_with_metadata(
            candidates,
            "test hints",
            "test-key",
            "openai/gpt-5-mini",
            min_coverage=0.75,
            max_retries=0,
        )

    assert result.ranked is False
    assert result.picks is candidates
    assert candidates[0].llm_score is None
    assert candidates[0].risk_summary == ""
    assert candidates[0].llm_sector == ""


def test_rank_candidates_with_metadata_tries_fallback_after_invalid_json() -> None:
    candidates = [
        Pick(rank=1, code="600519", name="贵州茅台", final_score=90.0, screen_score=90.0),
        Pick(rank=2, code="000001", name="平安银行", final_score=80.0, screen_score=80.0),
    ]
    called_models: list[str] = []

    def call_llm(_prompt, _api_key, model, _base_url, **kwargs):
        called_models.append(model)
        assert kwargs["fallback_models"] == []
        if model == "deepseek/deepseek-chat":
            return "I cannot provide structured output."
        return _ranking_response("600519", "000001")

    with patch("src.services.screening.ranker._call_llm", side_effect=call_llm):
        result = rank_candidates_with_metadata(
            candidates,
            "test hints",
            "test-key",
            "deepseek/deepseek-chat",
            fallback_models=["gemini/gemini-3-flash-preview"],
            min_coverage=1.0,
            max_retries=0,
        )

    assert result.ranked is True
    assert result.model_used == "gemini/gemini-3-flash-preview"
    assert result.attempted_models == [
        "deepseek/deepseek-chat",
        "gemini/gemini-3-flash-preview",
    ]
    assert called_models == result.attempted_models
    assert result.errors == []


def test_rank_candidates_with_metadata_reports_all_invalid_models() -> None:
    candidates = [Pick(rank=1, code="600519", name="贵州茅台", final_score=90.0, screen_score=90.0)]

    with patch("src.services.screening.ranker._call_llm", return_value="not-json"):
        result = rank_candidates_with_metadata(
            candidates,
            "test hints",
            "test-key",
            "deepseek/deepseek-chat",
            fallback_models=["openai/gpt-4o"],
            max_retries=0,
        )

    assert result.ranked is False
    assert result.picks is candidates
    assert result.failure_reason == "invalid_response"
    assert result.attempted_models == ["deepseek/deepseek-chat", "openai/gpt-4o"]
    assert len(result.errors) == 2


def test_rank_candidates_with_metadata_labels_truncated_json_distinctly() -> None:
    """A response cut off by max_tokens is valid JSON with the tail missing.

    Reporting it as "no_json_found" hides the real remedy (a larger
    LLM_MAX_TOKENS) behind a prompt-shaped explanation, so keep the two apart.
    """
    candidates = [Pick(rank=1, code="601166", name="兴业银行", final_score=90.0, screen_score=90.0)]
    truncated = '{"ranked":[{"code":"601166","llm_score":82,"thesis":"低估值修复'

    with patch("src.services.screening.ranker._call_llm", return_value=truncated):
        result = rank_candidates_with_metadata(
            candidates,
            "test hints",
            "test-key",
            "deepseek/deepseek-chat",
            fallback_models=[],
            max_retries=0,
        )

    assert result.ranked is False
    assert any("truncated_json" in error for error in result.errors)
    assert not any("no_json_found" in error for error in result.errors)


def test_rank_candidates_with_metadata_keeps_no_json_found_for_prose() -> None:
    """Plain prose is not truncation: it must stay on the original error code."""
    candidates = [Pick(rank=1, code="601166", name="兴业银行", final_score=90.0, screen_score=90.0)]

    with patch(
        "src.services.screening.ranker._call_llm",
        return_value="抱歉，我无法按要求输出 JSON。",
    ):
        result = rank_candidates_with_metadata(
            candidates,
            "test hints",
            "test-key",
            "deepseek/deepseek-chat",
            fallback_models=[],
            max_retries=0,
        )

    assert result.ranked is False
    assert any("no_json_found" in error for error in result.errors)
    assert not any("truncated_json" in error for error in result.errors)


def test_looks_like_truncated_json_finds_structure_after_prose_or_bare_fence() -> None:
    """The JSON need not start the response: models announce the result first.

    Demanding that the first non-space character be ``{``/``[`` classified these
    genuine truncations as "no_json_found", which hides the real remedy.
    """
    assert (
        _looks_like_truncated_json(
            '好的，以下是排序结果：\n```json\n{"ranked":[{"code":"601166"'
        )
        is True
    )
    assert (
        _looks_like_truncated_json('好的，结果如下：{"ranked":[{"code":"601166"')
        is True
    )
    # A bare opening fence with no newline and no closing fence.
    assert _looks_like_truncated_json('```{"a":') is True
    # Prose with no opening brace stays on the original error code.
    assert _looks_like_truncated_json("抱歉，我无法按要求输出 JSON。") is False
    assert _looks_like_truncated_json("") is False
    # Complete JSON followed by prose is not a truncation.
    assert _looks_like_truncated_json('{"ranked":[]}\n以上是排序结果。') is False


def test_rank_candidates_with_metadata_labels_prose_prefixed_truncation() -> None:
    """A truncated ranking prefixed by prose is still a truncation, not prose."""
    candidates = [Pick(rank=1, code="601166", name="兴业银行", final_score=90.0, screen_score=90.0)]
    truncated = (
        '好的，以下是排序结果：\n```json\n'
        '{"ranked":[{"code":"601166","llm_score":82,"thesis":"低估值修复'
    )

    with patch("src.services.screening.ranker._call_llm", return_value=truncated):
        result = rank_candidates_with_metadata(
            candidates,
            "test hints",
            "test-key",
            "deepseek/deepseek-chat",
            fallback_models=[],
            max_retries=0,
        )

    assert result.ranked is False
    assert any("truncated_json" in error for error in result.errors)
    assert not any("no_json_found" in error for error in result.errors)


def test_rank_candidates_with_metadata_salvages_partial_ranking_from_truncation() -> None:
    """The positive salvage path: a response cut off mid-object still ranks.

    The tests above only pin the unusable truncation (the *first* object was the
    one cut). When the cut lands after some candidates were emitted, their
    objects are complete JSON and the ranking is recoverable in part — silently
    discarding them would drop the whole section back to factor-only ordering.
    """
    codes = ["600519", "000001", "600036", "601318", "000858"]
    candidates = [
        Pick(rank=index + 1, code=code, name=code, final_score=90.0 - index, screen_score=90.0 - index)
        for index, code in enumerate(codes)
    ]
    complete = _ranking_response(*codes)
    # Cut inside the fifth object: four candidates survive, the tail does not.
    truncated = complete[: complete.index('"000858"') + len('"000858"')]

    with patch("src.services.screening.ranker._call_llm", return_value=truncated):
        result = rank_candidates_with_metadata(
            candidates,
            "test hints",
            "test-key",
            "deepseek/deepseek-chat",
            fallback_models=[],
            min_coverage=0.60,
            max_retries=0,
        )

    assert result.ranked is True
    assert result.coverage == 0.8
    assert result.errors == ["json_repaired:salvaged_truncated"]
    # The four salvaged picks keep their LLM fields; the unmatched fifth is still
    # appended so the section never loses a candidate.
    assert [pick.code for pick in result.picks[:4]] == codes[:4]
    assert result.picks[0].llm_score == 90.0
    assert result.picks[1].ranking_reason == "reason-000001"
    assert sorted(pick.code for pick in result.picks) == sorted(codes)


def test_salvage_ranking_objects_keeps_each_candidate_once_at_its_own_level() -> None:
    """A wrapper object that encloses a candidate is not itself a candidate.

    Returning both made the wrapper's own code race the pick nested inside it and
    left the duplicate for the caller to drop as duplicate_code/unknown_code.
    """
    errors: list[str] = []
    objects = _salvage_ranking_objects(
        '{"code": "OUT", "ranked": [{"code": "IN"}]}',
        errors,
    )

    assert [item["code"] for item in objects] == ["IN"]
    # The payload is complete: only a genuine truncation may carry that label.
    assert errors == []

    errors = []
    objects = _salvage_ranking_objects(
        '{"code": "OUT", "ranked": [{"code": "IN"}, {"code": "PART',
        errors,
    )

    assert [item["code"] for item in objects] == ["IN"]
    assert errors == ["json_repaired:salvaged_truncated"]


def test_ranker_max_tokens_defaults_match_the_screening_config_default() -> None:
    """Signature defaults that disagree with the config are a silent trap: a
    future caller that omits max_tokens would cut the ranking budget back to a
    quarter of its configured size."""
    import inspect

    from src.services.screening.config import Config as ScreeningConfig
    from src.services.screening.ranker import (
        _DEFAULT_LLM_MAX_TOKENS,
        _call_litellm_router,
        rank_candidates,
    )

    assert _DEFAULT_LLM_MAX_TOKENS == ScreeningConfig().llm_max_tokens == 8192
    for function in (rank_candidates, rank_candidates_with_metadata, _call_llm, _call_litellm_router):
        assert (
            inspect.signature(function).parameters["max_tokens"].default
            == _DEFAULT_LLM_MAX_TOKENS
        ), function.__name__
