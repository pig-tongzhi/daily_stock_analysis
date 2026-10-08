# -*- coding: utf-8 -*-
# Derived from AlphaSift revision 9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf.
# Licensed under Apache-2.0 and modified for daily_stock_analysis.
"""Read-only bridge from the local intelligence pool into L2 LLM context.

Two injection layers:

* **market level** — recent ``scope_type='market'`` rows shared by every
  candidate (:func:`build_market_intelligence_context`);
* **candidate level** — rows matched by the candidate's code
  (``scope_type='symbol'``) or by its stock name inside market-scope
  ``title``/``summary`` (:func:`fetch_candidate_intelligence_summary`).

Everything here is best-effort and read-only: a DB error, a missing table or an
empty pool degrades to ``""`` / ``[]`` and must never break a screening run.
Nothing in this module decides eligibility; it only supplies LLM research
material for candidates that already passed the deterministic layers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Iterable

from src.repositories.intelligence_repo import IntelligenceRepository
from src.services.screening.constants import MARKET_SECTION_TITLE
from src.services.screening.normalize import normalize_code, safe_text

logger = logging.getLogger(__name__)

__all__ = [
    "MARKET_SECTION_TITLE",
    "DEFAULT_MARKET_MAX_ITEMS",
    "DEFAULT_MARKET_MAX_CHARS",
    "DEFAULT_RECENCY_DAYS",
    "DEFAULT_CANDIDATE_LIMIT",
    "DEFAULT_CANDIDATE_MAX_CHARS",
    "MAX_SECTION_CHARS",
    "build_market_intelligence_context",
    "fetch_candidate_intelligence_summary",
]

DEFAULT_MARKET_MAX_ITEMS = 6
DEFAULT_MARKET_MAX_CHARS = 800
DEFAULT_RECENCY_DAYS = 7
DEFAULT_MARKET_TITLE_CHARS = 90
DEFAULT_MARKET_SUMMARY_CHARS = 60
DEFAULT_CANDIDATE_LIMIT = 3
DEFAULT_CANDIDATE_MAX_CHARS = 520
DEFAULT_CANDIDATE_TITLE_CHARS = 120
# Upper bound for both configured char budgets; the whole LLM context is
# already capped by ``llm_context_max_chars``.
MAX_SECTION_CHARS = 4000

# Match-basis markers surfaced to the LLM. A name-only hit is a substring
# match and may be an unrelated sentence (industry name, competitor news), so
# the ranker is told which basis produced the row.
MATCH_BASIS_CODE = "[代码匹配]"
MATCH_BASIS_NAME = "[名称匹配]"


def build_market_intelligence_context(
    *,
    limit: int = DEFAULT_MARKET_MAX_ITEMS,
    max_chars: int = DEFAULT_MARKET_MAX_CHARS,
    days: int = DEFAULT_RECENCY_DAYS,
    market: str | None = None,
) -> str:
    """Return a bounded, titled market-level block from the local pool.

    Returns ``""`` when the pool is empty, disabled, or unreadable.
    """
    capped_items = _coerce_positive_int(limit, DEFAULT_MARKET_MAX_ITEMS, maximum=100)
    capped_chars = _coerce_positive_int(max_chars, DEFAULT_MARKET_MAX_CHARS, maximum=MAX_SECTION_CHARS)
    rows = _safe_read(
        lambda repo: repo.list_recent_market_items(
            days=days,
            limit=capped_items,
            market=market,
        ),
        fallback=[],
        label="market",
    )
    lines: list[str] = []
    for row in rows:
        try:
            line = _format_market_line(row)
        except Exception as exc:  # noqa: BLE001 - one odd row must not void the section
            logger.warning("Screening intelligence market row skipped: %s", exc)
            continue
        if line:
            lines.append(f"- {line}")
        if len(lines) >= capped_items:
            break
    if not lines:
        return ""
    try:
        return _clip_with_marker("\n".join([MARKET_SECTION_TITLE, *lines]), capped_chars)
    except Exception as exc:  # noqa: BLE001 - formatting must never break a screening run
        logger.warning("Screening intelligence market block assembly failed: %s", exc)
        return ""


def fetch_candidate_intelligence_summary(
    code: str,
    name: str = "",
    *,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
    max_chars: int = DEFAULT_CANDIDATE_MAX_CHARS,
    days: int | None = DEFAULT_RECENCY_DAYS,
    market: str | None = None,
) -> str:
    """Return a short ``time source title`` block for one candidate.

    Mirrors :func:`~src.services.screening.candidate_context.fetch_stock_news_summary`
    in shape (``" | "``-joined items, character-capped) but reads the local
    intelligence pool instead of the network. Every item carries its match
    basis (``[代码匹配]`` / ``[名称匹配]``) so a name-substring hit can be
    discounted. Returns ``""`` on any failure.
    """
    normalized_code = normalize_code(code)
    normalized_name = safe_text(name, max_len=40)
    if not normalized_code and not normalized_name:
        return ""
    capped_items = _coerce_positive_int(limit, DEFAULT_CANDIDATE_LIMIT, maximum=20)
    capped_chars = _coerce_positive_int(max_chars, DEFAULT_CANDIDATE_MAX_CHARS, maximum=MAX_SECTION_CHARS)
    rows = _safe_read(
        lambda repo: repo.list_candidate_items(
            code=normalized_code,
            name=normalized_name,
            days=days,
            limit=capped_items,
            market=market,
        ),
        fallback=[],
        label=f"candidate:{normalized_code or normalized_name}",
    )
    items: list[str] = []
    seen: set[str] = set()
    for row in rows:
        try:
            item = _format_candidate_line(row, matched_code=normalized_code)
        except Exception as exc:  # noqa: BLE001 - one odd row must not void the section
            logger.warning("Screening intelligence candidate row skipped: %s", exc)
            continue
        if not item or item in seen:
            continue
        seen.add(item)
        items.append(item)
        if len(items) >= capped_items:
            break
    if not items:
        return ""
    try:
        return _compact_text(" | ".join(items), max_len=capped_chars)
    except Exception as exc:  # noqa: BLE001 - formatting must never break a screening run
        logger.warning("Screening intelligence candidate block assembly failed: %s", exc)
        return ""


def _format_market_line(row: object) -> str:
    title = _item_text(row, "title", max_len=DEFAULT_MARKET_TITLE_CHARS)
    if not title:
        return ""
    fields = [
        _format_item_time(_item_value(row, "published_at") or _item_value(row, "fetched_at")),
        _source_label(row),
        title,
    ]
    line = " ".join(field for field in fields if field)
    summary = _item_text(row, "summary", max_len=DEFAULT_MARKET_SUMMARY_CHARS)
    if summary and summary != title:
        line = f"{line}｜{summary}"
    return line


def _format_candidate_line(row: object, *, matched_code: str = "") -> str:
    title = _item_text(row, "title", max_len=DEFAULT_CANDIDATE_TITLE_CHARS)
    if not title:
        return ""
    fields = [
        _format_item_time(_item_value(row, "published_at") or _item_value(row, "fetched_at")),
        _source_label(row),
        _match_basis_marker(row, matched_code),
        title,
    ]
    return " ".join(field for field in fields if field)


def _match_basis_marker(row: object, matched_code: str) -> str:
    """``[代码匹配]`` when the row matched on symbol scope, else ``[名称匹配]``."""
    scope_type = _item_text(row, "scope_type", max_len=20).lower()
    scope_value = _item_text(row, "scope_value", max_len=64)
    if scope_type == "symbol" and (
        not matched_code or scope_value.lower() == matched_code.lower()
    ):
        return MATCH_BASIS_CODE
    return MATCH_BASIS_NAME


def _source_label(row: object) -> str:
    return _item_text(row, "source_name", max_len=40) or _item_text(row, "source", max_len=40)


def _format_item_time(value: object) -> str:
    """Best-effort time label; never raises on TEXT-ish stored values."""
    try:
        if value is None or isinstance(value, bool):
            return ""
        if isinstance(value, datetime):
            parsed: datetime | None = value
        elif isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp = timestamp / 1000
            parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(tzinfo=None)
        else:
            parsed = _parse_text_datetime(safe_text(value, max_len=64))
        if parsed is None:
            return ""
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001 - odd stored timestamps must never break screening
        return ""


def _parse_text_datetime(text: str) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y%m%d%H%M%S",
    ):
        try:
            return datetime.strptime(text, fmt)
        except (TypeError, ValueError):
            continue
    return None


def _item_value(row: object, key: str) -> object:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _item_text(row: object, key: str, *, max_len: int) -> str:
    try:
        return safe_text(_item_value(row, key), max_len=max_len)
    except Exception:  # noqa: BLE001 - formatting must never break a screening run
        return ""


def _safe_read(
    reader: Callable[[IntelligenceRepository], Iterable[object]],
    *,
    fallback: list[object],
    label: str,
) -> list[object]:
    """Run a repository read, degrading to ``fallback`` on any failure."""
    try:
        rows = reader(IntelligenceRepository())
    except Exception as exc:  # noqa: BLE001 - intelligence is optional by design
        logger.warning(
            "Screening intelligence pool read failed (fail-open, %s): %s",
            label,
            exc,
        )
        return list(fallback)
    if not rows:
        return []
    return list(rows)


def _clip_with_marker(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    marker = "\n...[intelligence_trimmed]"
    if max_chars <= len(marker) + 8:
        return text[:max_chars].rstrip()
    kept: list[str] = []
    for line in text.splitlines():
        candidate = "\n".join([*kept, line])
        if len(candidate) + len(marker) > max_chars:
            break
        kept.append(line)
    if not kept:
        return text[: max_chars - len(marker)].rstrip() + marker
    return "\n".join(kept).rstrip() + marker


def _compact_text(value: object, *, max_len: int) -> str:
    """Whitespace-collapse and hard-cap ``value``; never exceeds ``max_len``."""
    text = safe_text(value, max_len=max(max_len * 2, 240))
    if not text:
        return ""
    text = " ".join(text.replace("\n", " ").split())
    if len(text) <= max_len:
        return text
    ellipsis = "..."
    cut = text[: max(max_len - len(ellipsis), 0)]
    for delimiter in (" | ", "；", "。", "，", " "):
        idx = cut.rfind(delimiter)
        if idx >= max_len * 0.55:
            return cut[:idx].rstrip() + ellipsis
    return cut.rstrip() + ellipsis


def _coerce_positive_int(value: object, default: int, *, maximum: int | None = None) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    parsed = max(1, parsed)
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed
