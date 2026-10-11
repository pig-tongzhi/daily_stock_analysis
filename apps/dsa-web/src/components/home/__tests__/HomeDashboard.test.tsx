import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { decisionSignalsApi } from '../../../api/decisionSignals';
import { historyApi } from '../../../api/history';
import { fetchIntelligenceItems } from '../../../api/intelligence';
import { screeningApi } from '../../../api/screening';
import { systemConfigApi } from '../../../api/systemConfig';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import type { StockBarItem } from '../../../types/analysis';
import type { DecisionSignalItem, DecisionSignalOutcomeStatsResponse } from '../../../types/decisionSignals';
import { UI_LANGUAGE_STORAGE_KEY } from '../../../utils/uiLanguage';
import { HomeDashboard } from '../HomeDashboard';

vi.mock('../../../api/decisionSignals', () => ({
  decisionSignalsApi: {
    getOutcomeStats: vi.fn(),
    list: vi.fn(),
  },
}));

vi.mock('../../../api/intelligence', () => ({
  fetchIntelligenceItems: vi.fn(),
}));

vi.mock('../../../api/history', () => ({
  historyApi: {
    getList: vi.fn(),
  },
}));

// 首页改版后新增了选股复盘卡片，走既有 /api/v1/screening/tracking 接口。
vi.mock('../../../api/screening', () => ({
  screeningApi: {
    getTracking: vi.fn(),
  },
}));

vi.mock('../../../api/systemConfig', () => ({
  systemConfigApi: {
    getWatchlist: vi.fn(),
  },
}));

const stockIndexItems = [
  {
    canonicalCode: '600559.SH',
    displayCode: '600559',
    nameZh: '老白干酒',
    nameEn: 'Laobaigan',
    market: 'CN' as const,
    assetType: 'stock' as const,
    active: true,
    aliases: ['老白干'],
  },
];

vi.mock('../../../hooks/useStockIndex', () => ({
  useStockIndex: () => ({
    index: stockIndexItems,
    loading: false,
    error: null,
    fallback: false,
    loaded: true,
  }),
}));

/** 真实 /api/v1/screening/tracking 的字段形状（数值取生产库 2026-10-12 实测值）。 */
const screeningTrackingFixture = {
  enabled: true,
  windowDays: 60,
  generatedAt: '2026-10-11T22:23:02+00:00',
  runWindow: { days: 60, malformedRuns: 0 },
  total: {
    picks: 121,
    trackable: 39,
    untrackable: 82,
    hitRate: 0.6154,
    hitRateBasis: 'trackable_only',
    avgReturnPct: 0.47,
    medianReturnPct: 0.21,
    bestReturnPct: 14.74,
    worstReturnPct: -5.29,
    avgDaysHeld: 1.9,
    benchmarkCode: 'sh000300',
    benchmarkComparable: 39,
    beatBenchmarkRate: 0.8974,
    beatBenchmarkBasis: 'benchmark_comparable_trackable_picks',
    beatBenchmarkDenominator: 39,
    avgBenchmarkReturnPct: -0.85,
    medianBenchmarkReturnPct: -0.93,
    avgExcessReturnPct: 1.32,
    medianExcessReturnPct: 1.1,
    benchmarkUnavailableReasons: [],
  },
  strategies: [
    {
      strategy: 'volume_breakout',
      picks: 13,
      trackable: 8,
      untrackable: 5,
      hitRate: 0.625,
      hitRateBasis: 'trackable_only',
      avgReturnPct: 2.11,
      medianReturnPct: 1.12,
      bestReturnPct: 14.74,
      worstReturnPct: -5.29,
      avgDaysHeld: 2,
      benchmarkCode: 'sh000300',
      benchmarkComparable: 8,
      beatBenchmarkRate: 0.625,
      beatBenchmarkBasis: 'benchmark_comparable_trackable_picks',
      beatBenchmarkDenominator: 8,
      avgBenchmarkReturnPct: -0.93,
      medianBenchmarkReturnPct: -0.93,
      avgExcessReturnPct: 3.04,
      medianExcessReturnPct: 2.06,
      benchmarkUnavailableReasons: [],
    },
  ],
  picks: [],
  untrackable: [],
  untrackableReasons: [],
  dataFreshness: {
    stockDailyLatestDate: '2026-10-09',
    stockDailySeriesCount: 64,
    latestTrackableBarDate: '2026-10-09',
    pickForwardBarsAvailable: true,
  },
} as unknown as Awaited<ReturnType<typeof screeningApi.getTracking>>;

const statsFixture: DecisionSignalOutcomeStatsResponse = {
  engineVersion: 'decision-signal-v1',
  horizons: null,
  statuses: ['active', 'expired', 'invalidated', 'closed'],
  total: 39,
  completed: 19,
  unable: 20,
  hit: 12,
  miss: 7,
  neutral: 0,
  hitRatePct: 63.16,
  avgStockReturnPct: -1.6029,
  unableReasons: { insufficient_forward_bars: 20 },
  breakdowns: {},
};

const signalItems = [
  {
    id: 20,
    stockCode: '601668',
    stockName: '中国建筑',
    action: 'watch',
    status: 'active',
    createdAt: '2026-10-11T01:44:29.354500+08:00',
  },
  {
    id: 19,
    stockCode: '600519',
    stockName: '贵州茅台',
    action: 'buy',
    status: 'expired',
    createdAt: '2026-10-10T01:44:29.354500+08:00',
  },
] as unknown as DecisionSignalItem[];

function renderDashboard() {
  return render(
    <UiLanguageProvider>
      <HomeDashboard />
    </UiLanguageProvider>,
  );
}

/** 首页会传入真实的个股 bar 与大盘复盘历史，这里按真实字段形状构造。 */
const stockBarFixture = [
  {
    id: 67,
    stockCode: '603103',
    stockName: '横店影视',
    reportType: 'simple',
    sentimentScore: 45,
    operationAdvice: '观望',
    action: 'watch',
    actionLabel: '观望',
    analysisCount: 6,
    lastAnalysisTime: '2026-10-12T02:52:00.516180+08:00',
    marketPhaseSummary: {
      market: 'cn',
      phase: 'premarket',
      warnings: [],
    },
  },
  {
    id: 66,
    stockCode: '688063',
    stockName: '派能科技',
    reportType: 'simple',
    sentimentScore: 60,
    operationAdvice: '观望',
    action: 'buy',
    actionLabel: '买入',
    analysisCount: 6,
    lastAnalysisTime: '2026-10-12T02:51:34.816536+08:00',
    marketPhaseSummary: {
      market: 'cn',
      phase: 'premarket',
      warnings: [],
    },
  },
] as unknown as StockBarItem[];

const marketReviewItemFixture = {
  id: 61,
  queryId: 'market-q-61',
  stockCode: 'MARKET',
  stockName: '大盘复盘',
  reportType: 'market_review' as const,
  sentimentScore: 50,
  createdAt: '2026-10-12T02:47:18.357354+08:00',
};

function renderDashboardWithRealRows() {
  return render(
    <UiLanguageProvider>
      <HomeDashboard
        watchlistCodes={['603103', '688063']}
        stockBarItems={stockBarFixture}
        marketReviewHistoryItems={[marketReviewItemFixture] as never}
      />
    </UiLanguageProvider>,
  );
}

function mockHappyPath() {
  vi.mocked(decisionSignalsApi.getOutcomeStats).mockResolvedValue(statsFixture);
  vi.mocked(decisionSignalsApi.list).mockResolvedValue({ items: signalItems, total: 20, page: 1, pageSize: 5 });
  vi.mocked(fetchIntelligenceItems).mockResolvedValue({
    items: [
      {
        id: 1882,
        source_id: 5,
        source_name: 'NewsNow 雪球热门股票',
        source_type: 'newsnow',
        title: '中远海能',
        summary: '3.85% SH',
        url: 'https://xueqiu.com/s/SH600026',
        source: 'NewsNow 雪球热门股票',
        published_at: null,
        fetched_at: '2026-10-11T02:34:28.407583',
        scope_type: 'market',
        scope_value: null,
        market: 'cn',
      },
    ],
    total: 464,
    page: 1,
    page_size: 5,
  });
  // 首页看板对 /api/v1/history 有两次真实调用：最近分析（无 stockCode）
  // 和大盘复盘（stockCode='MARKET'）。按参数返回，才能验证“情绪分只来自大盘复盘”。
  vi.mocked(historyApi.getList).mockImplementation(async (params = {}) => {
    if (params.stockCode === 'MARKET') {
      return {
        total: 20,
        page: 1,
        limit: 1,
        items: [
          {
            id: 61,
            queryId: 'market-q-61',
            stockCode: 'MARKET',
            stockName: '大盘复盘',
            reportType: 'market_review',
            sentimentScore: 50,
            createdAt: '2026-10-12T02:47:18.357354+08:00',
          },
        ],
      } as Awaited<ReturnType<typeof historyApi.getList>>;
    }
    return {
      total: 32,
      page: 1,
      limit: 5,
      items: [
        {
          id: 32,
          queryId: 'q-32',
          stockCode: '601668',
          stockName: '中国建筑',
          trendPrediction: '震荡',
          createdAt: '2026-10-11T01:44:29.354500+08:00',
        },
      ],
    } as Awaited<ReturnType<typeof historyApi.getList>>;
  });
  vi.mocked(systemConfigApi.getWatchlist).mockResolvedValue(['600559', '999999']);
  vi.mocked(screeningApi.getTracking).mockResolvedValue(screeningTrackingFixture);
}

describe('HomeDashboard', () => {
  beforeEach(() => {
    localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
    vi.clearAllMocks();
    mockHappyPath();
  });

  it('renders real AI signal stats and the latest signals', async () => {
    renderDashboard();

    // 首页改版后 63.2% 同时出现在问候条 chip、AI 卡片 pill 和命中率圆环三处，
    // 这是刻意的重复强调；改用 testid 精确定位，断言强度没有降低。
    await waitFor(() => {
      expect(screen.getByTestId('playful-hero-hit-rate')).toHaveTextContent('63.2%');
    });
    expect(screen.getAllByText('63.2%').length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText('命中率').length).toBeGreaterThan(0);
    // 已完成 / 无法评估 是需求要求的最小指标集
    expect(screen.getAllByText('已完成').length).toBeGreaterThan(0);
    expect(screen.getByTestId('playful-hero-completed')).toHaveTextContent('19');
    expect(screen.getAllByText('无法评估').length).toBeGreaterThan(0);
    expect(screen.getByTestId('playful-hero-unable')).toHaveTextContent('20');
    expect(screen.getByText('命中 12 · 未中 7')).toBeInTheDocument();

    // 「中国建筑」同时出现在 AI 建议和最近分析两张卡里，因此用 getAllByText
    expect(screen.getAllByText('中国建筑').length).toBe(2);
    expect(screen.getByText('贵州茅台')).toBeInTheDocument();
    expect(screen.getAllByText('观望').length).toBeGreaterThan(0);
    expect(screen.getAllByText('有效').length).toBeGreaterThan(0);
    expect(vi.mocked(decisionSignalsApi.list)).toHaveBeenCalledWith({ page: 1, pageSize: 5 });
  });

  it('renders the newest intelligence items with a source link and time', async () => {
    renderDashboard();

    expect(await screen.findByText('共 464 条资讯')).toBeInTheDocument();
    const link = screen.getByRole('link', { name: '中远海能' });
    expect(link).toHaveAttribute('href', 'https://xueqiu.com/s/SH600026');
    expect(screen.getByText(/NewsNow 雪球热门股票 · /)).toBeInTheDocument();
    expect(vi.mocked(fetchIntelligenceItems)).toHaveBeenCalledWith({ page: 1, page_size: 5 });
  });

  it('renders the most recent analysis records', async () => {
    renderDashboard();

    expect(await screen.findByText('共 32 条记录')).toBeInTheDocument();
    expect(screen.getByText('震荡')).toBeInTheDocument();
    expect(vi.mocked(historyApi.getList)).toHaveBeenCalledWith({ page: 1, limit: 5 });
  });

  it('resolves watchlist codes to names and falls back to the raw code', async () => {
    renderDashboard();

    expect(await screen.findByText('当前关注 2 只')).toBeInTheDocument();
    expect(screen.getByText('老白干酒')).toBeInTheDocument();
    // 索引里没有的代码必须原样展示，不能变成空白。
    // 卡片同时展示名称行和代码行，因此 999999 会命中两次（名称回退 + 代码行）。
    expect(screen.getAllByText('999999').length).toBeGreaterThanOrEqual(1);
  });

  it('reuses external watchlist codes without requesting the endpoint again', async () => {
    render(
      <UiLanguageProvider>
        <HomeDashboard watchlistCodes={['600559']} />
      </UiLanguageProvider>,
    );

    expect(await screen.findByText('当前关注 1 只')).toBeInTheDocument();
    expect(screen.getByText('老白干酒')).toBeInTheDocument();
    expect(vi.mocked(systemConfigApi.getWatchlist)).not.toHaveBeenCalled();
  });

  it('shows an empty state instead of a blank card when a source has no rows', async () => {
    vi.mocked(decisionSignalsApi.getOutcomeStats).mockResolvedValue({ ...statsFixture, total: 0 });
    vi.mocked(fetchIntelligenceItems).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 5 });
    vi.mocked(historyApi.getList).mockResolvedValue({ total: 0, page: 1, limit: 5, items: [] });
    vi.mocked(systemConfigApi.getWatchlist).mockResolvedValue([]);

    renderDashboard();

    expect(await screen.findByText('暂无 AI 建议')).toBeInTheDocument();
    expect(screen.getByText('暂无资讯')).toBeInTheDocument();
    expect(screen.getByText('暂无分析记录')).toBeInTheDocument();
    expect(screen.getByText('暂无关注标的')).toBeInTheDocument();
  });

  it('surfaces a failed fetch as an ApiErrorAlert and retries on demand', async () => {
    vi.mocked(decisionSignalsApi.getOutcomeStats)
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValue(statsFixture);

    renderDashboard();

    // 首页有多张独立取数的卡片，失败只会亮一张卡；这里必须把重试点回
    // AI 建议卡片内部，而不是页面上任意一个“重试”（其它卡片的失败与本次断言无关）。
    const aiCard = await waitFor(() => {
      const card = screen.getByTestId('playful-ai-card');
      expect(within(card).getByRole('alert')).toBeInTheDocument();
      return card;
    });
    fireEvent.click(within(aiCard).getByRole('button', { name: '重试' }));

    await waitFor(() => {
      expect(vi.mocked(decisionSignalsApi.getOutcomeStats)).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(screen.getByTestId('playful-hero-hit-rate')).toHaveTextContent('63.2%');
    });
  });

  it('renders the market sentiment card from the real market-review sentiment score', async () => {
    renderDashboardWithRealRows();

    const sentimentCard = await screen.findByTestId('playful-sentiment-card');
    await waitFor(() => {
      expect(within(sentimentCard).getByTestId('playful-sentiment-value')).toHaveTextContent('50');
    });
    // 大盘复盘行的沪深300涨跌来自 screening tracking 的基准，不是自造数据
    expect(within(sentimentCard).getByText(/大盘复盘 · /)).toBeInTheDocument();
    expect(within(sentimentCard).getByText(/-0.85%/)).toBeInTheDocument();
  });

  it('omits the invented fear-greed previous value and never prints 42 or 38 as the index', async () => {
    renderDashboardWithRealRows();

    const sentimentCard = await screen.findByTestId('playful-sentiment-card');
    await waitFor(() => {
      expect(within(sentimentCard).getByTestId('playful-sentiment-value')).toHaveTextContent('50');
    });
    // 设计稿里的“42 / 前值 38”没有任何真实来源，必须完全不存在于情绪卡内
    expect(within(sentimentCard).queryByText('42')).toBeNull();
    expect(within(sentimentCard).queryByText('38')).toBeNull();
    expect(within(sentimentCard).queryByText(/前值/)).toBeNull();
  });

  it('renders the screening review metrics straight from the tracking endpoint', async () => {
    renderDashboardWithRealRows();

    const card = await screen.findByTestId('playful-screening-card');
    // 121 入选 / 90% 跑赢基准 / +0.47% 平均收益 / +1.32% 平均超额，全部来自 tracking
    await waitFor(() => {
      expect(within(card).getByText('121')).toBeInTheDocument();
    });
    expect(within(card).getByText('90%')).toBeInTheDocument();
    expect(within(card).getByText('+0.47%')).toBeInTheDocument();
    expect(within(card).getByText('+1.32%')).toBeInTheDocument();
    expect(within(card).getByText('62%')).toBeInTheDocument();
    expect(within(card).getByText(/sh000300 基准 -0.85%/)).toBeInTheDocument();
    expect(within(card).getByText(/行情库覆盖 64 只个股/)).toBeInTheDocument();
  });

  it('renders watchlist chips with the real per-stock score, phase and analysis count', async () => {
    renderDashboardWithRealRows();

    const card = await screen.findByTestId('playful-watchlist-card');
    const chips = await within(card).findAllByTestId('playful-stock-chip');
    expect(chips).toHaveLength(2);

    // 横店影视：真实情绪分 45 / 分析 6 次 / 盘前；设计稿里的 51、12 次都必须不存在
    const firstChip = chips[0];
    // 测试用股票索引里没有 603103，名称按既有回退规则显示原始代码
    expect(within(firstChip).getAllByText('603103').length).toBeGreaterThan(0);
    expect(within(firstChip).getByText('45')).toBeInTheDocument();
    expect(within(firstChip).getByText(/603103 · 盘前/)).toBeInTheDocument();
    expect(within(firstChip).getByText(/已分析 6 次/)).toBeInTheDocument();
    expect(within(firstChip).queryByText('51')).toBeNull();
    expect(within(firstChip).queryByText(/已分析 12 次/)).toBeNull();

    // 派能科技：真实情绪分 60
    expect(within(chips[1]).getByText('60')).toBeInTheDocument();
    // 没有真实涨跌幅字段时不得凭空渲染一个价格涨跌
    expect(within(card).queryByText(/\+\d+\.\d\d%/)).toBeNull();
  });

  it('shows no score instead of a placeholder when a watched stock has no analysis row', async () => {
    render(
      <UiLanguageProvider>
        <HomeDashboard
          watchlistCodes={['603103', '000001']}
          stockBarItems={stockBarFixture}
          marketReviewHistoryItems={[marketReviewItemFixture] as never}
        />
      </UiLanguageProvider>,
    );

    const card = await screen.findByTestId('playful-watchlist-card');
    const chips = await within(card).findAllByTestId('playful-stock-chip');
    // 000001 在 stock-bar 里没有任何真实记录：显式说明“暂无评分”，不编造数字
    expect(within(chips[1]).getByText('暂无评分')).toBeInTheDocument();
  });

  it('keeps the news meta line sourced from the intelligence pool total', async () => {
    renderDashboardWithRealRows();

    const card = await screen.findByTestId('playful-news-card');
    await waitFor(() => {
      expect(within(card).getByText('共 464 条资讯')).toBeInTheDocument();
    });
    expect(within(card).getByText('为你挑了 1 条')).toBeInTheDocument();
  });
});
