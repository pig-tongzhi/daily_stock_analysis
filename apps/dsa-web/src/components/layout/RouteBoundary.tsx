import type React from 'react';
import { Component, Suspense } from 'react';
import type { ErrorInfo } from 'react';
import { Outlet, useLocation } from 'react-router-dom';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { isChunkLoadError, reloadForFreshBundle } from '../../utils/chunkReload';

type PageLoadingFallbackProps = {
  fullPage?: boolean;
};

export const PageLoadingFallback: React.FC<PageLoadingFallbackProps> = ({ fullPage = true }) => (
  <div
    className={
      fullPage
        ? 'flex min-h-screen items-center justify-center bg-base'
        : 'flex min-h-[60vh] items-center justify-center'
    }
  >
    <div className="h-8 w-8 animate-spin rounded-full border-2 border-cyan/20 border-t-cyan" />
  </div>
);

type RouteErrorBoundaryProps = {
  children: React.ReactNode;
  resetKey: string;
  fullPage: boolean;
  text: {
    title: string;
    description: string;
    reload: string;
    backHome: string;
    updating: string;
  };
};

type RouteErrorBoundaryState = {
  hasError: boolean;
  /**
   * 真实错误信息。此前这里只保留 hasError，卡片对【任何】异常都显示同一句
   * 泛泛的提示，于是真正的失败原因不可见 —— 排查时只能在浏览器外部猜。
   * 保留原文后，用户看到的、截图给我的就是确切原因。
   */
  detail: string;
  /**
   * 因 stale chunk 正在自动重载。
   *
   * 重建后旧标签页会请求已不存在的分包，此时自动重载是对的，但此前仍会先把
   * 错误卡片渲染出来、再被重载打断 —— 用户看到的就是「点进去先报一次失败」。
   * 既然是可自动恢复的情况，就不该显示失败，而应显示正在更新。
   */
  reloading: boolean;
};

class RouteErrorBoundary extends Component<RouteErrorBoundaryProps, RouteErrorBoundaryState> {
  override state: RouteErrorBoundaryState = {
    hasError: false,
    detail: '',
    reloading: false,
  };

  static getDerivedStateFromError(error: unknown): RouteErrorBoundaryState {
    const detail =
      error instanceof Error
        ? `${error.name}: ${error.message}`
        : typeof error === 'string'
          ? error
          : String(error);
    return { hasError: true, detail, reloading: false };
  }

  override componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    // 带上路由与组件栈：控制台里这一条就够定位，不必再复现。
    console.error(
      'Route page failed to render or load',
      { resetKey: this.props.resetKey, message: error?.message, stack: error?.stack },
      errorInfo,
    );
    // A rebuilt bundle leaves this page holding stale chunk URLs, which surfaces
    // here as a failed dynamic import rather than anything the user did wrong.
    // Reload once to pick up the new manifest; anything else keeps the card.
    if (isChunkLoadError(error) && reloadForFreshBundle()) {
      // 重载已经发起：把卡片换成「正在更新」，别让可自愈的情况显示成失败。
      this.setState({ reloading: true });
    }
  }

  override componentDidUpdate(prevProps: RouteErrorBoundaryProps) {
    if (this.state.hasError && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ hasError: false, detail: '', reloading: false });
    }
  }

  override render() {
    if (!this.state.hasError) {
      return this.props.children;
    }

    // 可自动恢复：显示正在更新，而不是一张会被立刻打断的失败卡片。
    if (this.state.reloading) {
      return (
        <div
          className={
            this.props.fullPage
              ? 'flex min-h-screen flex-col items-center justify-center gap-4 bg-base px-4'
              : 'flex min-h-[60vh] flex-col items-center justify-center gap-4 px-2 py-8'
          }
        >
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-cyan/20 border-t-cyan" />
          <p className="text-sm text-secondary-text">{this.props.text.updating}</p>
        </div>
      );
    }

    return (
      <div
        className={
          this.props.fullPage
            ? 'flex min-h-screen items-center justify-center bg-base px-4'
            : 'flex min-h-[60vh] items-center justify-center px-2 py-8'
        }
      >
        <div className="w-full max-w-md rounded-2xl border border-border bg-card/94 p-6 text-center shadow-soft-card">
          <h1 className="text-xl font-semibold text-foreground">{this.props.text.title}</h1>
          <p className="mt-3 text-sm leading-6 text-secondary-text">
            {this.props.text.description}
          </p>
          {this.state.detail ? (
            // 显示真实原因：泛泛的提示无法排查，也让人无法准确转述问题。
            <details className="mt-4 rounded-xl border border-border/70 bg-base/60 p-3 text-left">
              <summary className="cursor-pointer text-xs font-medium text-secondary-text">
                技术详情
              </summary>
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-all text-[11px] leading-5 text-secondary-text">
                {this.state.detail}
              </pre>
            </details>
          ) : null}
          <div className="mt-5 flex flex-col gap-3 sm:flex-row sm:justify-center">
            <button
              type="button"
              className="btn-primary"
              onClick={() => window.location.reload()}
            >
              {this.props.text.reload}
            </button>
            <button
              type="button"
              className="rounded-xl border border-border/70 bg-card px-4 py-2 text-sm font-medium text-foreground transition-colors hover:bg-hover"
              onClick={() => window.location.assign('/')}
            >
              {this.props.text.backHome}
            </button>
          </div>
        </div>
      </div>
    );
  }
}

export const RouteBoundary: React.FC<{ children: React.ReactNode; fullPage?: boolean }> = ({
  children,
  fullPage = true,
}) => {
  const location = useLocation();
  const { t } = useUiLanguage();
  const resetKey = `${location.pathname}${location.search}`;

  return (
    <RouteErrorBoundary
      resetKey={resetKey}
      fullPage={fullPage}
      text={{
        title: t('routeError.title'),
        description: t('routeError.description'),
        reload: t('routeError.reload'),
        backHome: t('routeError.backHome'),
        updating: t('routeError.updating'),
      }}
    >
      <Suspense fallback={<PageLoadingFallback fullPage={fullPage} />}>{children}</Suspense>
    </RouteErrorBoundary>
  );
};

export const RouteOutletBoundary: React.FC = () => (
  <RouteBoundary fullPage={false}>
    <Outlet />
  </RouteBoundary>
);

export const StandaloneRouteBoundary: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <RouteBoundary fullPage>
    {children}
  </RouteBoundary>
);
