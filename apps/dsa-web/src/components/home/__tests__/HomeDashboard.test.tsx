import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { decisionSignalsApi } from '../../../api/decisionSignals';
import { historyApi } from '../../../api/history';
import { fetchIntelligenceItems } from '../../../api/intelligence';
import { systemConfigApi } from '../../../api/systemConfig';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
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
  vi.mocked(historyApi.getList).mockResolvedValue({
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
  } as Awaited<ReturnType<typeof historyApi.getList>>);
  vi.mocked(systemConfigApi.getWatchlist).mockResolvedValue(['600559', '999999']);
}

describe('HomeDashboard', () => {
  beforeEach(() => {
    localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
    vi.clearAllMocks();
    mockHappyPath();
  });

  it('renders real AI signal stats and the latest signals', async () => {
    renderDashboard();

    expect(await screen.findByText('63.2%')).toBeInTheDocument();
    expect(screen.getByText('命中率')).toBeInTheDocument();
    // 已完成 / 无法评估 是需求要求的最小指标集
    expect(screen.getByText('已完成')).toBeInTheDocument();
    expect(screen.getByText('19')).toBeInTheDocument();
    expect(screen.getByText('无法评估')).toBeInTheDocument();
    expect(screen.getByText('20')).toBeInTheDocument();
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
    // 索引里没有的代码必须原样展示，不能变成空白
    expect(screen.getByText('999999')).toBeInTheDocument();
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

    const alert = await screen.findByRole('alert');
    expect(alert).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '重试' }));

    await waitFor(() => {
      expect(vi.mocked(decisionSignalsApi.getOutcomeStats)).toHaveBeenCalledTimes(2);
    });
    expect(await screen.findByText('63.2%')).toBeInTheDocument();
  });
});
