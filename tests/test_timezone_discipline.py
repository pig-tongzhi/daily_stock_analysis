"""时间基准纪律的机器护栏（2026-10-21 起）。

背景：这套代码曾经 6 张表写 UTC、15 张表写本地时间，而没有任何文档说明，
于是"本地 since 比 UTC created_at"这类错误会静默差 8 小时 —— 2026-10-20
的选股重复跑 4 轮（2 小时）就是这么来的。

本文件把两条纪律钉成测试：
  1. src/ 里不允许再用 datetime.now(timezone.utc) 取"现在/今天"
     （外部契约需要的 ISO 输出除外，走显式白名单 + 写明理由）
  2. 测试里不允许给 *_at 字段塞写死的绝对日期而不冻结时钟
     （否则窗口过滤类测试会随日历自然失效）
"""

from __future__ import annotations

import ast
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
TESTS = REPO / "tests"

# ── 纪律 1：src/ 里允许出现 datetime.now(timezone.utc) 的位置 ──────────────
#
# 只放"外部契约必须有 UTC"的地方：这些 ISO 字符串会交给 LLM provider / 外部
# 报表，带明确偏移量，不会与库里的本地时间混比。
# 新增条目必须在同一行写 `# tz-ok: <理由>`，否则测试会拒绝。
# 只保留「必须用 UTC」的文件：
#   data_capability_service — 立刻 astimezone() 转本地，实际输出是本地
#   search_service          — 按 dt.tzinfo 分支，两种都处理
#   core/pipeline           — 内部恢复基准，不落库
#   screening/snapshot      — 缓存文件元数据，解析时统一换算成本地
#   screening/hotspot       — 板块热度缓存元数据，同上
#   schemas/analysis_*      — 纯序列化，不参与比较
# 其余凡是「写进数据库」的都已改成北京时间（2026-10-21 审计）。
UTC_NOW_ALLOW = {
    "src/services/data_capability_service.py",
    "src/search_service.py",
    "src/core/pipeline.py",
    "src/services/screening/snapshot.py",
    "src/services/screening/hotspot.py",
    "src/schemas/analysis_context_pack.py",
    "src/services/data_inventory_service.py",
}

_UTC_PATTERNS = (
    r"datetime\.now\(timezone\.utc\)\.date\(\)",   # 用 UTC 取"今天"
    r"datetime\.now\(timezone\.utc\)\s*[-+]",      # 与本地时间做算术
    r"\(\s*datetime\.now\(timezone\.utc\)\s*[-+]",
)


def _python_files(root: Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_utc_now_outside_allowlist():
    """src/ 里除白名单外，不允许出现 datetime.now(timezone.utc)。"""
    offenders = []
    for path in _python_files(SRC):
        rel = path.relative_to(REPO).as_posix()
        text = path.read_text(encoding="utf-8")
        if "datetime.now(timezone.utc)" not in text:
            continue
        if rel in UTC_NOW_ALLOW:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if "datetime.now(timezone.utc)" in line and "# tz-ok:" not in line:
                offenders.append(f"{rel}:{i}: {line.strip()[:100]}")
    assert not offenders, (
        "src/ 里出现未申报的 datetime.now(timezone.utc)：\n  "
        + "\n  ".join(offenders)
        + "\n\n统一用 datetime.now()（北京时间）。若确属外部契约，"
        "加入 UTC_NOW_ALLOW 并在该行写 `# tz-ok: 理由`。"
    )


def test_no_dangerous_utc_date_or_arithmetic():
    """禁止用 UTC 取"今天"或与本地时间做算术 —— 会静默差 8 小时。"""
    offenders = []
    for path in _python_files(SRC):
        rel = path.relative_to(REPO).as_posix()
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if "# tz-ok:" in line:
                continue
            if any(re.search(pat, line) for pat in _UTC_PATTERNS):
                offenders.append(f"{rel}:{i}: {line.strip()[:100]}")
    assert not offenders, (
        "禁止用 UTC 取日期/做时间算术（会与本地时间差 8 小时）：\n  "
        + "\n  ".join(offenders)
        + "\n\n改用 datetime.now() 或 local_naive_now()。"
    )


# ── 纪律 2：测试里写死的 *_at 日期会随日历失效 ─────────────────────────────

# 冻结了时钟的测试文件（时间为固定值，写死日期是安全的）
CLOCK_FROZEN_OK = {
    "tests/test_screening_schedule.py",
    "tests/test_screening_pick_export.py",
    "tests/test_decision_signal_repo.py",
    "tests/test_decision_signal_service.py",
    "tests/test_decision_signal_api.py",
    "tests/test_screening_history.py",
}

# 只盯"会被窗口过滤命中"的字段：published_at / fetched_at。
# created_at / updated_at 大多是确定性断言（契约测试固定值），写死是合理的，
# 全量禁止会逼出一份巨大的白名单，反而失去意义。
_TIMESTAMP_KW = re.compile(r"\b(published_at|fetched_at)\s*=\s*datetime\(")


def test_no_hardcoded_timestamp_fixtures_without_frozen_clock():
    """测试里给 *_at 字段塞写死的绝对日期时，必须冻结时钟或显式标注。

    否则"只查最近 N 天"这类断言会随日历自然失效 ——
    2026-10-11 写的 `published_at=datetime(2026, 10, 9)` 对上 7 天窗口，
    到 10-17 就必然失败，属于典型的"时间炸弹"。
    """
    offenders = []
    for path in _python_files(TESTS):
        rel = path.relative_to(REPO).as_posix()
        if rel in CLOCK_FROZEN_OK or rel == "tests/test_timezone_discipline.py":
            continue
        text = path.read_text(encoding="utf-8")
        frozen = ("_freeze(" in text) or ("freeze_time" in text) or ("freezegun" in text)
        for i, line in enumerate(text.splitlines(), 1):
            if _TIMESTAMP_KW.search(line):
                if frozen or "time-bomb-ok" in line:
                    continue
                offenders.append(f"{rel}:{i}: {line.strip()[:100]}")
    assert not offenders, (
        "测试里给时间字段塞了写死的日期，且该文件没有冻结时钟：\n  "
        + "\n  ".join(offenders)
        + "\n\n改用 `datetime.now() - timedelta(days=1)` 这类相对时间；"
        "确需固定值时，把文件加入 CLOCK_FROZEN_OK 或在该行写 `# time-bomb-ok: 理由`。"
    )


def test_local_naive_now_is_really_local():
    """local_naive_now() 必须等于 datetime.now()，且是 naive。"""
    from src.storage import local_naive_now

    value = local_naive_now()
    assert value.tzinfo is None, "local_naive_now() 不该带时区"
    assert abs((value - datetime.now()).total_seconds()) < 5, "local_naive_now() 必须是本地时间"
