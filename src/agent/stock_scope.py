# -*- coding: utf-8 -*-
"""Stock-scope helpers for ask-stock follow-up chat turns."""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


SWITCH_CLEANUP_KEYS = {
    "stock_name",
    "previous_analysis_summary",
    "previous_strategy",
    "previous_price",
    "previous_change_pct",
    "realtime_quote",
    "daily_history",
    "chip_distribution",
    "trend_result",
    "news_context",
    "fundamental_context",
    "market_structure_context",
    "analysis_context_pack_summary",
    "market_phase_context",
}

_STRONG_COMPARE_PATTERN = re.compile(r"比较|对比|vs\b|和[^，。,.!?！？]{0,40}比", re.IGNORECASE)
_WEAK_COMPARE_HINT_PATTERN = re.compile(r"差异(?!化)|区别|不同|相比|对照|比一比")
_CHOICE_COMPARE_PATTERN = re.compile(r"哪个|哪只|哪一个|谁更|更值得|更适合|怎么选|选哪|二选一")
_LINKED_COMPARE_PATTERN = re.compile(
    r"(?:和|与|跟|同)(?P<body>[^，。,.!?！？]{0,40})(?:差异(?!化)|区别|不同|相比|对照|比一比)"
)
_SWITCH_PATTERN = re.compile(r"换成|改看|分析|看看|研究|诊断")
_LOWERCASE_TICKER_PATTERN = re.compile(r"(?<![a-zA-Z.])([a-z]{2,5}(?:\.[a-z]{1,2})?)(?![a-zA-Z0-9])")
_EXCHANGE_TOKEN_CANDIDATES = {"SH", "SZ", "BJ", "HK", "SS"}
_CONTEXTUAL_INDICATOR_TOKENS = {"MA"}
_INDICATOR_CONTEXT_PATTERN = re.compile(
    r"指标|均线|移动平均|排列|多头|空头|金叉|死叉|支撑|压力|MA\d|SMA|EMA",
    re.IGNORECASE,
)

# Match complete explicit SH/SZ/CSI tokens; the registry parser remains the
# only authority that can promote a match to index identity.
_INDEX_TOKEN_PATTERNS = (
    (r"(?<![a-zA-Z0-9_])(?:sh|sz)\d{6}(?![a-zA-Z0-9_])", re.IGNORECASE),
    (r"(?<![a-zA-Z0-9_])csi\d{6}(?![a-zA-Z0-9_])", re.IGNORECASE),
    (
        r"(?<![a-zA-Z0-9_])\d{6}\.(?:sh|sz|csi)(?![a-zA-Z0-9_])",
        re.IGNORECASE,
    ),
)


def _extract_index_canonical_tokens(
    text: str,
    registry: Any,
) -> "tuple[list[tuple[int, int]], list[str]]":
    """Return full spans and canonicals for exact registered index tokens."""
    spans: List[tuple[int, int]] = []
    canonicals: List[str] = []
    for pattern, flags in _INDEX_TOKEN_PATTERNS:
        for match in re.finditer(pattern, text, flags):
            raw = match.group(0)
            try:
                from src.services.stock_list_parser import (
                    ParseStatus,
                    parse_analysis_target,
                )

                target = parse_analysis_target(raw, registry)
            except Exception:
                continue
            if target.asset_type != ParseStatus.INDEX:
                continue
            if not target.canonical_id:
                continue
            start, end = match.span()
            if any(s <= start and end <= e for s, e in spans):
                continue
            spans.append((start, end))
            canonicals.append(target.canonical_id)
    return spans, canonicals


def _is_inside_index_span(start: int, end: int, spans: List[tuple[int, int]]) -> bool:
    return any(span_start <= start and end <= span_end for span_start, span_end in spans)


def _has_ascii_token_boundaries(text: str, start: int, end: int) -> bool:
    def _is_word_char(char: str) -> bool:
        return bool(char) and char.isascii() and (char.isalnum() or char == "_")

    return (
        not _is_word_char(text[start - 1:start])
        and not _is_word_char(text[end:end + 1])
    )


@dataclass(frozen=True)
class StockScope:
    """Runtime stock-scope contract for one chat turn."""

    expected_stock_code: str = ""
    allowed_stock_codes: Set[str] = field(default_factory=set)
    mode: str = "maintain"

    def as_log_payload(self) -> Dict[str, Any]:
        return {
            "expected_stock_code": self.expected_stock_code,
            "allowed_stock_codes": sorted(self.allowed_stock_codes),
            "mode": self.mode,
        }


@dataclass(frozen=True)
class StockScopeResolution:
    """Result produced before a chat turn enters the agent loop."""

    effective_context: Dict[str, Any]
    stock_scope: Optional[StockScope]


def _normalize_stock_code(value: Any, registry: Optional[Any] = None) -> str:
    """Normalize a code, preserving exact registered index canonicals."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    try:
        from src.agent.tools.execution import _normalize_tool_stock_code

        normalized = _normalize_tool_stock_code(text, registry)
    except Exception:
        normalized = text.strip().upper()
    return normalized if isinstance(normalized, str) else str(normalized)


def _is_denied_candidate(candidate: str, text: str = "") -> bool:
    token = candidate.strip().upper()
    if token in _EXCHANGE_TOKEN_CANDIDATES:
        return True
    if token in _CONTEXTUAL_INDICATOR_TOKENS and _INDICATOR_CONTEXT_PATTERN.search(text or ""):
        return True
    try:
        from src.agent.orchestrator import _COMMON_WORDS

        return token in _COMMON_WORDS
    except Exception:
        return False


def _append_candidate(
    candidates: List[str],
    candidate: str,
    text: str = "",
    registry: Optional[Any] = None,
) -> None:
    normalized = _normalize_stock_code(candidate, registry)
    if not normalized or _is_denied_candidate(normalized, text):
        return
    if normalized not in candidates:
        candidates.append(normalized)


def extract_stock_codes(text: str, registry: Optional[Any] = None) -> List[str]:
    """Extract candidates; no registry preserves the legacy stock-only path."""
    if not text:
        return []

    candidates: List[str] = []
    index_spans: List[tuple[int, int]] = []
    if registry is not None:
        index_spans, canonicals = _extract_index_canonical_tokens(text, registry)
        for canonical in canonicals:
            if canonical not in candidates:
                candidates.append(canonical)

    for pattern, flags in (
        (r"(?<![a-zA-Z])(?:SH|SZ|BJ)\d{6}(?!\d)", re.IGNORECASE),
        (r"(?<![a-zA-Z])hk\d{4,5}(?!\d)", re.IGNORECASE),
        (r"(?<![a-zA-Z])\d{1,5}\.HK(?![a-zA-Z])", re.IGNORECASE),
        (r"(?<!\d)(?:[03648]\d{5}|92\d{4})(?!\d)", 0),
        (r"(?<!\d)\d{5}(?!\d)", 0),
        (r"(?<![a-zA-Z.])([A-Z]{2,5}(?:\.[A-Z]{1,2})?)(?![a-zA-Z0-9])", 0),
    ):
        for match in re.finditer(pattern, text, flags):
            start, end = match.span()
            if registry is not None and not _has_ascii_token_boundaries(text, start, end):
                continue
            if _is_inside_index_span(start, end, index_spans):
                continue
            raw = match.group(1) if match.lastindex else match.group(0)
            _append_candidate(candidates, raw, text, registry)

    if (
        _SWITCH_PATTERN.search(text)
        or _STRONG_COMPARE_PATTERN.search(text)
        or _WEAK_COMPARE_HINT_PATTERN.search(text)
        or _CHOICE_COMPARE_PATTERN.search(text)
    ):
        for match in _LOWERCASE_TICKER_PATTERN.finditer(text):
            start, end = match.span(1)
            if registry is not None and not _has_ascii_token_boundaries(text, start, end):
                continue
            if _is_inside_index_span(start, end, index_spans):
                continue
            _append_candidate(candidates, match.group(1), text, registry)

    return candidates


# --- Chinese stock names in free text -------------------------------------
# extract_stock_codes() only matches code-shaped tokens, so a message that
# names a company instead of quoting its code ("看看宁德时代") yields no
# candidates. Scope resolution then cannot tell an explicit request for another
# stock from a continuation of the current one, and pins the session to the
# stock already in context: every tool call for the new stock comes back as
# stock_scope_violation with retriable=False. Names are the common spelling in
# Chinese chat, so match them too.

_CJK_RUN_PATTERN = re.compile(r"[A-Za-z]{0,3}[\u4e00-\u9fff]+[A-Za-z0-9]{0,3}")
_NAME_MIN_LEN = 2
_NAME_MAX_LEN = 6  # longest A-share short name in practice; caps window cost
_NAME_INDEX_TTL_SECONDS = 300

# A few listed companies are named after ordinary Chinese phrases, so plain
# substring matching invents a stock: "哪个更值得买" contains 值得买 (300785).
# In that position the phrase follows a degree adverb, which never precedes a
# company name in a request ("更宁德时代" is not something anyone types), so a
# degree adverb immediately before a match means it is prose. 比 is deliberately
# absent: "比茅台好" genuinely refers to 茅台.
# Scope *switching* no longer depends on this hack (see the corroboration rule
# below), but the compare/extraction path still does: an uncorroborated match is
# still a candidate there, so removing it would put 300785 into the allowed set
# of "AAPL 和 TSLA 哪个更值得买".
_NAME_DEGREE_PREFIXES = frozenset("更最很挺太超蛮颇极")

# A name match may re-pin the session only when the message reads as an explicit
# request for that stock: a switch/analysis verb sits immediately before the
# name, allowing at most the two short function words a request puts between verb
# and object ("分析一下宁德时代", "看看这只比亚迪"), and what follows the name is
# the attribute or question the request is *about*.
#
# Both halves are needed because Chinese company names are also ordinary prose:
#
#   * The verb half alone rejects "祝你步步高升" / "好想你" / "三人行，必有我师" /
#     "这个人很有大智慧" — no request verb precedes the match.
#   * The tail half rejects "帮我看看步步高升的概率", the one trap the verb half
#     cannot see: 步步高 (002251) is the first three characters of the idiom
#     步步高升, and the verb 看看 does sit right in front of it. Prose continues
#     with whatever the sentence needs (升的概率); a request continues with the
#     stock attribute it asks about (的走势). A bounded tail is therefore accepted
#     only when it *opens* with such an attribute or question.
#
# "Ends its CJK run" is the degenerate case of the tail half (nothing follows, or
# only the run's ASCII suffix "万科A"). It is kept as the strict form for the
# first-turn branch, which has no current stock to fall back on and therefore
# must not invent a lock from a phrase like "帮我看看步步高升的概率"; the
# mid-session branch additionally accepts an attribute tail, because there the
# alternative to switching is blocking tool calls for the named stock (see the
# stock_scope_violation comment at the top of this section).
#
# Compare requests are unaffected: they widen the allowed set via
# _is_compare_message() rather than switching the expected code, so their names
# need neither a verb nor a tail ("宁德时代和比亚迪哪个好").
#
# Every quantifier below is bounded on purpose: an unbounded `*` over overlapping
# alternatives is a backtracking hazard on adversarial input. The two windows are
# bounded too, so a match costs O(1) regardless of message length: the verb sits
# a few characters before the name (64 leaves room for a long run of spaces) and
# a request's attribute/question sits a few characters after it (12 covers the
# longest lead-in "可不可以" plus the longest topic "什么情况").
_SWITCH_VERB_WINDOW_CHARS = 64
_SWITCH_TAIL_WINDOW_CHARS = 12
_SWITCH_VERB_FILLERS = r"(?:一下|一看|了解|看看|看|下|这|那|个|只|支|家|的|一){0,2}"
_SWITCH_VERB_ADJACENT_PATTERN = re.compile(
    r"(?:换成|改看|分析|看看|研究|诊断)"
    + _SWITCH_VERB_FILLERS
    + r"\s*$"
)
# Short attributive lead-ins a request may put between the name and its
# attribute ("宁德时代的走势", "宁德时代现在多少钱").
_SWITCH_TAIL_LEADS = (
    r"(?:的|之|现在|目前|近期|最近|近|今天|明天|后续|接下来|未来|一下|"
    r"这个|这只|这支|该|它)"
)
# Interrogative openers: whatever follows is the user's question, so any bounded
# tail is accepted after one of these ("是不是高估了", "为什么会跌").
_SWITCH_TAIL_QUESTION_LEADS = (
    r"(?:是不是|有没有|会不会|能不能|可不可以|值不值得|是否|为什么|怎么会|"
    r"还能|到底|究竟|大概|大约)"
)
# What a *request* asks about. Deliberately a vocabulary rather than a length
# bound: "升的概率" is short too, so length alone cannot separate the two.
_SWITCH_TAIL_TOPICS = (
    r"(?:"
    r"走势|行情|股价|价格|价位|估值|市值|市盈率|市净率|基本面|技术面|"
    r"消息面|资金面|财报|报表|财务|业绩|盈利|营收|利润|分红|股息|"
    r"三季报|一季报|中报|年报|季报|K线|k线|日线|周线|月线|分时|均线|"
    r"指标|成交量|量能|换手|资金|趋势|前景|未来|机会|风险|问题|逻辑|"
    r"原因|支撑|压力|新闻|公告|消息|事件|板块|概念|股票|表现|情况|"
    r"怎么样|怎样|咋样|如何|好不好|行不行|行吗|好吗|能买吗|该买吗|"
    r"能不能买|值得买吗|多少钱|现价|怎么走|什么情况|怎么看|怎么办|"
    r"能买|多少|K|k"
    r")"
)
# Anchored at the start of the tail only: what comes after the attribute is the
# rest of the user's sentence ("的走势怎么样"), so trailing text is allowed. The
# opener is what carries the signal.
_SWITCH_TAIL_PATTERN = re.compile(
    r"^(?:(?:"
    + _SWITCH_TAIL_LEADS
    + r")?"
    + _SWITCH_TAIL_TOPICS
    + r"|"
    + _SWITCH_TAIL_QUESTION_LEADS
    + r")"
)

_name_index_lock = threading.Lock()
# Single-flight for the expensive build. The fast path only needs
# _name_index_lock (a cache read); concurrent cold callers serialize on
# _name_index_build_lock, so exactly one builds and the rest reuse the result it
# publishes. The build itself runs outside _name_index_lock, so it can never
# re-enter it.
_name_index_build_lock = threading.Lock()
_name_index_cache: Optional[Tuple[float, Dict[str, str]]] = None


def _stock_name_index() -> Dict[str, str]:
    """Cached name->code index; empty when the resolver is unavailable."""
    global _name_index_cache
    with _name_index_lock:
        cached = _name_index_cache
        if cached is not None and (time.time() - cached[0]) < _NAME_INDEX_TTL_SECONDS:
            return cached[1]
    # Cold or expired: only one thread pays for the build; the others block
    # here and then observe the freshly published cache.
    with _name_index_build_lock:
        now = time.time()
        with _name_index_lock:
            cached = _name_index_cache
            if cached is not None and (now - cached[0]) < _NAME_INDEX_TTL_SECONDS:
                return cached[1]
        index: Dict[str, str] = {}
        try:
            from src.services.name_to_code_resolver import local_name_to_code_map

            index = local_name_to_code_map() or {}
        except Exception as exc:  # fail open: code matching keeps working
            index = {}
            logger.debug("Stock name index unavailable; name matching disabled: %s", exc)
        with _name_index_lock:
            _name_index_cache = (now, index)
        return index


def _name_matches_in_run(
    run: str, index: Dict[str, str], normalize
) -> List[Tuple[str, int, int]]:
    """Longest-match, non-overlapping lookups inside one CJK run.

    Returns ``(code, start, end)`` triples whose offsets are relative to *run*,
    so callers can reason about the match position (see
    :func:`_corroborated_name_codes`). Longest first plus overlap suppression is
    what keeps short fragments from inventing entities: a 2-character window
    inside a longer name would otherwise produce a wrong code whenever that
    fragment happens to be a real (different) stock name.
    """
    length = len(run)
    taken = [False] * length
    found: List[Tuple[str, int, int]] = []
    for size in range(min(_NAME_MAX_LEN, length), _NAME_MIN_LEN - 1, -1):
        for start in range(0, length - size + 1):
            if any(taken[start:start + size]):
                continue
            if start > 0 and run[start - 1] in _NAME_DEGREE_PREFIXES:
                continue
            code = index.get(normalize(run[start:start + size]))
            if not code:
                continue
            found.append((code, start, start + size))
            for position in range(start, start + size):
                taken[position] = True
    return found


def _corroborated_name_codes(
    text: str, *, allow_attribute_tail: bool = False
) -> Set[str]:
    """Name-matched codes the message names explicitly, not in passing prose.

    A match counts only when both hold:

    * a switch/analysis verb (plus at most a short function word) sits right
      before it (``看看宁德时代`` / ``分析万科A`` / ``分析一下宁德时代``); and
    * what follows it reads as the object of that request: either nothing but the
      run's ASCII suffix (``万科A``), or — with ``allow_attribute_tail`` — a
      bounded tail that opens with the stock attribute or question being asked
      about (``看看宁德时代的走势`` / ``诊断一下宁德时代的问题``).

    ``allow_attribute_tail`` is the mid-session relaxation. It is off for the
    first-turn branch, where a name match alone would fabricate a lock: the
    idiom in ``帮我看看步步高升的概率`` passes the verb half, so only the strict
    "nothing follows the name" test keeps that turn scopeless.
    """
    if not text:
        return set()
    runs = list(_CJK_RUN_PATTERN.finditer(text))
    if not runs:
        return set()
    index = _stock_name_index()
    if not index:
        return set()
    try:
        from src.services.name_to_code_resolver import normalize_stock_name
    except Exception:
        return set()
    corroborated: Set[str] = set()
    for run_match in runs:
        run = run_match.group(0)
        run_start = run_match.start()
        run_end = run_match.end()
        for code, start, end in _name_matches_in_run(run, index, normalize_stock_name):
            if run_start + end != run_end:
                # Mid-session the name may be followed by the attribute the user
                # asks about. The tail is bounded (constant window) and must
                # *start* with that attribute, not with an arbitrary continuation
                # of a longer phrase.
                if not allow_attribute_tail:
                    continue
                tail = run[end:end + _SWITCH_TAIL_WINDOW_CHARS]
                if not _SWITCH_TAIL_PATTERN.match(tail):
                    continue
            name_start = run_start + start
            prefix = text[max(0, name_start - _SWITCH_VERB_WINDOW_CHARS):name_start]
            if not _SWITCH_VERB_ADJACENT_PATTERN.search(prefix):
                continue
            corroborated.add(code)
    return corroborated


def _trusted_switch_candidates(
    text: str,
    candidates: List[str],
    registry: Optional[Any] = None,
    *,
    allow_attribute_tail: bool = False,
) -> Set[str]:
    """Candidates that may *re-pin* the session as a scope switch.

    Explicit codes are always trusted — they are unambiguous. A Chinese-name
    match needs corroboration, because company names are also ordinary prose.
    """
    trusted = set(extract_stock_codes(text, registry))
    trusted.update(
        _corroborated_name_codes(text, allow_attribute_tail=allow_attribute_tail)
    )
    return trusted & set(candidates)


def extract_stock_mentions(text: str, registry: Optional[Any] = None) -> List[str]:
    """Stock codes mentioned in *text*, by code or by Chinese name.

    Wraps :func:`extract_stock_codes` (unchanged, still a pure format check for
    ``web_intent_tokenizer``) with a local name lookup. Purely local: the index
    is a dict built from cached tables, so this stays off the network and cheap
    enough to run on every chat turn.
    """
    candidates = list(extract_stock_codes(text, registry))
    if not text:
        return candidates
    runs = _CJK_RUN_PATTERN.findall(text)
    if not runs:
        # Pure code / Latin input: skip the resolver import entirely, it is the
        # expensive part of this path and cannot help without a Chinese name.
        return candidates
    index = _stock_name_index()
    if not index:
        return candidates
    try:
        from src.services.name_to_code_resolver import normalize_stock_name
    except Exception:
        return candidates
    for run in runs:
        for code, _start, _end in _name_matches_in_run(run, index, normalize_stock_name):
            if code not in candidates:
                candidates.append(code)
    return candidates


def _is_compare_message(
    message: str,
    candidates: List[str],
    current_code: str,
    registry: Optional[Any] = None,
) -> bool:
    if _STRONG_COMPARE_PATTERN.search(message):
        return True
    new_candidates = {code for code in candidates if code != current_code}
    if len(new_candidates) >= 2:
        return True
    if _CHOICE_COMPARE_PATTERN.search(message) and len(candidates) >= 2:
        return True
    if not _WEAK_COMPARE_HINT_PATTERN.search(message):
        return False
    if len(candidates) >= 2:
        return True

    if not new_candidates:
        return False

    for match in _LINKED_COMPARE_PATTERN.finditer(message):
        body_candidates = set(extract_stock_mentions(f"比较 {match.group('body')}", registry))
        if body_candidates & new_candidates:
            return True
    return False


def _with_skills(context: Dict[str, Any], skills: Optional[Iterable[str]]) -> Dict[str, Any]:
    if skills is None:
        return context
    next_context = dict(context)
    next_context["skills"] = list(skills)
    return next_context


def _switch_context(context: Dict[str, Any], stock_code: str) -> Dict[str, Any]:
    next_context = {
        key: value
        for key, value in context.items()
        if key not in SWITCH_CLEANUP_KEYS and key != "allowed_stock_codes"
    }
    next_context["stock_code"] = stock_code
    next_context["stock_name"] = ""
    return next_context


def resolve_stock_scope(
    message: str,
    context: Optional[Dict[str, Any]],
    *,
    skills: Optional[Iterable[str]] = None,
    strict_initial_scope: bool = False,
    registry: Optional[Any] = None,
) -> StockScopeResolution:
    """Resolve one turn with a shared registry, failing open to stock semantics."""
    if registry is None:
        try:
            from src.services.stock_list_parser import default_index_registry

            registry = default_index_registry()
        except Exception:
            registry = None
    if registry is not None and not getattr(registry, "_entries", ()):
        registry = None

    original_context = dict(context or {})
    message_text = message or ""
    current_code = _normalize_stock_code(original_context.get("stock_code"), registry)
    invalid_context_code = bool(current_code and _is_denied_candidate(current_code, message_text))
    original_context.pop("allowed_stock_codes", None)
    if invalid_context_code:
        original_context.pop("stock_code", None)
        original_context.pop("stock_name", None)
        current_code = ""

    if not current_code:
        if invalid_context_code or strict_initial_scope:
            candidates = extract_stock_mentions(message_text, registry)
            trusted = _trusted_switch_candidates(message_text, candidates, registry)
            if len(candidates) >= 2:
                # A message naming two stocks is a comparison regardless of how
                # each name was corroborated.
                allowed = set(candidates)
                expected = ""
            elif len(candidates) == 1 and candidates[0] in trusted:
                allowed = {candidates[0]}
                expected = candidates[0]
            else:
                # An uncorroborated name is ordinary prose, not a target: keep
                # the "no candidates" behaviour rather than fabricating a scope
                # that could lock the session to an unrelated stock.
                allowed = set()
                expected = ""
            if strict_initial_scope and not invalid_context_code and not allowed:
                return StockScopeResolution(
                    effective_context=_with_skills(original_context, skills),
                    stock_scope=None,
                )
            effective_context = dict(original_context)
            mode = "switch" if expected else ("compare" if len(allowed) > 1 else "maintain")
            if expected:
                effective_context["stock_code"] = expected
                effective_context["stock_name"] = ""
            return StockScopeResolution(
                effective_context=_with_skills(effective_context, skills),
                stock_scope=StockScope(
                    expected_stock_code=expected,
                    allowed_stock_codes=allowed,
                    mode=mode,
                ),
            )
        return StockScopeResolution(
            effective_context=_with_skills(original_context, skills),
            stock_scope=None,
        )

    candidates = extract_stock_mentions(message_text, registry)
    new_candidates = [code for code in candidates if code != current_code]
    mode = "maintain"
    effective_context = dict(original_context)
    expected = current_code
    allowed = {current_code}

    if _is_compare_message(message_text, candidates, current_code, registry):
        mode = "compare"
        allowed.update(candidates)
    elif _SWITCH_PATTERN.search(message_text) and len(new_candidates) == 1:
        trusted = _trusted_switch_candidates(
            message_text, candidates, registry, allow_attribute_tail=True
        )
        if new_candidates[0] in trusted:
            mode = "switch"
            expected = new_candidates[0]
            allowed = {expected}
            effective_context = _switch_context(original_context, expected)

    effective_context["stock_code"] = expected if mode == "switch" else current_code
    effective_context = _with_skills(effective_context, skills)

    return StockScopeResolution(
        effective_context=effective_context,
        stock_scope=StockScope(
            expected_stock_code=expected,
            allowed_stock_codes=allowed,
            mode=mode,
        ),
    )
