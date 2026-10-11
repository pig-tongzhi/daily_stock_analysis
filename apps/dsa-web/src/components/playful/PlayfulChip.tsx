import type React from 'react';
import { cn } from '../../utils/cn';

/**
 * 圆角胶囊标签（对应 index.css 的 .playful-pill）。
 * 用于「命中率 63.2%」这类低强调的键值标签，语义颜色不参与涨跌含义。
 *
 * tone='default' 刻意不追加颜色工具类：`.playful-pill` 本身已经给了
 * `color: var(--playful-ink)`，再叠一层 text-* 只会制造同权重覆盖的隐患。
 */
export type PlayfulChipTone = 'default' | 'soft' | 'grape' | 'mint' | 'sun' | 'up' | 'down';

export type PlayfulChipSize = 'sm' | 'md';

const TONE_STYLES: Record<PlayfulChipTone, string> = {
  default: '',
  soft: 'text-[var(--playful-ink-soft)]',
  grape: 'text-[var(--playful-grape)]',
  mint: 'text-[var(--playful-mint)]',
  sun: 'text-[var(--playful-sun)]',
  up: 'text-[var(--playful-up)]',
  down: 'text-[var(--playful-down)]',
};

const SIZE_STYLES: Record<PlayfulChipSize, string> = {
  sm: 'px-2 py-0.5 text-[11px]',
  md: 'px-2.5 py-1 text-xs',
};

export interface PlayfulChipProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: PlayfulChipTone;
  size?: PlayfulChipSize;
  className?: string;
  children?: React.ReactNode;
}

export const PlayfulChip: React.FC<PlayfulChipProps> = ({
  tone = 'default',
  size = 'md',
  className = '',
  children,
  ...rest
}) => (
  <span
    className={cn('playful-pill font-bold', SIZE_STYLES[size], TONE_STYLES[tone], className)}
    {...rest}
  >
    {children}
  </span>
);

/**
 * 序号徽标：资讯列表左侧的 1/2/3/4 方块。
 * 与 PlayfulChip 分开是因为它是「位置」而不是「数值」，底色固定用浅蓝点缀色。
 */
export interface PlayfulRankBadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  className?: string;
  children?: React.ReactNode;
}

export const PlayfulRankBadge: React.FC<PlayfulRankBadgeProps> = ({
  className = '',
  children,
  ...rest
}) => (
  <span
    className={cn(
      'grid h-5 w-5 shrink-0 place-items-center rounded-lg bg-[var(--playful-tint-b)] text-[11px] font-extrabold text-[var(--playful-grape)]',
      className,
    )}
    {...rest}
  >
    {children}
  </span>
);
