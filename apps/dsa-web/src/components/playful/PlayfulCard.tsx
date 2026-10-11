import type React from 'react';
import { cn } from '../../utils/cn';

/**
 * 卡通友好的圆角卡片（对应 index.css 里的 .playful-card / .playful-card-soft / .playful-hero）。
 *
 * 三个变体的分工：
 * - default：普通内容卡，底色仍是既有中性 card 色，暖色只出现在边框与「贴纸感」阴影上；
 * - soft：卡片里的次级小块（自选股 chip 等），底色是暖色 surface；
 * - hero：页面顶部问候条，用三块低饱和点缀色拼暖渐变。
 *
 * 刻意不提供 variant="gradient" 之类的花哨选项：暖色是点缀，不是卡片底色。
 */
export type PlayfulCardVariant = 'default' | 'soft' | 'hero';
export type PlayfulCardPadding = 'none' | 'sm' | 'md' | 'lg';

const VARIANT_STYLES: Record<PlayfulCardVariant, string> = {
  default: 'playful-card',
  soft: 'playful-card-soft',
  hero: 'playful-hero',
};

const PADDING_STYLES: Record<PlayfulCardPadding, string> = {
  none: '',
  sm: 'p-3',
  md: 'p-4',
  lg: 'p-5',
};

export interface PlayfulCardProps extends React.HTMLAttributes<HTMLElement> {
  /** 语义化标签：列表项用 li，区块用 section，其余保持 div。 */
  as?: 'div' | 'section' | 'article' | 'li';
  variant?: PlayfulCardVariant;
  padding?: PlayfulCardPadding;
  className?: string;
  children?: React.ReactNode;
}

export const PlayfulCard: React.FC<PlayfulCardProps> = ({
  as = 'div',
  variant = 'default',
  padding = 'md',
  className = '',
  children,
  ...rest
}) => {
  const Tag = as as React.ElementType;
  return (
    <Tag className={cn(VARIANT_STYLES[variant], PADDING_STYLES[padding], className)} {...rest}>
      {children}
    </Tag>
  );
};
