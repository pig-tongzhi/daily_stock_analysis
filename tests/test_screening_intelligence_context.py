# -*- coding: utf-8 -*-
"""Tests for local intelligence pool injection into the screening L2 context.

Covers Layer A (shared market-level section), Layer B (candidate-level
``intelligence`` provider), the opt-in config flag, and the fail-open contract
(a DB error or an empty pool must degrade to "no intelligence context").
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from sqlalchemy import text

import src.config as app_config_module
from src.config import Config as AppConfig
from src.core.config_registry import get_field_definition
from src.repositories.intelligence_repo import IntelligenceRepository
from src.services.screening import config as screening_config_module
from src.services.screening import intelligence_context as intelligence_module
from src.services.screening import pipeline as screening_pipeline
from src.services.screening.candidate_context import collect_candidate_context
from src.services.screening.config import Config as ScreeningConfig
from src.services.screening.context import build_llm_context
from src.services.screening.intelligence_context import (
    MARKET_SECTION_TITLE,
    MATCH_BASIS_CODE,
    MATCH_BASIS_NAME,
    build_market_intelligence_context,
    fetch_candidate_intelligence_summary,
)
from src.services.screening.models import (
    HardFilterConfig,
    ScreeningConfig as StrategyScreeningConfig,
    Strategy,
)
from src.storage import INTELLIGENCE_ITEM_NULL_SCOPE_VALUE, DatabaseManager

SCREENING_STRATEGIES_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "services" / "screening" / "strategies"
)


class _IntelligencePoolTestCase(unittest.TestCase):
    """Seeds a throwaway intelligence pool per test."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(
            self._temp_dir.name, "screening_intel.db"
        )
        AppConfig._instance = None
        DatabaseManager.reset_instance()
        self.repo = IntelligenceRepository()
        self._url_counter = 0

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        AppConfig._instance = None
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def _seed(
        self,
        *,
        title: str,
        scope_type: str = "market",
        scope_value: str | None = None,
        summary: str = "",
        source_name: str = "NewsNow 财联社",
        published_at: datetime | None = None,
    ) -> None:
        self._url_counter += 1
        saved = self.repo.upsert_items(
            [
                {
                    "source_name": source_name,
                    "source_type": "newsnow",
                    "title": title,
                    "summary": summary,
                    "url": f"https://news.example.com/{self._url_counter}",
                    "source": source_name,
                    "published_at": published_at or (datetime.now() - timedelta(hours=1)),
                    "fetched_at": datetime.now(),
                    "scope_type": scope_type,
                    "scope_value": scope_value,
                    "market": "cn",
                }
            ]
        )
        self.assertEqual(saved, 1)

    @staticmethod
    def _candidate_df() -> pd.DataFrame:
        return pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    def _seed_raw_published_at(
        self,
        *,
        title: str,
        published_at: object,
        scope_type: str = "market",
        scope_value: str | None = None,
        market: str = "cn",
    ) -> None:
        """Insert a row bypassing the ORM so ``published_at`` can be junk TEXT.

        ``upsert_items`` binds through the ``DateTime`` type (which would reject
        or coerce the value), so the only way to reproduce the live pool's
        TEXT-ish timestamps is a raw SQL insert.
        """
        self._url_counter += 1
        db = DatabaseManager.get_instance()
        with db.get_session() as session:
            session.execute(
                text(
                    "INSERT INTO intelligence_items "
                    "(source_name, source_type, title, url, source, published_at, fetched_at, "
                    " scope_type, scope_value, market) "
                    "VALUES (:source_name, 'newsnow', :title, :url, :source, :published_at, "
                    " :fetched_at, :scope_type, :scope_value, :market)"
                ),
                {
                    "source_name": "NewsNow 财联社",
                    "title": title,
                    "url": f"https://raw.example.com/{self._url_counter}",
                    "source": "NewsNow 财联社",
                    "published_at": published_at,
                    "fetched_at": datetime.now(),
                    "scope_type": scope_type,
                    "scope_value": scope_value or INTELLIGENCE_ITEM_NULL_SCOPE_VALUE,
                    "market": market,
                },
            )
            session.commit()


class CandidateIntelligenceProviderTestCase(_IntelligencePoolTestCase):
    """Layer B — candidate-level pool rows."""

    def test_symbol_scoped_rows_are_returned(self) -> None:
        self._seed(
            title="贵州茅台获机构上调评级",
            scope_type="symbol",
            scope_value="600519",
        )

        summary = fetch_candidate_intelligence_summary("600519", "贵州茅台")

        self.assertIn("贵州茅台获机构上调评级", summary)
        self.assertIn("NewsNow 财联社", summary)

    def test_market_scope_row_mentioning_stock_name_is_matched(self) -> None:
        self._seed(title="白酒板块活跃，贵州茅台盘中走强")

        summary = fetch_candidate_intelligence_summary("600519", "贵州茅台")

        self.assertIn("贵州茅台盘中走强", summary)

    def test_market_scope_row_without_stock_name_is_ignored(self) -> None:
        self._seed(title="新能源车销量创单月新高", summary="整车与电池产业链受益。")

        self.assertEqual(fetch_candidate_intelligence_summary("600519", "贵州茅台"), "")

    def test_empty_pool_degrades_to_empty_text(self) -> None:
        self.assertEqual(fetch_candidate_intelligence_summary("600519", "贵州茅台"), "")
        self.assertEqual(build_market_intelligence_context(), "")

    def test_collect_candidate_context_intelligence_provider_returns_rows(self) -> None:
        self._seed(
            title="贵州茅台发布分红方案",
            scope_type="symbol",
            scope_value="600519",
        )
        df = self._candidate_df()

        rows, errors = collect_candidate_context(
            df,
            max_rows=1,
            providers=["intelligence"],
            intelligence_limit=2,
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertIn("贵州茅台发布分红方案", rows[0]["intelligence"])
        self.assertEqual(rows[0]["source_count"], 1)
        self.assertGreater(float(rows[0]["source_weight_score"]), 0.0)
        self.assertIn("本地资讯", rows[0]["context_summary"])

        context = build_llm_context(
            candidate_context_rows=rows,
            candidate_df=df,
            max_chars=2000,
        )
        self.assertIn("本地资讯", context)
        self.assertIn("贵州茅台发布分红方案", context)

    def test_collect_candidate_context_empty_pool_returns_no_rows(self) -> None:
        rows, errors = collect_candidate_context(
            self._candidate_df(),
            max_rows=1,
            providers=["intelligence"],
        )

        self.assertEqual(rows, [])
        self.assertEqual(errors, [])

    def test_legacy_providers_do_not_emit_intelligence(self) -> None:
        self._seed(
            title="贵州茅台发布分红方案",
            scope_type="symbol",
            scope_value="600519",
        )

        with patch(
            "src.services.screening.candidate_context.fetch_stock_news_summary",
            return_value="",
        ):
            rows, _errors = collect_candidate_context(
                self._candidate_df(),
                max_rows=1,
                providers=["news"],
            )

        self.assertEqual(rows, [])

    def test_candidate_summary_is_bounded_by_items_and_chars(self) -> None:
        for index in range(6):
            self._seed(
                title="贵州茅台" + f"第{index}条" + "长" * 200,
                scope_type="market",
            )

        text = fetch_candidate_intelligence_summary(
            "600519", "贵州茅台", limit=2, max_chars=120
        )

        self.assertTrue(text)
        self.assertLessEqual(len(text), 120)

    def test_multi_candidate_collection_uses_the_threaded_path_safely(self) -> None:
        codes = ["600519", "300750", "002594"]
        for code in codes:
            self._seed(title=f"{code} 本地资讯", scope_type="symbol", scope_value=code)
        df = pd.DataFrame([{"code": code, "name": f"名称{code}"} for code in codes])

        rows, errors = collect_candidate_context(
            df,
            max_rows=len(codes),
            providers=["intelligence"],
            intelligence_limit=2,
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(rows), len(codes))
        for row in rows:
            self.assertIn(row["code"], row["intelligence"])

    def test_market_context_is_bounded_by_items_and_chars(self) -> None:
        for index in range(10):
            self._seed(
                title=f"市场资讯标题{index}" + "长" * 40,
                summary="摘要内容" * 10,
            )

        uncapped = build_market_intelligence_context(limit=3, max_chars=4000)
        self.assertTrue(uncapped.startswith(MARKET_SECTION_TITLE))
        bullets = [line for line in uncapped.splitlines() if line.startswith("- ")]
        self.assertEqual(len(bullets), 3)

        capped = build_market_intelligence_context(limit=3, max_chars=200)
        self.assertLessEqual(len(capped), 200)
        self.assertTrue(capped.startswith(MARKET_SECTION_TITLE))
        self.assertLessEqual(
            len([line for line in capped.splitlines() if line.startswith("- ")]), 3
        )

    def test_recency_window_excludes_old_rows(self) -> None:
        self._seed(
            title="贵州茅台陈年旧闻",
            scope_type="market",
            published_at=datetime.now() - timedelta(days=90),
        )

        self.assertEqual(
            fetch_candidate_intelligence_summary("600519", "贵州茅台", days=7), ""
        )

    def test_fullwidth_code_is_normalised_on_the_query_side(self) -> None:
        self._seed(
            title="贵州茅台获机构上调评级",
            scope_type="symbol",
            scope_value="600519",
        )

        summary = fetch_candidate_intelligence_summary("６００５１９", "贵州茅台")

        self.assertIn("贵州茅台获机构上调评级", summary)

    def test_name_shorter_than_three_chars_is_not_matched(self) -> None:
        self._seed(title="茅台镇酒企走访调研")

        self.assertEqual(fetch_candidate_intelligence_summary("", "茅台"), "")
        self.assertEqual(self.repo.list_candidate_items(name="茅台", days=7), [])

    def test_like_metacharacters_in_name_are_escaped(self) -> None:
        self._seed(title="普通市场资讯一")
        self._seed(title="普通市场资讯二")

        # Unescaped, "%%%" becomes "%%%%%" and matches every row; "a_c" would
        # match any three-character window.
        self.assertEqual(self.repo.list_candidate_items(name="%%%", days=7), [])
        self.assertEqual(self.repo.list_candidate_items(name="a_c", days=7), [])
        self.assertEqual(fetch_candidate_intelligence_summary("", "%%%"), "")

    def test_code_matching_is_equality_not_a_wildcard_pattern(self) -> None:
        self._seed(title="普通市场资讯", scope_type="symbol", scope_value="600519")

        # "%" and "_" must not behave as wildcards against scope_value either.
        self.assertEqual(self.repo.list_candidate_items(code="%", days=7), [])
        self.assertEqual(self.repo.list_candidate_items(code="_", days=7), [])

    def test_match_basis_is_surfaced_for_code_and_name_matches(self) -> None:
        self._seed(
            title="贵州茅台获机构上调评级",
            scope_type="symbol",
            scope_value="600519",
        )
        self._seed(title="白酒板块活跃，贵州茅台盘中走强")

        summary = fetch_candidate_intelligence_summary("600519", "贵州茅台", limit=5)

        self.assertIn(MATCH_BASIS_CODE, summary)
        self.assertIn(MATCH_BASIS_NAME, summary)

    def test_chinese_construction_name_only_false_positive_is_marked(self) -> None:
        """Documented tradeoff: ``中国建筑`` also matches INDUSTRY news.

        A market row about the construction industry mentions the candidate's
        name as a substring but is not about the company. Longest-match /
        overlap suppression (``stock_scope.py``) does not help — there is no
        competing vocabulary — so the mitigation is that the formatted item is
        labelled ``[名称匹配]`` and the LLM can discount it.
        """
        self._seed(title="中国建筑行业新开工面积同比下降，基建投资承压")

        summary = fetch_candidate_intelligence_summary("601668", "中国建筑")

        self.assertIn("中国建筑行业新开工面积同比下降", summary)
        self.assertIn(MATCH_BASIS_NAME, summary)
        self.assertNotIn(MATCH_BASIS_CODE, summary)


class DefensivePublishedAtTestCase(_IntelligencePoolTestCase):
    """Real repository: one junk ``published_at`` must not void the section."""

    _JUNK_VALUES = ("garbage", "", "2026/06/17 09:00")

    def test_market_section_survives_unparseable_published_at(self) -> None:
        self._seed(title="有效市场资讯")
        for index, junk in enumerate(self._JUNK_VALUES):
            self._seed_raw_published_at(title=f"乱码时间市场资讯{index}", published_at=junk)

        # The production read (days=7) and the unbounded read must both return
        # the good row instead of degrading to an empty list.
        self.assertIn("有效市场资讯", build_market_intelligence_context(days=7))
        self.assertIn("有效市场资讯", build_market_intelligence_context(days=None))
        rows = self.repo.list_recent_market_items(days=None, limit=10)
        self.assertIn("有效市场资讯", [row.title for row in rows])
        for index in range(len(self._JUNK_VALUES)):
            self.assertNotIn(f"乱码时间市场资讯{index}", [row.title for row in rows])

    def test_candidate_summary_survives_unparseable_published_at(self) -> None:
        self._seed(
            title="贵州茅台机构评级上调",
            scope_type="symbol",
            scope_value="600519",
        )
        self._seed_raw_published_at(title="贵州茅台乱码时间", published_at="garbage")

        summary = fetch_candidate_intelligence_summary("600519", "贵州茅台", days=7)

        self.assertIn("贵州茅台机构评级上调", summary)
        self.assertNotIn("贵州茅台乱码时间", summary)


class DefensiveTimestampTestCase(_IntelligencePoolTestCase):
    """``published_at`` is TEXT-ish in practice; parsing must never throw."""

    _ODD_VALUES = (
        "not-a-date",
        "",
        None,
        "2026/06/17 09:00",
        "2026-13-45 99:99",
        1781760000000,
        1781760000,
        datetime(2026, 6, 17, 9, 0),
    )

    def test_market_formatting_survives_odd_published_at(self) -> None:
        for value in self._ODD_VALUES:
            row = SimpleNamespace(
                title="标题A",
                source_name="来源A",
                published_at=value,
                fetched_at=None,
                summary="",
            )
            with patch.object(
                IntelligenceRepository, "list_recent_market_items", return_value=[row]
            ):
                text = build_market_intelligence_context()

            self.assertIn("标题A", text, f"value={value!r}")

    def test_candidate_formatting_survives_odd_published_at(self) -> None:
        for value in self._ODD_VALUES:
            row = SimpleNamespace(
                title="标题B",
                source_name="来源B",
                published_at=value,
                fetched_at=None,
                summary="",
            )
            with patch.object(
                IntelligenceRepository, "list_candidate_items", return_value=[row]
            ):
                text = fetch_candidate_intelligence_summary("600519", "贵州茅台")

            self.assertIn("标题B", text, f"value={value!r}")


class IntelligenceContextLayerATestCase(_IntelligencePoolTestCase):
    """Layer A — shared market-level section inside ``build_llm_context``."""

    def test_market_section_is_appended_when_context_is_provided(self) -> None:
        self._seed(title="央行今日开展逆回购操作", summary="流动性保持合理充裕。")

        market_context = build_market_intelligence_context()

        self.assertIn(MARKET_SECTION_TITLE, market_context)
        context = build_llm_context(
            base_context="人工上下文",
            intelligence_context=market_context,
            max_chars=2000,
        )
        self.assertIn(MARKET_SECTION_TITLE, context)
        self.assertIn("央行今日开展逆回购操作", context)

    def test_section_title_is_not_duplicated(self) -> None:
        titled = f"{MARKET_SECTION_TITLE}\n- 标题"

        context = build_llm_context(intelligence_context=titled, max_chars=1000)

        self.assertEqual(context.count(MARKET_SECTION_TITLE), 1)

    def test_omitted_or_empty_intelligence_context_is_todays_behaviour(self) -> None:
        baseline = build_llm_context(base_context="人工上下文", max_chars=500)

        self.assertNotIn(MARKET_SECTION_TITLE, baseline)
        self.assertEqual(
            build_llm_context(
                base_context="人工上下文",
                intelligence_context="",
                max_chars=500,
            ),
            baseline,
        )

    def test_market_section_does_not_outrank_candidate_identity(self) -> None:
        """Trimming must drop the market-level block before candidate identity."""
        candidate_df = pd.DataFrame(
            [{"code": f"600{index:03d}", "name": f"股票{index}"} for index in range(20)]
        )
        intelligence_context = f"{MARKET_SECTION_TITLE}\n" + "\n".join(
            f"- 市场资讯{index}" + "长" * 40 for index in range(10)
        )

        context = build_llm_context(
            candidate_df=candidate_df,
            intelligence_context=intelligence_context,
            max_chars=600,
        )

        # Identity must survive, and the market-level block must be the section
        # the trim budget actually hits.
        self.assertIn("【候选身份】", context)
        self.assertIn("[context_trimmed]:market_intelligence", context)
        self.assertIn("trimmed=market_intelligence", context)
        self.assertNotIn("[context_trimmed]:candidate_identity", context)
        self.assertNotIn("trimmed=candidate_identity", context)


class FailOpenTestCase(_IntelligencePoolTestCase):
    """A DB error must never escape into a screening run."""

    def test_db_error_does_not_raise_out_of_collect_candidate_context(self) -> None:
        df = self._candidate_df()

        with patch.object(
            intelligence_module,
            "IntelligenceRepository",
            side_effect=RuntimeError("db down"),
        ):
            rows, errors = collect_candidate_context(
                df,
                max_rows=1,
                providers=["intelligence"],
            )

        self.assertEqual(rows, [])
        self.assertEqual(errors, [])

    def test_db_error_does_not_raise_out_of_build_llm_context(self) -> None:
        df = self._candidate_df()

        with patch.object(
            intelligence_module,
            "IntelligenceRepository",
            side_effect=RuntimeError("db down"),
        ):
            rows, errors = collect_candidate_context(
                df,
                max_rows=1,
                providers=["intelligence"],
            )
            market_context = build_market_intelligence_context()
            context = build_llm_context(
                base_context="人工上下文",
                candidate_context_rows=rows,
                candidate_df=df,
                intelligence_context=market_context,
                max_chars=1000,
            )

        self.assertEqual(errors, [])
        self.assertEqual(market_context, "")
        self.assertIn("人工上下文", context)
        self.assertNotIn(MARKET_SECTION_TITLE, context)

    def test_missing_table_is_treated_as_empty_pool(self) -> None:
        df = self._candidate_df()

        with patch.object(
            IntelligenceRepository,
            "list_candidate_items",
            side_effect=RuntimeError("no such table: intelligence_items"),
        ), patch.object(
            IntelligenceRepository,
            "list_recent_market_items",
            side_effect=RuntimeError("no such table: intelligence_items"),
        ):
            rows, errors = collect_candidate_context(
                df,
                max_rows=1,
                providers=["intelligence"],
            )
            market_context = build_market_intelligence_context()

        self.assertEqual(rows, [])
        self.assertEqual(errors, [])
        self.assertEqual(market_context, "")


class IntelligenceConfigFlagTestCase(unittest.TestCase):
    """The new flag is opt-in and its bounds are parsed defensively."""

    _KEYS = (
        "SCREENING_INTELLIGENCE_CONTEXT_ENABLED",
        "SCREENING_INTELLIGENCE_CONTEXT_MAX_ITEMS",
        "SCREENING_INTELLIGENCE_CONTEXT_MAX_CHARS",
        "SCREENING_INTELLIGENCE_CONTEXT_DAYS",
        "SCREENING_INTELLIGENCE_CONTEXT_CANDIDATE_LIMIT",
    )

    def tearDown(self) -> None:
        AppConfig._instance = None

    def test_screening_config_defaults_to_disabled(self) -> None:
        with patch.object(screening_config_module, "_load_env_file", lambda: None), patch.dict(
            os.environ, {}, clear=False
        ):
            for key in self._KEYS:
                os.environ.pop(key, None)
            config = ScreeningConfig.from_env()

        self.assertFalse(config.intelligence_context_enabled)
        self.assertEqual(config.intelligence_context_max_items, 6)
        self.assertEqual(config.intelligence_context_max_chars, 800)
        self.assertEqual(config.intelligence_context_days, 7)
        self.assertEqual(config.intelligence_context_candidate_limit, 3)
        self.assertNotIn("intelligence", config.llm_candidate_context_providers)

    def test_screening_config_reads_flag_and_bounds(self) -> None:
        with patch.object(screening_config_module, "_load_env_file", lambda: None), patch.dict(
            os.environ,
            {
                "SCREENING_INTELLIGENCE_CONTEXT_ENABLED": "true",
                "SCREENING_INTELLIGENCE_CONTEXT_MAX_ITEMS": "4",
                "SCREENING_INTELLIGENCE_CONTEXT_MAX_CHARS": "320",
                "SCREENING_INTELLIGENCE_CONTEXT_DAYS": "3",
                "SCREENING_INTELLIGENCE_CONTEXT_CANDIDATE_LIMIT": "2",
            },
            clear=False,
        ):
            config = ScreeningConfig.from_env()

        self.assertTrue(config.intelligence_context_enabled)
        self.assertEqual(config.intelligence_context_max_items, 4)
        self.assertEqual(config.intelligence_context_max_chars, 320)
        self.assertEqual(config.intelligence_context_days, 3)
        self.assertEqual(config.intelligence_context_candidate_limit, 2)

    def test_invalid_numeric_env_falls_back_instead_of_raising(self) -> None:
        with patch.object(screening_config_module, "_load_env_file", lambda: None), patch.dict(
            os.environ,
            {
                "SCREENING_INTELLIGENCE_CONTEXT_MAX_ITEMS": "not-a-number",
                "SCREENING_INTELLIGENCE_CONTEXT_MAX_CHARS": "-5",
            },
            clear=False,
        ):
            config = ScreeningConfig.from_env()

        self.assertEqual(config.intelligence_context_max_items, 6)
        self.assertEqual(config.intelligence_context_max_chars, 80)

    def test_huge_max_chars_is_clamped_in_both_parsers(self) -> None:
        with patch.object(screening_config_module, "_load_env_file", lambda: None), patch.dict(
            os.environ,
            {"SCREENING_INTELLIGENCE_CONTEXT_MAX_CHARS": str(10**9)},
            clear=False,
        ):
            config = ScreeningConfig.from_env()

        self.assertEqual(
            config.intelligence_context_max_chars,
            screening_config_module.MAX_INTELLIGENCE_CONTEXT_MAX_CHARS,
        )
        self.assertEqual(config.intelligence_context_max_chars, 4000)

        with patch.object(app_config_module, "setup_env", lambda: None), patch.dict(
            os.environ,
            {"SCREENING_INTELLIGENCE_CONTEXT_MAX_CHARS": str(10**9)},
            clear=False,
        ):
            AppConfig._instance = None
            app_config = AppConfig._load_from_env()

        self.assertEqual(app_config.screening_intelligence_context_max_chars, 4000)

    def test_main_app_config_defaults_off_and_reads_env(self) -> None:
        with patch.object(app_config_module, "setup_env", lambda: None), patch.dict(
            os.environ, {}, clear=False
        ):
            for key in self._KEYS:
                os.environ.pop(key, None)
            AppConfig._instance = None
            default_config = AppConfig._load_from_env()

        self.assertFalse(default_config.screening_intelligence_context_enabled)
        self.assertEqual(default_config.screening_intelligence_context_max_items, 6)
        self.assertEqual(default_config.screening_intelligence_context_max_chars, 800)
        self.assertEqual(default_config.screening_intelligence_context_days, 7)
        self.assertEqual(default_config.screening_intelligence_context_candidate_limit, 3)

        with patch.object(app_config_module, "setup_env", lambda: None), patch.dict(
            os.environ,
            {"SCREENING_INTELLIGENCE_CONTEXT_ENABLED": "true"},
            clear=False,
        ):
            AppConfig._instance = None
            enabled_config = AppConfig._load_from_env()

        self.assertTrue(enabled_config.screening_intelligence_context_enabled)

    def test_registry_registers_the_switch_like_screening_enabled(self) -> None:
        field = get_field_definition("SCREENING_INTELLIGENCE_CONTEXT_ENABLED")

        self.assertEqual(field["category"], "base")
        self.assertEqual(field["data_type"], "boolean")
        self.assertEqual(field["ui_control"], "switch")
        self.assertEqual(field["default_value"], "false")
        self.assertFalse(field["is_sensitive"])
        self.assertTrue(field["help_key"])
        self.assertTrue(field["examples"])
        self.assertTrue(field["docs"])


def _pipeline_snapshot() -> pd.DataFrame:
    df = pd.DataFrame(
        [
            {
                "code": "600519",
                "name": "贵州茅台",
                "price": 1700.0,
                "change_pct": 1.0,
                "amount": 2.0e9,
            },
            {
                "code": "300750",
                "name": "宁德时代",
                "price": 200.0,
                "change_pct": 0.5,
                "amount": 1.0e9,
            },
        ]
    )
    df.attrs.update({"snapshot_source": "sina", "source_errors": [], "fallback_used": False})
    return df


def _capture_pipeline_llm_context(monkeypatch, *, intelligence_enabled: bool) -> str:
    """Run one stubbed pipeline pass and return the context handed to the ranker."""
    strategy = Strategy(
        name="demo",
        display_name="Demo",
        description="demo",
        screening=StrategyScreeningConfig(
            enabled=True,
            market_scope=["cn"],
            hard_filters=HardFilterConfig(),
            factor_weights={"value": 1.0},
            max_output=2,
        ),
    )
    runtime_config = ScreeningConfig(
        llm_api_key="test-key",
        llm_model="gemini/gemini-2.5-flash",
        strategies_dir=SCREENING_STRATEGIES_DIR,
        risk_enabled=False,
        portfolio_diversity_enabled=False,
        post_analyzers=[],
        intelligence_context_enabled=intelligence_enabled,
        llm_candidate_context_enabled=False,
        llm_candidate_context_cache_enabled=False,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        screening_pipeline, "load_all_strategies", lambda _path: {"demo": strategy}
    )
    monkeypatch.setattr(
        screening_pipeline, "fetch_snapshot_with_fallback", lambda *args, **kwargs: _pipeline_snapshot()
    )
    monkeypatch.setattr(
        screening_pipeline, "apply_hard_filters", lambda df, _filters: df.copy()
    )
    monkeypatch.setattr(
        screening_pipeline,
        "compute_screen_scores",
        lambda df, _screening: df.assign(screen_score=88.0),
    )
    monkeypatch.setattr(screening_pipeline, "apply_dsa_provider_context", lambda picks, _ctx: [])
    monkeypatch.setattr(screening_pipeline, "apply_risk_overlay", lambda picks, **kwargs: (picks, []))
    monkeypatch.setattr(
        screening_pipeline, "apply_portfolio_overlay", lambda picks, **kwargs: (picks, [])
    )
    monkeypatch.setattr(screening_pipeline, "run_post_analyzers", lambda picks, **kwargs: (picks, []))

    def fake_rank(picks, *_args, **kwargs):
        captured["context"] = kwargs.get("context", "")
        return SimpleNamespace(
            picks=picks,
            market_view="",
            selection_logic="",
            portfolio_risk="",
            coverage=1.0,
            errors=[],
            model_used="fake",
            attempted_models=[],
            failure_reason="",
            ranked=True,
        )

    monkeypatch.setattr(screening_pipeline, "rank_candidates_with_metadata", fake_rank)

    screening_pipeline.screen("demo", use_llm=True, config=runtime_config)
    return str(captured.get("context", ""))


def test_pipeline_injects_intelligence_only_when_the_flag_is_enabled(tmp_path, monkeypatch) -> None:
    os.environ["DATABASE_PATH"] = str(tmp_path / "screening_intel.db")
    AppConfig._instance = None
    DatabaseManager.reset_instance()
    try:
        repo = IntelligenceRepository()
        now = datetime.now()
        saved = repo.upsert_items(
            [
                {
                    "source_name": "NewsNow 财联社",
                    "source_type": "newsnow",
                    "title": "央行开展逆回购操作，流动性保持合理充裕",
                    "summary": "公开市场净投放。",
                    "url": "https://news.example.com/pipeline-market",
                    "source": "NewsNow 财联社",
                    "published_at": now - timedelta(hours=1),
                    "fetched_at": now,
                    "scope_type": "market",
                    "scope_value": None,
                    "market": "cn",
                },
                {
                    "source_name": "NewsNow 财联社",
                    "source_type": "newsnow",
                    "title": "贵州茅台获机构上调评级",
                    "summary": "",
                    "url": "https://news.example.com/pipeline-symbol",
                    "source": "NewsNow 财联社",
                    "published_at": now - timedelta(hours=2),
                    "fetched_at": now,
                    "scope_type": "symbol",
                    "scope_value": "600519",
                    "market": "cn",
                },
            ]
        )
        assert saved == 2

        disabled_context = _capture_pipeline_llm_context(monkeypatch, intelligence_enabled=False)
        enabled_context = _capture_pipeline_llm_context(monkeypatch, intelligence_enabled=True)
    finally:
        DatabaseManager.reset_instance()
        AppConfig._instance = None
        os.environ.pop("DATABASE_PATH", None)

    assert MARKET_SECTION_TITLE not in disabled_context
    assert "央行开展逆回购操作" not in disabled_context
    assert "贵州茅台获机构上调评级" not in disabled_context

    assert MARKET_SECTION_TITLE in enabled_context
    assert "央行开展逆回购操作" in enabled_context
    assert "贵州茅台获机构上调评级" in enabled_context


def _capture_pipeline_providers(
    monkeypatch,
    *,
    intelligence_enabled: bool,
    candidate_context_enabled: bool,
    configured_providers: list[str] | None = None,
    explicit_providers: list[str] | None = None,
) -> list[str]:
    """Run one stubbed pipeline pass and return the provider list it used."""
    strategy = Strategy(
        name="demo",
        display_name="Demo",
        description="demo",
        screening=StrategyScreeningConfig(
            enabled=True,
            market_scope=["cn"],
            hard_filters=HardFilterConfig(),
            factor_weights={"value": 1.0},
            max_output=2,
        ),
    )
    runtime_config = ScreeningConfig(
        llm_api_key="test-key",
        llm_model="gemini/gemini-2.5-flash",
        strategies_dir=SCREENING_STRATEGIES_DIR,
        risk_enabled=False,
        portfolio_diversity_enabled=False,
        post_analyzers=[],
        intelligence_context_enabled=intelligence_enabled,
        llm_candidate_context_enabled=candidate_context_enabled,
        llm_candidate_context_providers=(
            ["news", "fund_flow", "announcement", "quote"]
            if configured_providers is None
            else list(configured_providers)
        ),
        llm_candidate_context_cache_enabled=False,
    )
    captured: dict[str, object] = {"providers": None}

    monkeypatch.setattr(
        screening_pipeline, "load_all_strategies", lambda _path: {"demo": strategy}
    )
    monkeypatch.setattr(
        screening_pipeline,
        "fetch_snapshot_with_fallback",
        lambda *args, **kwargs: _pipeline_snapshot(),
    )
    monkeypatch.setattr(
        screening_pipeline, "apply_hard_filters", lambda df, _filters: df.copy()
    )
    monkeypatch.setattr(
        screening_pipeline,
        "compute_screen_scores",
        lambda df, _screening: df.assign(screen_score=88.0),
    )
    monkeypatch.setattr(screening_pipeline, "apply_dsa_provider_context", lambda picks, _ctx: [])
    monkeypatch.setattr(screening_pipeline, "apply_risk_overlay", lambda picks, **kwargs: (picks, []))
    monkeypatch.setattr(
        screening_pipeline, "apply_portfolio_overlay", lambda picks, **kwargs: (picks, [])
    )
    monkeypatch.setattr(screening_pipeline, "run_post_analyzers", lambda picks, **kwargs: (picks, []))
    monkeypatch.setattr(
        screening_pipeline, "build_market_intelligence_context", lambda **kwargs: ""
    )

    def fake_collect(_df, **kwargs):
        captured["providers"] = list(kwargs.get("providers") or [])
        return [], []

    monkeypatch.setattr(screening_pipeline, "collect_candidate_context", fake_collect)
    monkeypatch.setattr(
        screening_pipeline,
        "rank_candidates_with_metadata",
        lambda picks, *args, **kwargs: SimpleNamespace(
            picks=picks,
            market_view="",
            selection_logic="",
            portfolio_risk="",
            coverage=1.0,
            errors=[],
            model_used="fake",
            attempted_models=[],
            failure_reason="",
            ranked=True,
        ),
    )

    screening_pipeline.screen(
        "demo",
        use_llm=True,
        config=runtime_config,
        candidate_context_providers=explicit_providers,
    )
    return list(captured["providers"] or [])


def test_pipeline_honours_explicit_providers_when_candidate_context_disabled(
    monkeypatch,
) -> None:
    """A-3(a): an explicit provider list must not be replaced by ["intelligence"]."""
    providers = _capture_pipeline_providers(
        monkeypatch,
        intelligence_enabled=True,
        candidate_context_enabled=False,
        explicit_providers=["news"],
    )

    assert "news" in providers
    assert "intelligence" in providers


def test_pipeline_master_switch_off_removes_configured_intelligence_provider(
    monkeypatch,
) -> None:
    """A-3(b): the master switch must be able to remove the provider it names."""
    providers = _capture_pipeline_providers(
        monkeypatch,
        intelligence_enabled=False,
        candidate_context_enabled=True,
        configured_providers=["intelligence"],
    )

    assert "intelligence" not in providers


if __name__ == "__main__":
    unittest.main()
