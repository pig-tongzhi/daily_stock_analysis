# -*- coding: utf-8 -*-
"""Runtime scheduler service for long-lived API/Web/Desktop processes."""

from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import subprocess
import threading
import _thread
import time
from datetime import datetime
from functools import partial
from queue import Empty
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Set

from src.config import Config, get_config
from src.scheduler import Scheduler, normalize_schedule_times

logger = logging.getLogger(__name__)
CLI_SCHEDULER_OWNER_ENV = "DSA_CLI_SCHEDULER_OWNS_SCHEDULE"
RUNTIME_SCHEDULER_FORCE_ENABLED_ENV = "DSA_RUNTIME_SCHEDULER_FORCE_ENABLED"
RUNTIME_SCHEDULER_RUN_IMMEDIATELY_ENV = "DSA_RUNTIME_SCHEDULER_RUN_IMMEDIATELY"
RUNTIME_SCHEDULER_SUPPRESS_START_ENV = "DSA_RUNTIME_SCHEDULER_SUPPRESS_START"
RUNTIME_SCHEDULER_ARGS_ENV = "DSA_RUNTIME_SCHEDULER_ARGS"
RUNTIME_SCHEDULER_TIMEOUT_ENV = "DSA_RUNTIME_SCHEDULER_TIMEOUT_SECONDS"
DEFAULT_RUNTIME_SCHEDULER_TIMEOUT_SECONDS = 45 * 60
_RUNTIME_ANALYSIS_LOCK = threading.Lock()
SCHEDULE_ARGS_OVERRIDE_KEYS = {
    "no_notify",
    "no_market_review",
    "dry_run",
    "force_run",
    "single_notify",
    "no_context_snapshot",
    "workers",
    "portfolio",
}


def run_with_global_analysis_lock(
    task_runner: Callable[[Config, Any, Optional[List[str]]], Any],
    config: Config,
    args: Any,
    stock_codes: Optional[List[str]] = None,
    *,
    blocking: bool = True,
) -> bool:
    """Execute a task while holding the shared runtime analysis lock."""
    if not _RUNTIME_ANALYSIS_LOCK.acquire(blocking=blocking):
        return False
    try:
        task_runner(config, args, stock_codes)
    finally:
        _RUNTIME_ANALYSIS_LOCK.release()
    return True


def _run_scheduled_analysis_process(
    result_queue: Any,
    stock_codes: Optional[List[str]],
    schedule_args_overrides: Dict[str, Any],
) -> None:
    """Run one analysis in a spawn-safe child process."""
    if os.name == "posix":
        try:
            os.setsid()
        except OSError:
            # Being a session leader already is equivalent to success. Any
            # other failure would make process-tree cleanup unsafe, so fail
            # before analysis can create descendants.
            if os.getsid(0) != os.getpid():
                raise
    service = RuntimeSchedulerService(schedule_args_overrides=schedule_args_overrides)
    success = service._run_analysis_locked(stock_codes)
    result_queue.put({"success": success, "error": service._last_error})


def _posix_descendant_process_ids(root_pid: int) -> Set[int]:
    """Return a best-effort snapshot of descendants before the root exits."""
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return set()

    children_by_parent: Dict[int, List[int]] = {}
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, parent_pid = (int(value) for value in parts)
        except ValueError:
            continue
        children_by_parent.setdefault(parent_pid, []).append(pid)

    descendants: Set[int] = set()
    pending = list(children_by_parent.get(root_pid, []))
    while pending:
        pid = pending.pop()
        if pid in descendants:
            continue
        descendants.add(pid)
        pending.extend(children_by_parent.get(pid, []))
    return descendants


def _terminate_analysis_process_tree(process: Any) -> None:
    """Stop an analysis worker and any descendants it created."""
    root_alive = process.is_alive()
    process_id = process.pid

    posix_process_groups: Set[int] = set()
    if os.name == "posix" and process_id:
        posix_process_groups.add(process_id)
        current_process_group = os.getpgrp()
        if root_alive:
            for descendant_pid in _posix_descendant_process_ids(process_id):
                try:
                    descendant_group = os.getpgid(descendant_pid)
                except ProcessLookupError:
                    continue
                if descendant_group != current_process_group:
                    posix_process_groups.add(descendant_group)

    try:
        if os.name == "posix" and process_id:
            for process_group in posix_process_groups:
                try:
                    os.killpg(process_group, signal.SIGTERM)
                except ProcessLookupError:
                    continue
            # The spawned worker calls setsid(), but stop/timeout can win the
            # race before that happens. In that window killpg(worker_pid, ...)
            # has no target, so also terminate the multiprocessing handle.
            if process.is_alive():
                process.terminate()
        elif os.name == "nt" and process_id:
            subprocess.run(
                ["taskkill", "/PID", str(process_id), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        elif root_alive:
            process.terminate()
    except (OSError, subprocess.SubprocessError):
        if root_alive:
            process.terminate()

    process.join(2)
    if os.name == "posix" and process_id:
        remaining_process_groups: Set[int] = set()
        for process_group in posix_process_groups:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            remaining_process_groups.add(process_group)
        if not remaining_process_groups:
            return
    elif not process.is_alive():
        return

    try:
        if os.name == "posix" and process_id:
            for process_group in remaining_process_groups:
                try:
                    os.killpg(process_group, signal.SIGKILL)
                except ProcessLookupError:
                    continue
        else:
            process.kill()
    except (OSError, AttributeError):
        if process.is_alive():
            process.terminate()
    process.join(10)


def _agent_event_monitor_interval_seconds(config: Config) -> int:
    """Return the validated Event Monitor polling interval in seconds."""
    interval_minutes = getattr(config, "agent_event_monitor_interval_minutes", 5)
    try:
        interval_minutes = max(1, int(interval_minutes))
    except (TypeError, ValueError):  # pragma: no cover - defensive branch
        logger.warning(
            "Invalid AGENT_EVENT_MONITOR_INTERVAL_MINUTES=%r; use fallback 5",
            interval_minutes,
        )
        interval_minutes = 5
    return interval_minutes * 60


def build_agent_event_monitor_background_tasks(
    config: Config,
    *,
    config_provider: Callable[[], Config],
) -> List[Dict[str, Any]]:
    """Build scheduler background tasks used by the runtime scheduler."""
    if not getattr(config, "agent_event_monitor_enabled", False):
        return []

    from src.services.alert_worker import AlertWorker

    interval_seconds = _agent_event_monitor_interval_seconds(config)
    try:
        alert_worker = AlertWorker(config_provider=config_provider)
    except Exception as exc:  # pragma: no cover - defensive branch
        logger.warning("Failed to initialize AlertWorker for event monitor: %s", exc)
        return []

    def event_monitor_task() -> None:
        stats = alert_worker.run_once()
        triggered_count = stats.get("triggered", 0)
        if triggered_count:
            logger.info("[EventMonitor] triggered %d alert(s)", triggered_count)

    return [{
        "task": event_monitor_task,
        "interval_seconds": interval_seconds,
        "run_immediately": True,
        "name": "agent_event_monitor",
    }]


def _decision_signal_outcome_interval_seconds(config: Config) -> int:
    interval_minutes = getattr(config, "decision_signal_outcome_auto_run_interval_minutes", 60)
    try:
        minutes = int(interval_minutes)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid DECISION_SIGNAL_OUTCOME_AUTO_RUN_INTERVAL_MINUTES=%r; use fallback 60",
            interval_minutes,
        )
        minutes = 60
    if minutes <= 0:
        minutes = 60
    return minutes * 60


def build_decision_signal_outcome_background_tasks(
    config: Config,
    *,
    config_provider: Callable[[], Config],
) -> List[Dict[str, Any]]:
    """Build the periodic decision-signal outcome evaluation task.

    到期验证必须自动运行。此前它只有一个手动 API 入口，结果是从未运行过 ——
    decision_signal_outcomes 长期为 0 行，整个反馈闭环（命中率 → 置信度校准 →
    影响下一次分析）始终是惰性的。
    """
    if not getattr(config, "decision_signal_outcome_auto_run_enabled", False):
        return []

    interval_seconds = _decision_signal_outcome_interval_seconds(config)

    def outcome_task() -> None:
        try:
            from src.services.decision_signal_outcome_service import (
                DecisionSignalOutcomeService,
            )

            service = DecisionSignalOutcomeService()
            result = service.run_outcomes()
            evaluated = int(result.get("evaluated") or 0)
            # 无论是否有新增都记录：本次修复查出的三条断裂线全都是沉默失败 ——
            # 验证器说 unable、LLM 解析器说 no_json_found、日志说"获取成功"但数据没落库。
            # 只有结果被持续写进日志，下一次静默退化才会被发现，而不是靠人工考古。
            stats = service.get_stats()
            logger.info(
                "[DecisionSignalOutcome] evaluated=%s created=%s updated=%s skipped=%s "
                "completed=%s unable=%s directional=%s range=%s unable_reasons=%s",
                evaluated,
                result.get("created"),
                result.get("updated"),
                result.get("skipped"),
                stats.get("completed"),
                stats.get("unable"),
                stats.get("directional_completed"),
                stats.get("range_completed"),
                stats.get("unable_reasons"),
            )
        except Exception:
            # 单次失败不能让后台任务永久死掉；下一轮继续。
            logger.exception("[DecisionSignalOutcome] scheduled run failed")

    return [{
        "task": outcome_task,
        "interval_seconds": interval_seconds,
        "run_immediately": True,
        "name": "decision_signal_outcome",
    }]


def _stock_daily_refresh_interval_seconds(config: Config) -> int:
    interval_minutes = getattr(config, "stock_daily_refresh_interval_minutes", 60)
    try:
        minutes = int(interval_minutes)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid STOCK_DAILY_REFRESH_INTERVAL_MINUTES=%r; use fallback 60",
            interval_minutes,
        )
        minutes = 60
    return minutes * 60 if minutes > 0 else 3600


def _tracked_daily_codes(config: Config) -> List[str]:
    """需要保持行情新鲜的代码：自选股 + 判断里出现过的标的。

    判断里出现过的代码同样必需 —— 588200 只出现在 decision_signals 里、不在
    自选股中，缺少它的行情的直接后果就是那条唯一的买入判断永远无法复盘。
    """
    codes: List[str] = []
    # stock_list 由配置解析而来，形态可能是 list 也可能是逗号分隔字符串。
    # 用 str() 粗暴转换会把 ['600559', ...] 的 repr 当成代码 —— 必须按类型处理。
    raw_list = getattr(config, "stock_list", None)
    if isinstance(raw_list, (list, tuple, set)):
        candidates = [str(item) for item in raw_list]
    else:
        candidates = str(raw_list or "").replace("，", ",").split(",")
    for item in candidates:
        item = item.strip().strip("'\"")
        if item:
            codes.append(item)
    try:
        from src.repositories.decision_signal_repo import DecisionSignalRepository

        # list() 返回 (records, total)，不是列表 —— 直接迭代会拿到元组本身。
        records, _total = DecisionSignalRepository().list(page=1, page_size=200)
        for row in records:
            code = str(getattr(row, "stock_code", "") or "").strip()
            if code:
                codes.append(code)
    except Exception:
        # 用 warning 而不是 debug：这个查询失败会漏掉"只出现在 decision_signals 里"
        # 的标的（例如 588200），而漏掉的后果正是那条判断永远无法复盘。
        # 静默吞异常本身就是本阶段在治的病，不能自己再犯一次。
        logger.warning(
            "[StockDailyRefresh] 读取 decision_signals 代码失败；本次将只刷新自选股",
            exc_info=True,
        )
    return list(dict.fromkeys(codes))


def build_stock_daily_refresh_background_tasks(
    config: Config,
    *,
    config_provider: Callable[[], Config],
) -> List[Dict[str, Any]]:
    """Build the periodic local daily-bar refresh task.

    行情此前只在跑分析时顺带更新，于是它会在没人分析时静默停摆 —— 本次发现
    stock_daily 停在 2026-09-30，而 10-08/10-09 早已过去。到期验证依赖 K 线，
    所以行情新鲜度不能依附于分析频率。
    """
    if not getattr(config, "stock_daily_refresh_enabled", False):
        return []

    interval_seconds = _stock_daily_refresh_interval_seconds(config)

    def refresh_task() -> None:
        try:
            from data_provider.base import DataFetcherManager
            from src.services.stock_code_utils import resolve_daily_stock_identity
            from src.storage import DatabaseManager

            current = config_provider() if callable(config_provider) else config
            codes = _tracked_daily_codes(current)
            if not codes:
                return
            db = DatabaseManager.get_instance()
            manager = DataFetcherManager()
            refreshed = failed = 0
            for raw in codes:
                identity = resolve_daily_stock_identity(raw)
                stored = identity.code_candidates[0] if identity is not None else raw
                try:
                    df, source = manager.get_daily_data(stored, days=30)
                    if df is None or getattr(df, "empty", True):
                        continue
                    db.save_daily_data(df, stored, source)
                    refreshed += 1
                except Exception:
                    failed += 1
            logger.info(
                "[StockDailyRefresh] codes=%s refreshed=%s failed=%s",
                len(codes),
                refreshed,
                failed,
            )
        except Exception:
            # 单次失败不能让后台任务永久死掉；下一轮继续。
            logger.exception("[StockDailyRefresh] scheduled run failed")

    return [{
        "task": refresh_task,
        "interval_seconds": interval_seconds,
        "run_immediately": False,
        "name": "stock_daily_refresh",
    }]


class RuntimeSchedulerService:
    """Manage scheduled analysis inside the current API/Web/Desktop process."""

    def __init__(
        self,
        *,
        config_provider: Callable[[], Config] = get_config,
        task_runner: Optional[Callable[[Config, Any, Optional[List[str]]], Any]] = None,
        owns_schedule: Optional[bool] = None,
        force_enabled: bool = False,
        run_immediately_in_background: bool = False,
        background_tasks_provider: Optional[Callable[[Config], List[Dict[str, Any]]]] = None,
        schedule_args_overrides: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._config_provider = config_provider
        self._task_runner = task_runner
        if owns_schedule is None:
            owns_schedule = os.getenv(CLI_SCHEDULER_OWNER_ENV, "").strip().lower() not in {
                "1",
                "true",
                "yes",
                "on",
            }
        self._owns_schedule = owns_schedule
        self._force_enabled = force_enabled
        self._run_immediately_in_background = run_immediately_in_background
        self._background_tasks_provider = background_tasks_provider
        self._schedule_args_overrides = {
            key: value
            for key, value in (schedule_args_overrides or {}).items()
            if key in SCHEDULE_ARGS_OVERRIDE_KEYS
        }
        self._background_task_cache: Dict[str, Dict[str, Any]] = {}
        self._background_task_registered_names: Set[str] = set()
        self._lock = threading.RLock()
        self._run_lock = _RUNTIME_ANALYSIS_LOCK
        self._scheduler: Optional[Scheduler] = None
        self._thread: Optional[threading.Thread] = None
        self._enabled = False
        self._last_run_at: Optional[str] = None
        self._last_success_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_skipped_at: Optional[str] = None
        self._last_skip_reason: Optional[str] = None
        self._analysis_process_target = _run_scheduled_analysis_process
        self._analysis_process: Optional[Any] = None
        self._analysis_process_lock = threading.Lock()
        self._analysis_generation = 0

    def _make_schedule_args(self) -> SimpleNamespace:
        defaults = {
            "schedule": True,
            "no_run_immediately": True,
            "no_notify": False,
            "no_market_review": False,
            "dry_run": False,
            "force_run": False,
            "single_notify": False,
            "no_context_snapshot": False,
            "market_review": False,
            "serve": False,
            "serve_only": True,
            "stocks": None,
            "portfolio": None,
            "workers": None,
        }
        defaults.update(self._schedule_args_overrides)
        return SimpleNamespace(**defaults)

    def _reload_config(self) -> Config:
        from main import _reload_runtime_config

        return _reload_runtime_config()

    def _record_analysis_busy_skip(self) -> None:
        self._last_skipped_at = datetime.now().isoformat()
        self._last_skip_reason = "analysis_already_running"
        logger.warning("Runtime scheduler skipped run: analysis already running")

    def _run_analysis_locked(self, stock_codes: Optional[List[str]]) -> bool:
        try:
            config = self._reload_config()
            runner = self._task_runner
            if runner is None:
                from main import run_scheduled_analysis

                runner = run_scheduled_analysis
            self._last_run_at = datetime.now().isoformat()
            result = runner(config, self._make_schedule_args(), stock_codes)
            if result is False:
                raise RuntimeError("runtime scheduled analysis reported failure")
            self._last_success_at = datetime.now().isoformat()
            self._last_error = None
            return True
        except Exception as exc:  # noqa: BLE001 - scheduled runs must not kill API process.
            self._last_error = str(exc)
            logger.exception("Runtime scheduled analysis failed: %s", exc)
            return False

    def _run_analysis_once(self, stock_codes: Optional[List[str]] = None) -> bool:
        if not self._run_lock.acquire(blocking=False):
            self._record_analysis_busy_skip()
            return False
        try:
            self._run_analysis_locked(stock_codes)
        finally:
            self._run_lock.release()
        return True

    def _analysis_timeout_seconds(self) -> int:
        try:
            value = os.getenv(
                RUNTIME_SCHEDULER_TIMEOUT_ENV,
                str(DEFAULT_RUNTIME_SCHEDULER_TIMEOUT_SECONDS),
            )
            return max(60, int(value))
        except ValueError:
            logger.warning(
                "Invalid %s; using %ss",
                RUNTIME_SCHEDULER_TIMEOUT_ENV,
                DEFAULT_RUNTIME_SCHEDULER_TIMEOUT_SECONDS,
            )
            return DEFAULT_RUNTIME_SCHEDULER_TIMEOUT_SECONDS

    def _build_timeout_last_error(
        self,
        *,
        timeout_seconds: int,
        run_started_at: datetime,
        stock_codes: Optional[List[str]],
    ) -> str:
        """Collect partial DB results and build a structured timeout error.

        Notify-channel failures are fail-open and do not change this string.
        Collect/import failures are also fail-open: this may return a
        ``completed=0`` structured message (indistinguishable from no saved
        rows) or, if the helper itself raises, the baseline timeout fallback.
        ``status().last_error`` therefore cannot be used as a collect-failure
        signal; grep ``Failed to collect completed analyses after timeout``
        or ``Timeout partial delivery failed open``.
        """
        fallback = f"runtime scheduled analysis timed out after {timeout_seconds}s"
        try:
            from src.services.analysis_timeout_partial import handle_runtime_analysis_timeout

            config = None
            try:
                config = self._config_provider()
            except Exception as exc:  # noqa: BLE001 - config is optional for diagnostics
                logger.warning("Timeout partial notify could not load config: %s", exc)

            no_notify = bool(getattr(self._make_schedule_args(), "no_notify", False))
            outcome = handle_runtime_analysis_timeout(
                timeout_seconds=timeout_seconds,
                run_started_at=run_started_at,
                stock_codes=stock_codes,
                no_notify=no_notify,
                config=config,
            )
            if outcome.notified:
                logger.info(
                    "Timeout partial notification sent: completed=%s pending=%s",
                    len(outcome.completed),
                    len(outcome.pending_codes),
                )
            elif outcome.notify_skipped_reason:
                logger.info(
                    "Timeout partial notification skipped (%s): completed=%s pending=%s",
                    outcome.notify_skipped_reason,
                    len(outcome.completed),
                    len(outcome.pending_codes),
                )
            return outcome.error_message or fallback
        except Exception as exc:  # noqa: BLE001 - never lose the original timeout signal
            logger.warning("Timeout partial delivery failed open: %s", exc)
            return fallback

    def _apply_timeout_partial_outcome(self, context: Dict[str, Any]) -> None:
        """Enrich timeout diagnostics after the run lock is released.

        Runs in a daemon thread so DB/import work cannot delay the next run or
        keep the watchdog finally block occupied.

        Consistency with ``status()``:
        - The watchdog first writes a baseline ``timed out after Ns`` string
          under ``_analysis_process_lock``, then releases ``_run_lock``.
        - This thread later replaces ``_last_error`` with the structured
          completed/pending message, still under ``_analysis_process_lock``.
        - The replace is skipped if ``generation`` no longer matches (a newer
          run started) or if ``_last_error`` no longer contains
          ``timed out after`` (another outcome already replaced it).
        - ``status()`` reads ``_last_error`` without that lock. CPython
          pointer assignment is atomic, so callers observe either the baseline
          or the fully replaced string—never a torn mix. They may briefly see
          the baseline until this thread finishes.
        - Notify-channel exceptions stay inside this thread and cannot
          re-acquire ``_run_lock``. Collect/import fail-open is only visible
          in warning logs, not as a distinct ``last_error`` code.
        """
        generation = context.get("generation")

        def _enrich() -> None:
            try:
                enriched = self._build_timeout_last_error(
                    timeout_seconds=int(context["timeout_seconds"]),
                    run_started_at=context["run_started_at"],
                    stock_codes=context.get("stock_codes"),
                )
            except Exception as exc:  # noqa: BLE001 - baseline timeout error remains
                logger.warning("Timeout partial enrichment failed open: %s", exc)
                return
            with self._analysis_process_lock:
                if generation is not None and generation != self._analysis_generation:
                    return
                current = self._last_error or ""
                if "timed out after" not in current:
                    return
                self._last_error = enriched

        threading.Thread(
            target=_enrich,
            daemon=True,
            name="runtime-timeout-partial",
        ).start()

    def _run_analysis_with_watchdog(
        self,
        stock_codes: Optional[List[str]] = None,
        *,
        lock_held: bool = False,
        generation: Optional[int] = None,
    ) -> None:
        if not lock_held and not self._run_lock.acquire(blocking=False):
            self._record_analysis_busy_skip()
            return
        if generation is None:
            with self._analysis_process_lock:
                generation = self._analysis_generation

        result_queue = None
        timeout_partial_context: Optional[Dict[str, Any]] = None
        try:
            context = multiprocessing.get_context("spawn")
            result_queue = context.Queue()
            process = context.Process(
                target=self._analysis_process_target,
                args=(result_queue, stock_codes, dict(self._schedule_args_overrides)),
                name="runtime-scheduled-analysis",
            )
            timeout = self._analysis_timeout_seconds()
            run_started_at = datetime.now()
            with self._analysis_process_lock:
                if generation != self._analysis_generation:
                    return
                process.start()
                self._analysis_process = process
                self._last_run_at = run_started_at.isoformat()

            result = None
            deadline = time.monotonic() + timeout
            while result is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    result = result_queue.get(timeout=min(0.2, remaining))
                except Empty:
                    if not process.is_alive():
                        deadline = min(deadline, time.monotonic() + 2)

            if result is None and process.is_alive():
                logger.error(
                    "Runtime scheduled analysis exceeded %ss; terminating worker",
                    timeout,
                )
                _terminate_analysis_process_tree(process)
                with self._analysis_process_lock:
                    if generation != self._analysis_generation:
                        return
                    # Baseline error first so status.running can clear quickly.
                    self._last_error = (
                        f"runtime scheduled analysis timed out after {timeout}s"
                    )
                    timeout_partial_context = {
                        "timeout_seconds": timeout,
                        "run_started_at": run_started_at,
                        "stock_codes": stock_codes,
                        "generation": generation,
                    }
                return

            if result is None:
                exit_code = process.exitcode
                _terminate_analysis_process_tree(process)
                with self._analysis_process_lock:
                    if generation != self._analysis_generation:
                        return
                    self._last_error = (
                        "runtime scheduled analysis worker exited without a result "
                        f"(exit code {exit_code})"
                    )
                return

            process.join(2)
            if process.is_alive():
                _terminate_analysis_process_tree(process)
                with self._analysis_process_lock:
                    if generation != self._analysis_generation:
                        return
                    self._last_error = "runtime scheduled analysis worker did not exit"
                return

            with self._analysis_process_lock:
                if generation != self._analysis_generation:
                    return
                if result.get("success"):
                    self._last_success_at = datetime.now().isoformat()
                    self._last_error = None
                else:
                    self._last_error = result.get("error") or "runtime scheduled analysis failed"
        except Exception as exc:  # noqa: BLE001 - watchdog failures must release the scheduler.
            with self._analysis_process_lock:
                if generation == self._analysis_generation:
                    self._last_error = str(exc)
            logger.exception("Runtime scheduler watchdog failed: %s", exc)
        finally:
            self._run_lock.release()
            if "process" in locals():
                with self._analysis_process_lock:
                    if self._analysis_process is process:
                        self._analysis_process = None
            if result_queue is not None:
                result_queue.cancel_join_thread()
                result_queue.close()
            if timeout_partial_context is not None:
                self._apply_timeout_partial_outcome(timeout_partial_context)

    def _start_analysis_watchdog(
        self,
        stock_codes: Optional[List[str]] = None,
        *,
        generation: Optional[int] = None,
    ) -> bool:
        with self._analysis_process_lock:
            current_generation = self._analysis_generation
            if generation is not None and generation != current_generation:
                return False
            generation = current_generation
        if not self._run_lock.acquire(blocking=False):
            self._record_analysis_busy_skip()
            return False
        worker = threading.Thread(
            target=lambda: self._run_analysis_with_watchdog(
                stock_codes,
                lock_held=True,
                generation=generation,
            ),
            daemon=True,
            name="runtime-scheduler-watchdog",
        )
        try:
            worker.start()
        except Exception:
            self._run_lock.release()
            raise
        return True

    def _current_times(self) -> List[str]:
        config = self._config_provider()
        return normalize_schedule_times(
            getattr(config, "schedule_times", None),
            fallback_time=getattr(config, "schedule_time", "18:00"),
        )

    def _is_schedule_enabled(self, config: Config) -> bool:
        return self._force_enabled or bool(getattr(config, "schedule_enabled", False))

    def _current_background_tasks(self, config: Config) -> List[Dict[str, Any]]:
        if self._background_tasks_provider is not None:
            return self._background_tasks_provider(config)
        # 三个后台任务并列注册：事件监控、到期验证、行情刷新互不依赖，各自独立开关。
        return [
            *self._current_agent_event_monitor_background_tasks(config),
            *self._current_decision_signal_outcome_background_tasks(config),
            *self._current_stock_daily_refresh_background_tasks(config),
        ]

    def _current_stock_daily_refresh_background_tasks(self, config: Config) -> List[Dict[str, Any]]:
        name = "stock_daily_refresh"
        if not getattr(config, "stock_daily_refresh_enabled", False):
            self._background_task_cache.pop(name, None)
            self._background_task_registered_names.discard(name)
            return []

        cached = self._background_task_cache.get(name)
        if cached is None:
            entries = build_stock_daily_refresh_background_tasks(
                config,
                config_provider=self._reload_config,
            )
            if not entries:
                self._background_task_cache.pop(name, None)
                self._background_task_registered_names.discard(name)
                return []
            cached = dict(entries[0])
            cached["name"] = name
            self._background_task_cache[name] = cached
            interval_seconds = int(cached["interval_seconds"])
        else:
            interval_seconds = _stock_daily_refresh_interval_seconds(config)

        run_immediately = bool(cached.get("run_immediately", False))
        self._background_task_registered_names.add(name)
        return [{
            "task": cached["task"],
            "interval_seconds": interval_seconds,
            "run_immediately": run_immediately,
            "name": name,
        }]

    def _current_decision_signal_outcome_background_tasks(self, config: Config) -> List[Dict[str, Any]]:
        name = "decision_signal_outcome"
        if not getattr(config, "decision_signal_outcome_auto_run_enabled", False):
            self._background_task_cache.pop(name, None)
            self._background_task_registered_names.discard(name)
            return []

        cached = self._background_task_cache.get(name)
        if cached is None:
            entries = build_decision_signal_outcome_background_tasks(
                config,
                config_provider=self._reload_config,
            )
            if not entries:
                self._background_task_cache.pop(name, None)
                self._background_task_registered_names.discard(name)
                return []
            cached = dict(entries[0])
            cached["name"] = name
            self._background_task_cache[name] = cached
            interval_seconds = int(cached["interval_seconds"])
        else:
            interval_seconds = _decision_signal_outcome_interval_seconds(config)

        run_immediately = bool(cached.get("run_immediately", False))
        self._background_task_registered_names.add(name)
        return [{
            "task": cached["task"],
            "interval_seconds": interval_seconds,
            "run_immediately": run_immediately,
            "name": name,
        }]

    def _has_enabled_background_tasks(self, config: Config) -> bool:
        """是否有任何后台维护任务被启用。

        后台维护（到期验证、行情刷新、事件监控）与"每日跑一次全量分析"是两件
        互不依赖的事：前者按分钟级节奏维护数据，后者是每天一次的重活。
        若后者关闭就不启动调度线程，那些维护任务会永远不运行 —— 这正是
        decision_signal_outcomes 长期为空的原因之一。
        """
        for attr in (
            "decision_signal_outcome_auto_run_enabled",
            "stock_daily_refresh_enabled",
            "agent_event_monitor_enabled",
        ):
            if getattr(config, attr, False):
                return True
        return False

    def _current_agent_event_monitor_background_tasks(self, config: Config) -> List[Dict[str, Any]]:
        name = "agent_event_monitor"
        if not getattr(config, "agent_event_monitor_enabled", False):
            self._background_task_cache.pop(name, None)
            self._background_task_registered_names.discard(name)
            return []

        cached = self._background_task_cache.get(name)
        if cached is None:
            entries = build_agent_event_monitor_background_tasks(
                config,
                config_provider=self._reload_config,
            )
            if not entries:
                self._background_task_cache.pop(name, None)
                self._background_task_registered_names.discard(name)
                return []
            cached = dict(entries[0])
            cached["name"] = name
            self._background_task_cache[name] = cached
            interval_seconds = int(cached["interval_seconds"])
        else:
            interval_seconds = _agent_event_monitor_interval_seconds(config)

        run_immediately = (
            bool(cached.get("run_immediately", False))
            and name not in self._background_task_registered_names
        )
        self._background_task_registered_names.add(name)
        return [{
            "task": cached["task"],
            "interval_seconds": interval_seconds,
            "run_immediately": run_immediately,
            "name": name,
        }]

    @staticmethod
    def _run_in_background_thread(target: Callable[[], None]) -> None:
        """Run a callback in a background thread without blocking startup."""
        try:
            _thread.start_new_thread(target, ())
            return
        except Exception:
            # Best-effort fallback for environments where the low-level thread API
            # is unavailable or restricted.
            thread = threading.Thread(target=target, daemon=True)
            thread.start()

    def start(self, *, run_immediately: bool = False) -> None:
        with self._lock:
            if not self._owns_schedule:
                self.stop()
                return
            config = self._config_provider()
            schedule_enabled = self._is_schedule_enabled(config)
            background_tasks = self._current_background_tasks(config)
            # 每日分析关闭、但存在后台维护任务时仍须启动调度线程：
            # 否则到期验证与行情刷新永远不会运行。
            if not schedule_enabled and not background_tasks:
                self.stop()
                return
            self.stop()
            with self._analysis_process_lock:
                generation = self._analysis_generation
            scheduled_analysis = partial(
                self._start_analysis_watchdog,
                generation=generation,
            )
            times = normalize_schedule_times(
                getattr(config, "schedule_times", None),
                fallback_time=getattr(config, "schedule_time", "18:00"),
            )
            scheduler = Scheduler(
                schedule_time=getattr(config, "schedule_time", "18:00"),
                schedule_times=times,
                schedule_times_provider=self._current_times,
                register_signals=False,
            )
            if schedule_enabled:
                # 只有每日分析开启时才注册每日任务；否则调度线程仅服务后台维护。
                if run_immediately and self._run_immediately_in_background:
                    scheduler.set_daily_task(scheduled_analysis, run_immediately=False)
                else:
                    scheduler.set_daily_task(
                        scheduled_analysis,
                        run_immediately=run_immediately,
                    )
            for entry in background_tasks:
                scheduler.add_background_task(
                    entry["task"],
                    interval_seconds=entry["interval_seconds"],
                    run_immediately=entry.get("run_immediately", False),
                    name=entry.get("name"),
                )
            if run_immediately and self._run_immediately_in_background:
                self._run_in_background_thread(scheduled_analysis)
            thread = threading.Thread(
                target=scheduler.run,
                daemon=True,
                name="runtime-scheduler",
            )
            self._scheduler = scheduler
            self._thread = thread
            self._enabled = True
            thread.start()

    def stop(self) -> None:
        with self._lock:
            with self._analysis_process_lock:
                self._analysis_generation += 1
                process = self._analysis_process
            scheduler = self._scheduler
            if scheduler is not None:
                scheduler.stop()
            if process is not None:
                _terminate_analysis_process_tree(process)
            self._scheduler = None
            self._thread = None
            self._enabled = False

    def reconcile_from_config(
        self,
        *,
        run_immediately: bool = False,
        clear_enabled_override: bool = False,
    ) -> None:
        if clear_enabled_override:
            self._force_enabled = False
        if not self._owns_schedule:
            self.stop()
            return
        config = self._config_provider()
        # 每日分析或任一后台维护任务启用，都应保持调度线程运行。
        if self._is_schedule_enabled(config) or self._has_enabled_background_tasks(config):
            self.start(run_immediately=run_immediately)
        else:
            self.stop()

    def run_now(self) -> Dict[str, Any]:
        if not self._start_analysis_watchdog():
            return {
                "accepted": False,
                "running": True,
                "reason": "analysis_already_running",
            }
        return {"accepted": True, "running": True}

    def is_background_task_active(self, name: str) -> bool:
        """Return whether a named task is registered on the live scheduler."""
        with self._lock:
            scheduler = self._scheduler
            thread = self._thread
            if (
                not self._enabled
                or scheduler is None
                or thread is None
                or not thread.is_alive()
            ):
                return False
            return any(
                str(entry.get("name") or "") == name
                for entry in getattr(scheduler, "_background_tasks", [])
            )

    def status(self) -> Dict[str, Any]:
        scheduler = self._scheduler
        jobs = scheduler.schedule.get_jobs() if scheduler is not None else []
        next_run = None
        if jobs:
            next_run = min(job.next_run for job in jobs).isoformat()
        if scheduler is not None:
            schedule_times = list(getattr(scheduler, "schedule_times", []))
        else:
            try:
                schedule_times = self._current_times()
            except Exception:  # pragma: no cover - defensive status fallback
                schedule_times = []
        running = self._run_lock.locked()
        return {
            "enabled": self._enabled,
            "running": running,
            "schedule_times": schedule_times,
            "next_run_at": next_run,
            "last_run_at": self._last_run_at,
            "last_success_at": self._last_success_at,
            # Unlocked read: may briefly show the baseline timeout string
            # until _apply_timeout_partial_outcome finishes. See that method.
            "last_error": self._last_error,
            "last_skipped_at": self._last_skipped_at,
            "last_skip_reason": self._last_skip_reason,
        }
