import { render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import DataInventoryPage from '../DataInventoryPage';

const { get } = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock('../../api/index', () => ({
  default: { get },
}));

const inventoryResponse = {
  generated_at: '2026-10-11T05:45:00+00:00',
  database: {
    path: '/Users/test/data/stock_analysis.db',
    exists: true,
    size_mb: 8.09,
  },
  groups: [
    {
      name: '行情 / 个股',
      tables: [
        {
          table: 'market_snapshot',
          label: '全市场横截面',
          note: '每次选股落库一份',
          rows: 5565,
          stocks: 5565,
          exists: true,
          latest: '2026-10-10',
          freshness: 'fresh',
          age_hours: 1.2,
          columns: ['code', 'industry', 'price'],
          fill: [
            { column: 'code', filled: 5565, pct: 100, empty: false },
            { column: 'industry', filled: 0, pct: 0, empty: true },
            { column: 'price', filled: 5565, pct: 100, empty: false },
          ],
        },
      ],
    },
    {
      name: '系统',
      tables: [
        {
          table: 'llm_usage',
          label: 'Token 用量',
          note: 'LLM 调用审计',
          rows: 690,
          stocks: 13,
          exists: true,
          latest: null,
          freshness: 'unknown',
          age_hours: null,
          columns: ['id', 'model'],
          fill: [
            { column: 'id', filled: 690, pct: 100, empty: false },
            { column: 'model', filled: 690, pct: 100, empty: false },
          ],
        },
      ],
    },
  ],
  gaps: [
    {
      table: 'market_snapshot',
      issue: 'empty_columns',
      detail: '全市场横截面 有整列为空: industry',
    },
    {
      table: 'screening_runs',
      issue: 'table_missing',
      detail: '选股运行 表不存在',
    },
  ],
};

function renderPage() {
  return render(
    <UiLanguageProvider>
      <DataInventoryPage />
    </UiLanguageProvider>
  );
}

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem('dsa.uiLanguage', 'zh');
  vi.clearAllMocks();
  get.mockResolvedValue({ data: inventoryResponse });
});

describe('DataInventoryPage', () => {
  it('renders each table row with its rows and stocks counts', async () => {
    renderPage();

    expect(await screen.findByRole('heading', { name: '数据观测' })).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith('/api/v1/data/inventory');

    const snapshotRow = screen.getByTestId('data-table-market_snapshot');
    expect(within(snapshotRow).getByText('全市场横截面')).toBeInTheDocument();
    // 行数与股票数都渲染在同一行里（该表两者都是 5565）
    expect(within(snapshotRow).getAllByText('5565')).toHaveLength(2);
    expect(within(snapshotRow).getByText('新鲜')).toBeInTheDocument();

    const usageRow = screen.getByTestId('data-table-llm_usage');
    expect(within(usageRow).getByText('690')).toBeInTheDocument();
    expect(within(usageRow).getByText('13')).toBeInTheDocument();
    expect(within(usageRow).getByText('未知')).toBeInTheDocument();
  });

  it('renders gaps with their label and detail text', async () => {
    renderPage();

    const gapCard = await screen.findByTestId('data-gap-market_snapshot-empty_columns-0');
    expect(within(gapCard).getByText('全市场横截面')).toBeInTheDocument();
    expect(within(gapCard).getByText('整列为空')).toBeInTheDocument();
    expect(within(gapCard).getByText('全市场横截面 有整列为空: industry')).toBeInTheDocument();

    // 表缺失属于另一类缺口，展示为危险等级而不是「整列为空」
    const missingCard = screen.getByTestId('data-gap-screening_runs-table_missing-1');
    expect(within(missingCard).getByText('表缺失')).toBeInTheDocument();
    expect(within(missingCard).getByText('选股运行 表不存在')).toBeInTheDocument();
  });

  it('flags empty columns in the column fill indicator', async () => {
    renderPage();

    const snapshotRow = await screen.findByTestId('data-table-market_snapshot');
    const emptyFlags = within(snapshotRow).getAllByTestId('empty-column-flag');
    expect(emptyFlags).toHaveLength(1);
    expect(emptyFlags[0]).toHaveTextContent('industry');

    // 没有全空列的表不应该出现红色标记
    const usageRow = screen.getByTestId('data-table-llm_usage');
    expect(within(usageRow).queryAllByTestId('empty-column-flag')).toHaveLength(0);
    expect(within(usageRow).getByText('每一列都有数据')).toBeInTheDocument();
  });

  it('shows the localized error alert when the request fails', async () => {
    get.mockRejectedValue(new Error('boom'));

    renderPage();

    expect(await screen.findByText('数据清单加载失败')).toBeInTheDocument();
    expect(screen.queryByTestId('data-table-market_snapshot')).not.toBeInTheDocument();
  });
});
