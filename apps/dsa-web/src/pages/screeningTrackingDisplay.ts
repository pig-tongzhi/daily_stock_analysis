import type { UiTextKey } from '../i18n/uiText';

/**
 * 复盘区块的原因码 → i18n key 映射。
 *
 * 为什么单独一个模块：原因码是前后端之间的契约（后端只回机器码，人话在前端），
 * 而 `toCamelCase` 会深度改写对象键 —— 一旦哪天有人把它们塞回 map，`no_daily_rows`
 * 会静默变成 `noDailyRows`。把这些映射集中在一处并加测试，比散在 2400 行的页面里
 * 更容易发现契约漂移。
 */

/** 入选本身拿不到前瞻收益的原因码。 */
export function trackingReasonKey(reason: string | null | undefined): UiTextKey {
  switch (reason) {
    case 'missing_pick_price':
      return 'screening.tracking.reason.missing_pick_price';
    case 'unknown_identity':
      return 'screening.tracking.reason.unknown_identity';
    case 'no_daily_rows':
      return 'screening.tracking.reason.no_daily_rows';
    case 'no_forward_bars':
      return 'screening.tracking.reason.no_forward_bars';
    default:
      return 'screening.tracking.reason.generic';
  }
}

/**
 * 基准拿不到的原因码。
 *
 * 四种分开是刻意的：用户下一步动作不同 ——
 * benchmark_series_missing → 去抓指数；benchmark_window_not_covered → 补更早/更新的指数 K 线；
 * pick_untrackable → 先让入选自己可复盘；benchmark_disabled → 去开开关。
 */
export function trackingBenchmarkReasonKey(reason: string | null | undefined): UiTextKey {
  switch (reason) {
    case 'market_snapshot_is_single_date_cross_section':
      return 'screening.tracking.benchmark.reason.market_snapshot_is_single_date_cross_section';
    case 'no_index_and_no_multi_date_snapshot':
      return 'screening.tracking.benchmark.reason.no_index_and_no_multi_date_snapshot';
    case 'benchmark_probe_failed':
      return 'screening.tracking.benchmark.reason.benchmark_probe_failed';
    case 'index_series_found':
      return 'screening.tracking.benchmark.reason.index_series_found';
    case 'benchmark_series_missing':
      return 'screening.tracking.benchmark.reason.benchmark_series_missing';
    case 'benchmark_window_not_covered':
      return 'screening.tracking.benchmark.reason.benchmark_window_not_covered';
    case 'benchmark_disabled':
      return 'screening.tracking.benchmark.reason.benchmark_disabled';
    case 'pick_untrackable':
      return 'screening.tracking.benchmark.reason.pick_untrackable';
    default:
      return 'screening.tracking.benchmark.reason.no_benchmark_series';
  }
}

/**
 * 基准指数的中文/英文名。返回 null 表示没有内置译名，调用方直接显示代码本身 ——
 * 宁可显示 `sh000300` 也不能显示一个猜错的名字。
 */
export function benchmarkIndexLabelKey(code: string | null | undefined): UiTextKey | null {
  switch ((code || '').trim().toLowerCase()) {
    case 'sh000300':
      return 'screening.tracking.benchmark.index.sh000300';
    case 'sh000905':
      return 'screening.tracking.benchmark.index.sh000905';
    case 'sh000852':
      return 'screening.tracking.benchmark.index.sh000852';
    default:
      return null;
  }
}

/** 带符号的百分比；null/NaN 一律返回 '-'，不用 0 冒充"持平"。 */
export function formatSignedPercent(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(Number(value))) {
    return '-';
  }
  const number = Number(value);
  return `${number > 0 ? '+' : ''}${number.toFixed(digits)}%`;
}

/** 比例（0~1）转百分比；null 返回 '-'，不显示成 0%。 */
export function formatRatePercent(value: number | null | undefined, digits = 0): string {
  if (value == null || Number.isNaN(Number(value))) {
    return '-';
  }
  return `${(Number(value) * 100).toFixed(digits)}%`;
}
