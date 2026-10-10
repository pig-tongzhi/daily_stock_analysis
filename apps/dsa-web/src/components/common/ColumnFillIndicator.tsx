import type React from 'react';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { cn } from '../../utils/cn';
import { Tooltip } from './Tooltip';

/** 与后端 fill 字段结构兼容的最小契约，避免 common 层依赖具体页面 API 类型 */
export type ColumnFillSegment = {
  column: string;
  pct: number;
  empty: boolean;
};

interface ColumnFillIndicatorProps {
  segments: ColumnFillSegment[];
  className?: string;
}

/**
 * 列填充指示器：每列一个小竖条 + 全空列的显式字段名。
 *
 * 为什么不用纯百分比数字：一屏要扫十几张表、上百个字段，
 * 只有把「哪一列整列为空」变成一眼可见的色块和字段名，
 * 才能发现 industry 这类「表里有数据但该列从未写入」的问题。
 *
 * 逐列明细放在 Tooltip 里而不是每个色块各挂一个：llm_usage 有 50 列，
 * 50 个 Tooltip 会带来 50 套监听与 portal，代价远大于收益。
 */
export const ColumnFillIndicator: React.FC<ColumnFillIndicatorProps> = ({ segments, className = '' }) => {
  const { t } = useUiLanguage();
  const emptySegments = segments.filter((segment) => segment.empty);
  const filledCount = segments.length - emptySegments.length;

  if (!segments.length) {
    return <span className="text-xs text-secondary-text">{t('dataInventory.fill.noColumns')}</span>;
  }

  const summary = t('dataInventory.fill.summary', { filled: filledCount, total: segments.length });

  return (
    <div className={cn('space-y-1.5', className)} data-testid="column-fill-indicator">
      <div className="flex items-center gap-2">
        <Tooltip
          content={(
            <span className="flex w-56 flex-col gap-0.5">
              {segments.map((segment) => (
                <span key={segment.column} className="flex items-center justify-between gap-3">
                  <span className={cn('truncate font-mono', segment.empty ? 'text-danger' : 'text-foreground')}>
                    {segment.column}
                  </span>
                  <span className={cn('shrink-0', segment.empty ? 'text-danger' : 'text-secondary-text')}>
                    {`${segment.pct}%`}
                  </span>
                </span>
              ))}
            </span>
          )}
        >
          <span className="flex max-w-[220px] flex-wrap gap-px" role="img" aria-label={summary}>
            {segments.map((segment) => (
              <span
                key={segment.column}
                className={cn(
                  'h-3.5 w-1.5 rounded-[2px]',
                  segment.empty ? 'bg-danger' : segment.pct >= 100 ? 'bg-cyan/70' : 'bg-warning'
                )}
              />
            ))}
          </span>
        </Tooltip>
        <span className="whitespace-nowrap text-xs text-secondary-text">{summary}</span>
      </div>
      {emptySegments.length ? (
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-[11px] font-medium text-danger">
            {t('dataInventory.fill.emptyCount', { count: emptySegments.length })}
          </span>
          {emptySegments.map((segment) => (
            <span
              key={segment.column}
              data-testid="empty-column-flag"
              className="inline-flex items-center rounded border border-danger/30 bg-danger/10 px-1.5 py-0.5 font-mono text-[10px] text-danger"
            >
              {segment.column}
            </span>
          ))}
        </div>
      ) : (
        <span className="text-[11px] text-success">{t('dataInventory.fill.allFilled')}</span>
      )}
    </div>
  );
};
