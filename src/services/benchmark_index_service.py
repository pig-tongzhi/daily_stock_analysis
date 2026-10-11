# -*- coding: utf-8 -*-
"""基准指数（沪深300 等）：把指数日线落到本地 ``stock_daily``，供选股复盘做同期对比。

为什么需要这个模块：选股复盘此前只能说"某只入选股涨了 +0.47%"。本地
``stock_daily`` 里只有个股、没有任何指数序列，于是这个 +0.47% 究竟是选股能力
还是大盘 beta 无法回答 —— 大盘同期 +2% 时 +0.47% 其实是跑输。指数日线本身
一直拿得到（``ak.stock_zh_index_daily("sh000300")`` 约 6000 行、0.5 秒），
缺的只是"抓下来并落到本地"这一步。

为什么复用 ``stock_daily`` 而不是新增表：这张表的列已经足够通用
（``code`` / ``date`` / OHLC / ``volume`` / ``amount`` / ``pct_chg`` /
``canonical_id`` / ``data_source``），复盘的读路径也已经按 ``canonical_id``
对齐；再开一张表会带来第二套存储、第二个同步入口和第二份新鲜度口径，收益为零。

指数行如何避免被当成个股（这决定不能被任何既有代码路径误处理）：

1. ``code`` 写**显式指数形式**（``sh000300``）而不是裸码 ``000300``。storage 的
   canonical_id 推导契约规定：裸码恒为*个股* canonical（``000300`` ->
   ``sz000300``），只有显式指数形式才推导为指数 canonical_id。用显式形式，
   ``canonical_id`` 与 ``code`` 都落在指数语义上，复盘按 canonical_id 对齐时
   不会串进个股桶。
2. ``data_source`` 固定写 ``akshare_index``，让"哪些行是指数"在 SQL 里一眼可辨
   （``WHERE data_source = 'akshare_index'``），不必依赖正则猜代码形态。
3. 显式 ``sh`` 前缀还让 storage 的两个自愈迁移天然跳过这些行：
   ``_backfill_stock_daily_canonical_id`` 只处理 ``canonical_id IS NULL`` 的行，
   ``_backfill_canonical_ids`` 只修"裸六位数字码且 canonical 命中指数"的行，
   两者都不会改写显式指数行。

fail-open：抓取或写库任何一步失败都只记日志并返回统计，不抛异常 ——
基准缺失应该只让复盘显示"基准不可用"，不该让整个行情刷新停摆。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 默认基准：沪深300 为主，中证500 / 中证1000 用于看覆盖面（大小盘风格）。
# 顺序即优先级：复盘取第一个本地有 K 线的作为主基准。
DEFAULT_BENCHMARK_INDEX_CODES: Tuple[str, ...] = ("sh000300", "sh000905", "sh000852")
PRIMARY_BENCHMARK_INDEX_CODE = DEFAULT_BENCHMARK_INDEX_CODES[0]
# 指数行的 data_source 标记。个股行沿用各自 fetcher 名（如 AkshareFetcher），
# 因此这个值可以安全地当"这是指数"的判据。
INDEX_DATA_SOURCE = "akshare_index"
# 每次抓取保留多少自然日的历史。指数日线接口一次返回全量（约 6000 行），
# 全量写库会让每轮刷新无意义地 UPSERT 六千行 × N 个指数；而复盘只需要覆盖
# 选股回看窗口（默认 60 天）再加冗余。800 个自然日 ≈ 3 年交易日，足够覆盖
# 任何现实的入选窗口，同时把单次写库压到几百行。
DEFAULT_HISTORY_DAYS = 800


def resolve_benchmark_index_codes(config: Any = None) -> List[str]:
    """解析启用的基准指数代码；未配置时退回默认清单。

    ``config.screening_benchmark_index_codes`` 可能是 list（配置解析而来）
    也可能是逗号分隔字符串（直接构造 Config 时）。用 str() 粗暴转换会把
    ``['sh000300']`` 的 repr 当成代码 —— 必须按类型处理，这一点与
    ``runtime_scheduler._tracked_daily_codes`` 处理 ``stock_list`` 的写法一致。
    """
    if config is not None and not bool(
        getattr(config, "screening_benchmark_index_enabled", True)
    ):
        return []

    raw = getattr(config, "screening_benchmark_index_codes", None) if config is not None else None
    if raw is None or raw == "":
        return list(DEFAULT_BENCHMARK_INDEX_CODES)

    if isinstance(raw, (list, tuple, set)):
        candidates = [str(item) for item in raw]
    else:
        candidates = str(raw).replace("，", ",").split(",")

    resolved: List[str] = []
    for item in candidates:
        code = item.strip().strip("'\"").lower()
        if code and code not in resolved:
            resolved.append(code)
    return resolved


def _slice_recent(frame: Any, history_days: int) -> Any:
    """只保留最近 ``history_days`` 个自然日的行。"""
    if frame is None or getattr(frame, "empty", True):
        return frame
    import pandas as pd

    cutoff = date.today() - timedelta(days=max(1, int(history_days)))
    parsed = pd.to_datetime(frame["date"], errors="coerce", format="mixed")
    keep = parsed.dt.date >= cutoff
    return frame.loc[keep].copy()


def _ensure_pct_chg(frame: Any) -> Any:
    """补出 ``pct_chg``：接口只给 OHLCV，涨跌幅由 close 自行推导。

    首行没有前一日收盘价，填 0 而不是留 NaN —— 与 akshare_fetcher 处理指数
    时保持同一口径。
    """
    import pandas as pd

    if frame is None or getattr(frame, "empty", True):
        return frame
    if "close" not in frame.columns or "pct_chg" in frame.columns:
        return frame
    close = pd.to_numeric(frame["close"], errors="coerce")
    pct_chg = close.pct_change(fill_method=None) * 100
    pct_chg = pct_chg.replace([float("inf"), float("-inf")], float("nan"))
    if not pct_chg.empty and pd.isna(pct_chg.iloc[0]):
        pct_chg.iloc[0] = 0.0
    result = frame.copy()
    result["pct_chg"] = pct_chg
    return result


def fetch_index_daily(code: str, *, history_days: int = DEFAULT_HISTORY_DAYS) -> Any:
    """抓取指数日线；返回含 date/open/high/low/close/volume 的 DataFrame。

    两条路径，按顺序尝试：

    1. ``ak.stock_zh_index_daily`` —— 实测可用（sh000300 6005 行 / 0.23 秒），
       且走的是新浪源，不受东财代理问题影响。
    2. 既有的 ``DataFetcherManager.get_daily_data`` 指数链路
       （Tencent -> Akshare -> TickFlow -> Yfinance）—— 复用系统既有的多源
       fallback，避免本模块成为单点。

    两个都失败时抛异常，由 ``sync_benchmark_index_bars`` 记日志并跳过该指数。
    """
    errors: List[str] = []

    try:
        import akshare as ak

        frame = ak.stock_zh_index_daily(symbol=code)
        if frame is not None and not getattr(frame, "empty", True):
            required = {"date", "open", "high", "low", "close"}
            missing = sorted(required - set(frame.columns))
            if missing:
                raise ValueError(f"缺少必需列: {', '.join(missing)}")
            if "volume" not in frame.columns:
                frame = frame.copy()
                frame["volume"] = None
            result = _ensure_pct_chg(_slice_recent(frame, history_days))
            if not result.empty:
                return result
            errors.append(f"stock_zh_index_daily 在最近 {history_days} 天内没有数据")
        else:
            errors.append("stock_zh_index_daily 返回空结果")
    except Exception as exc:  # noqa: BLE001 - 记录后继续走 fallback
        errors.append(f"stock_zh_index_daily: {exc}")

    try:
        from data_provider.base import DataFetcherManager

        frame, source = DataFetcherManager().get_daily_data(code, days=history_days)
        if frame is not None and not getattr(frame, "empty", True):
            logger.info("[BenchmarkIndex] %s 经 %s 兜底抓取成功", code, source)
            return frame
        errors.append("DataFetcherManager 返回空结果")
    except Exception as exc:  # noqa: BLE001 - 汇总后统一抛出
        errors.append(f"DataFetcherManager: {exc}")

    raise RuntimeError("; ".join(errors) or "未知抓取失败")


def save_index_bars(db_manager: Any, code: str, frame: Any, *, data_source: str = INDEX_DATA_SOURCE) -> int:
    """把指数日线写进 ``stock_daily``，返回新增行数。

    ``canonical_id`` 必须用 storage 自己的推导结果（``sh000300`` -> ``sh000300``），
    不自己拼字符串：推导失败说明这个代码在指数注册表里不存在，此时**不写库**
    并返回 0 —— 写进去的只会是一份复盘永远对不上号的孤儿数据。
    """
    canonical_id = db_manager._derive_canonical_id(code)
    if not canonical_id:
        logger.warning(
            "[BenchmarkIndex] %s 不在指数注册表中，推导不出 canonical_id；跳过写库",
            code,
        )
        return 0
    return int(db_manager.save_daily_data(frame, code, data_source, canonical_id=canonical_id) or 0)


def sync_benchmark_index_bars(
    *,
    config: Any = None,
    db_manager: Any = None,
    codes: Optional[Sequence[str]] = None,
    history_days: int = DEFAULT_HISTORY_DAYS,
) -> List[Dict[str, Any]]:
    """抓取并落库所有启用的基准指数，返回逐个指数的结果统计。

    fail-open：单个指数失败只影响它自己（``ok=False`` + ``error``），不影响
    其它指数，也不向上抛异常 —— 调用方是后台刷新任务。
    """
    resolved_codes = list(codes) if codes is not None else resolve_benchmark_index_codes(config)
    results: List[Dict[str, Any]] = []
    if not resolved_codes:
        return results

    if db_manager is None:
        from src.storage import DatabaseManager

        db_manager = DatabaseManager.get_instance()

    for code in resolved_codes:
        entry: Dict[str, Any] = {"code": code, "ok": False, "rows": 0, "error": None}
        try:
            frame = fetch_index_daily(code, history_days=history_days)
            entry["rows"] = save_index_bars(db_manager, code, frame)
            entry["ok"] = True
        except Exception as exc:  # noqa: BLE001 - 单个指数失败不拖垮其它指数
            entry["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("[BenchmarkIndex] 抓取 %s 失败: %s", code, exc, exc_info=True)
        results.append(entry)

    logger.info(
        "[BenchmarkIndex] sync results: %s",
        ", ".join(
            f"{item['code']}={'ok' if item['ok'] else 'fail'}(+{item['rows']})" for item in results
        ),
    )
    return results
