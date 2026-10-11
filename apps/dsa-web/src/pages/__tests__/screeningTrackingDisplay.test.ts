import { describe, expect, it } from 'vitest';
import { UI_TEXT, type UiLanguage, type UiTextKey } from '../../i18n/uiText';
import {
  benchmarkIndexLabelKey,
  formatRatePercent,
  formatSignedPercent,
  trackingBenchmarkReasonKey,
  trackingReasonKey,
} from '../screeningTrackingDisplay';

const LANGUAGES: UiLanguage[] = ['zh', 'en'];

const expectTranslatedInEveryLanguage = (key: UiTextKey) => {
  for (const language of LANGUAGES) {
    const value = UI_TEXT[language][key];
    expect(value, `${key} missing for ${language}`).toBeTruthy();
    expect(typeof value).toBe('string');
  }
};

describe('screeningTrackingDisplay reason-code contract', () => {
  // 原因码是后端契约：新增一个码就必须同时补上 zh/en 文案，否则用户会看到 key。
  it.each([
    ['missing_pick_price', 'screening.tracking.reason.missing_pick_price'],
    ['unknown_identity', 'screening.tracking.reason.unknown_identity'],
    ['no_daily_rows', 'screening.tracking.reason.no_daily_rows'],
    ['no_forward_bars', 'screening.tracking.reason.no_forward_bars'],
    ['something_new', 'screening.tracking.reason.generic'],
    [null, 'screening.tracking.reason.generic'],
  ])('maps pick reason %s', (reason, expected) => {
    const key = trackingReasonKey(reason as string | null);
    expect(key).toBe(expected);
    expectTranslatedInEveryLanguage(key);
  });

  it.each([
    ['market_snapshot_is_single_date_cross_section'],
    ['no_index_and_no_multi_date_snapshot'],
    ['no_benchmark_series'],
    ['benchmark_probe_failed'],
    ['index_series_found'],
    ['benchmark_series_missing'],
    ['benchmark_window_not_covered'],
    ['benchmark_disabled'],
    ['pick_untrackable'],
  ])('maps benchmark reason %s to an existing zh+en key', (reason) => {
    const key = trackingBenchmarkReasonKey(reason);
    // 原因码不能原样变成 i18n key 之外的东西：key 必须以稳定的前缀开头。
    expect(key.startsWith('screening.tracking.benchmark.reason.')).toBe(true);
    expect(key).toBe(`screening.tracking.benchmark.reason.${reason}`);
    expectTranslatedInEveryLanguage(key);
  });

  it('falls back to no_benchmark_series for an unknown benchmark reason', () => {
    const key = trackingBenchmarkReasonKey('totally_unknown_code');
    expect(key).toBe('screening.tracking.benchmark.reason.no_benchmark_series');
    expectTranslatedInEveryLanguage(key);
  });
});

describe('screeningTrackingDisplay index labels', () => {
  it.each([
    ['sh000300', 'screening.tracking.benchmark.index.sh000300'],
    ['sh000905', 'screening.tracking.benchmark.index.sh000905'],
    ['sh000852', 'screening.tracking.benchmark.index.sh000852'],
    ['SH000300', 'screening.tracking.benchmark.index.sh000300'],
  ])('labels %s', (code, expected) => {
    const key = benchmarkIndexLabelKey(code);
    expect(key).toBe(expected);
    expectTranslatedInEveryLanguage(key as UiTextKey);
  });

  it('returns null for an unlabelled index so the raw code is shown instead of a guess', () => {
    expect(benchmarkIndexLabelKey('sz399001')).toBeNull();
    expect(benchmarkIndexLabelKey(null)).toBeNull();
    expect(benchmarkIndexLabelKey('')).toBeNull();
  });

  it('labels the three built-in benchmarks', () => {
    expect(benchmarkIndexLabelKey('sh000300')).toBe('screening.tracking.benchmark.index.sh000300');
    expect(benchmarkIndexLabelKey('sh000905')).toBe('screening.tracking.benchmark.index.sh000905');
    expect(benchmarkIndexLabelKey('sh000852')).toBe('screening.tracking.benchmark.index.sh000852');
  });
});

describe('screeningTrackingDisplay number formatting', () => {
  it('never renders a missing excess as 0', () => {
    // "没有基准" 与 "与基准持平" 必须长得不一样。
    expect(formatSignedPercent(null)).toBe('-');
    expect(formatSignedPercent(undefined)).toBe('-');
    expect(formatSignedPercent(Number.NaN)).toBe('-');
    expect(formatSignedPercent(0)).toBe('0.00%');
    expect(formatSignedPercent(-4.36)).toBe('-4.36%');
    expect(formatSignedPercent(1.32)).toBe('+1.32%');
  });

  it('formats a rate, and a missing rate as a dash rather than 0%', () => {
    expect(formatRatePercent(null)).toBe('-');
    expect(formatRatePercent(undefined)).toBe('-');
    expect(formatRatePercent(0)).toBe('0%');
    expect(formatRatePercent(0.8974)).toBe('90%');
  });
});
