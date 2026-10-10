import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Activity, History, Newspaper, Star } from 'lucide-react';
import { decisionSignalsApi } from '../../api/decisionSignals';
import { getParsedApiError, type ParsedApiError } from '../../api/error';
import { historyApi } from '../../api/history';
import { fetchIntelligenceItems } from '../../api/intelligence';
import { systemConfigApi } from '../../api/systemConfig';
import { ApiErrorAlert, Badge, Card, EmptyState, Loading, StatCard } from '../common';
import { DashboardPanelHeader } from '../dashboard';
import { useStockIndex } from '../../hooks/useStockIndex';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiLanguage, UiTextKey } from '../../i18n/uiText';
import type { DecisionAction } from '../../types/analysis';
import type { DecisionSignalStatus } from '../../types/decisionSignals';
import {
  buildDecisionActionLabelMap,
  getDecisionActionLabel,
  getDecisionActionTone,
  type DecisionActionTone,
} from '../../utils/decisionAction';

type BadgeVariant = NonNullable<React.ComponentProps<typeof Badge>['variant']>;

/**
 * 首页看板每次挂载都会真实请求四个接口，卡片数量固定，这里刻意不做懒加载，
 * 保证“打开首页即可见数据”的诉求不被折叠/分页稀释。
 */
const RECENT_ITEMS_LIMIT = 5;

const STATUS_LABEL_KEYS: Record<DecisionSignalStatus, UiTextKey> = {
  active: 'decisionSignals.active',
  expired: 'decisionSignals.expired',
  invalidated: 'decisionSignals.invalidated',
  closed: 'decisionSignals.closed',
  archived: 'decisionSignals.archived',
};

// 复用决策信号页已有的语义色，避免同一个状态在首页和信号页出现两种颜色。
const STATUS_BADGE_VARIANTS: Record<DecisionSignalStatus, BadgeVariant> = {
  active: 'success',
  expired: 'warning',
  invalidated: 'danger',
  closed: 'default',
  archived: 'history',
};

const ACTION_BADGE_VARIANTS: Record<DecisionActionTone, BadgeVariant> = {
  success: 'success',
  warning: 'warning',
  danger: 'danger',
  default: 'default',
};

/**
 * 共享的 `utils/format` 固定按 zh-CN 输出，英文界面下会出现中文日期格式。
 * 这里按当前界面语言选 locale，与资讯页、信号卡片的做法保持一致。
 */
function formatDashboardTime(value: string | null | undefined, language: UiLanguage): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(language === 'en' ? 'en-US' : 'zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

function formatPercent(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(1)}%` : '—';
}

interface DashboardResource<T> {
  data: T | null;
  loading: boolean;
  error: ParsedApiError | null;
  reload: () => void;
}

interface DashboardResourceState<T> {
  data: T | null;
  error: ParsedApiError | null;
  /** 是否已有一次请求落地；用它区分“还没返回”和“返回了但数据为空”。 */
  settled: boolean;
}

/**
 * 每张卡片各自取数：任意一个接口失败只影响它自己那张卡，
 * 其余卡片仍能展示已经拿到的真实数据；失败必须落到 error 上，
 * 交给 ApiErrorAlert 呈现，避免出现静默留白的“空卡片”。
 *
 * 状态只在请求回调里更新，不在 effect 体内同步 setState，避免级联渲染。
 */
function useDashboardResource<T>(load: () => Promise<T>): DashboardResource<T> {
  const [state, setState] = useState<DashboardResourceState<T>>({
    data: null,
    error: null,
    settled: false,
  });
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    load()
      .then((result) => {
        if (cancelled) return;
        setState({ data: result, error: null, settled: true });
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        setState({ data: null, error: getParsedApiError(cause), settled: true });
      });
    return () => {
      cancelled = true;
    };
  }, [load, reloadToken]);

  const reload = useCallback(() => {
    // 重试时先清掉错误并回到未落地状态，卡片立刻显示 loading 而不是继续展示旧错误。
    setState({ data: null, error: null, settled: false });
    setReloadToken((token) => token + 1);
  }, []);

  return {
    data: state.data,
    loading: !state.settled && state.error === null,
    error: state.error,
    reload,
  };
}

const ResourceError: React.FC<{ error: ParsedApiError; onRetry: () => void }> = ({ error, onRetry }) => {
  const { t } = useUiLanguage();
  return (
    <ApiErrorAlert
      error={error}
      actionLabel={t('homeDashboard.retry')}
      onAction={onRetry}
    />
  );
};

interface DashboardListItem {
  key: React.Key;
  title: React.ReactNode;
  meta: React.ReactNode;
  trailing?: React.ReactNode;
}

const DashboardList: React.FC<{ items: DashboardListItem[] }> = ({ items }) => (
  <ul className="divide-y divide-border/40">
    {items.map((item) => (
      <li key={item.key} className="flex items-start justify-between gap-3 py-2">
        <div className="min-w-0">
          <div className="text-sm font-medium leading-relaxed text-foreground">{item.title}</div>
          <div className="mt-0.5 text-xs text-secondary-text">{item.meta}</div>
        </div>
        {item.trailing ? <div className="flex shrink-0 items-center gap-1.5">{item.trailing}</div> : null}
      </li>
    ))}
  </ul>
);

const SubSectionTitle: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <p className="label-uppercase mb-1">{children}</p>
);

interface WatchlistCardProps {
  /** 首页已经通过 useWatchlist 拉取过的自选股代码；传入即复用，不再重复请求同一接口。 */
  codes?: string[];
  /** 外部来源的加载态（看板独立使用时由自己的请求决定）。 */
  loading?: boolean;
}

const WatchlistCard: React.FC<WatchlistCardProps> = ({ codes, loading }) => {
  const { t, language } = useUiLanguage();
  // 首页挂载看板时会传 codes，此时不再打第二个 /api/v1/stocks/watchlist 请求：
  // 同一接口重复请求既浪费，也会打乱父组件按顺序返回的 mock/缓存语义。
  const external = codes !== undefined;
  const load = useCallback(
    () => (external ? Promise.resolve(codes as string[]) : systemConfigApi.getWatchlist()),
    [codes, external],
  );
  const watchlist = useDashboardResource<string[]>(load);

  // 自选股接口只返回代码，名称需要本地股票索引补全；索引还没到位或查不到时回退到原始代码。
  const { index: stockIndex } = useStockIndex();
  const nameByCode = useMemo(() => {
    const map = new Map<string, string>();
    for (const item of stockIndex) {
      const name = language === 'en' ? item.nameEn || item.nameZh : item.nameZh;
      if (!name) continue;
      const keys = [item.displayCode, item.canonicalCode, ...(item.aliases ?? [])];
      for (const key of keys) {
        if (!key) continue;
        const normalized = key.toUpperCase();
        if (!map.has(normalized)) {
          map.set(normalized, name);
        }
      }
    }
    return map;
  }, [stockIndex, language]);

  const values = external ? codes : watchlist.data ?? [];
  const pending = external ? Boolean(loading) : watchlist.loading;
  const failure = external ? null : watchlist.error;

  return (
    <Card title={t('homeDashboard.watchlist.title')} padding="md">
      {pending ? (
        <Loading />
      ) : failure ? (
        <ResourceError error={failure} onRetry={watchlist.reload} />
      ) : values.length === 0 ? (
        <EmptyState
          icon={<Star className="h-6 w-6" />}
          title={t('homeDashboard.watchlist.emptyTitle')}
          description={t('homeDashboard.watchlist.emptyDescription')}
        />
      ) : (
        <>
          <p className="text-xs text-secondary-text">
            {t('homeDashboard.watchlist.count', { count: values.length })}
          </p>
          <ul className="mt-3 flex flex-wrap gap-2">
            {values.map((code) => (
              <li key={code}>
                <Badge variant="info" size="md">
                  {nameByCode.get(code.toUpperCase()) || code}
                </Badge>
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
};

export interface HomeDashboardProps {
  /** 首页已经通过 useWatchlist 拉取过的自选股代码；传入即复用，不再重复请求同一接口。 */
  watchlistCodes?: string[];
  /** 首页自选股的加载态，用于驱动自选股卡片的 Loading（看板独立使用时由自身请求决定）。 */
  watchlistLoading?: boolean;
}

export const HomeDashboard: React.FC<HomeDashboardProps> = ({ watchlistCodes, watchlistLoading }) => {
  const { t, language } = useUiLanguage();

  const loadStats = useCallback(() => decisionSignalsApi.getOutcomeStats(), []);
  const loadSignals = useCallback(() => decisionSignalsApi.list({ page: 1, pageSize: RECENT_ITEMS_LIMIT }), []);
  const loadNews = useCallback(
    () => fetchIntelligenceItems({ page: 1, page_size: RECENT_ITEMS_LIMIT }),
    [],
  );
  const loadHistory = useCallback(
    () => historyApi.getList({ page: 1, limit: RECENT_ITEMS_LIMIT }),
    [],
  );

  const stats = useDashboardResource(loadStats);
  const signals = useDashboardResource(loadSignals);
  const news = useDashboardResource(loadNews);
  const history = useDashboardResource(loadHistory);

  const actionLabels = useMemo(() => buildDecisionActionLabelMap(t), [t]);

  const renderActionBadge = (action: DecisionAction) => (
    <Badge variant={ACTION_BADGE_VARIANTS[getDecisionActionTone(action, null, null)]}>
      {getDecisionActionLabel(action, null, null, '—', actionLabels) ?? '—'}
    </Badge>
  );

  const signalItems = signals.data?.items ?? [];
  const newsItems = news.data?.items ?? [];
  const historyItems = history.data?.items ?? [];

  return (
    <section className="mb-4" data-testid="home-dashboard-cards">
      <DashboardPanelHeader
        eyebrow={t('homeDashboard.eyebrow')}
        title={t('homeDashboard.title')}
      />

      <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
        {/* AI 建议概览：命中率来自 outcomes/stats，列表来自 signals 列表接口 */}
        <Card title={t('homeDashboard.aiSignals.title')} padding="md">
          {stats.loading ? (
            <Loading />
          ) : stats.error ? (
            <ResourceError error={stats.error} onRetry={stats.reload} />
          ) : !stats.data || stats.data.total === 0 ? (
            <EmptyState
              icon={<Activity className="h-6 w-6" />}
              title={t('homeDashboard.aiSignals.emptyTitle')}
              description={t('homeDashboard.aiSignals.emptyDescription')}
            />
          ) : (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
              <StatCard
                label={t('homeDashboard.aiSignals.hitRate')}
                value={formatPercent(stats.data.hitRatePct)}
                hint={t('homeDashboard.aiSignals.hitMiss', {
                  hit: stats.data.hit,
                  miss: stats.data.miss,
                })}
                tone="success"
              />
              <StatCard
                label={t('homeDashboard.aiSignals.completed')}
                value={stats.data.completed}
                hint={t('homeDashboard.aiSignals.completedHint', { total: stats.data.total })}
                tone="primary"
              />
              <StatCard
                label={t('homeDashboard.aiSignals.unable')}
                value={stats.data.unable}
                hint={t('homeDashboard.aiSignals.unableHint')}
                tone="warning"
              />
            </div>
          )}

          <div className="mt-4 border-t border-border/40 pt-3">
            <SubSectionTitle>{t('homeDashboard.aiSignals.recent')}</SubSectionTitle>
            {signals.loading ? (
              <Loading />
            ) : signals.error ? (
              <ResourceError error={signals.error} onRetry={signals.reload} />
            ) : signalItems.length === 0 ? (
              <p className="py-2 text-sm text-secondary-text">{t('homeDashboard.aiSignals.noRecent')}</p>
            ) : (
              <DashboardList
                items={signalItems.map((item) => ({
                  key: item.id,
                  title: item.stockName || item.stockCode,
                  meta: `${item.stockCode} · ${formatDashboardTime(item.createdAt, language)}`,
                  trailing: (
                    <>
                      {renderActionBadge(item.action)}
                      <Badge variant={STATUS_BADGE_VARIANTS[item.status]}>
                        {t(STATUS_LABEL_KEYS[item.status])}
                      </Badge>
                    </>
                  ),
                }))}
              />
            )}
          </div>
        </Card>

        {/* 今日资讯：直接取资讯池最新落库的条目 */}
        <Card title={t('homeDashboard.news.title')} padding="md">
          {news.loading ? (
            <Loading />
          ) : news.error ? (
            <ResourceError error={news.error} onRetry={news.reload} />
          ) : newsItems.length === 0 ? (
            <EmptyState
              icon={<Newspaper className="h-6 w-6" />}
              title={t('homeDashboard.news.emptyTitle')}
              description={t('homeDashboard.news.emptyDescription')}
            />
          ) : (
            <>
              <p className="text-xs text-secondary-text">
                {t('homeDashboard.news.total', { total: news.data?.total ?? newsItems.length })}
              </p>
              <DashboardList
                items={newsItems.map((item) => ({
                  key: item.id,
                  title: item.url ? (
                    <a
                      href={item.url}
                      target="_blank"
                      rel="noreferrer"
                      className="line-clamp-2 transition-colors hover:text-cyan"
                    >
                      {item.title}
                    </a>
                  ) : (
                    <span className="line-clamp-2">{item.title}</span>
                  ),
                  meta: `${item.source_name || item.source || '—'} · ${formatDashboardTime(
                    item.published_at ?? item.fetched_at,
                    language,
                  )}`,
                }))}
              />
            </>
          )}
        </Card>

        {/* 最近分析：沿用历史列表接口，首页只展示最新若干条 */}
        <Card title={t('homeDashboard.analysis.title')} padding="md">
          {history.loading ? (
            <Loading />
          ) : history.error ? (
            <ResourceError error={history.error} onRetry={history.reload} />
          ) : historyItems.length === 0 ? (
            <EmptyState
              icon={<History className="h-6 w-6" />}
              title={t('homeDashboard.analysis.emptyTitle')}
              description={t('homeDashboard.analysis.emptyDescription')}
            />
          ) : (
            <>
              <p className="text-xs text-secondary-text">
                {t('homeDashboard.analysis.total', { total: history.data?.total ?? historyItems.length })}
              </p>
              <DashboardList
                items={historyItems.map((item) => ({
                  key: item.id,
                  title: item.stockName || item.stockCode,
                  meta: `${item.stockCode} · ${formatDashboardTime(item.createdAt, language)}`,
                  trailing: item.trendPrediction ? (
                    <Badge variant="info">{item.trendPrediction}</Badge>
                  ) : undefined,
                }))}
              />
            </>
          )}
        </Card>

        {/* 自选股：只有 /api/v1/stocks/watchlist 这一处真实来源，因此只做展示不做增删 */}
        <WatchlistCard codes={watchlistCodes} loading={watchlistLoading} />
      </div>
    </section>
  );
};

export default HomeDashboard;
