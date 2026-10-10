import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { lazy } from 'react';
import type React from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { RouteOutletBoundary } from '../RouteBoundary';
import { Shell } from '../Shell';

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({
    authEnabled: false,
    logout: vi.fn().mockResolvedValue(undefined),
  }),
}));

vi.mock('../../../stores/agentChatStore', () => {
  const state = { completionBadge: false };

  return {
    useAgentChatStore: (selector?: (value: typeof state) => unknown) => (
      selector ? selector(state) : state
    ),
  };
});

describe('RouteOutletBoundary', () => {
  it('catches rejected lazy route imports inside the shell and resets on navigation', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const BrokenLazyRoute = lazy(() => (
      Promise.reject(new Error('chunk load failed')) as Promise<{ default: React.ComponentType }>
    ));

    try {
      render(
        <MemoryRouter initialEntries={['/chat']}>
          <Routes>
            <Route
              element={(
                <Shell>
                  <RouteOutletBoundary />
                </Shell>
              )}
            >
              <Route path="/chat" element={<BrokenLazyRoute />} />
              <Route path="/portfolio" element={<div data-testid="portfolio-page">Portfolio</div>} />
            </Route>
          </Routes>
        </MemoryRouter>,
      );

      expect(screen.getByRole('navigation', { name: '主导航' })).toBeInTheDocument();
      expect(await screen.findByRole('heading', { name: '页面加载失败' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '重新加载页面' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '返回首页' })).toBeInTheDocument();

      fireEvent.click(screen.getByRole('link', { name: '持仓' }));

      expect(await screen.findByTestId('portfolio-page')).toBeInTheDocument();
      expect(screen.queryByRole('heading', { name: '页面加载失败' })).not.toBeInTheDocument();
    } finally {
      consoleError.mockRestore();
    }
  });
});

describe('RouteOutletBoundary error detail', () => {
  it('shows the real error text so a failure can be reported instead of guessed at', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const BrokenLazyRoute = lazy(() => (
      Promise.reject(new TypeError('Cannot read properties of undefined (reading x)')) as Promise<{
        default: React.ComponentType;
      }>
    ));

    try {
      render(
        <MemoryRouter initialEntries={['/chat']}>
          <Routes>
            <Route
              element={(
                <Shell>
                  <RouteOutletBoundary />
                </Shell>
              )}
            >
              <Route path="/chat" element={<BrokenLazyRoute />} />
            </Route>
          </Routes>
        </MemoryRouter>,
      );

      await screen.findByRole('heading', { name: '页面加载失败' });
      // 泛泛的提示无法排查；真实错误必须出现在卡片上。
      expect(screen.getByText('技术详情')).toBeInTheDocument();
      expect(
        screen.getByText(/TypeError: Cannot read properties of undefined/),
      ).toBeInTheDocument();
    } finally {
      consoleError.mockRestore();
    }
  });
});

describe('RouteOutletBoundary stale-chunk recovery', () => {
  it('shows 正在更新 instead of the failure card when a chunk is stale', async () => {
    // 重建后旧标签页会请求已不存在的分包。此前会先把失败卡片渲染出来、再被
    // 自动重载打断，用户看到的就是「点进去先报一次失败」——可自愈的情况
    // 不该显示成失败。
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const reload = vi.fn();
    const originalReload = window.location.reload;
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, reload },
    });
    window.sessionStorage.clear();

    const StaleChunkRoute = lazy(() => (
      Promise.reject(new TypeError('Failed to fetch dynamically imported module: /assets/x.js')) as Promise<{
        default: React.ComponentType;
      }>
    ));

    try {
      render(
        <MemoryRouter initialEntries={['/chat']}>
          <Routes>
            <Route
              element={(
                <Shell>
                  <RouteOutletBoundary />
                </Shell>
              )}
            >
              <Route path="/chat" element={<StaleChunkRoute />} />
            </Route>
          </Routes>
        </MemoryRouter>,
      );

      expect(await screen.findByText('正在更新到最新版本…')).toBeInTheDocument();
      expect(screen.queryByRole('heading', { name: '页面加载失败' })).not.toBeInTheDocument();
      expect(reload).toHaveBeenCalled();
    } finally {
      consoleError.mockRestore();
      Object.defineProperty(window, 'location', {
        configurable: true,
        value: { ...window.location, reload: originalReload },
      });
      window.sessionStorage.clear();
    }
  });

  it('keeps reloading for a repeat failure, then stops at the window limit', async () => {
    // 旧行为是「15 秒内只许重载一次」，于是第二次失败直接落到失败卡片 —— 而第二次
    // 恰恰最常见（重载后用户立刻再点，或重载又撞上另一处旧引用）。现在改为窗口内
    // 计数：60 秒内最多 3 次，既能自愈连续故障，又不会因构建真损坏而无限重载。
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const reload = vi.fn();
    const originalReload = window.location.reload;
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, reload },
    });

    const StaleChunkRoute = lazy(() => (
      Promise.reject(new TypeError('Failed to fetch dynamically imported module: /assets/z.js')) as Promise<{
        default: React.ComponentType;
      }>
    ));

    const renderOnce = () => render(
      <MemoryRouter initialEntries={['/chat']}>
        <Routes>
          <Route
            element={(
              <Shell>
                <RouteOutletBoundary />
              </Shell>
            )}
          >
            <Route path="/chat" element={<StaleChunkRoute />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    try {
      // 已用掉 3 次额度 → 不再重载，显示失败卡片
      window.sessionStorage.clear();
      window.sessionStorage.setItem('dsh.chunkReloadAt', String(Date.now()));
      window.sessionStorage.setItem('dsh.chunkReloadCount', '3');
      const first = renderOnce();
      expect(await screen.findByRole('heading', { name: '页面加载失败' })).toBeInTheDocument();
      expect(reload).not.toHaveBeenCalled();
      first.unmount();

      // 额度未满 → 仍然重载
      window.sessionStorage.clear();
      window.sessionStorage.setItem('dsh.chunkReloadAt', String(Date.now()));
      window.sessionStorage.setItem('dsh.chunkReloadCount', '1');
      renderOnce();
      await waitFor(() => expect(reload).toHaveBeenCalled());
    } finally {
      consoleError.mockRestore();
      Object.defineProperty(window, 'location', {
        configurable: true,
        value: { ...window.location, reload: originalReload },
      });
      window.sessionStorage.clear();
    }
  });
});
