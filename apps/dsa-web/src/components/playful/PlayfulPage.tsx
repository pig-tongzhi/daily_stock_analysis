import type React from 'react';
import { cn } from '../../utils/cn';

/**
 * 卡通友好（playful）页面骨架。
 *
 * 首页把 `.playful-page` 暖色底直接写在自己的根节点上，其它页面要沿用同一套风格
 * 时不应该再各自复制这段类名。`bare` 用于「页面自己已经控制布局」的场景
 * （例如首页那种整屏 flex 工作区），此时只挂暖色底，不套居中容器。
 */
export interface PlayfulPageProps extends React.HTMLAttributes<HTMLElement> {
  /** true：只应用 playful 底色，不再套 AppPage 的居中/内边距容器。 */
  bare?: boolean;
  className?: string;
  children?: React.ReactNode;
}

/** 与 AppPage 保持一致的容器尺寸，避免同一套页面在暖色底上左右错位。 */
const CONTAINED_STYLES = 'mx-auto min-h-full w-full max-w-7xl px-4 pb-8 pt-4 md:px-6 lg:px-8';

export const PlayfulPage: React.FC<PlayfulPageProps> = ({
  bare = false,
  className = '',
  children,
  ...rest
}) => (
  <div className={cn('playful-page', bare ? '' : CONTAINED_STYLES, className)} {...rest}>
    {children}
  </div>
);

export interface PlayfulSectionProps extends React.HTMLAttributes<HTMLElement> {
  className?: string;
  children?: React.ReactNode;
}

/**
 * 一屏之内的一组卡片。首页的看板是把多张 playful 卡片纵向堆叠，
 * 这里固定「纵向 + 统一间距」，页面不需要再重复 flex/gap 组合。
 */
export const PlayfulSection: React.FC<PlayfulSectionProps> = ({
  className = '',
  children,
  ...rest
}) => (
  <section className={cn('flex flex-col gap-4', className)} {...rest}>
    {children}
  </section>
);
