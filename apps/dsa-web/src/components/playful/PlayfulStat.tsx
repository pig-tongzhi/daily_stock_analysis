import type React from 'react';
import { cn } from '../../utils/cn';

/**
 * 指标小块（首页叫 StatChip / MiniMetric）。
 *
 * 两档尺寸对应首页两种真实用法：
 * - md：问候条里的主指标 chip（大字号数字）；
 * - sm：卡片底部的 5 列小指标（小字号数字）。
 *
 * 颜色只走 `tone` 枚举 + playful 令牌，并且刻意保留 A 股「红涨绿跌」的
 * up/down 语义（--playful-up / --playful-down），避免调用方再随手写死颜色。
 */
export type PlayfulStatTone =
  | 'default'
  | 'ink'
  | 'soft'
  | 'grape'
  | 'mint'
  | 'sun'
  | 'up'
  | 'down';

export type PlayfulStatSize = 'sm' | 'md';

const TONE_STYLES: Record<PlayfulStatTone, string> = {
  default: 'text-foreground',
  ink: 'text-[var(--playful-ink)]',
  soft: 'text-[var(--playful-ink-soft)]',
  grape: 'text-[var(--playful-grape)]',
  mint: 'text-[var(--playful-mint)]',
  sun: 'text-[var(--playful-sun)]',
  up: 'text-[var(--playful-up)]',
  down: 'text-[var(--playful-down)]',
};

const SIZE_STYLES: Record<PlayfulStatSize, { wrapper: string; value: string; label: string }> = {
  md: {
    wrapper: 'min-w-[4.5rem] px-3 py-2 text-center',
    value: 'block text-lg font-extrabold leading-tight tabular-nums',
    label: 'text-[11px] font-semibold playful-ink-soft',
  },
  sm: {
    wrapper: 'px-1.5 py-1.5 text-center',
    value: 'block text-sm font-extrabold tabular-nums',
    label: 'block text-[10px] font-semibold leading-tight playful-ink-soft',
  },
};

export interface PlayfulStatProps extends React.HTMLAttributes<HTMLDivElement> {
  label: React.ReactNode;
  value: React.ReactNode;
  tone?: PlayfulStatTone;
  size?: PlayfulStatSize;
  className?: string;
}

export const PlayfulStat: React.FC<PlayfulStatProps> = ({
  label,
  value,
  tone = 'default',
  size = 'md',
  className = '',
  ...rest
}) => {
  const styles = SIZE_STYLES[size];
  return (
    <div className={cn('playful-chip', styles.wrapper, className)} {...rest}>
      <b className={cn(styles.value, TONE_STYLES[tone])}>{value}</b>
      <span className={styles.label}>{label}</span>
    </div>
  );
};

export interface PlayfulStatGridProps extends React.HTMLAttributes<HTMLDivElement> {
  className?: string;
  children?: React.ReactNode;
}

/**
 * 指标小块的自适应栅格：窄屏 2 列 / 中屏 3 列 / 宽屏 5 列。
 * 首页实测过 flex-wrap 会把第 5 个指标挤到单独一行（看起来像漏了一项），
 * 所以这里固定成栅格而不是 wrap。
 */
export const PlayfulStatGrid: React.FC<PlayfulStatGridProps> = ({
  className = '',
  children,
  ...rest
}) => (
  <div className={cn('grid grid-cols-2 gap-1.5 sm:grid-cols-3 xl:grid-cols-5', className)} {...rest}>
    {children}
  </div>
);
