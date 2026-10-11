import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  fetchEnabledIntelligenceSources,
  fetchIntelligenceItems,
  fetchIntelligenceSources,
} from '../../api/intelligence';
import type { IntelligenceItem, IntelligenceSource } from '../../api/intelligence';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import { UI_LANGUAGE_STORAGE_KEY } from '../../utils/uiLanguage';
import IntelligencePage from '../IntelligencePage';

// 只 mock 资讯 API：playful 改造属于视觉层，测试要盯住的是
// 「接口返回什么就渲染什么」以及分页/筛选/展开这些既有行为没有被动到。
vi.mock('../../api/intelligence', () => ({
  fetchIntelligenceItems: vi.fn(),
  fetchIntelligenceSources: vi.fn(),
  fetchEnabledIntelligenceSources: vi.fn(),
}));

/** 真实资讯池条目的字段形状（snake_case 直接透传，与 api/intelligence.ts 一致）。 */
const itemsFixture: IntelligenceItem[] = [
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
  {
    id: 1900,
    source_id: 3,
    source_name: '东方财富快讯',
    source_type: 'rss',
    title: '央行开展 6000 亿元 MLF 操作',
    summary: '为维护银行体系流动性合理充裕，央行今日开展 6000 亿元中期借贷便利操作。',
    url: 'https://finance.eastmoney.com/a/202610121234567890.html',
    source: '东方财富快讯',
    published_at: '2026-10-12T09:30:00+08:00',
    fetched_at: '2026-10-12T09:31:00+08:00',
    scope_type: 'symbol',
    scope_value: '600519',
    market: 'cn',
  },
];

const sourcesFixture: IntelligenceSource[] = [
  {
    id: 3,
    name: '东方财富快讯',
    source_type: 'rss',
    url: 'https://finance.eastmoney.com/rss',
    enabled: true,
    scope_type: 'market',
    scope_value: null,
    market: 'cn',
    description: null,
    last_status: 'ok',
    last_error: null,
    last_fetched_at: '2026-10-12T09:31:00+08:00',
    created_at: null,
    updated_at: null,
  },
  {
    id: 7,
    name: '失效情报源',
    source_type: 'rss',
    url: 'https://example.com/feed',
    enabled: false,
    scope_type: 'symbol',
    scope_value: '600519',
    market: 'cn',
    description: null,
    last_status: 'error',
    last_error: 'HTTP 404',
    last_fetched_at: null,
    created_at: null,
    updated_at: null,
  },
];

function renderPage() {
  return render(
    <UiLanguageProvider>
      <IntelligencePage />
    </UiLanguageProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
  vi.mocked(fetchIntelligenceItems).mockImplementation(async (params = {}) => ({
    items: itemsFixture,
    total: 464,
    page: params.page ?? 1,
    page_size: 20,
  }));
  vi.mocked(fetchIntelligenceSources).mockResolvedValue({
    items: sourcesFixture,
    total: sourcesFixture.length,
    page: 1,
    page_size: 20,
  });
  vi.mocked(fetchEnabledIntelligenceSources).mockResolvedValue({
    source_count: 2,
    saved_count: 5,
  });
});

describe('IntelligencePage (playful shell)', () => {
  it('renders the real headlines returned by the API together with the API-reported total', async () => {
    renderPage();

    expect(await screen.findByText('中远海能')).toBeInTheDocument();
    expect(screen.getByText('央行开展 6000 亿元 MLF 操作')).toBeInTheDocument();
    // 总数只来自接口 total（464），页面只有 2 行也不会被渲染成 2
    expect(screen.getAllByText('464').length).toBeGreaterThan(0);
    // 首屏请求参数保持原样：默认近 3 天、第 1 页、每页 20 条
    expect(vi.mocked(fetchIntelligenceItems)).toHaveBeenCalledWith(
      expect.objectContaining({
        market: undefined,
        scope_type: undefined,
        days: 3,
        query: undefined,
        page: 1,
        page_size: 20,
      }),
    );
  });

  it('keeps the list summary honest when a page holds fewer rows than the total', async () => {
    renderPage();

    expect(
      await screen.findAllByText('共 464 条 · 第 1 / 24 页'),
    ).not.toHaveLength(0);
  });

  it('numbers rows continuously across pages and refetches with the next page', async () => {
    renderPage();

    expect(await screen.findByLabelText('第 1 条资讯')).toBeInTheDocument();

    fireEvent.click(screen.getByText('下一页'));

    // 第 2 页首行序号是 21，不是重新从 1 开始
    expect(await screen.findByLabelText('第 21 条资讯')).toBeInTheDocument();
    await waitFor(() =>
      expect(vi.mocked(fetchIntelligenceItems)).toHaveBeenLastCalledWith(
        expect.objectContaining({ page: 2 }),
      ),
    );
  });

  it('sends the same filter params as before when the market filter changes', async () => {
    renderPage();
    await screen.findByText('中远海能');

    fireEvent.change(screen.getByLabelText('市场'), { target: { value: 'us' } });

    await waitFor(() =>
      expect(vi.mocked(fetchIntelligenceItems)).toHaveBeenLastCalledWith(
        expect.objectContaining({ market: 'us', page: 1 }),
      ),
    );
  });

  it('expands and collapses a summary without refetching', async () => {
    renderPage();
    await screen.findByText('中远海能');

    fireEvent.click(screen.getAllByText('展开摘要')[0]);
    expect(screen.getAllByText('收起摘要').length).toBeGreaterThan(0);

    fireEvent.click(screen.getAllByText('收起摘要')[0]);
    await waitFor(() => expect(screen.queryAllByText('收起摘要')).toHaveLength(0));

    expect(vi.mocked(fetchIntelligenceItems)).toHaveBeenCalledTimes(1);
  });

  it('shows the playful empty state when the pool returns nothing', async () => {
    vi.mocked(fetchIntelligenceItems).mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 20,
    });

    renderPage();

    expect(await screen.findByText('暂无资讯')).toBeInTheDocument();
    expect(screen.queryByText('展开摘要')).not.toBeInTheDocument();
  });

  it('renders every source with its real enabled state and last error', async () => {
    renderPage();

    // 同一个源名会同时出现在资讯行的 source 字段和情报源列表里
    expect(await screen.findAllByText('东方财富快讯')).not.toHaveLength(0);
    expect(screen.getByText('已启用')).toBeInTheDocument();
    expect(screen.getByText('已停用')).toBeInTheDocument();
    expect(screen.getByText('HTTP 404')).toBeInTheDocument();
  });

  it('surfaces the fetch-now result notice without inventing counts', async () => {
    renderPage();
    await screen.findByText('中远海能');

    fireEvent.click(screen.getByText('立即拉取'));

    expect(
      await screen.findByText('拉取完成：处理 2 个源，新增 5 条'),
    ).toBeInTheDocument();
    expect(vi.mocked(fetchEnabledIntelligenceSources)).toHaveBeenCalledTimes(1);
  });
});
