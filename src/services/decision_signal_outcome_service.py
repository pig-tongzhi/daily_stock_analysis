# -*- coding: utf-8 -*-
"""DecisionSignal feedback, forward outcome, and stats service."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
import json
import logging
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.config import get_config
from src.core.backtest_engine import BacktestEngine, EvaluationConfig
from src.core.trading_calendar import resolve_historical_daily_bar_date
from src.repositories.decision_signal_outcome_repo import (
    DecisionSignalOutcomeRepository,
    OutcomeStatsRow,
)
from src.repositories.decision_signal_repo import DecisionSignalRepository
from src.repositories.stock_repo import StockRepository
from src.schemas.decision_profile import VALID_DECISION_PROFILES
from src.services.decision_signal_data_quality import normalize_decision_signal_data_quality
from src.services.stock_code_utils import resolve_daily_stock_identity
from src.services.stock_daily_window_resolver import resolve_stock_daily_window
from src.services.decision_signal_service import (
    HORIZONS,
    SIGNAL_STATUSES,
    SOURCE_TYPES,
    DecisionSignalNotFoundError,
    DecisionSignalService,
)
from src.storage import (
    DatabaseManager,
    DecisionSignalFeedbackRecord,
    DecisionSignalOutcomeRecord,
    DecisionSignalRecord,
)
from src.utils.sanitize import sanitize_decision_signal_text


logger = logging.getLogger(__name__)

DECISION_SIGNAL_OUTCOME_ENGINE_VERSION = "decision-signal-v1"
SUPPORTED_OUTCOME_HORIZONS = {
    "1d": 1,
    "3d": 3,
    "5d": 5,
    "10d": 10,
}
DEFAULT_STATS_STATUSES = ("active", "expired", "invalidated", "closed")
OUTCOME_VALUES = frozenset({"hit", "miss", "neutral"})
# 方向性预期 vs 区间预期。区分二者是必需的：区间判断（「观望」）的命中
# 门槛天然更容易达到，如果把它混进方向性命中率，一个从不下方向判断的
# 模型反而会因为"区间说对了"而获得置信度上调 —— 指标会奖励懦弱。
# 因此校准只看方向性样本，区间成绩单独报告。
DIRECTIONAL_EXPECTATIONS = frozenset({"up", "not_down", "not_up"})
RANGE_EXPECTATIONS = frozenset({"flat"})
EVAL_STATUSES = frozenset({"completed", "unable"})
FEEDBACK_VALUES = frozenset({"useful", "not_useful"})
FEEDBACK_SOURCES = frozenset({"web", "api"})
HOLDING_STATES = frozenset({"holding", "empty", "unknown"})
RETRYABLE_UNABLE_REASONS = frozenset({
    "missing_anchor_price",
    "invalid_anchor_price",
    "insufficient_forward_bars",
    "missing_end_close",
    "invalid_end_close",
    # 方向映射会随语义修正而变（例如 watch 从 None 改为 flat），若该原因不可重试，
    # 旧行会被永久判定为死路，无法在映射更新后自愈。这个分支在读取任何行情之前
    # 就返回，所以重试几乎零成本。
    "non_directional_action",
})
BATCH_CANDIDATE_SCAN_PAGE_SIZE = 500
MIN_PROFILE_CALIBRATION_SAMPLE_SIZE = 30
MIN_REVIEW_SAMPLE_SIZE = 10
REVIEW_UPGRADE_HIT_RATE_PCT = 60.0
REVIEW_DOWNGRADE_HIT_RATE_PCT = 40.0
REVIEW_MAX_UNABLE_RATE_PCT = 50.0
REVIEW_WEAK_DATA_QUALITY_LEVELS = frozenset({"low", "poor"})
REVIEW_COMMON_MISS_REASONS_LIMIT = 3
REVIEW_REASON_LABEL_PATTERN = re.compile(r"[a-z0-9][a-z0-9_\-]{0,31}")
REVIEW_OTHER_REASON_LABEL = "other"
PROFILE_SOURCES = frozenset({
    "auto_default",
    "backfill_defaulted",
    "legacy_unknown",
    "user_selected",
})
PROFILE_CALIBRATION_BREAKDOWN_DIMENSIONS = (
    ("decision_profile", ("decision_profile",)),
    ("decision_profile_action", ("decision_profile", "action")),
    ("decision_profile_horizon", ("decision_profile", "horizon")),
    ("decision_profile_market_phase", ("decision_profile", "market_phase")),
    (
        "decision_profile_data_quality_level",
        ("decision_profile", "data_quality_level"),
    ),
    ("profile_source", ("profile_source",)),
)


class DecisionSignalOutcomeService:
    """Business logic for signal outcomes, stats, and feedback."""

    def __init__(
        self,
        *,
        repo: Optional[DecisionSignalOutcomeRepository] = None,
        signal_repo: Optional[DecisionSignalRepository] = None,
        stock_repo: Optional[StockRepository] = None,
        db_manager: Optional[DatabaseManager] = None,
    ):
        self.repo = repo or DecisionSignalOutcomeRepository(db_manager)
        self.signal_repo = signal_repo or DecisionSignalRepository(db_manager)
        self.stock_repo = stock_repo or StockRepository(db_manager)

    def run_outcomes(
        self,
        *,
        signal_id: Optional[int] = None,
        horizons: Optional[List[str]] = None,
        force: bool = False,
        market: Optional[str] = None,
        stock_code: Optional[str] = None,
        action: Optional[str] = None,
        source_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        signal_id_norm = self._optional_positive_int(signal_id, "signal_id")
        market_norm = DecisionSignalService._normalize_optional_market(market)
        action_norm = DecisionSignalService._normalize_optional_action(action)
        source_type_norm = self._normalize_optional_enum(source_type, SOURCE_TYPES, "source_type")
        status_norm = self._normalize_optional_enum(status, SIGNAL_STATUSES, "status")
        stock_codes_norm = DecisionSignalService._stock_filter_codes(stock_code, market=market_norm)
        horizons_norm = self._normalize_horizons(horizons)
        safe_limit = max(1, min(int(limit), 500))

        statuses = [status_norm] if status_norm else None
        if signal_id_norm is None and statuses is None:
            statuses = list(DEFAULT_STATS_STATUSES)

        if signal_id_norm is None and not force:
            signals = self._list_actionable_candidate_signals(
                stock_codes=stock_codes_norm,
                market=market_norm,
                action=action_norm,
                source_type=source_type_norm,
                statuses=statuses,
                requested_horizons=horizons_norm,
                limit=safe_limit,
            )
        else:
            signals = self.repo.list_candidate_signals(
                signal_id=signal_id_norm,
                stock_codes=stock_codes_norm,
                market=market_norm,
                action=action_norm,
                source_type=source_type_norm,
                statuses=statuses,
                limit=safe_limit,
            )
        if signal_id_norm is not None and not signals:
            raise DecisionSignalNotFoundError(f"Decision signal not found: {signal_id_norm}")

        items: List[Dict[str, Any]] = []
        created_count = 0
        updated_count = 0
        skipped_count = 0

        for signal in signals:
            for horizon in self._horizons_for_signal(signal, horizons_norm):
                existing = self.repo.get_outcome(
                    signal_id=signal.id,
                    horizon=horizon,
                    engine_version=DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
                )
                if existing is not None and not force and not self._should_recompute_outcome(existing):
                    skipped_count += 1
                    items.append(self._serialize_outcome(existing))
                    continue

                fields = self._evaluate_signal_horizon(signal, horizon)
                row, created = self.repo.upsert_outcome(fields)
                if created:
                    created_count += 1
                else:
                    updated_count += 1
                items.append(self._serialize_outcome(row))

        return {
            "items": items,
            "evaluated": created_count + updated_count,
            "created": created_count,
            "updated": updated_count,
            "skipped": skipped_count,
            "engine_version": DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
        }

    def _list_actionable_candidate_signals(
        self,
        *,
        stock_codes: Optional[List[str]],
        market: Optional[str],
        action: Optional[str],
        source_type: Optional[str],
        statuses: Optional[List[str]],
        requested_horizons: Optional[List[str]],
        limit: int,
    ) -> List[DecisionSignalRecord]:
        selected: List[DecisionSignalRecord] = []
        selected_ids = set()
        retryable_reserve: List[Tuple[datetime, int, DecisionSignalRecord]] = []
        retryable_ids = set()
        offset = 0

        while len(selected) < limit:
            page = self.repo.list_candidate_signals(
                stock_codes=stock_codes,
                market=market,
                action=action,
                source_type=source_type,
                statuses=statuses,
                offset=offset,
                limit=BATCH_CANDIDATE_SCAN_PAGE_SIZE,
            )
            if not page:
                break

            outcomes = self.repo.list_outcomes_for_signals(
                signal_ids=[int(signal.id) for signal in page],
                engine_version=DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
            )
            outcomes_by_key: Dict[Tuple[int, str], DecisionSignalOutcomeRecord] = {
                (int(row.signal_id), row.horizon): row
                for row in outcomes
            }

            for signal in page:
                actionability, retryable_at = self._candidate_actionability(
                    signal,
                    requested_horizons=requested_horizons,
                    outcomes_by_key=outcomes_by_key,
                )
                signal_id = int(signal.id)
                if actionability == "missing":
                    if signal_id not in selected_ids:
                        selected.append(signal)
                        selected_ids.add(signal_id)
                    if len(selected) >= limit:
                        break
                elif actionability == "retryable" and signal_id not in retryable_ids:
                    retryable_reserve.append((retryable_at, signal_id, signal))
                    retryable_ids.add(signal_id)

            offset += len(page)
            if len(page) < BATCH_CANDIDATE_SCAN_PAGE_SIZE:
                break

        if len(selected) < limit:
            retryable_reserve.sort(key=lambda item: (item[0], item[1]))
            for _retryable_at, signal_id, signal in retryable_reserve:
                signal_id = int(signal.id)
                if signal_id in selected_ids:
                    continue
                selected.append(signal)
                selected_ids.add(signal_id)
                if len(selected) >= limit:
                    break

        return selected

    def _candidate_actionability(
        self,
        signal: DecisionSignalRecord,
        *,
        requested_horizons: Optional[List[str]],
        outcomes_by_key: Dict[Tuple[int, str], DecisionSignalOutcomeRecord],
    ) -> Tuple[Optional[str], Optional[datetime]]:
        retryable_times: List[datetime] = []
        signal_id = int(signal.id)
        for horizon in self._horizons_for_signal(signal, requested_horizons):
            existing = outcomes_by_key.get((signal_id, horizon))
            if existing is None:
                return "missing", None
            if self._should_recompute_outcome(existing):
                retryable_times.append(self._outcome_retryable_sort_time(existing))
        if retryable_times:
            return "retryable", min(retryable_times)
        return None, None

    @staticmethod
    def _outcome_retryable_sort_time(row: DecisionSignalOutcomeRecord) -> datetime:
        return row.updated_at or row.created_at or datetime.min

    @staticmethod
    def _should_recompute_outcome(row: DecisionSignalOutcomeRecord) -> bool:
        return row.eval_status == "unable" and row.unable_reason in RETRYABLE_UNABLE_REASONS

    def list_outcomes(
        self,
        *,
        signal_id: Optional[int] = None,
        horizon: Optional[str] = None,
        engine_version: Optional[str] = None,
        eval_status: Optional[str] = None,
        outcome: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        signal_id_norm = self._optional_positive_int(signal_id, "signal_id")
        horizon_norm = self._normalize_optional_enum(horizon, HORIZONS, "horizon")
        engine_version_norm = str(engine_version or DECISION_SIGNAL_OUTCOME_ENGINE_VERSION).strip()
        eval_status_norm = self._normalize_optional_enum(eval_status, EVAL_STATUSES, "eval_status")
        outcome_norm = self._normalize_optional_enum(outcome, OUTCOME_VALUES, "outcome")
        safe_page = max(1, int(page))
        safe_page_size = max(1, min(int(page_size), 100))
        rows, total = self.repo.list_outcomes(
            signal_id=signal_id_norm,
            horizon=horizon_norm,
            engine_version=engine_version_norm,
            eval_status=eval_status_norm,
            outcome=outcome_norm,
            page=safe_page,
            page_size=safe_page_size,
        )
        return {
            "items": [self._serialize_outcome(row) for row in rows],
            "total": total,
            "page": safe_page,
            "page_size": safe_page_size,
        }

    def list_signal_outcomes(self, signal_id: int) -> Dict[str, Any]:
        signal_id_norm = self._require_existing_signal(signal_id).id
        return self.list_outcomes(
            signal_id=signal_id_norm,
            engine_version=DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
            page=1,
            page_size=100,
        )

    def get_stats(
        self,
        *,
        horizons: Optional[List[str]] = None,
        engine_version: Optional[str] = None,
        statuses: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        engine_version_norm = str(engine_version or DECISION_SIGNAL_OUTCOME_ENGINE_VERSION).strip()
        horizons_norm = self._normalize_horizons(horizons)
        statuses_norm = (
            [self._normalize_enum(item, SIGNAL_STATUSES, "status") for item in statuses]
            if statuses
            else list(DEFAULT_STATS_STATUSES)
        )
        stats_rows = self.repo.list_stats_rows(
            engine_version=engine_version_norm,
            horizons=horizons_norm,
            statuses=statuses_norm,
        )
        rows = [stats_row.outcome for stats_row in stats_rows]
        dimensions = (
            "action",
            "market",
            "market_phase",
            "source_type",
            "source_agent",
            "plan_quality",
            "data_quality_level",
            "holding_state",
        )
        breakdowns = {
            dimension: self._breakdown(rows, dimension)
            for dimension in dimensions
        }
        return {
            **self._aggregate(rows),
            "engine_version": engine_version_norm,
            "horizons": horizons_norm,
            "statuses": statuses_norm,
            "breakdowns": breakdowns,
            "profile_calibration": self._profile_calibration(stats_rows),
        }

    def get_stock_review(
        self,
        stock_code: str,
        *,
        horizon: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate a read-only, low-sensitivity review of signal outcomes for one stock.

        The payload follows the ReviewMemory contract: objective aggregates are
        always returned as-is, while ``confidence_adjustment`` stays ``observe``
        (with an explanatory caution note) whenever the evidence base is weak —
        insufficient completed samples, a high unable rate, or dominantly weak
        data quality.  The review is observational context only and must not be
        used as a buy/sell strength signal.

        The stock filter reuses ``DecisionSignalService._stock_filter_codes`` so
        alias inputs (``00700`` / ``00700.HK`` / ``HK00700``, ``600519.SH``,
        lowercase US tickers) resolve to the same stored canonical identity as
        every other decision-signal entry point.
        """
        code = str(stock_code or "").strip()
        if not code:
            raise ValueError("stock_code must not be empty")
        filter_codes = DecisionSignalService._stock_filter_codes(code) or [code]
        canonical_code = filter_codes[0]
        horizon_norm = self._normalize_optional_enum(
            horizon, frozenset(SUPPORTED_OUTCOME_HORIZONS), "horizon"
        )
        horizons_norm = [horizon_norm] if horizon_norm else sorted(SUPPORTED_OUTCOME_HORIZONS)
        stats_rows = self.repo.list_stats_rows(
            engine_version=DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
            horizons=horizons_norm,
            statuses=list(DEFAULT_STATS_STATUSES),
            stock_codes=filter_codes,
        )
        rows = [stats_row.outcome for stats_row in stats_rows]
        aggregate = self._aggregate(rows)
        if stats_rows:
            # Echo the stored identity of the aggregated data, so alias inputs
            # (e.g. "00700") report the same canonical code as every other
            # decision-signal endpoint (e.g. "HK00700").
            canonical_code = next(
                (row.stock_code for row in stats_rows if row.stock_code),
                canonical_code,
            )

        sample_size = int(aggregate["total"])
        completed = int(aggregate["completed"])
        unable = int(aggregate["unable"])
        unable_rate_pct = round(unable / sample_size * 100, 2) if sample_size else 0.0
        dominant_quality = self._dominant_data_quality_level(rows)

        adjustment = "observe"
        directional_completed = int(aggregate["directional_completed"])
        directional_hit_rate_pct = aggregate["directional_hit_rate_pct"]
        range_completed = int(aggregate["range_completed"])
        if sample_size == 0:
            notes = "no decision-signal outcome data for this stock yet"
        elif directional_completed < MIN_REVIEW_SAMPLE_SIZE:
            # 校准只看方向性样本。区间样本（「观望」）再多也不能支撑置信度
            # 调整 —— 否则一个从不下方向判断的模型会因为"区间说对了"被上调。
            detail = (
                f"insufficient directional sample: {directional_completed} directional "
                f"outcomes (< {MIN_REVIEW_SAMPLE_SIZE})"
            )
            if range_completed:
                detail += (
                    f"; {range_completed} range outcomes recorded separately"
                    f" (hit_rate={aggregate['range_hit_rate_pct']}%)"
                )
            notes = f"{detail}; observation only"
        elif unable_rate_pct > REVIEW_MAX_UNABLE_RATE_PCT:
            notes = (
                f"high unable rate: {unable_rate_pct}% of outcomes could not be "
                "evaluated; observation only"
            )
        elif dominant_quality in REVIEW_WEAK_DATA_QUALITY_LEVELS:
            notes = f"weak data quality (dominant level: {dominant_quality}); observation only"
        else:
            if directional_hit_rate_pct >= REVIEW_UPGRADE_HIT_RATE_PCT:
                adjustment = "upgrade"
            elif directional_hit_rate_pct <= REVIEW_DOWNGRADE_HIT_RATE_PCT:
                adjustment = "downgrade"
            else:
                adjustment = "neutral"
            notes = (
                f"{directional_completed} directional outcomes, "
                f"directional_hit_rate={directional_hit_rate_pct}%; "
                f"{range_completed} range outcomes, range_hit_rate="
                f"{aggregate['range_hit_rate_pct']}%; "
                "observation only, not a trading signal"
            )

        return {
            "stock_code": canonical_code,
            "scope": "stock",
            "sample_size": sample_size,
            "completed": completed,
            "hit_rate_pct": aggregate["hit_rate_pct"],
            "directional_completed": directional_completed,
            "directional_hit_rate_pct": directional_hit_rate_pct,
            "range_completed": range_completed,
            "range_hit_rate_pct": aggregate["range_hit_rate_pct"],
            "avg_return_pct": aggregate["avg_stock_return_pct"],
            "common_miss_reasons": self._common_miss_reasons(rows, aggregate),
            "confidence_adjustment": adjustment,
            "notes": notes,
        }

    @staticmethod
    def _dominant_data_quality_level(rows: List[DecisionSignalOutcomeRecord]) -> str:
        """Return the most frequent data-quality level across outcome rows."""
        if not rows:
            return "unknown"
        counts = Counter(str(getattr(row, "data_quality_level", None) or "unknown") for row in rows)
        return counts.most_common(1)[0][0]

    def _common_miss_reasons(
        self,
        rows: List[DecisionSignalOutcomeRecord],
        aggregate: Dict[str, Any],
    ) -> List[str]:
        """Surface common miss reasons under the low-sensitivity contract.

        Prefers feedback ``reason_code`` values recorded on missed signals;
        falls back to the top ``unable_reason`` labels when no feedback exists.
        Raw feedback notes are never included.
        """
        missed_signal_ids = [
            int(row.signal_id)
            for row in rows
            if row.eval_status == "completed" and row.outcome == "miss"
        ]
        reason_codes = self.repo.list_feedback_reason_codes(signal_ids=missed_signal_ids)
        if reason_codes:
            labels = [self._normalize_reason_label(code) for code in reason_codes]
            return [code for code, _ in Counter(labels).most_common(REVIEW_COMMON_MISS_REASONS_LIMIT)]
        unable_reasons = aggregate.get("unable_reasons") or {}
        return [
            self._normalize_reason_label(reason)
            for reason, _ in sorted(unable_reasons.items(), key=lambda item: (-int(item[1]), str(item[0])))
            [:REVIEW_COMMON_MISS_REASONS_LIMIT]
        ]

    @staticmethod
    def _normalize_reason_label(value: Any) -> str:
        """Constrain a free-text reason code to the low-sensitivity label set.

        Feedback ``reason_code`` is free text, and ReviewMemory payloads are
        consumed by agent prompts (``[Memory: decision-signal review]``), so
        anything that is not a simple slug-shaped label (``stale_news``,
        ``missing_end_close``…) is bucketed as ``other`` instead of being
        passed through verbatim into the prompt surface.
        """
        text = str(value or "").strip()
        if REVIEW_REASON_LABEL_PATTERN.fullmatch(text):
            return text
        return REVIEW_OTHER_REASON_LABEL

    def get_feedback(self, signal_id: int) -> Dict[str, Any]:
        signal = self._require_existing_signal(signal_id)
        row = self.repo.get_feedback(signal_id=signal.id)
        if row is None:
            return {
                "signal_id": signal.id,
                "feedback_value": None,
                "reason_code": None,
                "note": None,
                "source": None,
                "created_at": None,
                "updated_at": None,
            }
        return self._serialize_feedback(row)

    def put_feedback(
        self,
        signal_id: int,
        *,
        feedback_value: str,
        reason_code: Optional[str] = None,
        note: Optional[str] = None,
        source: str = "api",
    ) -> Dict[str, Any]:
        signal = self._require_existing_signal(signal_id)
        fields = {
            "signal_id": signal.id,
            "feedback_value": self._normalize_enum(feedback_value, FEEDBACK_VALUES, "feedback_value"),
            "reason_code": self._optional_public_text(reason_code, "reason_code", max_length=64),
            "note": self._optional_public_text(note, "note", max_length=1000),
            "source": self._normalize_enum(source or "api", FEEDBACK_SOURCES, "source"),
        }
        row = self.repo.upsert_feedback(fields)
        return self._serialize_feedback(row)

    def _evaluate_signal_horizon(self, signal: DecisionSignalRecord, horizon: str) -> Dict[str, Any]:
        base = self._snapshot_fields(signal, horizon)
        direction = self._direction_for_action(signal.action)
        if direction is None:
            return self._unable_fields(base, reason="non_directional_action")

        eval_days = SUPPORTED_OUTCOME_HORIZONS.get(horizon)
        if eval_days is None:
            return self._unable_fields(base, reason="unsupported_horizon", direction_expected=direction)

        anchor_date = self._anchor_date(signal)
        if anchor_date is None:
            return self._unable_fields(base, reason="missing_anchor_date", direction_expected=direction)

        # 逐一遍历候选形态，并在【同一形态内部】取 start + forward。
        # 这是 stock_daily_window_resolver 的既有契约（"Start and forward bars are
        # never combined across code shapes"），skill/backtest 两个循环早已这样做，
        # 只有这里漏了。直接按 signal.stock_code 精确查询会让 000063 永远取不到
        # stock_daily 里以 000063.SZ 存储的行情，而 storage.py 的 AC 4 明确规定
        # 读取路径继续使用 code 列，所以修正放在服务层而不是仓储层。
        identity = resolve_daily_stock_identity(signal.stock_code)
        code_candidates = (
            list(identity.code_candidates) if identity is not None else [signal.stock_code]
        )
        window = resolve_stock_daily_window(
            stock_repo=self.stock_repo,
            code_candidates=code_candidates,
            expected_start_date=anchor_date,
            eval_window_days=eval_days,
        )
        start_bar = window.start_bar if window is not None else None
        start_price = getattr(start_bar, "close", None)
        if start_price is None:
            return self._unable_fields(
                base,
                reason="missing_anchor_price",
                direction_expected=direction,
                anchor_date=anchor_date,
                eval_window_days=eval_days,
            )
        if not self._is_positive_finite(start_price):
            return self._unable_fields(
                base,
                reason="invalid_anchor_price",
                direction_expected=direction,
                anchor_date=anchor_date,
                eval_window_days=eval_days,
                start_price=start_price,
            )

        forward_bars = list(window.forward_bars)
        evaluation = BacktestEngine.evaluate_decision_signal(
            direction_expected=direction,
            anchor_date=anchor_date,
            start_price=float(start_price),
            forward_bars=forward_bars,
            config=EvaluationConfig(
                eval_window_days=eval_days,
                neutral_band_pct=self._neutral_band_pct(),
                engine_version=DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
            ),
        )
        return {
            **base,
            "eval_status": evaluation.get("eval_status"),
            "outcome": evaluation.get("outcome"),
            "direction_expected": direction,
            "direction_correct": evaluation.get("direction_correct"),
            "unable_reason": evaluation.get("unable_reason"),
            "anchor_date": anchor_date,
            "eval_window_days": eval_days,
            "start_price": evaluation.get("start_price", start_price),
            "end_close": evaluation.get("end_close"),
            "max_high": evaluation.get("max_high"),
            "min_low": evaluation.get("min_low"),
            "stock_return_pct": evaluation.get("stock_return_pct"),
        }

    @staticmethod
    def _direction_for_action(action: Optional[str]) -> Optional[str]:
        if action in {"buy", "add"}:
            return "up"
        if action == "hold":
            return "not_down"
        if action in {"reduce", "sell", "avoid"}:
            return "not_up"
        # 「观望」是一条区间预期，不是弃权：权威刻度把 40-59 定义为观望，
        # 且 BacktestEngine.infer_direction_expected("观望") 已经返回 "flat"，
        # docs/full-guide.md 也把「观望/等待/wait」记为「价格在中性带内」。
        # 在此之前 watch 映射为 None，导致最常见的 action 永远无法被评分，
        # 整个验证闭环因此从未闭合。
        #
        # 「alert」仍返回 None —— 它是真正的提醒，没有方向语义。
        if action == "watch":
            return "flat"
        return None

    @staticmethod
    def _neutral_band_pct() -> float:
        """中性带宽度，来自 backtest_neutral_band_pct。

        这里曾硬编码 2.0，而 BacktestService 一直读配置。同一条判断在两个引擎里
        用不同的带宽度会对同一段行情给出不同结论 —— 不一致本身就是缺陷。
        """
        config = get_config()
        raw = getattr(config, "backtest_neutral_band_pct", 2.0)
        try:
            value = abs(float(raw))
        except (TypeError, ValueError):
            return 2.0
        return value if value > 0 else 2.0

    def _snapshot_fields(self, signal: DecisionSignalRecord, horizon: str) -> Dict[str, Any]:
        data_quality_level = self._data_quality_level(signal)
        holding_state = self._holding_state(signal)
        return {
            "signal_id": signal.id,
            "horizon": horizon,
            "engine_version": DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
            "action": signal.action,
            "market": signal.market,
            "market_phase": signal.market_phase,
            "source_type": signal.source_type,
            "source_agent": signal.source_agent,
            "plan_quality": signal.plan_quality,
            "data_quality_level": data_quality_level,
            "holding_state": holding_state,
        }

    @staticmethod
    def _unable_fields(
        base: Dict[str, Any],
        *,
        reason: str,
        direction_expected: Optional[str] = None,
        anchor_date: Optional[date] = None,
        eval_window_days: Optional[int] = None,
        start_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        return {
            **base,
            "eval_status": "unable",
            "outcome": None,
            "direction_expected": direction_expected,
            "direction_correct": None,
            "unable_reason": reason,
            "anchor_date": anchor_date,
            "eval_window_days": eval_window_days,
            "start_price": start_price,
            "end_close": None,
            "max_high": None,
            "min_low": None,
            "stock_return_pct": None,
        }

    def _anchor_date(self, signal: DecisionSignalRecord) -> Optional[date]:
        """The trading day whose close this signal's expectation is measured from.

        优先级（依 resolve_historical_daily_bar_date 的文档，effective_daily_bar_date
        才是主要权威）：

          1. ``market_phase_summary.effective_daily_bar_date`` —— 创建时已解析好的权威值
          2. 用交易日历解析 ``session_date``
          3. 同上解析 ``created_at``

        为什么需要 2/3：全部 19 个历史信号都缺 effective_daily_bar_date，而它们的
        session_date 是 2026-10-02 / 10-05（国庆假期，无 K 线）。直接采用会话日会
        得到 missing_anchor_price 并永久无法评分。交易日历能把假期映射到假期前最后
        一个交易日（2026-09-30），那里有数据。

        解析失败时【保留原值】而不是返回 None —— 宁可维持既有行为，也不要因为新增
        的解析步骤让结果变得更差。
        """
        metadata = self._json_loads(signal.metadata_json)
        session_date: Optional[date] = None
        phase: Optional[str] = None
        if isinstance(metadata, dict):
            summary = metadata.get("market_phase_summary")
            if isinstance(summary, dict):
                effective = self._parse_date(summary.get("effective_daily_bar_date"))
                if effective is not None:
                    return effective
                session_date = self._parse_date(summary.get("session_date"))
                raw_phase = summary.get("phase")
                phase = str(raw_phase).strip().lower() if raw_phase else None

        target = session_date if session_date is not None else self._parse_date(signal.created_at)
        if target is None:
            return None
        resolved = resolve_historical_daily_bar_date(
            signal.market, target, phase or "non_trading"
        )
        return resolved if resolved is not None else target

    def _data_quality_level(self, signal: DecisionSignalRecord) -> str:
        raw_summary = signal.data_quality_summary_json
        if raw_summary and raw_summary.strip():
            try:
                summary = json.loads(raw_summary)
            except json.JSONDecodeError as exc:
                logger.warning("Invalid decision signal sidecar source JSON: %s", exc)
                return "unknown"
            explicit_level = self._explicit_data_quality_level(summary)
            if explicit_level is not None:
                return self._short_label(explicit_level)
        metadata = self._json_loads(signal.metadata_json)
        if isinstance(metadata, dict):
            return normalize_decision_signal_data_quality(metadata.get("data_quality_level"))
        return "unknown"

    @staticmethod
    def _explicit_data_quality_level(value: Any) -> Optional[Any]:
        if isinstance(value, dict):
            for key in ("level", "quality_level"):
                level = value.get(key)
                if level not in (None, ""):
                    return level
            nested = value.get("data_quality")
            if isinstance(nested, dict) and nested.get("level") not in (None, ""):
                return nested.get("level")
            return None
        if isinstance(value, str) and value.strip():
            return value
        return None

    def _holding_state(self, signal: DecisionSignalRecord) -> str:
        metadata = self._json_loads(signal.metadata_json)
        if isinstance(metadata, dict):
            value = str(metadata.get("holding_state") or "").strip().lower()
            if value in HOLDING_STATES:
                return value
        return "unknown"

    @staticmethod
    def _short_label(value: Any) -> str:
        text = str(value or "").strip().lower()
        return text[:24] or "unknown"

    @staticmethod
    def _json_loads(value: Optional[str]) -> Any:
        if not value:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            logger.warning("Invalid decision signal sidecar source JSON: %s", exc)
            return None

    @staticmethod
    def _parse_date(value: Any) -> Optional[date]:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
        return None

    @staticmethod
    def _is_positive_finite(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0

    def _horizons_for_signal(self, signal: DecisionSignalRecord, requested: Optional[List[str]]) -> List[str]:
        if requested:
            return requested
        horizon = str(signal.horizon or "").strip()
        if horizon:
            return [horizon]
        return list(SUPPORTED_OUTCOME_HORIZONS.keys())

    def _require_existing_signal(self, signal_id: int) -> DecisionSignalRecord:
        signal_id_norm = self._optional_positive_int(signal_id, "signal_id")
        row = self.signal_repo.get(signal_id_norm)
        if row is None:
            raise DecisionSignalNotFoundError(f"Decision signal not found: {signal_id_norm}")
        return row

    @staticmethod
    def _optional_positive_int(value: Any, field_name: str) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an integer") from exc
        if number <= 0:
            raise ValueError(f"{field_name} must be positive")
        return number

    @staticmethod
    def _normalize_enum(value: Any, allowed: Iterable[str], field_name: str) -> str:
        text = str(value or "").strip()
        allowed_set = set(allowed)
        if text not in allowed_set:
            allowed_text = ", ".join(sorted(allowed_set))
            raise ValueError(f"{field_name} must be one of {allowed_text}")
        return text

    @classmethod
    def _normalize_optional_enum(cls, value: Any, allowed: Iterable[str], field_name: str) -> Optional[str]:
        if value in (None, ""):
            return None
        return cls._normalize_enum(value, allowed, field_name)

    def _normalize_horizons(self, values: Optional[List[str]]) -> Optional[List[str]]:
        if not values:
            return None
        out: List[str] = []
        for value in values:
            horizon = self._normalize_enum(value, HORIZONS, "horizon")
            if horizon not in out:
                out.append(horizon)
        return out

    @staticmethod
    def _optional_public_text(value: Any, field_name: str, *, max_length: int) -> Optional[str]:
        if value in (None, ""):
            return None
        text = sanitize_decision_signal_text(value)
        if not text:
            return None
        if len(text) > max_length:
            raise ValueError(f"{field_name} must be at most {max_length} characters")
        return text

    @staticmethod
    def _serialize_outcome(row: DecisionSignalOutcomeRecord) -> Dict[str, Any]:
        return {
            "id": row.id,
            "signal_id": row.signal_id,
            "horizon": row.horizon,
            "engine_version": row.engine_version,
            "eval_status": row.eval_status,
            "outcome": row.outcome,
            "direction_expected": row.direction_expected,
            "direction_correct": row.direction_correct,
            "unable_reason": row.unable_reason,
            "anchor_date": row.anchor_date.isoformat() if row.anchor_date else None,
            "eval_window_days": row.eval_window_days,
            "start_price": row.start_price,
            "end_close": row.end_close,
            "max_high": row.max_high,
            "min_low": row.min_low,
            "stock_return_pct": row.stock_return_pct,
            "action": row.action,
            "market": row.market,
            "market_phase": row.market_phase,
            "source_type": row.source_type,
            "source_agent": row.source_agent,
            "plan_quality": row.plan_quality,
            "data_quality_level": row.data_quality_level,
            "holding_state": row.holding_state,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @staticmethod
    def _serialize_feedback(row: DecisionSignalFeedbackRecord) -> Dict[str, Any]:
        return {
            "signal_id": row.signal_id,
            "feedback_value": row.feedback_value,
            "reason_code": row.reason_code,
            "note": row.note,
            "source": row.source,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def _profile_calibration(self, stats_rows: List[OutcomeStatsRow]) -> Dict[str, Any]:
        samples: List[Dict[str, Any]] = []
        for stats_row in stats_rows:
            outcome = stats_row.outcome
            samples.append({
                "outcome": outcome,
                "decision_profile": self._profile_dimension(stats_row.decision_profile),
                "action": str(outcome.action or "unknown"),
                "horizon": str(outcome.horizon or "unknown"),
                "market_phase": str(outcome.market_phase or "unknown"),
                "data_quality_level": str(outcome.data_quality_level or "unknown"),
                "profile_source": self._profile_source(stats_row.metadata_json),
            })
        breakdowns = {
            name: self._profile_calibration_breakdown(samples, dimensions)
            for name, dimensions in PROFILE_CALIBRATION_BREAKDOWN_DIMENSIONS
        }
        return {
            "minimum_completed_sample_size": MIN_PROFILE_CALIBRATION_SAMPLE_SIZE,
            "breakdowns": breakdowns,
        }

    def _profile_calibration_breakdown(
        self,
        samples: List[Dict[str, Any]],
        dimensions: Tuple[str, ...],
    ) -> List[Dict[str, Any]]:
        grouped: Dict[Tuple[str, ...], List[DecisionSignalOutcomeRecord]] = defaultdict(list)
        for sample in samples:
            key = tuple(str(sample.get(dimension) or "unknown") for dimension in dimensions)
            grouped[key].append(sample["outcome"])

        buckets = [
            {
                "dimensions": dict(zip(dimensions, values)),
                **self._profile_calibration_aggregate(rows),
            }
            for values, rows in grouped.items()
        ]
        return sorted(
            buckets,
            key=lambda item: (
                -int(item["total"]),
                tuple(str(item["dimensions"][dimension]) for dimension in dimensions),
            ),
        )

    def _profile_calibration_aggregate(
        self,
        rows: List[DecisionSignalOutcomeRecord],
    ) -> Dict[str, Any]:
        aggregate = self._aggregate(rows)
        sample_sufficient = int(aggregate["completed"]) >= MIN_PROFILE_CALIBRATION_SAMPLE_SIZE
        direction_denominator = int(aggregate["hit"]) + int(aggregate["miss"])
        adverse_excursions = [
            value
            for row in rows
            if (value := self._row_max_adverse_excursion_pct(row)) is not None
        ]
        return {
            "total": aggregate["total"],
            "completed": aggregate["completed"],
            "unable": aggregate["unable"],
            "hit": aggregate["hit"],
            "miss": aggregate["miss"],
            "neutral": aggregate["neutral"],
            "sample_sufficient": sample_sufficient,
            "hit_rate_pct": aggregate["hit_rate_pct"] if sample_sufficient else None,
            "avg_stock_return_pct": aggregate["avg_stock_return_pct"] if sample_sufficient else None,
            "miss_rate_pct": (
                round(int(aggregate["miss"]) / direction_denominator * 100, 2)
                if sample_sufficient and direction_denominator
                else None
            ),
            "unable_rate_pct": (
                round(int(aggregate["unable"]) / int(aggregate["total"]) * 100, 2)
                if sample_sufficient and int(aggregate["total"])
                else None
            ),
            "max_adverse_excursion_pct": (
                round(max(adverse_excursions), 4)
                if sample_sufficient and adverse_excursions
                else None
            ),
        }

    @classmethod
    def _row_max_adverse_excursion_pct(
        cls,
        row: DecisionSignalOutcomeRecord,
    ) -> Optional[float]:
        if not cls._is_positive_finite(row.start_price):
            return None
        start_price = float(row.start_price)
        if row.action in {"buy", "add", "hold", "watch", "alert"}:
            if not cls._is_positive_finite(row.min_low):
                return None
            return max(0.0, (start_price - float(row.min_low)) / start_price * 100)
        if row.action in {"sell", "reduce", "avoid"}:
            if not cls._is_positive_finite(row.max_high):
                return None
            return max(0.0, (float(row.max_high) - start_price) / start_price * 100)
        return None

    @staticmethod
    def _profile_dimension(value: Any) -> str:
        profile = str(value or "").strip().lower()
        return profile if profile in VALID_DECISION_PROFILES else "unknown"

    def _profile_source(self, metadata_json: Optional[str]) -> str:
        metadata = self._json_loads(metadata_json)
        if not isinstance(metadata, dict):
            return "unknown"
        profile_source = str(metadata.get("profile_source") or "").strip().lower()
        return profile_source if profile_source in PROFILE_SOURCES else "unknown"

    def _breakdown(self, rows: List[DecisionSignalOutcomeRecord], dimension: str) -> List[Dict[str, Any]]:
        grouped: Dict[str, List[DecisionSignalOutcomeRecord]] = defaultdict(list)
        for row in rows:
            value = getattr(row, dimension, None)
            key = str(value or "unknown")
            grouped[key].append(row)
        buckets = [
            {
                "dimension": dimension,
                "value": value,
                **self._aggregate(bucket_rows),
            }
            for value, bucket_rows in grouped.items()
        ]
        return sorted(buckets, key=lambda item: (-int(item["total"]), str(item["value"])))

    @staticmethod
    def _aggregate(rows: List[DecisionSignalOutcomeRecord]) -> Dict[str, Any]:
        total = len(rows)
        completed = [row for row in rows if row.eval_status == "completed"]
        unable = [row for row in rows if row.eval_status == "unable"]
        hit = sum(1 for row in completed if row.outcome == "hit")
        miss = sum(1 for row in completed if row.outcome == "miss")
        neutral = sum(1 for row in completed if row.outcome == "neutral")
        denominator = hit + miss
        returns = [
            float(row.stock_return_pct)
            for row in completed
            if row.stock_return_pct is not None
        ]
        unable_reasons = Counter(row.unable_reason or "unknown" for row in unable)

        # 按预期类型分开统计：方向性样本用于校准，区间样本单独报告。
        # 混在一起会让"从不表态"看起来比"敢表态"更准（见 DIRECTIONAL_EXPECTATIONS 注释）。
        directional = [
            row for row in completed
            if (row.direction_expected or "") in DIRECTIONAL_EXPECTATIONS
        ]
        ranged = [
            row for row in completed
            if (row.direction_expected or "") in RANGE_EXPECTATIONS
        ]
        directional_hit = sum(1 for row in directional if row.outcome == "hit")
        directional_miss = sum(1 for row in directional if row.outcome == "miss")
        directional_denominator = directional_hit + directional_miss
        range_hit = sum(1 for row in ranged if row.outcome == "hit")
        range_miss = sum(1 for row in ranged if row.outcome == "miss")
        range_denominator = range_hit + range_miss

        return {
            "total": total,
            "completed": len(completed),
            "unable": len(unable),
            "hit": hit,
            "miss": miss,
            "neutral": neutral,
            "hit_rate_pct": round(hit / denominator * 100, 2) if denominator else None,
            "avg_stock_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
            "unable_reasons": dict(sorted(unable_reasons.items())),
            "directional_completed": directional_denominator,
            "directional_hit": directional_hit,
            "directional_miss": directional_miss,
            "directional_hit_rate_pct": (
                round(directional_hit / directional_denominator * 100, 2)
                if directional_denominator
                else None
            ),
            "range_completed": range_denominator,
            "range_hit": range_hit,
            "range_miss": range_miss,
            "range_hit_rate_pct": (
                round(range_hit / range_denominator * 100, 2)
                if range_denominator
                else None
            ),
        }
