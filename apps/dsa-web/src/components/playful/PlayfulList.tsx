import type React from 'react';
import { cn } from '../../utils/cn';

/**
 * playful 列表：首页多张卡片都在用「虚线分隔的紧凑行」表达一组记录
 * （最近 AI 建议 / 最近分析），抽出来后各页面不用再手写
 * `border-b border-dashed playful-dashed ... last:border-b-0`。
 */
export interface PlayfulListProps extends React.HTMLAttributes<HTMLUListElement> {
  className?: string;
  children?: React.ReactNode;
}

export const PlayfulList: React.FC<PlayfulListProps> = ({ className = '', children, ...rest }) => (
  <ul className={cn('flex flex-col', className)} {...rest}>
    {children}
  </ul>
);

export interface PlayfulListItemProps extends React.LiHTMLAttributes<HTMLLIElement> {
  /**
   * 行密度。首页实际用到两种：
   * - compact：紧凑的「名称 + 元信息 + 右侧状态」行（最近 AI 建议 / 最近分析）；
   * - roomy：资讯那种带序号、内容更高的行。
   */
  density?: 'compact' | 'roomy';
  className?: string;
  children?: React.ReactNode;
}

const DENSITY_STYLES: Record<'compact' | 'roomy', string> = {
  compact: 'items-center gap-2 py-1.5 text-xs',
  roomy: 'gap-2.5 py-2',
};

export const PlayfulListItem: React.FC<PlayfulListItemProps> = ({
  density = 'compact',
  className = '',
  children,
  ...rest
}) => (
  <li
    className={cn(
      'flex border-b border-dashed playful-dashed last:border-b-0',
      DENSITY_STYLES[density],
      className,
    )}
    {...rest}
  >
    {children}
  </li>
);
