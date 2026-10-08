# -*- coding: utf-8 -*-
"""Repository helpers for persisted market / symbol intelligence."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, delete, desc, func, or_, select
from sqlalchemy.exc import IntegrityError

from src.storage import DatabaseManager, IntelligenceItem, IntelligenceSource, INTELLIGENCE_ITEM_NULL_SCOPE_VALUE

# ``published_at`` is effectively TEXT in the live pool. GLOB (not LIKE) is the
# right shape check for an ISO timestamp: GLOB's single-character wildcard is
# ``?``; ``_`` is a literal underscore, so a LIKE-style ``____-__-__*`` pattern
# would match nothing at all.
_ISO_TIMESTAMP_GLOB = "????-??-??*"
# A 1-2 character stock name is too short to defend against substring
# false positives (e.g. ``中国建筑`` inside ``中国建筑行业``).
_MIN_NAME_MATCH_CHARS = 3
_LIKE_ESCAPE_CHAR = "\\"


class IntelligenceRepository:
    """DB access layer for configurable intelligence sources and items."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def create_source(self, fields: Dict[str, Any]) -> IntelligenceSource:
        with self.db.get_session() as session:
            row = IntelligenceSource(**fields)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get_source(self, source_id: int) -> Optional[IntelligenceSource]:
        with self.db.get_session() as session:
            return session.execute(
                select(IntelligenceSource).where(IntelligenceSource.id == source_id).limit(1)
            ).scalar_one_or_none()

    def get_source_by_name(self, name: str) -> Optional[IntelligenceSource]:
        with self.db.get_session() as session:
            return session.execute(
                select(IntelligenceSource).where(IntelligenceSource.name == name).limit(1)
            ).scalar_one_or_none()

    def update_source_enabled(self, source_id: int, enabled: bool) -> None:
        with self.db.get_session() as session:
            row = session.execute(
                select(IntelligenceSource).where(IntelligenceSource.id == source_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return
            row.enabled = bool(enabled)
            row.updated_at = datetime.now()
            session.commit()

    def list_sources(
        self,
        *,
        enabled: Optional[bool] = None,
        source_type: Optional[str] = None,
        scope_type: Optional[str] = None,
        market: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[IntelligenceSource], int]:
        conditions = []
        if enabled is not None:
            conditions.append(IntelligenceSource.enabled.is_(enabled))
        if source_type:
            conditions.append(IntelligenceSource.source_type == source_type)
        if scope_type:
            conditions.append(IntelligenceSource.scope_type == scope_type)
        if market:
            conditions.append(IntelligenceSource.market == market)
        where_clause = and_(*conditions) if conditions else True
        safe_page = max(1, int(page))
        safe_size = max(1, min(int(page_size), 100))
        with self.db.get_session() as session:
            total = session.execute(
                select(func.count(IntelligenceSource.id)).select_from(IntelligenceSource).where(where_clause)
            ).scalar() or 0
            rows = session.execute(
                select(IntelligenceSource)
                .where(where_clause)
                .order_by(desc(IntelligenceSource.updated_at), desc(IntelligenceSource.id))
                .offset((safe_page - 1) * safe_size)
                .limit(safe_size)
            ).scalars().all()
            return list(rows), int(total)

    def update_source_status(
        self,
        source_id: int,
        *,
        status: str,
        error: Optional[str] = None,
        fetched_at: Optional[datetime] = None,
    ) -> None:
        with self.db.get_session() as session:
            row = session.execute(
                select(IntelligenceSource).where(IntelligenceSource.id == source_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return
            row.last_status = status
            row.last_error = error
            if fetched_at is not None:
                row.last_fetched_at = fetched_at
            row.updated_at = datetime.now()
            session.commit()

    def upsert_items(self, items: Iterable[Dict[str, Any]]) -> int:
        saved = 0
        with self.db.get_session() as session:
            for fields in items:
                url = (fields.get("url") or "").strip()
                title = (fields.get("title") or "").strip()
                if not url or not title:
                    continue
                item_fields = dict(fields)
                scope_value = self._normalize_scope_value(item_fields.get("scope_value"))
                item_fields["scope_value"] = scope_value
                source_id = item_fields.get("source_id")
                conditions = [
                    IntelligenceItem.url == url,
                    IntelligenceItem.source_type == (item_fields.get("source_type") or "rss"),
                    IntelligenceItem.scope_type == (item_fields.get("scope_type") or "market"),
                    IntelligenceItem.market == (item_fields.get("market") or "cn"),
                ]
                if source_id is None:
                    conditions.append(IntelligenceItem.source_id.is_(None))
                    conditions.append(IntelligenceItem.source_name == item_fields.get("source_name"))
                else:
                    conditions.append(IntelligenceItem.source_id == source_id)
                conditions.append(IntelligenceItem.scope_value == scope_value)
                existing = session.execute(
                    select(IntelligenceItem).where(and_(*conditions)).limit(1)
                ).scalar_one_or_none()
                if existing is not None:
                    existing.summary = item_fields.get("summary") or existing.summary
                    existing.source = item_fields.get("source") or existing.source
                    existing.published_at = item_fields.get("published_at") or existing.published_at
                    existing.fetched_at = item_fields.get("fetched_at") or datetime.now()
                    existing.raw_payload = item_fields.get("raw_payload") or existing.raw_payload
                    continue
                try:
                    with session.begin_nested():
                        session.add(IntelligenceItem(**item_fields))
                        session.flush()
                    saved += 1
                except IntegrityError:
                    continue
            session.commit()
        return saved

    def list_items(
        self,
        *,
        scope_type: Optional[str] = None,
        scope_value: Optional[str] = None,
        market: Optional[str] = None,
        query: Optional[str] = None,
        days: Optional[int] = None,
        published_days: Optional[int] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[IntelligenceItem], int]:
        conditions = []
        if scope_type:
            conditions.append(IntelligenceItem.scope_type == scope_type)
        if scope_value:
            normalized_scope = self._normalize_scope_value(scope_value)
            if scope_type == "symbol":
                conditions.append(func.lower(IntelligenceItem.scope_value) == normalized_scope.lower())
            else:
                conditions.append(IntelligenceItem.scope_value == normalized_scope)
        if market:
            conditions.append(IntelligenceItem.market == market)
        if query:
            pattern = f"%{query.strip()}%"
            conditions.append(or_(IntelligenceItem.title.like(pattern), IntelligenceItem.summary.like(pattern)))
        if days is not None:
            conditions.append(IntelligenceItem.fetched_at >= datetime.now() - timedelta(days=max(1, int(days))))
        if published_days is not None:
            published_cutoff = datetime.now() - timedelta(days=max(1, int(published_days)))
            conditions.append(IntelligenceItem.published_at >= published_cutoff)
        where_clause = and_(*conditions) if conditions else True
        safe_page = max(1, int(page))
        safe_size = max(1, min(int(page_size), 100))
        with self.db.get_session() as session:
            total = session.execute(
                select(func.count(IntelligenceItem.id)).select_from(IntelligenceItem).where(where_clause)
            ).scalar() or 0
            rows = session.execute(
                select(IntelligenceItem)
                .where(where_clause)
                .order_by(desc(func.coalesce(IntelligenceItem.published_at, IntelligenceItem.fetched_at)), desc(IntelligenceItem.id))
                .offset((safe_page - 1) * safe_size)
                .limit(safe_size)
            ).scalars().all()
            return list(rows), int(total)

    def list_recent_market_items(
        self,
        *,
        days: Optional[int] = None,
        limit: int = 20,
        market: Optional[str] = None,
    ) -> List[IntelligenceItem]:
        """Read-only: newest market-scope items, for shared LLM background context.

        Narrow companion to :meth:`list_items` for callers that only need a
        bounded, recency-windowed slice and no total count.
        """
        conditions = [IntelligenceItem.scope_type == "market"]
        if market:
            conditions.append(IntelligenceItem.market == market)
        conditions.append(self._parseable_published_at_condition())
        condition = self._recency_condition(days)
        if condition is not None:
            conditions.append(condition)
        with self.db.get_session() as session:
            return list(
                session.execute(
                    select(IntelligenceItem)
                    .where(and_(*conditions))
                    .order_by(
                        desc(func.coalesce(IntelligenceItem.published_at, IntelligenceItem.fetched_at)),
                        desc(IntelligenceItem.id),
                    )
                    .limit(self._bounded_item_limit(limit))
                )
                .scalars()
                .all()
            )

    def list_candidate_items(
        self,
        *,
        code: Optional[str] = None,
        name: Optional[str] = None,
        days: Optional[int] = None,
        limit: int = 20,
        market: Optional[str] = None,
    ) -> List[IntelligenceItem]:
        """Read-only: items tied to one candidate, newest first.

        A candidate matches when either:

        * ``scope_type='symbol'`` and ``scope_value`` equals ``code``, or
        * ``scope_type='market'`` and the stock name appears in ``title`` /
          ``summary`` (the case that catches a candidate mentioned inside
          general market news).
        """
        conditions = []
        if market:
            conditions.append(IntelligenceItem.market == market)
        conditions.append(self._parseable_published_at_condition())
        match_conditions = []
        normalized_code = _normalize_query_text(code)
        if normalized_code:
            match_conditions.append(
                and_(
                    IntelligenceItem.scope_type == "symbol",
                    func.lower(IntelligenceItem.scope_value) == normalized_code.lower(),
                )
            )
        normalized_name = _normalize_query_text(name)
        if len(normalized_name) >= _MIN_NAME_MATCH_CHARS:
            pattern = f"%{_escape_like(normalized_name)}%"
            match_conditions.append(
                and_(
                    IntelligenceItem.scope_type == "market",
                    or_(
                        IntelligenceItem.title.like(pattern, escape=_LIKE_ESCAPE_CHAR),
                        IntelligenceItem.summary.like(pattern, escape=_LIKE_ESCAPE_CHAR),
                    ),
                )
            )
        if not match_conditions:
            return []
        conditions.append(
            match_conditions[0] if len(match_conditions) == 1 else or_(*match_conditions)
        )
        condition = self._recency_condition(days)
        if condition is not None:
            conditions.append(condition)
        with self.db.get_session() as session:
            return list(
                session.execute(
                    select(IntelligenceItem)
                    .where(and_(*conditions))
                    .order_by(
                        desc(func.coalesce(IntelligenceItem.published_at, IntelligenceItem.fetched_at)),
                        desc(IntelligenceItem.id),
                    )
                    .limit(self._bounded_item_limit(limit))
                )
                .scalars()
                .all()
            )

    @staticmethod
    def _recency_condition(days: Optional[int]) -> Optional[Any]:
        """Recency filter over published_at with a fetched_at fallback."""
        if days is None:
            return None
        try:
            window = max(1, int(days))
        except (TypeError, ValueError):
            return None
        cutoff = datetime.now() - timedelta(days=window)
        return func.coalesce(IntelligenceItem.published_at, IntelligenceItem.fetched_at) >= cutoff

    @staticmethod
    def _parseable_published_at_condition() -> Any:
        """Drop ``published_at`` values SQLAlchemy cannot materialise.

        ``published_at`` is stored as TEXT in practice. SQLAlchemy's SQLite
        ``DateTime`` result processor runs ``datetime.fromisoformat`` while
        rows are materialised, so a single junk value (``''``, ``'garbage'``,
        ``'2026/06/17 09:00'``) raises ``ValueError`` for the whole read and
        silently voids the section. Filtering in SQL keeps the parseable rows
        (including ``NULL``, which falls back to ``fetched_at``).

        The GLOB check rejects non-ISO shapes; ``strftime`` returns ``NULL``
        for shape-valid but calendar-invalid values such as ``2026-13-45``.
        SQLite still normalises a few impossible dates (``2026-02-30``), so
        this is defence in depth rather than a proof.
        """
        return or_(
            IntelligenceItem.published_at.is_(None),
            and_(
                IntelligenceItem.published_at.op("GLOB")(_ISO_TIMESTAMP_GLOB),
                func.strftime("%Y-%m-%d %H:%M:%S", IntelligenceItem.published_at).is_not(None),
            ),
        )

    @staticmethod
    def _bounded_item_limit(limit: int) -> int:
        try:
            return max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            return 20

    @staticmethod
    def _normalize_scope_value(value: Any) -> str:
        normalized = str(value or "").strip()
        return normalized or INTELLIGENCE_ITEM_NULL_SCOPE_VALUE

    def apply_retention(self, retention_days: int) -> int:
        cutoff = datetime.now() - timedelta(days=max(1, int(retention_days)))
        with self.db.get_session() as session:
            result = session.execute(delete(IntelligenceItem).where(IntelligenceItem.fetched_at < cutoff))
            session.commit()
            return int(result.rowcount or 0)


def _normalize_query_text(value: Any) -> str:
    """NFKC-fold query-side code/name text (``６００５１９`` -> ``600519``)."""
    try:
        text = unicodedata.normalize("NFKC", str(value or ""))
    except (TypeError, ValueError):
        text = str(value or "")
    return text.strip()


def _escape_like(value: str) -> str:
    """Escape LIKE metacharacters so a name like ``%`` cannot match every row."""
    return (
        value.replace(_LIKE_ESCAPE_CHAR, _LIKE_ESCAPE_CHAR * 2)
        .replace("%", f"{_LIKE_ESCAPE_CHAR}%")
        .replace("_", f"{_LIKE_ESCAPE_CHAR}_")
    )
