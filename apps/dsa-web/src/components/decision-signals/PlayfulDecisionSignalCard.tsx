import type React from 'react';
import { PanelRightOpen } from 'lucide-react';
import { Badge } from '../common';
import { PlayfulCard, PlayfulChip, PlayfulStat } from '../playful';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiLanguage, UiTextKey } from '../../i18n/uiText';
import type { DecisionSignalItem, DecisionSignalStatus } from '../../types/decisionSignals';
import {
  buildDecisionActionLabelMap,
  getDecisionActionLabel,
  getDecisionActionTone,
  type DecisionActionTone,
} from '../../utils/decisionAction';
import { cn } from '../../utils/cn';
import { getDecisionSignalProfileLabel } from '../../utils/decisionSignalProfile';
import { parseDecisionSignalDate } from '../../utils/decisionSignalTime';
import {
  getDecisionSignalHorizonLabel,
  getDecisionSignalMarketLabel,
  getDecisionSignalMarketPhaseLabel,
  getDecisionSignalPlanQualityLabel,
} from '../../utils/decisionSignalLabels';

/**
 * 「卡通友好」版决策信号卡片。
 *
 * 为什么不直接改 `DecisionSignalDisplay.DecisionSignalCard`：
 * 那个文件同时被持仓页（PortfolioSignalSummary）引用，本轮改造的边界只允许
 * 新增文件、不改共享组件；因此这里按同一份数据契约重画一张卡片，
 * 页面切过来即可，接口字段、文案 key 与详情按钮的 aria-label 全部保持一致。
 *
 * 视觉取向：
 * - 动作（买入/持有/观望…）是每行最需要一眼看清的信息，所以放大成醒目的
 *   大号胶囊，语义色仍走既有 success/warning/danger 令牌（与首页 Badge 一致）；
 * - 评分/置信度/周期走 PlayfulStat，和首页的指标小块同一套观感；
 * - 风险、催化等长文本用低饱和的暖色小块承载，不再抢占动作的注意力。
 */

type BadgeVariant = 'default' | 'success' | 'warning' | 'danger' | 'info' | 'history';

const STATUS_LABEL_KEYS: Record<DecisionSignalStatus, UiTextKey> = {
  active: 'decisionSignals.active',
  expired: 'decisionSignals.expired',
  invalidated: 'decisionSignals.invalidated',
  closed: 'decisionSignals.closed',
  archived: 'decisionSignals.archived',
};

// 状态与动作的语义色沿用原卡片/首页的映射，避免同一状态在两个页面出现两种颜色。
const STATUS_VARIANTS: Record<DecisionSignalStatus, BadgeVariant> = {
  active: 'success',
  expired: 'warning',
  invalidated: 'danger',
  closed: 'default',
  archived: 'history',
};

/**
 * 动作胶囊的强调底色。刻意只用既有的语义色令牌做「淡底 + 同色描边」，
 * 不引入新的红绿含义（A 股红涨绿跌的约定由 --playful-up/down 单独承担）。
 * 动作不再走 Badge：它是每行最需要被看见的信息，需要比状态标签更重的字号与留白。
 */
const ACTION_EMPHASIS_STYLES: Record<DecisionActionTone, string> = {
  success: 'border-success/30 bg-success/10 text-success',
  warning: 'border-warning/30 bg-warning/10 text-warning',
  danger: 'border-danger/30 bg-danger/10 text-danger',
  default: 'border-border/60 bg-elevated/60 text-foreground',
};

const LOCALE_BY_LANGUAGE: Record<UiLanguage, string> = {
  zh: 'zh-CN',
  en: 'en-US',
};

function formatDateTime(value: string | null | undefined, language: UiLanguage): string {
  const date = parseDecisionSignalDate(value);
  if (!date) return '-';
  return new Intl.DateTimeFormat(LOCALE_BY_LANGUAGE[language], {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '-';
  return Number(value).toFixed(2).replace(/\.?0+$/, '');
}

function formatConfidence(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '-';
  const normalized = Math.abs(value) <= 1 ? value * 100 : value;
  return `${formatNumber(normalized)}%`;
}

function formatEntryRange(item: DecisionSignalItem): string {
  const hasLow = item.entryLow !== null && item.entryLow !== undefined;
  const hasHigh = item.entryHigh !== null && item.entryHigh !== undefined;
  if (hasLow && hasHigh) {
    return item.entryLow === item.entryHigh
      ? formatNumber(item.entryLow)
      : `${formatNumber(item.entryLow)} - ${formatNumber(item.entryHigh)}`;
  }
  if (hasLow) return formatNumber(item.entryLow);
  if (hasHigh) return formatNumber(item.entryHigh);
  return '-';
}

function hasDisplayValue(value: string): boolean {
  return value !== '-';
}

function getActionLabel(item: DecisionSignalItem, t: (key: UiTextKey) => string): string {
  return getDecisionActionLabel(
    item.action,
    item.actionLabel,
    null,
    t('decisionSignals.action'),
    buildDecisionActionLabelMap(t),
  ) ?? t('decisionSignals.action');
}

type PlayfulTextTone = 'default' | 'info' | 'warning' | 'danger';

// 长文本块只做「淡底 + 同色文字」的弱强调：它们是理由/风险等背景信息，
// 不该和动作胶囊抢同一层注意力。
const TEXT_TONE_STYLES: Record<PlayfulTextTone, string> = {
  default: 'border-[var(--playful-line)] bg-[var(--playful-surface-soft)] text-secondary-text',
  info: 'border-cyan/25 bg-cyan/10 text-cyan',
  warning: 'border-warning/25 bg-warning/10 text-warning',
  danger: 'border-danger/25 bg-danger/10 text-danger',
};

const PlayfulTextBlock: React.FC<{
  label: string;
  value?: string | null;
  tone?: PlayfulTextTone;
}> = ({ label, value, tone = 'default' }) => {
  const normalized = value?.trim();
  if (!normalized) return null;
  return (
    <div className={cn('rounded-2xl border px-3 py-2.5', TEXT_TONE_STYLES[tone])}>
      <p className="text-[11px] font-bold text-current/80">{label}</p>
      <p className="mt-1 line-clamp-2 text-sm leading-5 text-current">{normalized}</p>
    </div>
  );
};

export interface PlayfulDecisionSignalCardProps {
  item: DecisionSignalItem;
  onSelect?: (item: DecisionSignalItem) => void;
  selected?: boolean;
}

export const PlayfulDecisionSignalCard: React.FC<PlayfulDecisionSignalCardProps> = ({
  item,
  onSelect,
  selected = false,
}) => {
  const { language, t } = useUiLanguage();
  const actionLabel = getActionLabel(item, t);
  const actionTone = getDecisionActionTone(item.action, item.actionLabel, null);
  const profileLabel = getDecisionSignalProfileLabel(item, t);
  const interactive = Boolean(onSelect);
  const stockLabel = item.stockName || item.stockCode;
  const entryRange = formatEntryRange(item);
  const pricePlanItems = [
    { label: t('decisionSignals.entryRange'), value: entryRange, tone: 'default' as const },
    { label: t('decisionSignals.stopLoss'), value: formatNumber(item.stopLoss), tone: 'danger' as const },
    { label: t('decisionSignals.targetPrice'), value: formatNumber(item.targetPrice), tone: 'success' as const },
  ].filter((entry) => hasDisplayValue(entry.value));

  return (
    <PlayfulCard
      as="article"
      padding="md"
      className={cn(
        'flex h-full flex-col',
        interactive ? 'transition-colors hover:border-[var(--playful-grape)]' : '',
        selected ? 'ring-2 ring-[var(--playful-grape)]' : '',
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        {/* 动作胶囊：整张卡片里最需要被一眼看到的信息 */}
        <span
          className={cn(
            'inline-flex items-center rounded-2xl border px-3 py-1 text-lg font-extrabold tracking-wide',
            ACTION_EMPHASIS_STYLES[actionTone],
          )}
        >
          {actionLabel}
        </span>
        <Badge variant={STATUS_VARIANTS[item.status]}>{t(STATUS_LABEL_KEYS[item.status])}</Badge>
      </div>

      <div className="mt-3 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h3 className="text-base font-extrabold text-foreground">{stockLabel}</h3>
        <span className="font-mono text-xs playful-ink-soft">{item.stockCode}</span>
        <span className="ml-auto flex flex-wrap items-center gap-1.5">
          <PlayfulChip size="sm" tone="soft">
            {t('decisionSignals.profile')}: {profileLabel}
          </PlayfulChip>
          <PlayfulChip size="sm" tone="soft">
            {getDecisionSignalMarketLabel(item.market, t)}
          </PlayfulChip>
          {/* 创建时间单独成节点：它是这条信号的时间锚点，也便于测试与读屏精确定位 */}
          <span className="text-[11px] font-semibold playful-ink-soft">
            {formatDateTime(item.createdAt, language)}
          </span>
        </span>
      </div>

      {/* 固定三列放「评分 / 置信度 / 周期」：PlayfulStatGrid 在宽屏是 5 列，
          这里只有 3 个指标，用 5 列栅格会留出两个空位，所以直接写 3 列。 */}
      <div className="mt-3 grid grid-cols-3 gap-2">
        <PlayfulStat size="sm" label={t('decisionSignals.score')} value={formatNumber(item.score)} tone="ink" />
        <PlayfulStat
          size="sm"
          label={t('decisionSignals.confidence')}
          value={formatConfidence(item.confidence)}
          tone="grape"
        />
        <PlayfulStat
          size="sm"
          label={t('decisionSignals.horizon')}
          value={getDecisionSignalHorizonLabel(item.horizon, t)}
          tone="soft"
        />
      </div>

      {pricePlanItems.length > 0 ? (
        <div className="mt-3 grid gap-2 rounded-2xl border border-[var(--playful-line)] bg-[var(--playful-surface-soft)] px-3 py-2.5 sm:grid-cols-3">
          {pricePlanItems.map((entry) => (
            <div key={entry.label} className="min-w-0">
              <p className="truncate text-[11px] font-semibold playful-ink-soft">{entry.label}</p>
              <p
                className={cn(
                  'mt-1 truncate text-sm font-extrabold tabular-nums',
                  entry.tone === 'danger' ? 'text-danger' : entry.tone === 'success' ? 'text-success' : 'text-foreground',
                )}
              >
                {entry.value}
              </p>
            </div>
          ))}
        </div>
      ) : null}

      <div className="mt-3 grid gap-2">
        <PlayfulTextBlock label={t('decisionSignals.reason')} value={item.reason} />
        <PlayfulTextBlock label={t('decisionSignals.catalystSummary')} value={item.catalystSummary} tone="info" />
        <PlayfulTextBlock label={t('decisionSignals.watchConditions')} value={item.watchConditions} />
        <PlayfulTextBlock label={t('decisionSignals.riskSummary')} value={item.riskSummary} tone="warning" />
        <PlayfulTextBlock label={t('decisionSignals.invalidation')} value={item.invalidation} tone="danger" />
      </div>

      <div className="mt-3 flex flex-1 flex-wrap items-end gap-1.5 text-xs">
        <PlayfulChip size="sm" tone="soft">
          {t('decisionSignals.planQuality')}: {getDecisionSignalPlanQualityLabel(item.planQuality, t)}
        </PlayfulChip>
        <PlayfulChip size="sm" tone="soft">
          {t('decisionSignals.marketPhase')}: {getDecisionSignalMarketPhaseLabel(item.marketPhase, t)}
        </PlayfulChip>
        <PlayfulChip size="sm" tone="soft">
          {t('decisionSignals.expiresAt')}: {formatDateTime(item.expiresAt, language)}
        </PlayfulChip>
        {item.sourceReportId ? (
          <PlayfulChip size="sm" tone="soft">
            {t('decisionSignals.sourceReport')}: #{item.sourceReportId}
          </PlayfulChip>
        ) : null}
        {interactive ? (
          <button
            type="button"
            onClick={() => onSelect?.(item)}
            className="btn-secondary ml-auto inline-flex items-center gap-1.5 !px-3 !py-1.5 !text-xs"
            aria-label={t('decisionSignals.viewDetailsFor', { stock: stockLabel })}
          >
            <PanelRightOpen className="h-3.5 w-3.5" />
            {t('common.details')}
          </button>
        ) : null}
      </div>
    </PlayfulCard>
  );
};

export default PlayfulDecisionSignalCard;
