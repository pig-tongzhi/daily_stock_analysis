import type React from 'react';
import { cn } from '../../utils/cn';
import { EmptyState } from '../common/EmptyState';

/**
 * playful 语气的空态。
 *
 * 首页的看板卡片用的一直是公共 EmptyState（虚线边 + 居中图标/标题/说明），
 * 抽这一层不是再造一个空态组件，而是把「playful 卡片里该怎么摆空态」固化下来，
 * 避免每个页面各自拼一遍 className，也避免有人把空态换成纯色卡片、
 * 破坏「暖色只做点缀」的克制取向。
 *
 * 需要更暖的表面时传 `className="playful-card"` 即可，其余保持项目既有观感。
 */
export interface PlayfulEmptyStateProps {
  title: string;
  description?: string;
  icon?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}

export const PlayfulEmptyState: React.FC<PlayfulEmptyStateProps> = ({
  title,
  description,
  icon,
  action,
  className,
}) => (
  <EmptyState
    title={title}
    description={description}
    icon={icon}
    action={action}
    className={cn('border-dashed', className)}
  />
);
