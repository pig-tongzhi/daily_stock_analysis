import type React from 'react';
import { cn } from '../../utils/cn';

/**
 * playful 进度轨道（对应 index.css 的 .playful-track）。
 * 首页用它画恐惧贪婪指数条和选股策略条形图；两者结构完全一样，
 * 只有高度、色调和宽度不同，所以统一成一个组件。
 */
export type PlayfulTrackTone = 'grape' | 'mint' | 'sun' | 'up' | 'down' | 'neutral';
export type PlayfulTrackSize = 'sm' | 'md' | 'lg';

const TONE_STYLES: Record<PlayfulTrackTone, string> = {
  grape: 'bg-[var(--playful-grape)]',
  mint: 'bg-[var(--playful-mint)]',
  sun: 'bg-[var(--playful-sun)]',
  up: 'bg-[var(--playful-up)]',
  down: 'bg-[var(--playful-down)]',
  neutral: 'bg-[var(--playful-ink-soft)]',
};

const SIZE_STYLES: Record<PlayfulTrackSize, string> = {
  sm: 'h-2',
  md: 'h-4',
  lg: 'h-6',
};

export interface PlayfulTrackProps extends React.HTMLAttributes<HTMLElement> {
  /** 进度百分比，超出 0~100 会被夹住，避免条形图溢出圆角。 */
  value: number;
  tone?: PlayfulTrackTone;
  size?: PlayfulTrackSize;
  /** 轨道节点标签：默认 span（首页某处在 span 内），需要块级时传 div。 */
  as?: 'span' | 'div';
  /** 进度条本身的最小宽度百分比，防止极小值看起来像没渲染。 */
  minValue?: number;
  className?: string;
}

export const PlayfulTrack: React.FC<PlayfulTrackProps> = ({
  value,
  tone = 'grape',
  size = 'sm',
  as = 'span',
  minValue = 0,
  className = '',
  ...rest
}) => {
  const Tag = as as React.ElementType;
  const clamped = Math.max(minValue, Math.min(100, Number.isFinite(value) ? value : 0));
  return (
    <Tag
      className={cn('playful-track overflow-hidden rounded-full', SIZE_STYLES[size], className)}
      {...rest}
    >
      <span
        className={cn('block h-full rounded-full', TONE_STYLES[tone])}
        style={{ width: `${clamped}%` }}
      />
    </Tag>
  );
};
