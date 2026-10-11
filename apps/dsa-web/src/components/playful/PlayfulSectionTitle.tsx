import type React from 'react';
import { cn } from '../../utils/cn';

/**
 * 区块标题：emoji/图标 + 标题 + 右侧次要信息（meta）。
 *
 * 首页原本叫 PlayfulCardTitle，是页内私有组件；抽出来后其它页面可以只写
 * `<PlayfulSectionTitle emoji="🎯" title=... meta=... />`，不用再记类名。
 * emoji 只做视觉标记、不承载语义，因此始终 aria-hidden；需要语义时用 `icon` 传图标。
 */
export interface PlayfulSectionTitleProps {
  /** 区块标记 emoji（与 icon 二选一）。 */
  emoji?: string;
  /** 图标节点，用于需要 outline / 统一线型的场景（与 emoji 二选一）。 */
  icon?: React.ReactNode;
  title: React.ReactNode;
  /** 右侧次要信息：时间、总数、统计口径等。 */
  meta?: React.ReactNode;
  /** 追加在标题右侧的自定义节点（Badge、按钮等）。 */
  children?: React.ReactNode;
  className?: string;
  /** 标题层级，默认 h3；整页级区块可传 h2。 */
  as?: 'h2' | 'h3' | 'h4';
}

export const PlayfulSectionTitle: React.FC<PlayfulSectionTitleProps> = ({
  emoji,
  icon,
  title,
  meta,
  children,
  className = '',
  as = 'h3',
}) => {
  const Tag = as;
  return (
    <div className={cn('flex items-center gap-2', className)}>
      {emoji ? <span className="text-base leading-none" aria-hidden="true">{emoji}</span> : null}
      {icon ? <span className="shrink-0 text-[var(--playful-grape)]" aria-hidden="true">{icon}</span> : null}
      <Tag className="text-sm font-semibold tracking-tight text-foreground">{title}</Tag>
      {meta ? <span className="ml-auto text-[11px] font-medium playful-ink-soft">{meta}</span> : null}
      {children}
    </div>
  );
};
