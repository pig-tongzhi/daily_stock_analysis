import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Activity, History, Newspaper, Star } from 'lucide-react';
import { decisionSignalsApi } from '../../api/decisionSignals';
import { getParsedApiError, type ParsedApiError } from '../../api/error';
import { historyApi } from '../../api/history';
import { fetchIntelligenceItems, type IntelligenceItem } from '../../api/intelligence';
import { screeningApi } from '../../api/screening';
import { systemConfigApi } from '../../api/systemConfig';
import { ApiErrorAlert, Badge, EmptyState, Loading, ScoreGauge } from '../common';
import { HomePlayfulMascot } from './HomePlayfulMascot';
import { useStockIndex } from '../../hooks/useStockIndex';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiLanguage, UiTextKey } from '../../i18n/uiText';
import { getReportText } from '../../utils/reportLanguage';
import type { HistoryItem, MarketPhaseValue, StockBarItem } from '../../types/analysis';
import type { DecisionSignalStatus } from '../../types/decisionSignals';
import {
  buildDecisionActionLabelMap,
  getDecisionActionLabel,
  getDecisionActionTone,
  type DecisionActionTone,
} from '../../utils/decisionAction';

type BadgeVariant = NonNullable<React.ComponentProps<typeof Badge>['variant']>;

/**
 * 首页看板每次挂载都会真实请求接口，卡片数量固定，这里刻意不做懒加载，
 * 保证“打开首页即可见数据”的诉求不被折叠/分页稀释。
 *
 * 资讯条数固定 4 条：设计稿的“帮你挑了 4 条”对应一个克制的信息密度，
 * 全部数字仍然来自接口返回的 items，不做任何补数。
 */
const RECENT_SIGNALS_LIMIT = 5;
// 资讯只请求并展示 5 条，再从中挑 4 条展示：不额外扩大请求量，
// 也不需要为了“露出 4 条”而多打一次接口。
const NEWS_FETCH_LIMIT = 5;
const NEWS_VISIBLE_LIMIT = 4;

/**
 * 策略条形图的四档低饱和色：只区分不同策略，不表达好坏，
 * 避免把“选股策略”渲染成红绿涨跌语义。
 */
/** 稳定的空数组：避免 ?? [] 每次渲染产生新引用，触发下游 useMemo 反复计算。 */
const EMPTY_STOCK_ITEMS: StockBarItem[] = [];

const STRATEGY_BAR_TONES = [
  'bg-[var(--playful-mint)]',
  'bg-[var(--playful-grape)]',
  'bg-[var(--playful-sun)]',
  'bg-[var(--playful-down)]',
];

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
 * 市场阶段文案复用决策信号页已有的 key，避免同一种阶段在首页出现第二套译法。
 */
const MARKET_PHASE_LABEL_KEYS: Record<MarketPhaseValue, UiTextKey> = {
  premarket: 'decisionSignals.marketPhase.premarket',
  closing_auction: 'decisionSignals.marketPhase.closing_auction',
  intraday: 'decisionSignals.marketPhase.intraday',
  lunch_break: 'decisionSignals.marketPhase.lunch_break',
  postmarket: 'decisionSignals.marketPhase.postmarket',
  non_trading: 'decisionSignals.marketPhase.non_trading',
  unknown: 'decisionSignals.marketPhase.unknown',
};

function formatDateTime(value: string | null | undefined, language: UiLanguage): string {
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

function formatClock(value: string | null | undefined, language: UiLanguage): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(language === 'en' ? 'en-US' : 'zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

/** 千分位在 zh-CN 下会把 2026 显示成 2,026，日期一律用 useGrouping:false 输出。 */
function formatDayKey(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('en-CA', { numberingSystem: 'latn' }).format(date).replace(/\//g, '-');
}

function formatPercent(value: number | null | undefined, digits = 1): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${value.toFixed(digits)}%`
    : '—';
}

function formatSignedPercent(value: number | null | undefined, digits = 2): string | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  const sign = value > 0 ? '+' : '';
  return `${sign}${value.toFixed(digits)}%`;
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

/** 卡通友好的分区标题：emoji 只作为区块标记，不承载语义。 */
const PlayfulCardTitle: React.FC<{
  emoji: string;
  title: string;
  meta?: string;
  children?: React.ReactNode;
}> = ({ emoji, title, meta, children }) => (
  <div className="flex items-center gap-2">
    <span className="text-base leading-none" aria-hidden="true">{emoji}</span>
    <h3 className="text-sm font-semibold tracking-tight text-foreground">{title}</h3>
    {meta ? <span className="ml-auto text-[11px] font-medium playful-ink-soft">{meta}</span> : null}
    {children}
  </div>
);

const StatChip: React.FC<{
  label: string;
  value: React.ReactNode;
  tone: string;
  testId?: string;
}> = ({ label, value, tone, testId }) => (
  <div className="playful-chip min-w-[4.5rem] px-3 py-2 text-center" data-testid={testId}>
    <b className={`block text-lg font-extrabold leading-tight tabular-nums ${tone}`}>{value}</b>
    <span className="text-[11px] font-semibold playful-ink-soft">{label}</span>
  </div>
);

const MiniMetric: React.FC<{
  label: string;
  value: React.ReactNode;
  tone?: string;
}> = ({ label, value, tone }) => (
  <div className="playful-chip px-1.5 py-1.5 text-center">
    <b className={`block text-sm font-extrabold tabular-nums ${tone ?? 'text-foreground'}`}>{value}</b>
    <span className="block text-[10px] font-semibold leading-tight playful-ink-soft">{label}</span>
  </div>
);

export interface HomeDashboardProps {
  /** 首页已经通过 useWatchlist 拉取过的自选股代码；传入即复用，不再重复请求同一接口。 */
  watchlistCodes?: string[];
  /** 首页自选股的加载态，用于驱动自选股卡片的 Loading（看板独立使用时由自身请求决定）。 */
  watchlistLoading?: boolean;
  /**
   * 首页已经拉取的个股条（含情绪分、分析次数、市场阶段），直接复用；
   * 不传时自选股卡片退化为仅展示代码，不编造任何评分。
   */
  stockBarItems?: StockBarItem[];
  /** 首页已经拉取的大盘复盘历史，用于取真实的恐惧贪婪指数与沪深300涨跌。 */
  marketReviewHistoryItems?: HistoryItem[];
  /**
   * 首页正在选中/删除自选股时，卡片上的“+ 添加自选股”入口会跳到搜索框；
   * 这里只负责展示，不做增删，保持原有自选股动作的唯一入口。
   */
  onFocusSearch?: () => void;
}

export const HomeDashboard: React.FC<HomeDashboardProps> = ({
  watchlistCodes,
  watchlistLoading,
  stockBarItems,
  marketReviewHistoryItems,
  onFocusSearch,
}) => {
  const { t, language } = useUiLanguage();

  const loadStats = useCallback(() => decisionSignalsApi.getOutcomeStats(), []);
  const loadSignals = useCallback(
    () => decisionSignalsApi.list({ page: 1, pageSize: RECENT_SIGNALS_LIMIT }),
    [],
  );
  const loadNews = useCallback(
    () => fetchIntelligenceItems({ page: 1, page_size: NEWS_FETCH_LIMIT }),
    [],
  );
  const loadHistory = useCallback(
    () => historyApi.getList({ page: 1, limit: RECENT_SIGNALS_LIMIT }),
    [],
  );
  // 恐惧贪婪指数唯一真源：大盘复盘记录里的 sentiment_score。
  const loadMarketReview = useCallback(
    () => historyApi.getList({ stockCode: 'MARKET', limit: 1 }),
    [],
  );
  // 选股复盘必须走既有 tracking 接口，不在前端重算命中率/超额。
  const loadScreeningTracking = useCallback(
    () => screeningApi.getTracking({ days: 60 }),
    [],
  );
  // 未传自选股代码时（看板独立使用），仍然只请求既有的 watchlist 接口。
  const externalWatchlist = watchlistCodes !== undefined;
  const loadWatchlist = useCallback(
    () => (externalWatchlist ? Promise.resolve(watchlistCodes as string[]) : systemConfigApi.getWatchlist()),
    [externalWatchlist, watchlistCodes],
  );

  const stats = useDashboardResource(loadStats);
  const signals = useDashboardResource(loadSignals);
  const news = useDashboardResource(loadNews);
  const history = useDashboardResource(loadHistory);
  const marketReview = useDashboardResource(loadMarketReview);
  const screening = useDashboardResource(loadScreeningTracking);
  const watchlist = useDashboardResource<string[]>(loadWatchlist);

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

  const actionLabels = useMemo(() => buildDecisionActionLabelMap(t), [t]);
  const fearGreedLabel = getReportText(language).fearGreedIndex;

  const signalItems = signals.data?.items ?? [];
  const newsItems = (news.data?.items ?? []).slice(0, NEWS_VISIBLE_LIMIT);
  const historyItems = history.data?.items ?? [];

  const effectiveMarketReviewItems = marketReviewHistoryItems ?? marketReview.data?.items ?? [];
  const latestMarketReview = effectiveMarketReviewItems[0] ?? null;
  const sentimentScore = typeof latestMarketReview?.sentimentScore === 'number'
    ? latestMarketReview.sentimentScore
    : null;

  // 空数组字面量每次都换引用，会让下面的 useMemo 每帧都重算；这里固定住引用。
  const effectiveStockItems = useMemo<StockBarItem[]>(
    () => stockBarItems ?? EMPTY_STOCK_ITEMS,
    [stockBarItems],
  );
  const watchlistCodesResolved = watchlist.data ?? [];
  const watchlistPending = watchlist.loading || Boolean(watchlistLoading);
  const stockItemByCode = useMemo(() => {
    const map = new Map<string, StockBarItem>();
    for (const item of effectiveStockItems) {
      if (item.stockCode === 'MARKET') continue;
      map.set(item.stockCode.toUpperCase(), item);
    }
    return map;
  }, [effectiveStockItems]);

  // 市场阶段 / 数据时间取“最新一次个股分析”的真实记录，不做任何推算。
  const latestStockItem = useMemo(
    () => effectiveStockItems.find((item) => item.stockCode !== 'MARKET') ?? null,
    [effectiveStockItems],
  );
  const marketPhase = latestStockItem?.marketPhaseSummary?.phase ?? null;
  const marketPhaseLabel = marketPhase ? t(MARKET_PHASE_LABEL_KEYS[marketPhase]) : null;
  const latestDataTime = latestStockItem?.lastAnalysisTime ?? latestMarketReview?.createdAt ?? null;

  const screeningTotal = screening.data?.total ?? null;
  const screeningStrategies = useMemo(
    () => (screening.data?.strategies ?? [])
      .filter((row) => row.trackable > 0)
      .sort((left, right) => (right.trackable - left.trackable)),
    [screening.data],
  );
  const maxStrategyTrackable = screeningStrategies.reduce(
    (max, row) => Math.max(max, row.trackable),
    0,
  );
  const screeningWindowDays = screening.data?.windowDays ?? 60;
  const stockPoolSeries = screening.data?.dataFreshness?.stockDailySeriesCount ?? null;
  const stockPoolLatestDate = screening.data?.dataFreshness?.stockDailyLatestDate ?? null;

  const benchmarkChange = formatSignedPercent(screeningTotal?.avgBenchmarkReturnPct ?? null);

  return (
    <section className="mb-4 flex flex-col gap-4" data-testid="home-dashboard-cards">
      {/* ---------- 问候条 ---------- */}
      <div className="playful-hero flex flex-wrap items-center gap-4 px-5 py-4" data-testid="playful-hero">
        <HomePlayfulMascot className="h-[72px] w-[104px] shrink-0" />
        <div className="min-w-[12rem] flex-1">
          <h2 className="text-lg font-extrabold tracking-tight text-foreground">
            {t('homeDashboard.playful.heroGreeting')}
          </h2>
          <p className="mt-1 text-xs font-semibold playful-ink-soft">
            {stats.data
              ? t('homeDashboard.playful.heroSubtitle', {
                total: stats.data.total,
                hit: stats.data.hit,
              })
              : t('homeDashboard.playful.heroSubtitleEmpty')}
          </p>
        </div>
        {stats.data ? (
          <div className="flex flex-wrap gap-2" data-testid="playful-hero-chips">
            <StatChip
              label={t('homeDashboard.playful.hitRate')}
              value={formatPercent(stats.data.hitRatePct)}
              tone="text-[var(--playful-grape)]"
              testId="playful-hero-hit-rate"
            />
            <StatChip
              label={t('homeDashboard.playful.completed')}
              value={stats.data.completed}
              tone="text-[var(--playful-mint)]"
              testId="playful-hero-completed"
            />
            <StatChip
              label={t('homeDashboard.playful.unable')}
              value={stats.data.unable}
              tone="text-[var(--playful-ink)]"
              testId="playful-hero-unable"
            />
          </div>
        ) : null}
      </div>

      {/* ---------- AI 建议 + 市场情绪 ---------- */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1.45fr_1fr]">
        <div className="playful-card flex flex-col p-4" data-testid="playful-ai-card">
          <PlayfulCardTitle
            emoji="🎯"
            title={t('homeDashboard.aiSignals.title')}
            meta={t('homeDashboard.eyebrow')}
          />
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
            <div className="mt-3 flex flex-col gap-4 sm:flex-row">
              {/* 复用既有 ScoreGauge 渲染命中率圆环，不重新实现一套仪表盘 */}
              <div className="flex shrink-0 justify-center sm:justify-start" data-testid="playful-hit-rate-gauge">
                <ScoreGauge
                  score={stats.data.hitRatePct ?? 0}
                  size="sm"
                  showLabel={false}
                  language="zh"
                />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap gap-2">
                  <span className="playful-pill px-2.5 py-1 text-xs font-bold">
                    {t('homeDashboard.playful.hitRate')} <span className="tabular-nums">{formatPercent(stats.data.hitRatePct)}</span>
                  </span>
                  <span className="playful-pill px-2.5 py-1 text-xs font-bold">
                    {t('homeDashboard.aiSignals.completed')} <span className="tabular-nums">{stats.data.completed}</span>
                  </span>
                  <span className="playful-pill px-2.5 py-1 text-xs font-bold">
                    {t('homeDashboard.aiSignals.unable')} <span className="tabular-nums">{stats.data.unable}</span>
                  </span>
                </div>
                <p className="mt-2 text-xs font-semibold playful-ink-soft">
                  {t('homeDashboard.aiSignals.hitMiss', { hit: stats.data.hit, miss: stats.data.miss })}
                </p>

                <div className="mt-3 border-t border-dashed playful-dashed pt-2">
                  <p className="label-uppercase mb-1">{t('homeDashboard.aiSignals.recent')}</p>
                  {signals.loading ? (
                    <Loading />
                  ) : signals.error ? (
                    <ResourceError error={signals.error} onRetry={signals.reload} />
                  ) : signalItems.length === 0 ? (
                    <p className="py-2 text-sm playful-ink-soft">{t('homeDashboard.aiSignals.noRecent')}</p>
                  ) : (
                    <ul className="flex flex-col">
                      {signalItems.map((item) => (
                        <li
                          key={item.id}
                          className="flex items-center gap-2 border-b border-dashed playful-dashed py-1.5 text-xs last:border-b-0"
                        >
                          <span className="font-bold text-foreground">{item.stockName || item.stockCode}</span>
                          <span className="playful-ink-soft">
                            {item.stockCode} · {formatDateTime(item.createdAt, language)}
                          </span>
                          <span className="ml-auto flex items-center gap-1.5">
                            <Badge variant={ACTION_BADGE_VARIANTS[getDecisionActionTone(item.action, null, null)]}>
                              {getDecisionActionLabel(item.action, null, null, '—', actionLabels) ?? '—'}
                            </Badge>
                            <Badge variant={STATUS_BADGE_VARIANTS[item.status]}>
                              {t(STATUS_LABEL_KEYS[item.status])}
                            </Badge>
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>

        <div className="playful-card flex flex-col p-4" data-testid="playful-sentiment-card">
          <PlayfulCardTitle
            emoji="🌡️"
            title={t('homeDashboard.playful.sentimentTitle')}
            meta={t('homeDashboard.playful.sentimentSubtitle')}
          />
          {marketReview.loading && marketReviewHistoryItems === undefined ? (
            <Loading />
          ) : marketReview.error ? (
            <ResourceError error={marketReview.error} onRetry={marketReview.reload} />
          ) : sentimentScore === null ? (
            <EmptyState
              icon={<Activity className="h-6 w-6" />}
              title={fearGreedLabel}
              description={t('homeDashboard.playful.sentimentUnavailable')}
            />
          ) : (
            // 情绪卡片在宽屏上和左侧 AI 卡片等高，居中会留下一大片空白；
            // 这里改为顶部对齐，让内容跟着卡片顶端走。
            <div className="mt-4 flex flex-col items-center gap-2">
              {/* 真实恐惧贪婪指数：analysis_history(code='MARKET') 的 sentiment_score */}
              <div className="text-center" data-testid="playful-sentiment-value">
                <p className="text-[11px] font-semibold playful-ink-soft">{fearGreedLabel}</p>
                <p className="text-3xl font-extrabold leading-tight tabular-nums text-foreground">
                  {sentimentScore}
                </p>
              </div>
              <div className="playful-track h-2 w-full overflow-hidden rounded-full">
                <span
                  className="block h-full rounded-full bg-[var(--playful-grape)]"
                  style={{ width: `${Math.max(0, Math.min(100, sentimentScore))}%` }}
                />
              </div>
              {latestMarketReview ? (
                <p className="playful-note w-full px-3 py-2 text-center text-[11px] font-bold">
                  {benchmarkChange
                    ? t('homeDashboard.playful.marketReviewLine', {
                      date: formatDateTime(latestMarketReview.createdAt, language),
                      change: benchmarkChange,
                    })
                    : t('homeDashboard.playful.marketReviewNoIndex', {
                      date: formatDateTime(latestMarketReview.createdAt, language),
                    })}
                </p>
              ) : null}
            </div>
          )}
        </div>
      </div>

      {/* ---------- 今日资讯 + 选股复盘 ---------- */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1.45fr_1fr]">
        <div className="playful-card flex flex-col p-4" data-testid="playful-news-card">
          <PlayfulCardTitle
            emoji="📰"
            title={t('homeDashboard.news.title')}
          />
          {/* 资讯池总量单独一行：设计稿把“共 N 条 · 帮你挑了 M 条”塞进标题右侧，
              但总量本身是独立事实，单独成行更易读，也不影响“挑了 4 条”的表达。 */}
          {news.data ? (
            <p className="mt-1 flex flex-wrap items-center gap-x-2 text-[11px] font-semibold playful-ink-soft">
              <span>{t('homeDashboard.news.total', { total: news.data.total })}</span>
              {newsItems.length > 0 ? (
                <span>{t('homeDashboard.playful.newsPicked', { shown: newsItems.length })}</span>
              ) : null}
            </p>
          ) : null}
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
            <ul className="mt-2 flex flex-col">
              {newsItems.map((item: IntelligenceItem, index) => (
                <li
                  key={item.id}
                  className="flex gap-2.5 border-b border-dashed playful-dashed py-2 last:border-b-0"
                >
                  <span className="grid h-5 w-5 shrink-0 place-items-center rounded-lg bg-[var(--playful-tint-b)] text-[11px] font-extrabold text-[var(--playful-grape)]">
                    {index + 1}
                  </span>
                  <div className="min-w-0">
                    {item.url ? (
                      <a
                        href={item.url}
                        target="_blank"
                        rel="noreferrer"
                        className="line-clamp-2 text-xs font-semibold leading-relaxed text-foreground transition-colors hover:text-[var(--playful-grape)]"
                      >
                        {item.title}
                      </a>
                    ) : (
                      <span className="line-clamp-2 text-xs font-semibold leading-relaxed text-foreground">
                        {item.title}
                      </span>
                    )}
                    {/* 真实元信息：来源 + 该条入库抓取时间 */}
                    <p className="mt-0.5 text-[11px] font-medium playful-ink-soft">
                      {item.source_name || item.source || '—'} · {t('homeDashboard.playful.newsFetchedAt', {
                        time: formatDateTime(item.published_at ?? item.fetched_at, language),
                      })}
                    </p>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="playful-card flex flex-col p-4" data-testid="playful-screening-card">
          <PlayfulCardTitle
            emoji="📈"
            title={t('homeDashboard.playful.screeningTitle')}
            meta={screeningTotal
              ? t('homeDashboard.playful.screeningSubtitle', {
                days: screeningWindowDays,
                trackable: screeningTotal.trackable,
              })
              : undefined}
          />
          {screening.loading ? (
            <Loading />
          ) : screening.error ? (
            <ResourceError error={screening.error} onRetry={screening.reload} />
          ) : !screeningTotal || screeningTotal.picks === 0 ? (
            <EmptyState
              icon={<Activity className="h-6 w-6" />}
              title={t('homeDashboard.playful.screeningTitle')}
              description={t('homeDashboard.playful.screeningEmpty', { days: screeningWindowDays })}
            />
          ) : (
            <>
              <div className="mt-3 flex flex-col gap-1.5">
                {screeningStrategies.length === 0 ? (
                  <p className="text-xs playful-ink-soft">
                    {t('homeDashboard.playful.screeningEmpty', { days: screeningWindowDays })}
                  </p>
                ) : screeningStrategies.map((row, index) => {
                  const width = maxStrategyTrackable > 0
                    ? Math.max(6, Math.round((row.trackable / maxStrategyTrackable) * 100))
                    : 0;
                  return (
                    <div key={row.strategy} className="flex items-center gap-2.5">
                      <span className="w-[5.5rem] shrink-0 truncate text-xs font-bold playful-ink-soft">
                        {row.strategy}
                      </span>
                      <span className="playful-track h-4 flex-1 overflow-hidden rounded-full">
                        <span
                          className={`block h-full rounded-full ${STRATEGY_BAR_TONES[index % STRATEGY_BAR_TONES.length]}`}
                          style={{ width: `${width}%` }}
                        />
                      </span>
                      <span className="w-12 shrink-0 text-right text-xs font-extrabold tabular-nums text-[var(--playful-mint)]">
                        {formatPercent(row.hitRate === null ? null : row.hitRate * 100, 0)}
                      </span>
                    </div>
                  );
                })}
              </div>
              {/* 5 个指标必须一行放完：flex-wrap 会把第 5 个“跑赢基准”挤到单独一行，
                  看起来像漏了一项。这里用格网固定在 5 列（窄屏 2 列 / 中屏 3 列）。 */}
              <div className="mt-3 grid grid-cols-2 gap-1.5 sm:grid-cols-3 xl:grid-cols-5">
                <MiniMetric label={t('homeDashboard.playful.screeningMetricPicks')} value={screeningTotal.picks} />
                <MiniMetric
                  label={t('homeDashboard.playful.screeningMetricHitRate')}
                  value={screeningTotal.hitRate === null ? '—' : formatPercent(screeningTotal.hitRate * 100, 0)}
                  tone="text-[var(--playful-up)]"
                />
                <MiniMetric
                  label={t('homeDashboard.playful.screeningMetricAvgReturn')}
                  value={screeningTotal.avgReturnPct === null ? '—' : formatSignedPercent(screeningTotal.avgReturnPct)}
                  tone="text-[var(--playful-up)]"
                />
                <MiniMetric
                  label={t('homeDashboard.playful.screeningMetricAvgExcess')}
                  value={screeningTotal.avgExcessReturnPct === null ? '—' : formatSignedPercent(screeningTotal.avgExcessReturnPct)}
                  tone="text-[var(--playful-up)]"
                />
                <MiniMetric
                  label={t('homeDashboard.playful.screeningMetricBeat')}
                  value={screeningTotal.beatBenchmarkRate === null
                    ? '—'
                    : formatPercent(screeningTotal.beatBenchmarkRate * 100, 0)}
                />
              </div>
              {screeningTotal.benchmarkCode && benchmarkChange ? (
                <p className="mt-3 text-[11px] font-semibold playful-ink-soft">
                  {t('homeDashboard.playful.screeningBenchmark', {
                    code: screeningTotal.benchmarkCode,
                    return: benchmarkChange,
                  })}
                  {stockPoolSeries !== null && stockPoolLatestDate
                    ? ` · ${t('homeDashboard.playful.stockPoolNote', {
                      series: stockPoolSeries,
                      date: formatDayKey(stockPoolLatestDate),
                    })}`
                    : ''}
                </p>
              ) : null}
            </>
          )}
        </div>
      </div>

      {/* ---------- 自选股 chips ---------- */}
      <div className="playful-card p-4" data-testid="playful-watchlist-card">
        <PlayfulCardTitle
          emoji="⭐"
          title={t('homeDashboard.watchlist.title')}
          meta={watchlistCodesResolved.length > 0
            ? t('homeDashboard.playful.watchlistSubtitle', {
              phase: marketPhaseLabel ?? t('homeDashboard.playful.watchlistEmptyPhase'),
              time: formatDateTime(latestDataTime, language),
            })
            : undefined}
        />
        {watchlistPending ? (
          <Loading />
        ) : watchlist.error ? (
          <ResourceError error={watchlist.error} onRetry={watchlist.reload} />
        ) : watchlistCodesResolved.length === 0 ? (
          <EmptyState
            icon={<Star className="h-6 w-6" />}
            title={t('homeDashboard.watchlist.emptyTitle')}
            description={t('homeDashboard.watchlist.emptyDescription')}
          />
        ) : (
          <>
            <p className="mt-1 text-xs font-semibold playful-ink-soft">
              {t('homeDashboard.watchlist.count', { count: watchlistCodesResolved.length })}
            </p>
            <ul className="mt-2.5 grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
              {watchlistCodesResolved.map((code) => {
                const stockInfo = stockItemByCode.get(code.toUpperCase());
                const phase = stockInfo?.marketPhaseSummary?.phase;
                const score = stockInfo?.sentimentScore;
                // 设计稿里每只自选股还带一个涨跌幅，但 /api/v1/history/stocks 返回的
                // StockBarItem 只有情绪分/建议/分析次数，没有行情涨跌字段。
                // 没有真实来源就不展示，绝不用情绪分或建议反推一个涨跌幅。
                return (
                  <li key={code} className="playful-card-soft px-3 py-2" data-testid="playful-stock-chip">
                    <p className="truncate text-xs font-extrabold text-foreground">
                      {nameByCode.get(code.toUpperCase()) || code}
                    </p>
                    <p className="mt-0.5 truncate text-[11px] font-semibold playful-ink-soft">
                      {code}
                      {phase ? ` · ${t(MARKET_PHASE_LABEL_KEYS[phase])}` : ''}
                    </p>
                    <div className="mt-1.5 flex items-center justify-between gap-1">
                      {typeof score === 'number' ? (
                        <span className="text-sm font-extrabold tabular-nums text-[var(--playful-grape)]">
                          {score}
                        </span>
                      ) : (
                        <span className="text-[11px] font-semibold playful-ink-soft">
                          {t('homeDashboard.playful.watchlistNoScore')}
                        </span>
                      )}
                      <span className="flex items-center gap-1">
                        {stockInfo ? (
                          <Badge variant={ACTION_BADGE_VARIANTS[getDecisionActionTone(stockInfo.action ?? null, null, null)]}>
                            {stockInfo.actionLabel
                              ?? getDecisionActionLabel(stockInfo.action ?? null, null, null, '—', actionLabels)
                              ?? '—'}
                          </Badge>
                        ) : null}
                      </span>
                    </div>
                    {stockInfo ? (
                      <p className="mt-1 text-[10px] font-semibold playful-ink-soft">
                        {t('homeDashboard.playful.analysisCount', { count: stockInfo.analysisCount })}
                        {' · '}
                        {formatClock(stockInfo.lastAnalysisTime, language)}
                      </p>
                    ) : null}
                  </li>
                );
              })}
              <li
                className="flex min-h-[4.5rem] items-center justify-center rounded-2xl border border-dashed playful-dashed px-3 py-2 text-center text-[11px] font-extrabold text-[var(--playful-grape)]"
              >
                {onFocusSearch ? (
                  <button type="button" onClick={onFocusSearch} className="w-full">
                    + {t('homeDashboard.watchlist.title')}
                  </button>
                ) : (
                  <span>+ {t('homeDashboard.watchlist.title')}</span>
                )}
              </li>
            </ul>
          </>
        )}
      </div>

      {/* ---------- 最近分析：保留原有历史列表，信息最完整 ---------- */}
      <div className="playful-card p-4" data-testid="playful-history-card">
        <PlayfulCardTitle
          emoji="🕒"
          title={t('homeDashboard.analysis.title')}
          meta={history.data ? t('homeDashboard.analysis.total', { total: history.data.total }) : undefined}
        />
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
          <ul className="mt-2 flex flex-col">
            {historyItems.map((item) => (
              <li
                key={item.id}
                className="flex items-center gap-2 border-b border-dashed playful-dashed py-1.5 text-xs last:border-b-0"
              >
                <span className="font-bold text-foreground">{item.stockName || item.stockCode}</span>
                <span className="playful-ink-soft">
                  {item.stockCode} · {formatDateTime(item.createdAt, language)}
                </span>
                {item.trendPrediction ? (
                  <span className="ml-auto">
                    <Badge variant="info">{item.trendPrediction}</Badge>
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
};

export default HomeDashboard;
