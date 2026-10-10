import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { stockProfileApi } from '../../../api/stockProfile';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import type { StockProfileResponse } from '../../../types/stockProfile';
import { StockProfilePanel } from '../StockProfilePanel';

vi.mock('../../../api/stockProfile', () => ({
  stockProfileApi: {
    getProfile: vi.fn(),
  },
}));

function buildProfile(overrides: Partial<StockProfileResponse> = {}): StockProfileResponse {
  return {
    requestedCode: '002567',
    canonicalCode: '002567',
    market: 'cn',
    asOf: '2026-10-11T03:22:11+08:00',
    quote: {
      status: 'fresh',
      data: {
        stockCode: '002567',
        stockName: '唐人神',
        currentPrice: 3.67,
        change: 0.05,
        changePercent: 1.38,
        open: 3.62,
        high: 3.68,
        low: 3.6,
        prevClose: 3.62,
        volume: 14149000,
        amount: 51539681,
        updateTime: '2026-10-11T03:21:57',
      },
      limitations: [],
    },
    history: {
      status: 'fresh',
      period: 'daily',
      data: [
        { date: '2026-10-08', open: 3.6, high: 3.65, low: 3.58, close: 3.62, volume: 1000, amount: 2000, changePercent: 0.5 },
        { date: '2026-10-09', open: 3.62, high: 3.68, low: 3.6, close: 3.67, volume: 14149005, amount: 51539681, changePercent: 1.38 },
      ],
      limitations: [],
    },
    research: {
      status: 'unavailable',
      data: { latestReport: null, recentReports: [], structuredReport: null },
      limitations: ['no_reports'],
    },
    intelligence: {
      status: 'unavailable',
      items: [],
      limitations: ['no_symbol_intelligence'],
    },
    portfolio: {
      status: 'partial',
      data: { held: false, matchedMarkets: [] },
      limitations: ['cached_positions_only'],
    },
    monitors: {
      status: 'fresh',
      data: { totalRuleCount: 0, enabledRuleCount: 0, ruleIds: [] },
      limitations: [],
    },
    evidenceQuality: {
      status: 'partial',
      blocks: {
        quote: 'fresh',
        history: 'fresh',
        research: 'unavailable',
        intelligence: 'unavailable',
        portfolio: 'partial',
        monitors: 'fresh',
      },
      limitations: ['no_reports', 'no_symbol_intelligence', 'cached_positions_only'],
    },
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function renderPanel(stockCode = '002567') {
  return render(
    <UiLanguageProvider>
      <StockProfilePanel stockCode={stockCode} />
    </UiLanguageProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem('dsa.uiLanguage', 'zh');
  vi.clearAllMocks();
});

describe('StockProfilePanel', () => {
  it('renders the fresh quote price, change and daily-bar coverage', async () => {
    vi.mocked(stockProfileApi.getProfile).mockResolvedValue(buildProfile());

    renderPanel();

    expect(await screen.findByText('3.67')).toBeInTheDocument();
    expect(screen.getByText('唐人神')).toBeInTheDocument();
    expect(screen.getByText('+0.05')).toBeInTheDocument();
    expect(screen.getByText('+1.38%')).toBeInTheDocument();
    expect(screen.getByText('可用 2 根日线 · 最新 2026-10-09')).toBeInTheDocument();
    expect(stockProfileApi.getProfile).toHaveBeenCalledWith('002567');
    // 证据质量必须逐块给出状态，而不是只给一个总状态。
    expect(screen.getByText('证据质量')).toBeInTheDocument();
    expect(screen.getAllByText('已就绪').length).toBeGreaterThan(1);
    expect(screen.getAllByText('暂无').length).toBeGreaterThan(0);
  });

  it('states the reason for an unavailable block instead of rendering a blank card', async () => {
    vi.mocked(stockProfileApi.getProfile).mockResolvedValue(buildProfile());

    renderPanel();

    expect(await screen.findByText('信息池目前没有收录该股票的个股级资讯。')).toBeInTheDocument();
    expect(screen.getByText('信息池中没有该股票的分析报告记录。')).toBeInTheDocument();
    // 板块标题仍然在，用户能看出「这块查过了，结论是没有覆盖」。
    expect(screen.getByText('个股资讯')).toBeInTheDocument();
    expect(screen.getByText('研究/报告')).toBeInTheDocument();
  });

  it('explains an unavailable quote and an empty daily-bar block with their reason codes', async () => {
    vi.mocked(stockProfileApi.getProfile).mockResolvedValue(buildProfile({
      quote: { status: 'unavailable', data: null, limitations: ['quote_unavailable'] },
      history: { status: 'unavailable', period: 'daily', data: [], limitations: ['history_unavailable'] },
    }));

    renderPanel();

    expect(await screen.findByText('行情源暂未返回该股票的实时行情。')).toBeInTheDocument();
    expect(screen.getByText('该股票暂无可用日线数据。')).toBeInTheDocument();
    expect(screen.getByText('实时行情')).toBeInTheDocument();
    expect(screen.getByText('日线数据')).toBeInTheDocument();
    expect(screen.queryByText('最新价')).not.toBeInTheDocument();
  });

  it('renders intelligence items with source, time and link when coverage exists', async () => {
    const profile = buildProfile();
    profile.intelligence = {
      status: 'fresh',
      items: [
        {
          id: 11,
          sourceName: '证券时报',
          sourceType: 'news',
          title: '唐人神发布三季度业绩预告',
          url: 'https://example.com/news/11',
          source: 'stcn',
          publishedAt: '2026-10-10T09:30:00+08:00',
          scopeType: 'symbol',
          scopeValue: '002567',
          market: 'cn',
        },
      ],
      limitations: [],
    };
    vi.mocked(stockProfileApi.getProfile).mockResolvedValue(profile);

    renderPanel();

    const link = await screen.findByRole('link', { name: '唐人神发布三季度业绩预告' });
    expect(link).toHaveAttribute('href', 'https://example.com/news/11');
    expect(screen.getByText(/证券时报/)).toBeInTheDocument();
  });

  it('ignores an out-of-order response so a stale stock cannot clobber a newer one', async () => {
    const firstRequest = deferred<StockProfileResponse>();
    const secondRequest = deferred<StockProfileResponse>();
    vi.mocked(stockProfileApi.getProfile)
      .mockReturnValueOnce(firstRequest.promise)
      .mockReturnValueOnce(secondRequest.promise);

    const { rerender } = renderPanel('002567');
    rerender(
      <UiLanguageProvider>
        <StockProfilePanel stockCode="600519" />
      </UiLanguageProvider>,
    );

    const newerProfile = buildProfile({
      requestedCode: '600519',
      canonicalCode: '600519',
      quote: {
        status: 'fresh',
        data: {
          stockCode: '600519',
          stockName: '贵州茅台',
          currentPrice: 1263,
          change: 7.21,
          changePercent: 0.57,
        },
        limitations: [],
      },
    });
    await act(async () => {
      secondRequest.resolve(newerProfile);
      await secondRequest.promise;
    });

    expect(await screen.findByText('贵州茅台')).toBeInTheDocument();

    await act(async () => {
      firstRequest.resolve(buildProfile());
      await firstRequest.promise;
    });

    // 晚到的旧股票响应必须被丢弃：既不能换成旧名字，也不能换成旧价格。
    expect(screen.queryByText('唐人神')).not.toBeInTheDocument();
    expect(screen.queryByText('3.67')).not.toBeInTheDocument();
    expect(screen.getByText('贵州茅台')).toBeInTheDocument();
    expect(screen.getByText('1263.00')).toBeInTheDocument();
  });

  it('shows the API error alert instead of a silent blank when the profile request fails', async () => {
    vi.mocked(stockProfileApi.getProfile).mockRejectedValue(new Error('profile down'));

    renderPanel();

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('个股信息加载失败');
    expect(alert).toHaveTextContent('profile down');
  });
});
