# -*- coding: utf-8 -*-
# Derived from AlphaSift revision 9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf.
# Licensed under Apache-2.0 and modified for daily_stock_analysis.
"""Market snapshot fetcher.

Fetches full-market real-time snapshots for screening.
This is separate from single-stock realtime quotes.
"""

import logging
import json
import os
import random
import threading
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.services.screening.source_guard import call_with_timeout, parse_source_timeout_seconds

logger = logging.getLogger(__name__)

_SNAPSHOT_CACHE_VERSION = 1
_DEFAULT_TUSHARE_HTTP_URL = "http://api.waditu.com"

# ── Wind（万得）MCP 行情源 ──────────────────────────────────────────────
# 端点必须用 vserver_stock_data：vserver_wind_tools 上没有行情工具。
_WIND_MCP_URL = "https://mcp.wind.com.cn/vserver_stock_data/mcp/"
# 单批代码数：实测 100 只约 15 秒；200 只会返回"服务暂时不可用"。
_WIND_BATCH_SIZE = 100
# 并发批次：实测 6 并发仍全部成功，总计 600 只 / 18.5 秒。
_WIND_MAX_WORKERS = 6
_WIND_BATCH_TIMEOUT = 180
_WIND_BATCH_RETRIES = 3


class WindQuotaExhausted(RuntimeError):
    """Wind 当日调用额度已用完。

    Wind 用 HTTP 200 + body 里的 code=429 表达这个错误，且明确写着
    "重试无法恢复额度，额度将于次日 0 点重置"。所以遇到它必须立刻放弃
    并把 Wind 标记为"今日不可用"，否则 56 个批次每个重试 3 次、退避
    2/4/8 秒，白白多等十几分钟。
    """


# 进程级：一旦确认配额耗尽，本进程内不再尝试 Wind（服务每天都会重启，
# 跨天自动失效）。value 是记录时的 monotonic 时间，仅用于日志。
_WIND_QUOTA_EXHAUSTED_AT: float | None = None
# 只取这 22 个指标（Wind 单次可返回 34 个，多取会显著变慢）
_WIND_INDEXES = (
    "最新交易日,交易时间,中文简称,最新成交价,前收盘价,今日开盘价,今日最高价,今日最低价,"
    "涨跌幅,涨跌,成交量,成交额,换手率,量比,振幅,"
    "市盈率(TTM),市盈率(LYR),市净率(LF),总市值2,"
    "总市值1,流通市值,总股本,流通份额,上市日期,"
    "52周最高,52周最低,5日涨跌幅,20日涨跌幅,60日涨跌幅,年初至今涨跌幅,"
    "股息率,涨停价,跌停价,交易状态"
)
# Wind 中文字段 → 标准列名
_WIND_FIELD_MAP = {
    "最新交易日": "trade_date",
    "交易时间": "trade_time",
    "中文简称": "name",
    "最新成交价": "price",
    "前收盘价": "prev_close",
    "今日开盘价": "open",
    "今日最高价": "high",
    "今日最低价": "low",
    "涨跌幅": "change_pct",
    "涨跌": "change_amount",
    "成交量": "volume",
    "成交额": "amount",
    "换手率": "turnover_rate",
    "量比": "volume_ratio",
    "振幅": "amplitude",
    "市盈率(TTM)": "pe_ttm",
    "市盈率(LYR)": "pe_lyr",
    "市净率(LF)": "pb_ratio",
    # 口径决定（2026-10-21 实测）：用 总市值2 = 总股本 × A股价。
    # 为什么不用 总市值1：它对不上任何单一股本×价格（工行 28,700 亿，既非
    # 总股本 3,564×8.58=30,580，也非流通 2,696×8.58=23,133），实际是
    # A股按A股价 + H股按H股价的混合值，跨股票不可比。
    # 总市值2 对万科/茅台/平安/工行四只票全部精确等于 总股本×A股价。
    "总市值2": "total_mv",
    "流通市值": "circ_mv",
    "总股本": "total_shares",
    "流通份额": "free_shares",
    "上市日期": "listing_date",
    "52周最高": "high_52w",
    "52周最低": "low_52w",
    "5日涨跌幅": "change_5d",
    "20日涨跌幅": "change_20d",
    "60日涨跌幅": "change_60d",
    "年初至今涨跌幅": "change_ytd",
    "股息率": "dividend_yield",
    "涨停价": "limit_up",
    "跌停价": "limit_down",
    "交易状态": "trade_status",
    "Wind代码": "code",
}
_EM_REQUEST_MIN_INTERVAL_SECONDS = 1.0
_EM_REQUEST_JITTER_SECONDS = 0.3
_SOURCE_HEALTH_FAILURE_THRESHOLD = 3
_SOURCE_HEALTH_COOLDOWN_SECONDS = 5 * 60
_SNAPSHOT_CALL_TIMEOUT_SECONDS = 60.0
_EM_SESSION: requests.Session | None = None
_EM_LAST_REQUEST_AT = 0.0
_EM_LOCK = threading.Lock()
_SOURCE_HEALTH: dict[str, dict[str, object]] = {}
_SOURCE_HEALTH_LOCK = threading.Lock()


def fetch_cn_snapshot(source: str = "efinance") -> pd.DataFrame:
    """Fetch A-share full-market snapshot.

    Returns a DataFrame with columns:
        code, name, price, change_pct, amount, total_mv, circ_mv,
        pe_ratio, pb_ratio, volume_ratio, turnover_rate

    Raises RuntimeError if the source is unavailable.
    """
    if source == "sina":
        return _call_snapshot_wrapper(_fetch_sina, source=source)
    elif source == "efinance":
        return _call_snapshot_wrapper(_fetch_efinance, source=source)
    elif source == "akshare_em":
        return _call_snapshot_wrapper(_fetch_akshare_em, source=source)
    elif source == "em_datacenter":
        return _call_snapshot_wrapper(_fetch_em_datacenter, source=source)
    elif source == "wind":
        return _call_snapshot_wrapper(_fetch_wind, source=source)
    elif source == "tushare":
        return _call_snapshot_wrapper(_fetch_tushare, source=source)
    else:
        raise ValueError(f"Unknown snapshot source: {source}")


def fetch_snapshot_with_fallback(
    sources: list[str],
    *,
    required_columns: list[str] | None = None,
    fallback_snapshot_path: str | Path | None = None,
    fallback_max_age_hours: float | None = None,
    cache_ttl_seconds: float = 0.0,
    market: str = "cn",
) -> pd.DataFrame:
    """Try live sources, optionally falling back to the last-good snapshot."""
    if market == "us":
        return _fetch_us_snapshot_with_fallback(required_columns)

    errors = []
    required = required_columns or []
    if cache_ttl_seconds > 0:
        cached = _read_last_good_snapshot(
            fallback_snapshot_path,
            required_columns=required,
            source_errors=[],
            max_age_hours=cache_ttl_seconds / 3600.0,
            fresh=True,
            requested_snapshot_sources=sources,
        )
        if cached is not None:
            return cached

    for source in sources:
        disabled_reason = _source_disabled_reason(source)
        if disabled_reason:
            errors.append(f"{source}: {disabled_reason}")
            continue
        try:
            df = fetch_cn_snapshot(source)
            if not df.empty:
                missing = _missing_required_columns(df, required)
                if missing:
                    error = f"missing required columns {','.join(missing)}"
                    errors.append(f"{source}: {error}")
                    _record_source_failure(source, error)
                    continue
                df.attrs.setdefault("snapshot_source", source)
                # 补齐行业/概念：Wind 与新浪都没有这两个维度，东财有。
                # 放在"已经拿到行情"之后，且失败不阻塞（行业不参与打分）。
                if source != "em_datacenter":
                    df = _enrich_classification(df, source=source)
                df.attrs["source_errors"] = list(errors)
                df.attrs["fallback_used"] = False
                df.attrs["stale"] = False
                df.attrs["stale_age_hours"] = None
                _write_last_good_snapshot(
                    fallback_snapshot_path,
                    df,
                    source_priority=sources,
                )
                _record_source_success(source, rows=len(df))
                logger.info("Snapshot fetched from %s: %d rows", source, len(df))
                return df
            errors.append(f"{source}: returned empty data")
            _record_source_failure(source, "returned empty data")
        except Exception as e:
            errors.append(f"{source}: {e}")
            _record_source_failure(source, e)
            logger.warning("Snapshot source %s failed: %s", source, e)

    cached = _read_last_good_snapshot(
        fallback_snapshot_path,
        required_columns=required,
        source_errors=errors,
        max_age_hours=fallback_max_age_hours,
    )
    if cached is not None:
        return cached

    raise RuntimeError(f"All snapshot sources failed: {'; '.join(errors)}")


def _fetch_us_snapshot_with_fallback(
    required_columns: list[str] | None = None,
) -> pd.DataFrame:
    """Fetch US equity snapshot via yfinance adapter."""
    from src.services.screening.snapshot_us import fetch_us_snapshot

    df = fetch_us_snapshot()
    missing = _missing_required_columns(df, required_columns or [])
    if missing:
        logger.warning("US snapshot missing columns: %s", ",".join(missing))
    return df


def _missing_required_columns(df: pd.DataFrame, required_columns: list[str]) -> list[str]:
    missing: list[str] = []
    for col in required_columns:
        if col not in df.columns:
            missing.append(col)
            continue
        if df[col].dropna().empty:
            missing.append(col)
    return missing


# 各源需要的时间差一个数量级，必须分开设超时：
#   wind          56 批 / 6 并发实测 156 秒
#   em_datacenter 11 次请求实测 4~16 秒
#   sina          56 次请求约 30~60 秒
# 用同一个 60 秒会把 wind 永远掐死（2026-10-21 实测：wind 每次都被
# "timed out after 60s" 打回，然后静默降级到东财，主源形同虚设）。
_SOURCE_CALL_TIMEOUTS = {
    "wind": 300.0,
    "em_datacenter": 180.0,
    "sina": 180.0,
}


def _call_snapshot_wrapper(fetcher, *, source: str) -> pd.DataFrame:
    return call_with_timeout(
        fetcher,
        timeout_sec=_snapshot_call_timeout_seconds(source),
        label=f"snapshot source {source}",
    )


def _snapshot_call_timeout_seconds(source: str | None = None) -> float | None:
    default = _SOURCE_CALL_TIMEOUTS.get(source or "", _SNAPSHOT_CALL_TIMEOUT_SECONDS)
    return parse_source_timeout_seconds(
        "SCREENING_SNAPSHOT_CALL_TIMEOUT_SEC",
        default=default,
    )


def _source_disabled_reason(source: str) -> str | None:
    now = time.monotonic()
    with _SOURCE_HEALTH_LOCK:
        state = _SOURCE_HEALTH.get(source)
        if not state:
            return None
        disabled_until = float(state.get("disabled_until", 0.0))
        if disabled_until <= now:
            if disabled_until:
                state["disabled_until"] = 0.0
            return None
        return f"temporarily disabled for {disabled_until - now:.1f}s after repeated failures"


def _record_source_success(source: str, *, rows: int | None = None) -> None:
    with _SOURCE_HEALTH_LOCK:
        state = _SOURCE_HEALTH.setdefault(source, {"failures": 0.0, "disabled_until": 0.0})
        successes = float(state.get("successes", 0.0)) + 1.0
        state["successes"] = successes
        state["failures"] = 0.0
        state["disabled_until"] = 0.0
        state["last_success_at"] = time.time()
        if rows is not None:
            state["last_rows"] = float(rows)
            previous_avg = float(state.get("avg_rows", rows))
            state["avg_rows"] = previous_avg + (float(rows) - previous_avg) / successes


def _record_source_failure(source: str, error: object | None = None) -> None:
    now = time.monotonic()
    with _SOURCE_HEALTH_LOCK:
        state = _SOURCE_HEALTH.setdefault(source, {"failures": 0.0, "disabled_until": 0.0})
        failures = float(state.get("failures", 0.0)) + 1.0
        state["failures"] = failures
        state["total_failures"] = float(state.get("total_failures", 0.0)) + 1.0
        state["last_failure_at"] = time.time()
        if error is not None:
            state["last_error"] = " ".join(str(error).split())
        if failures >= _SOURCE_HEALTH_FAILURE_THRESHOLD:
            state["disabled_until"] = now + _SOURCE_HEALTH_COOLDOWN_SECONDS


def snapshot_source_health_snapshot(
    sources: list[str] | tuple[str, ...] | None = None,
) -> dict[str, dict[str, float | bool | str]]:
    """Return in-process snapshot-source health without exposing credentials."""
    now = time.monotonic()
    requested = tuple(sources or tuple(_SOURCE_HEALTH))
    with _SOURCE_HEALTH_LOCK:
        snapshot: dict[str, dict[str, float | bool | str]] = {}
        for source in requested:
            state = dict(_SOURCE_HEALTH.get(source, {}))
            disabled_until = float(state.get("disabled_until", 0.0))
            cooldown_remaining = max(disabled_until - now, 0.0)
            snapshot[source] = {
                "successes": float(state.get("successes", 0.0)),
                "failures": float(state.get("failures", 0.0)),
                "total_failures": float(state.get("total_failures", 0.0)),
                "last_rows": float(state.get("last_rows", 0.0)),
                "avg_rows": float(state.get("avg_rows", 0.0)),
                "disabled": disabled_until > now,
                "cooldown_remaining_seconds": round(cooldown_remaining, 4),
                "last_success_at": float(state.get("last_success_at", 0.0)),
                "last_failure_at": float(state.get("last_failure_at", 0.0)),
                "last_error": str(state.get("last_error", "")),
            }
    return snapshot


def _write_last_good_snapshot(
    path_like: str | Path | None,
    df: pd.DataFrame,
    *,
    source_priority: list[str] | None = None,
) -> None:
    if path_like is None:
        return
    path = Path(path_like)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _SNAPSHOT_CACHE_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "snapshot_source": str(df.attrs.get("snapshot_source", "")),
                "source_priority": [
                    str(source).strip()
                    for source in (source_priority or [])
                    if str(source).strip()
                ],
                "row_count": int(len(df)),
                "columns": list(df.columns),
            },
            "frame": json.loads(
                df.to_json(orient="split", date_format="iso", force_ascii=False)
            ),
        }
        tmp_path = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)
    except Exception as exc:  # noqa: BLE001 - live snapshot should remain usable.
        logger.warning("Failed to write last-good snapshot cache %s: %s", path, exc)


def _read_last_good_snapshot(
    path_like: str | Path | None,
    *,
    required_columns: list[str],
    source_errors: list[str],
    max_age_hours: float | None = None,
    fresh: bool = False,
    requested_snapshot_sources: list[str] | None = None,
) -> pd.DataFrame | None:
    if path_like is None:
        return None
    path = Path(path_like)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != _SNAPSHOT_CACHE_VERSION:
            raise ValueError("unsupported cache version")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("missing cache metadata")
        cached_snapshot_source = str(metadata.get("snapshot_source", "")).strip()
        if fresh and requested_snapshot_sources is not None:
            requested_priority = [
                str(source).strip()
                for source in requested_snapshot_sources
                if str(source).strip()
            ]
            cached_priority_raw = metadata.get("source_priority")
            cached_priority = (
                [str(source).strip() for source in cached_priority_raw if str(source).strip()]
                if isinstance(cached_priority_raw, list)
                else []
            )
            if cached_priority:
                if cached_priority != requested_priority:
                    raise ValueError(
                        "cached snapshot priority "
                        f"{','.join(cached_priority)} does not match requested priority "
                        f"{','.join(requested_priority)}"
                    )
            elif not requested_priority or cached_snapshot_source != requested_priority[0]:
                # Legacy cache entries did not persist the configured chain.
                # Reuse them only when their actual provider is still the
                # current primary source; a new live fetch will upgrade the
                # metadata and allow same-chain fallback reuse afterwards.
                raise ValueError(
                    "legacy cached snapshot source "
                    f"{cached_snapshot_source or '<missing>'} does not match requested primary "
                    f"{requested_priority[0] if requested_priority else '<missing>'}"
                )
        frame = payload.get("frame")
        if not isinstance(frame, dict):
            raise ValueError("missing cached frame")
        columns = frame.get("columns")
        data = frame.get("data")
        if not isinstance(columns, list) or not isinstance(data, list):
            raise ValueError("malformed cached frame")
        stale_age_hours = _cache_stale_age_hours(
            stat.st_mtime,
            created_at=str(payload.get("created_at", "")),
        )
        if max_age_hours is not None and max_age_hours >= 0 and stale_age_hours > max_age_hours:
            raise ValueError(
                f"cache stale_age_hours={stale_age_hours:.4g} exceeds max_age_hours={max_age_hours:.4g}"
            )
        cached = pd.DataFrame(data, columns=columns)
        if cached.empty:
            raise ValueError("cached snapshot is empty")
        missing = _missing_required_columns(cached, required_columns)
        if missing:
            raise ValueError(f"missing required columns {','.join(missing)}")
    except Exception as exc:  # noqa: BLE001 - invalid cache should not mask live errors.
        source_errors.append(f"last_good_cache: {exc}")
        return None

    cached.attrs["snapshot_source"] = "last_good_cache"
    cached.attrs["fallback_used"] = not fresh
    cached.attrs["cache_used"] = True
    cached.attrs["stale"] = not fresh
    cached.attrs["stale_age_hours"] = stale_age_hours
    cached.attrs["source_errors"] = list(source_errors)
    cached.attrs["last_good_snapshot_source"] = str(
        metadata.get("snapshot_source", "")
    )
    if isinstance(metadata, dict):
        cached.attrs["last_good_created_at"] = str(payload.get("created_at", ""))
    if fresh:
        logger.info(
            "Using fresh snapshot cache %s: age_hours=%s rows=%d",
            path,
            stale_age_hours,
            len(cached),
        )
    else:
        logger.warning(
            "Using last-good snapshot cache %s after live source failures: %s",
            path,
            "; ".join(source_errors),
        )
    return cached


def _cache_stale_age_hours(mtime: float, *, created_at: str = "") -> float:
    # _parse_created_at 返回**本地 naive**；fallback 与 now 必须同口径，
    # 否则 naive 减 aware 会抛 TypeError（2026-10-21 统一时间基准时踩过）。
    modified = _parse_created_at(created_at) or datetime.fromtimestamp(mtime)
    age_hours = (datetime.now() - modified).total_seconds() / 3600.0
    return round(max(age_hours, 0.0), 4)


def _parse_created_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed                      # naive 视为本地时间
    return parsed.astimezone().replace(tzinfo=None)   # 带时区 → 换算到本地 naive


# 东财只取分类字段（行业/概念/板块），用于给 Wind/新浪 补齐它们缺的维度。
_EM_CLASSIFICATION_STY = "SECUCODE,SECURITY_CODE,INDUSTRY,CONCEPT,BOARD_NAME"
_EM_CLASSIFICATION_TIMEOUT = 45
# 分类补充是"锦上添花"，绝不能拖住主流程：
#   · 整个步骤硬超时 75 秒（超时就放弃，行情数据已经拿到了）
#   · 单请求 15 秒、最多 12 页（正常 11 页拉完 5,224 只）
#   · 失败只记日志与 source_errors，不影响选股
_EM_ENRICH_HARD_TIMEOUT = 75.0
_EM_ENRICH_PAGE_TIMEOUT = 15
_EM_ENRICH_MAX_PAGES = 12


def _fetch_em_classification() -> dict[str, dict[str, object]]:
    """只取东财的 行业/概念，返回 code -> 分类字典。

    三个实测得来的约束（2026-10-21）：
      · 请求里带上 BOARD_NAME 时，东财每页从 500 条掉到 350 条且后续分页全空，
        所以只要 INDUSTRY + CONCEPT（board_name 在本仓库零引用）。
      · 用"只有分类字段"的精简 sty 时同样会截断，必须带上行情字段一起请求。
      · 这是辅助数据：不重试、单请求 15 秒、最多 12 页，失败就放弃并记日志。
    """
    url = "https://data.eastmoney.com/dataapi/xuangu/list"
    page_size = 500
    page = 1
    merged: dict[str, dict[str, object]] = {}
    while page <= _EM_ENRICH_MAX_PAGES:
        params = {
            "st": "SECURITY_CODE",
            "sr": "1",
            "ps": str(page_size),
            "p": str(page),
            # 必须带行情字段，否则东财会截断（实测只回 348 条且第 2 页为空）
            "sty": (
                "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,NEW_PRICE,"
                "INDUSTRY,CONCEPT"
            ),
            "filter": (
                '(MARKET+in+("上交所主板","深交所主板","深交所创业板",'
                '"上交所科创板","北交所"))'
            ),
            "source": "SELECT_SECURITIES",
            "client": "WEB",
        }
        response = _eastmoney_get_noretry(
            url,
            params=params,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://data.eastmoney.com/xuangu/",
            },
            timeout=_EM_ENRICH_PAGE_TIMEOUT,
        )
        payload = response.json()
        if not payload.get("success"):
            raise RuntimeError(
                f"em classification error: {payload.get('message', 'unknown')}"
            )
        result = payload.get("result") or {}
        items = result.get("data") or []
        for item in items:
            code = str(item.get("SECURITY_CODE") or "").strip()
            if not code:
                continue
            merged[code] = {
                "industry": item.get("INDUSTRY"),
                "concepts": item.get("CONCEPT"),
            }
        total = result.get("count") or 0
        if not items or len(items) < page_size:
            break
        if page * page_size >= total:
            break
        page += 1
    if not merged:
        raise RuntimeError("em classification returned no rows")
    return merged


def _enrich_classification(df: pd.DataFrame, *, source: str) -> pd.DataFrame:
    """给缺分类列的源补上 industry / concepts / board_name。

    fail-open：补不上就用原表（行业只影响喂给 LLM 的上下文，不参与打分）。
    """
    if df is None or df.empty or "code" not in df.columns:
        return df
    has_industry = "industry" in df.columns and df["industry"].notna().any()
    has_concepts = "concepts" in df.columns and df["concepts"].notna().any()
    if has_industry and has_concepts:
        return df
    try:
        # 硬超时：东财慢或挂掉时最多等 75 秒，绝不让选股跟着耗。
        classification = call_with_timeout(
            _fetch_em_classification,
            timeout_sec=_EM_ENRICH_HARD_TIMEOUT,
            label="em classification enrich",
        )
    except Exception as exc:  # noqa: BLE001 - 补不上不影响主流程
        logger.warning("[Snapshot] 东财分类补充失败（%s 源将缺行业/概念）: %s", source, exc)
        try:
            df.attrs["source_errors"] = list(df.attrs.get("source_errors") or []) + [
                f"classification_enrich: {exc}"
            ]
        except Exception:
            pass
        return df
    if not classification:
        return df

    import json as _json

    industries, concepts, boards = [], [], []
    for code in df["code"].astype(str):
        info = classification.get(code.split(".")[0]) or {}
        industries.append(info.get("industry"))
        board = info.get("board_name")
        boards.append(board)
        raw_concepts = info.get("concepts")
        if isinstance(raw_concepts, (list, tuple)):
            concepts.append(_json.dumps(list(raw_concepts), ensure_ascii=False))
        elif raw_concepts:
            concepts.append(str(raw_concepts))
        else:
            concepts.append(None)

    if not has_industry:
        df["industry"] = industries
    else:
        df["industry"] = df["industry"].where(df["industry"].notna(), pd.Series(industries, index=df.index))
    if not has_concepts:
        df["concepts"] = concepts
    else:
        df["concepts"] = df["concepts"].where(df["concepts"].notna(), pd.Series(concepts, index=df.index))
    if "board_name" not in df.columns:
        df["board_name"] = boards

    filled = int(pd.Series(industries).notna().sum())
    logger.info("[Snapshot] 东财分类补充完成：%s 只票获得行业（源=%s）", filled, source)
    df.attrs["classification_enriched"] = True
    return df


def _mark_wind_quota_exhausted() -> None:
    global _WIND_QUOTA_EXHAUSTED_AT
    if _WIND_QUOTA_EXHAUSTED_AT is None:
        _WIND_QUOTA_EXHAUSTED_AT = time.monotonic()
        logger.warning(
            "[Snapshot] Wind 当日额度已耗尽，今天剩余时间不再尝试（次日 0 点重置）"
        )


def _wind_quota_exhausted() -> bool:
    return _WIND_QUOTA_EXHAUSTED_AT is not None


def _wind_token() -> str:
    """读 Wind MCP 密钥。只从环境变量读，绝不硬编码进仓库。"""
    for key in ("WIND_MCP_TOKEN", "WIND_TOKEN", "WIND_API_KEY"):
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def _wind_to_windcode(code: str) -> str:
    """6 位代码 → Wind 代码后缀。已带后缀的原样返回。"""
    text = str(code or "").strip().upper()
    if "." in text:
        return text
    if not text:
        return ""
    if text.startswith("6"):
        return f"{text}.SH"
    if text.startswith(("0", "3")):
        return f"{text}.SZ"
    if text.startswith(("4", "8", "92")):
        return f"{text}.BJ"
    return f"{text}.SZ"


def _wind_parse_sse(text: str) -> dict:
    """解析 MCP 的 SSE 响应，取出 JSON-RPC 结果。

    注意：SSE 规范允许一个事件的多行 data 用换行拼接；服务端返回的大 JSON
    会被拆成多行 `data:`。只取最后一行会把 JSON 截断
    （表现为 "Unterminated string starting at …"）。
    """
    # 正常结果走 SSE（event:/data: 分帧），但错误响应（例如配额耗尽的 429）
    # 会直接回一段纯 JSON —— 先尝试整体解析，避免把 429 误判成"解析失败"。
    stripped = text.strip()
    if stripped.startswith("{") and "data:" not in stripped:
        message = json.loads(stripped)
        if isinstance(message, dict) and ("result" in message or "error" in message):
            return _check_wind_message(message)

    events: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            current.append(line[5:].lstrip())
        elif not line.strip():
            if current:
                events.append(current)
                current = []
    if current:
        events.append(current)
    if not events:
        raise RuntimeError("wind: empty sse response")

    message = None
    for chunk in events:
        # 先试"整事件拼接"（标准做法），再退回逐行
        for candidate in ("\n".join(chunk), *reversed(chunk)):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
                message = parsed
                break
        if message is not None:
            break
    if message is None:
        raise RuntimeError("wind: no json-rpc message in sse response")
    return _check_wind_message(message)


def _check_wind_message(message: dict) -> dict:
    if message.get("error"):
        raise RuntimeError(f"wind rpc error: {message['error']}")
    result = message.get("result") or {}
    if result.get("isError"):
        structured = result.get("structuredContent") or {}
        message = structured.get("message") or result.get("content")
        code = structured.get("code")
        if code == 429 or "当日调用次数已达上限" in str(message or ""):
            raise WindQuotaExhausted(f"wind quota exhausted (code={code}): {message}")
        raise RuntimeError(f"wind tool error (code={code}): {message}")
    content = result.get("content") or []
    if not content:
        return {}
    return json.loads(content[0].get("text") or "{}")


def _wind_call_batch(windcodes: list[str], token: str) -> list[dict]:
    """拉一批（≤100 只）行情，带重试。"""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "get_stock_price_indicators",
            "arguments": {
                "windcode": ",".join(windcodes),
                "indexes": _WIND_INDEXES,
            },
        },
    }
    last_error: Exception | None = None
    for attempt in range(1, _WIND_BATCH_RETRIES + 1):
        try:
            response = requests.post(
                _WIND_MCP_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                timeout=_WIND_BATCH_TIMEOUT,
            )
            response.raise_for_status()
            # Content-Type 是 text/event-stream 且不带 charset，requests 会按
            # ISO-8859-1 解码，导致中文字段名全部变成 mojibake（"最新交易日" 读成
            # "æœ€æ–°äº¤æ˜“æ—¥"），字段映射全部失配。必须显式指定 utf-8。
            response.encoding = "utf-8"
            parsed = _wind_parse_sse(response.text)
            data = (parsed.get("data") or {})
            columns = [c["name"] for c in (data.get("columns") or [])]
            rows = data.get("rows") or []
            out = []
            for values in rows:
                record = dict(zip(columns, values))
                mapped = {}
                for wind_name, std_name in _WIND_FIELD_MAP.items():
                    if wind_name in record:
                        mapped[std_name] = record[wind_name]
                raw_code = str(mapped.get("code") or "").strip()
                if raw_code:
                    # Wind 给的是 600519.SH；全库统一用 6 位代码，
                    # 否则跨源去重/关联全部失配（2026-10-21 冒烟测试踩到）。
                    mapped["code"] = raw_code.split(".")[0].zfill(6) if raw_code.split(".")[0].isdigit() else raw_code
                    mapped["wind_code"] = raw_code
                    out.append(mapped)
            return out
        except WindQuotaExhausted as exc:
            # 配额类错误重试无意义（Wind 自己说的），直接向上抛。
            _mark_wind_quota_exhausted()
            raise
        except Exception as exc:  # noqa: BLE001 - 批级重试
            last_error = exc
            _record_source_failure("wind", f"batch attempt {attempt}: {exc}")
            if attempt < _WIND_BATCH_RETRIES:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"wind batch failed after {_WIND_BATCH_RETRIES} attempts: {last_error}")


def _fetch_wind() -> pd.DataFrame:
    """从 Wind MCP 拉全市场当日行情（并发分批）。

    为什么需要一份"代码清单"：Wind 的行情工具必须显式传 windcode 列表，
    没有"返回全市场"的接口。清单优先取 tushare/东财已经拿到的全市场代码，
    拿不到就退到库里最近一次的代码集合。

    实测性能：100 只/批、6 并发 → 600 只 18.5 秒 → 全市场约 3 分钟。
    """
    from concurrent.futures import ThreadPoolExecutor

    if _wind_quota_exhausted():
        raise WindQuotaExhausted("wind quota already exhausted today")
    token = _wind_token()
    if not token:
        raise RuntimeError("wind requires WIND_MCP_TOKEN")

    codes = _wind_universe_codes()
    if not codes:
        raise RuntimeError("wind: no universe codes available")

    windcodes = [_wind_to_windcode(c) for c in codes]
    windcodes = [c for c in windcodes if c]
    batches = [
        windcodes[i : i + _WIND_BATCH_SIZE]
        for i in range(0, len(windcodes), _WIND_BATCH_SIZE)
    ]

    records: list[dict] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=_WIND_MAX_WORKERS) as executor:
        futures = [executor.submit(_wind_call_batch, batch, token) for batch in batches]
        quota_hit = False
        for future in futures:
            try:
                records.extend(future.result())
            except WindQuotaExhausted:
                # 配额耗尽：其余批次必然同样失败，立刻收手
                quota_hit = True
                break
            except Exception as exc:  # noqa: BLE001 - 单批失败不致命
                errors.append(f"wind batch: {exc}")
        if quota_hit:
            for future in futures:
                future.cancel()
            raise WindQuotaExhausted("wind quota exhausted mid-run")

    if not records:
        raise RuntimeError(f"wind returned no rows (errors={len(errors)})")

    # 完整性闸门：批次部分失败时会把"半份市场"当成功，选股就会在一个残缺
    # 股票池里挑票，而且没有任何告警（2026-10-21 实测：56 批只成功一半，
    # 返回 2,899/5,567 行）。低于期望数量的 95% 一律视为失败，交给东财。
    expected = len(windcodes)
    got = len(records)
    if expected and got < expected * 0.95:
        raise RuntimeError(
            f"wind incomplete: got {got}/{expected} rows "
            f"({got * 100 // expected}%), failed batches={len(errors)}"
        )

    df = pd.DataFrame(records)
    # 数据日：Wind 每条都带「最新交易日」（如 20261020）
    trade_dates = [str(v) for v in df.get("trade_date", pd.Series(dtype=str)).unique().tolist()]
    trade_dates = [d for d in trade_dates if d and d not in ("0", "None", "nan")]
    if trade_dates:
        df.attrs["trade_date"] = max(trade_dates)
    # 单位统一：Wind 的市值/成交额已是"元"
    for col in ("total_mv", "circ_mv", "amount", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if errors:
        df.attrs["source_errors"] = errors
    return _normalize(df, source="wind")


def _wind_universe_codes() -> list[str]:
    """Wind 需要的代码清单：优先库内全市场，其次 config 全市场文件。"""
    try:
        from src.storage import DatabaseManager

        manager = DatabaseManager.get_instance()
        with manager.get_session() as session:
            from sqlalchemy import text as _text

            rows = session.execute(
                _text(
                    "SELECT code FROM market_snapshot "
                    "WHERE as_of=(SELECT MAX(as_of) FROM market_snapshot) "
                    "GROUP BY code"
                )
            ).fetchall()
        codes = [str(r[0]).strip() for r in rows if r and r[0]]
        if codes:
            return codes
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[Snapshot] wind 代码清单取 market_snapshot 失败: {exc}")

    # 兜底 1：stock_daily 里出现过的代码
    try:
        from src.storage import DatabaseManager

        manager = DatabaseManager.get_instance()
        with manager.get_session() as session:
            from sqlalchemy import text as _text

            rows = session.execute(
                _text("SELECT DISTINCT code FROM stock_daily")
            ).fetchall()
        codes = [str(r[0]).strip() for r in rows if r and r[0]]
        if codes:
            return codes
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[Snapshot] wind 代码清单取 stock_daily 失败: {exc}")

    # 兜底 2：回测项目的全市场清单文件
    for candidate in (
        Path(__file__).resolve().parents[3] / "p0项目" / "策略回测-2026H1" / "config" / "universe_full.json",
    ):
        try:
            if candidate.exists():
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                codes = []
                if isinstance(payload, dict):
                    payload = payload.get("codes") or payload.get("symbols") or []
                for item in payload or []:
                    text = item.get("code") if isinstance(item, dict) else item
                    if text:
                        codes.append(str(text).strip().split(".")[0])
                codes = [c for c in codes if c]
                if codes:
                    return codes
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Snapshot] wind 代码清单读 universe_full.json 失败: {exc}")
    return []


def _fetch_efinance() -> pd.DataFrame:
    """Fetch via efinance."""
    import efinance as ef

    df = ef.stock.get_realtime_quotes()
    if df is None or df.empty:
        raise RuntimeError("efinance returned empty data")
    return _normalize(df, source="efinance")


def _fetch_akshare_em() -> pd.DataFrame:
    """Fetch via akshare (eastmoney)."""
    import akshare as ak

    df = ak.stock_zh_a_spot_em()
    if df is None or df.empty:
        raise RuntimeError("akshare returned empty data")
    return _normalize(df, source="akshare_em")


def _fetch_sina() -> pd.DataFrame:
    """Fetch A-share full-market snapshot directly from Sina Finance.

    Sina's market-center endpoint is a lightweight direct HTTP source with PE,
    PB, turnover and market-cap fields. It gives the screening engine another non-wrapper,
    non-Eastmoney-first snapshot option before falling back to Eastmoney-heavy
    sources.
    """
    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    page = 1
    # Sina caps this endpoint at 100 rows. Use the cap to reduce full-market
    # pagination round trips without changing the response contract.
    page_size = 100
    all_items = []
    while True:
        resp = requests.get(
            url,
            params={
                "page": page,
                "num": page_size,
                "sort": "symbol",
                "asc": 1,
                "node": "hs_a",
                "symbol": "",
                "_s_r_a": "page",
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://vip.stock.finance.sina.com.cn/mkt/",
            },
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json()
        if not isinstance(items, list):
            raise RuntimeError("sina snapshot returned malformed data")
        if not items:
            break
        all_items.extend(items)
        if len(items) < page_size:
            break
        page += 1
    if not all_items:
        raise RuntimeError("sina returned empty data")
    df = pd.DataFrame(all_items)
    for col in ("mktcap", "nmc"):
        if col in df.columns:
            # Sina exposes market caps in ten-thousand yuan; normalize to yuan.
            df[col] = pd.to_numeric(df[col], errors="coerce") * 10000
    return _normalize(df, source="sina")


def _fetch_em_datacenter() -> pd.DataFrame:
    """Fetch via eastmoney datacenter xuangu API.

    This works even on weekends (returns last trading day data).
    """
    url = "https://data.eastmoney.com/dataapi/xuangu/list"
    all_items = []
    page = 1
    page_size = 500

    while True:
        params = {
            "st": "SECURITY_CODE",
            "sr": "1",
            "ps": str(page_size),
            "p": str(page),
            "sty": "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,NEW_PRICE,"
                   "CHANGE_RATE,VOLUME_RATIO,DEAL_AMOUNT,TURNOVERRATE,"
                   "PE9,PBNEWMRQ,TOTAL_MARKET_CAP,CIRCULATION_MARKET_CAP,"
                   # 注意：不要加 BOARD_NAME —— 实测只要带上它，东财每页就
                   # 从 500 条掉到 350 条且后续分页全空（只能拿到 350 只票）。
                   # board_name 在本仓库零引用，去掉无损失。
                   "INDUSTRY,CONCEPT,MAX_TRADE_DATE",
            "filter": '(MARKET+in+("上交所主板","深交所主板","深交所创业板","上交所科创板","北交所"))',
            "source": "SELECT_SECURITIES",
            "client": "WEB",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/xuangu/",
        }

        resp = _eastmoney_get(url, params=params, headers=headers, timeout=30)
        data = resp.json()

        if not data.get("success"):
            raise RuntimeError(f"em_datacenter API error: {data.get('message', 'unknown')}")

        items = data["result"]["data"]
        all_items.extend(items)

        total_count = data["result"]["count"]
        if page * page_size >= total_count:
            break
        page += 1

    if not all_items:
        raise RuntimeError("em_datacenter returned no data")

    df = pd.DataFrame(all_items)
    normalized = _normalize(df, source="em_datacenter")
    # 东财每条都带 MAX_TRADE_DATE（如 2026-10-20）—— 这是权威的数据日，
    # 不能再用"什么时候跑的"去推（凌晨补跑会把前一天的行情标成今天）。
    trade_dates = []
    if "MAX_TRADE_DATE" in normalized.columns:
        trade_dates = [
            str(v).strip() for v in normalized["MAX_TRADE_DATE"].dropna().unique().tolist()
        ]
    trade_dates = [d for d in trade_dates if d and d not in ("0", "None", "nan")]
    if trade_dates:
        normalized.attrs["trade_date"] = max(trade_dates)
    return normalized


def _eastmoney_get_noretry(url: str, **kwargs) -> requests.Response:
    """单次请求、不重试、带节流的东财 GET。

    用于「拿不到也不该影响主流程」的辅助数据（分类补充）：
    重试只会把一次失败放大成几十秒的等待。
    """
    global _EM_LAST_REQUEST_AT
    with _EM_LOCK:
        elapsed = time.monotonic() - _EM_LAST_REQUEST_AT
        interval = _eastmoney_request_interval_seconds()
        if elapsed < interval:
            time.sleep(interval - elapsed)
        response = requests.get(
            url,
            headers=kwargs.get("headers"),
            params=kwargs.get("params"),
            timeout=kwargs.get("timeout", _EM_ENRICH_PAGE_TIMEOUT),
        )
        _EM_LAST_REQUEST_AT = time.monotonic()
    response.raise_for_status()
    return response


def _eastmoney_get(url: str, **kwargs) -> requests.Response:
    """GET EastMoney endpoints through one throttled shared session.

    EastMoney endpoints are useful but more sensitive to bursty access than
    lightweight direct sources. Keeping all direct calls behind one retrying
    session, a process-wide interval, and a little jitter follows the same
    anti-ban pattern used by a-stock-data's ``em_get`` helper.
    """
    global _EM_LAST_REQUEST_AT, _EM_SESSION
    with _EM_LOCK:
        if _EM_SESSION is None:
            _EM_SESSION = _build_eastmoney_session()
        elapsed = time.monotonic() - _EM_LAST_REQUEST_AT
        interval = _eastmoney_request_interval_seconds()
        if elapsed < interval:
            time.sleep(interval - elapsed)
        response = _EM_SESSION.get(url, **kwargs)
        _EM_LAST_REQUEST_AT = time.monotonic()
    response.raise_for_status()
    return response


def _build_eastmoney_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _eastmoney_request_interval_seconds() -> float:
    min_interval = _float_env(
        "SCREENING_EASTMONEY_MIN_INTERVAL_SEC",
        _EM_REQUEST_MIN_INTERVAL_SECONDS,
    )
    jitter = _float_env(
        "SCREENING_EASTMONEY_JITTER_SEC",
        _EM_REQUEST_JITTER_SECONDS,
    )
    return max(min_interval, 0.0) + random.uniform(0.0, max(jitter, 0.0))


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return float(value) if value else float(default)


def _fetch_tushare() -> pd.DataFrame:
    """Fetch latest available A-share snapshot via Tushare Pro.

    Tushare is not a real-time source here. It is used as a resilient fallback
    by joining the latest open trading day's daily quote and daily_basic data.
    """
    token = (
        os.getenv("TUSHARE_TOKEN", "").strip()
        or os.getenv("TUSHARE_API_TOKEN", "").strip()
    )
    if not token:
        raise RuntimeError("tushare requires TUSHARE_TOKEN")

    import tushare as ts

    pro = ts.pro_api(token)
    _configure_tushare_client(pro, token=token)
    trade_date = _resolve_tushare_trade_date(pro)
    daily = pro.daily(
        trade_date=trade_date,
        fields="ts_code,trade_date,close,pct_chg,amount",
    )
    daily_basic = pro.daily_basic(
        trade_date=trade_date,
        fields="ts_code,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv",
    )
    stock_basic = pro.stock_basic(
        exchange="",
        list_status="L",
        fields="ts_code,symbol,name,industry",
    )

    if daily is None or daily.empty:
        raise RuntimeError(f"tushare daily returned empty data for {trade_date}")
    if daily_basic is None or daily_basic.empty:
        raise RuntimeError(f"tushare daily_basic returned empty data for {trade_date}")

    return _prepare_tushare_snapshot(daily, daily_basic, stock_basic)


def _configure_tushare_client(pro: object, *, token: str) -> None:
    try:
        setattr(pro, "_DataApi__token", token)
    except Exception:
        pass

    http_url = (
        os.getenv("TUSHARE_API_URL", "").strip()
        or os.getenv("TUSHARE_HTTP_URL", "").strip()
        or _DEFAULT_TUSHARE_HTTP_URL
    )
    try:
        setattr(pro, "_DataApi__http_url", http_url)
    except Exception:
        pass


def _resolve_tushare_trade_date(pro) -> str:
    """Return the latest open trade date for Tushare requests."""
    explicit = os.getenv("TUSHARE_TRADE_DATE", "").strip()
    if explicit:
        return explicit

    end = date.today()
    start = end - timedelta(days=30)
    calendar = pro.trade_cal(
        exchange="",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        is_open="1",
        fields="cal_date,is_open",
    )
    if calendar is None or calendar.empty or "cal_date" not in calendar.columns:
        raise RuntimeError("tushare trade_cal returned no open trading days")
    return str(calendar["cal_date"].max())


def _prepare_tushare_snapshot(
    daily: pd.DataFrame,
    daily_basic: pd.DataFrame,
    stock_basic: pd.DataFrame | None,
) -> pd.DataFrame:
    """Join and unit-normalize Tushare tables into the common snapshot schema."""
    merged = daily.merge(daily_basic, on="ts_code", how="left")
    if stock_basic is not None and not stock_basic.empty:
        merged = merged.merge(stock_basic, on="ts_code", how="left")
    if "symbol" not in merged.columns:
        merged["symbol"] = merged["ts_code"].astype(str).str.split(".").str[0]
    else:
        fallback_symbol = merged["ts_code"].astype(str).str.split(".").str[0]
        merged["symbol"] = merged["symbol"].fillna(fallback_symbol)

    # Tushare units: amount is thousand yuan; market caps are ten-thousand yuan.
    for col, multiplier in {
        "amount": 1000,
        "total_mv": 10000,
        "circ_mv": 10000,
    }.items():
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce") * multiplier

    return _normalize(merged, source="tushare")


def snapshot_trade_date(df: pd.DataFrame) -> date | None:
    """从快照 attrs 里取数据日并转成 date。

    数据源给的是 "20261020"（Wind）或 "2026-10-20"（东财）；都没有时返回 None，
    调用方必须显式决定怎么兜底，不允许默默用"今天"。
    """
    if df is None:
        return None
    raw = getattr(df, "attrs", {}).get("trade_date")
    if not raw:
        return None
    text = str(raw).strip().replace("-", "").replace("/", "")[:8]
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except ValueError:
            return None
    return None


def _normalize(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """Normalize column names to a standard schema.

    Standard columns: code, name, price, change_pct, amount, total_mv,
                      circ_mv, pe_ratio, pb_ratio, volume_ratio, turnover_rate
    """
    df = df.copy()

    if source == "efinance":
        standard_cols = {
            "code": ["股票代码", "代码"],
            "name": ["股票名称", "名称"],
            "price": ["最新价"],
            "change_pct": ["涨跌幅"],
            "amount": ["成交额"],
            "total_mv": ["总市值"],
            "circ_mv": ["流通市值"],
            "pe_ratio": ["动态市盈率", "市盈率(动)"],
            "pb_ratio": ["市净率"],
            "volume_ratio": ["量比"],
            "turnover_rate": ["换手率"],
            "industry": ["行业", "所属行业", "行业板块"],
            "concepts": ["概念", "概念题材", "题材"],
        }
    elif source == "akshare_em":
        standard_cols = {
            "code": ["代码"],
            "name": ["名称"],
            "price": ["最新价"],
            "change_pct": ["涨跌幅"],
            "amount": ["成交额"],
            "total_mv": ["总市值"],
            "circ_mv": ["流通市值"],
            "pe_ratio": ["市盈率-动态", "市盈率(动)"],
            "pb_ratio": ["市净率"],
            "volume_ratio": ["量比"],
            "turnover_rate": ["换手率"],
            "industry": ["行业", "所属行业", "行业板块"],
            "concepts": ["概念", "概念题材", "题材"],
        }
    elif source == "sina":
        standard_cols = {
            "code": ["code"],
            "name": ["name"],
            "price": ["trade"],
            "change_pct": ["changepercent"],
            "amount": ["amount"],
            "total_mv": ["mktcap"],
            "circ_mv": ["nmc"],
            "pe_ratio": ["per"],
            "pb_ratio": ["pb"],
            "turnover_rate": ["turnoverratio"],
        }
    elif source == "em_datacenter":
        standard_cols = {
            "code": ["SECURITY_CODE"],
            "name": ["SECURITY_NAME_ABBR"],
            "price": ["NEW_PRICE"],
            "change_pct": ["CHANGE_RATE"],
            "amount": ["DEAL_AMOUNT"],
            "total_mv": ["TOTAL_MARKET_CAP"],
            "circ_mv": ["CIRCULATION_MARKET_CAP"],
            "pe_ratio": ["PE9"],
            "pb_ratio": ["PBNEWMRQ"],
            "volume_ratio": ["VOLUME_RATIO"],
            "turnover_rate": ["TURNOVERRATE"],
            "industry": ["INDUSTRY", "INDUSTRY_NAME", "BOARD_NAME"],
            "concepts": ["CONCEPT", "CONCEPT_NAME", "THEME_NAME"],
        }
    elif source == "wind":
        # 列已在 _fetch_wind 里映射成标准名，这里只做同义兜底。
        standard_cols = {
            "code": ["code", "Wind代码"],
            "name": ["name", "中文简称"],
            "price": ["price", "最新成交价"],
            "change_pct": ["change_pct", "涨跌幅"],
            "amount": ["amount", "成交额"],
            "total_mv": ["total_mv", "总市值2", "总市值1"],
            "circ_mv": ["circ_mv", "流通市值"],
            "pe_ttm": ["pe_ttm", "市盈率(TTM)"],
            "pb_ratio": ["pb_ratio", "市净率(LF)"],
            "volume_ratio": ["volume_ratio", "量比"],
            "turnover_rate": ["turnover_rate", "换手率"],
        }
    elif source == "tushare":
        standard_cols = {
            "code": ["symbol", "code"],
            "name": ["name"],
            "price": ["close"],
            "change_pct": ["pct_chg"],
            "amount": ["amount"],
            "total_mv": ["total_mv"],
            "circ_mv": ["circ_mv"],
            "pe_ratio": ["pe"],
            "pb_ratio": ["pb"],
            "volume_ratio": ["volume_ratio"],
            "turnover_rate": ["turnover_rate"],
            "industry": ["industry"],
            "concepts": ["concepts"],
        }
    else:
        standard_cols = {}

    df = _rename_standard_columns(df, standard_cols)

    # Coerce numeric columns
    numeric_cols = [
        "price", "change_pct", "amount", "total_mv", "circ_mv",
        "pe_ratio", "pe_ttm", "pe_lyr", "pb_ratio", "volume_ratio", "turnover_rate",
        "prev_close", "open", "high", "low", "change_amount", "amplitude",
        "volume", "dividend_yield", "total_shares", "free_shares",
        "high_52w", "low_52w", "change_5d", "change_20d", "change_60d", "change_ytd",
        "limit_up", "limit_down",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Drop rows without a valid price
    if "price" in df.columns:
        df = df.dropna(subset=["price"])
        df = df[df["price"] > 0]

    df.attrs["snapshot_source"] = source
    return df


def _rename_standard_columns(
    df: pd.DataFrame,
    standard_cols: dict[str, list[str]],
) -> pd.DataFrame:
    """Rename the first matching source column for each standard field."""
    rename_map: dict[str, str] = {}
    for standard_name, candidates in standard_cols.items():
        for candidate in candidates:
            if candidate in df.columns:
                rename_map[candidate] = standard_name
                break
    return df.rename(columns=rename_map)
