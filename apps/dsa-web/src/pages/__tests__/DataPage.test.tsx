import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import DataPage from '../DataPage';

// 两个子页面各自有独立的测试文件（DataInventoryPage.test.tsx / TokenUsagePage.test.tsx），
// 这里只验证「合并成一个数据页的两个 Tab」的契约，不重复它们的取数逻辑。
vi.mock('../DataInventoryPage', () => ({
  default: ({ embedded }: { embedded?: boolean }) => (
    <div data-testid="inventory-panel">inventory embedded={String(embedded)}</div>
  ),
}));

vi.mock('../TokenUsagePage', () => ({
  default: ({ embedded }: { embedded?: boolean }) => (
    <div data-testid="usage-panel">usage embedded={String(embedded)}</div>
  ),
}));

const LocationProbe = () => {
  const location = useLocation();
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>;
};

function renderDataPage(entry: string, initialTab?: 'inventory' | 'usage') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route
          path="/data"
          element={(
            <>
              <DataPage initialTab={initialTab} />
              <LocationProbe />
            </>
          )}
        />
        <Route
          path="/usage"
          element={(
            <>
              <DataPage initialTab="usage" />
              <LocationProbe />
            </>
          )}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe('DataPage', () => {
  it('默认展示数据观测 Tab，复用侧栏入口文案作为 Tab 名', () => {
    renderDataPage('/data');

    expect(screen.getByTestId('inventory-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('usage-panel')).not.toBeInTheDocument();
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((tab) => tab.textContent)).toEqual(['数据', '用量']);
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true');
  });

  it('支持 /data?tab=usage 直接进入用量 Tab', () => {
    renderDataPage('/data?tab=usage');

    expect(screen.getByTestId('usage-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('inventory-panel')).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '用量' })).toHaveAttribute('aria-selected', 'true');
  });

  it('旧的 /usage 路由预选用量 Tab，URL 保持不变', () => {
    renderDataPage('/usage');

    expect(screen.getByTestId('usage-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('inventory-panel')).not.toBeInTheDocument();
    // 不做重定向：旧链接必须继续原样可用。
    expect(screen.getByTestId('location')).toHaveTextContent('/usage');
  });

  it('点击 Tab 会切换到规范 URL 并切换面板', () => {
    renderDataPage('/usage');

    fireEvent.click(screen.getByRole('tab', { name: '数据' }));
    expect(screen.getByTestId('location')).toHaveTextContent('/data');
    expect(screen.getByTestId('inventory-panel')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: '用量' }));
    expect(screen.getByTestId('location')).toHaveTextContent('/data?tab=usage');
    expect(screen.getByTestId('usage-panel')).toBeInTheDocument();
  });

  it('两个子页面都以 embedded 方式渲染，避免 <main> 嵌套', () => {
    renderDataPage('/data?tab=usage');

    // embedded=true 表示子页面不再各自渲染 AppPage（由 DataPage 统一提供）。
    expect(screen.getByTestId('usage-panel')).toHaveTextContent('embedded=true');
  });
});
