import React, { useEffect, useState } from 'react';
import { Activity, BarChart3, Bell, BriefcaseBusiness, Database, Gauge, Home, LogOut, MessageSquareQuote, Newspaper, Search, Settings2 } from 'lucide-react';
import { NavLink, useLocation } from 'react-router-dom';
import { SCREENING_CONFIG_CHANGED_EVENT, SYSTEM_CONFIG_CHANGED_EVENT, screeningApi } from '../../api/screening';
import { useAuth } from '../../contexts/AuthContext';
import { useAgentChatStore } from '../../stores/agentChatStore';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiTextKey } from '../../i18n/uiText';
import { cn } from '../../utils/cn';
import { ConfirmDialog } from '../common/ConfirmDialog';
import { StatusDot } from '../common/StatusDot';
import { UiLanguageToggle } from '../i18n/UiLanguageToggle';
import { ThemeToggle } from '../theme/ThemeToggle';

type SidebarNavProps = {
  collapsed?: boolean;
  onNavigate?: () => void;
  variant?: 'default' | 'rail';
};

type NavItem = {
  key: string;
  labelKey: UiTextKey;
  to: string;
  icon: React.ComponentType<{ className?: string }>;
  exact?: boolean;
  badge?: 'completion';
  /**
   * 额外算作“当前项”的路径。用于入口已经合并、但独立路由必须保留的场景：
   * /usage 已并入 /data，从 /usage 进来时仍然要高亮「数据」。
   */
  matchPaths?: string[];
  /**
   * 默认是否从侧栏隐藏。
   *
   * 与既有的 showScreeningNav 是同一套机制：入口定义永远保留在 NAV_ITEMS 里，
   * 只由过滤条件决定是否渲染，因此隐藏/恢复都不会影响已经注册的路由。
   * true 表示“功能仍在、路由仍可直达，只是不再占据侧栏名额”。
   */
  hiddenByDefault?: boolean;
};

/**
 * 侧栏入口（当前可见 8 项）。
 *
 * 隐藏项一律用「过滤」而不是「删除」的方式下线：页面组件、路由、
 * 接口调用全部保持原样，只有侧栏这一处不渲染，避免出现“入口没了、
 * 代码也一起被删掉、想恢复得重写一遍”的情况。
 * 恢复方式：把对应条目的 hiddenByDefault 改成 false。
 */
const NAV_ITEMS: NavItem[] = [
  { key: 'home', labelKey: 'layout.nav.home', to: '/', icon: Home, exact: true },
  { key: 'chat', labelKey: 'layout.nav.chat', to: '/chat', icon: MessageSquareQuote, badge: 'completion' },
  { key: 'screening', labelKey: 'layout.nav.screening', to: '/screening', icon: Search },
  { key: 'portfolio', labelKey: 'layout.nav.portfolio', to: '/portfolio', icon: BriefcaseBusiness },
  { key: 'decision-signals', labelKey: 'layout.nav.decisionSignals', to: '/decision-signals', icon: Activity },
  { key: 'intelligence', labelKey: 'layout.nav.intelligence', to: '/intelligence', icon: Newspaper },
  {
    key: 'backtest',
    labelKey: 'layout.nav.backtest',
    to: '/backtest',
    icon: BarChart3,
    // 隐藏原因：两张回测表（backtest_runs / backtest_results）当前都是空的，
    // 没有任何可回测的数据，入口留在侧栏只会把人带到一个永远空白的页面。
    // 路由 /backtest 与 BacktestPage 仍然保留，可直接用 URL 访问。
    hiddenByDefault: true,
  },
  {
    key: 'alerts',
    labelKey: 'layout.nav.alerts',
    to: '/alerts',
    icon: Bell,
    // 隐藏原因：告警功能完全未被使用——四张告警表都没有数据，页面只会渲染空态。
    // 路由 /alerts 与 AlertsPage 仍然保留，可直接用 URL 访问。
    hiddenByDefault: true,
  },
  {
    key: 'usage',
    labelKey: 'layout.nav.usage',
    to: '/usage',
    icon: Gauge,
    // 隐藏原因：用量与数据都属于系统可观测面，并排两个侧栏入口是冗余的，
    // 已合并为「数据」页的第二个 Tab（见 pages/DataPage.tsx）。
    // 路由 /usage 仍然保留（预选用量 Tab），旧链接和既有测试都不会失效。
    hiddenByDefault: true,
  },
  {
    key: 'data',
    labelKey: 'layout.nav.data',
    to: '/data',
    icon: Database,
    // /usage 已并入本页，从旧的 /usage 链接进来时仍然高亮这一项。
    matchPaths: ['/usage'],
  },
  { key: 'settings', labelKey: 'layout.nav.settings', to: '/settings', icon: Settings2 },
];

/**
 * 是否在侧栏展示某个入口。
 *
 * 与既有实现保持一致：选股入口由后端配置开关（showScreeningNav）决定，
 * 其余入口由定义上的 hiddenByDefault 决定，默认可见。
 */
function shouldShowNavItem(item: NavItem, showScreeningNav: boolean): boolean {
  if (item.key === 'screening') {
    return showScreeningNav;
  }
  return item.hiddenByDefault !== true;
}

/** 当前路径是否属于该入口（含 matchPaths 里合并进来的旧入口）。 */
function isNavItemActive(item: NavItem, pathname: string, navLinkActive: boolean): boolean {
  if (navLinkActive) {
    return true;
  }
  return (item.matchPaths ?? []).some(
    (path) => pathname === path || pathname.startsWith(`${path}/`),
  );
}

export const SidebarNav: React.FC<SidebarNavProps> = ({ collapsed = false, onNavigate, variant = 'default' }) => {
  const { authEnabled, logout } = useAuth();
  const { t } = useUiLanguage();
  const completionBadge = useAgentChatStore((state) => state.completionBadge);
  const [showLogoutConfirm, setShowLogoutConfirm] = useState(false);
  const [showScreeningNav, setShowScreeningNav] = useState(false);

  useEffect(() => {
    let active = true;

    const refreshScreeningStatus = async () => {
      try {
        const status = await screeningApi.getStatus();
        if (active) {
          setShowScreeningNav(status.enabled);
        }
      } catch {
        if (active) {
          setShowScreeningNav(false);
        }
      }
    };

    void refreshScreeningStatus();
    window.addEventListener(SCREENING_CONFIG_CHANGED_EVENT, refreshScreeningStatus);
    window.addEventListener(SYSTEM_CONFIG_CHANGED_EVENT, refreshScreeningStatus);

    return () => {
      active = false;
      window.removeEventListener(SCREENING_CONFIG_CHANGED_EVENT, refreshScreeningStatus);
      window.removeEventListener(SYSTEM_CONFIG_CHANGED_EVENT, refreshScreeningStatus);
    };
  }, []);

  // 统一走过滤：可见入口 = 选股开关 + 每条自己的 hiddenByDefault。
  const navItems = NAV_ITEMS.filter((item) => shouldShowNavItem(item, showScreeningNav));
  const { pathname } = useLocation();
  const isRail = variant === 'rail';
  const itemBaseClass = cn(
    'group relative flex h-[var(--nav-item-height)] w-full items-center overflow-hidden rounded-2xl border border-transparent text-sm leading-none text-secondary-text transition-all',
    isRail
      ? 'justify-center gap-2.5 px-2'
      : collapsed
        ? 'justify-center px-0'
        : 'gap-3 px-[var(--nav-item-padding-x)]'
  );
  const itemInteractiveClass = cn(
    itemBaseClass,
    'hover:bg-[var(--nav-hover-bg)] hover:text-foreground'
  );
  const itemActiveClass = 'border-[var(--nav-active-border)] bg-[var(--nav-active-bg)] font-medium text-[hsl(var(--primary))]';
  const itemIconClass = cn(isRail ? 'h-[18px] w-[18px]' : 'h-5 w-5', 'shrink-0');
  const itemLabelClass = cn('truncate', isRail ? 'text-center' : '');

  return (
    <div className="flex h-full flex-col">
      <div
        className={cn(
          'flex items-center',
          isRail ? 'mb-5 justify-center gap-2 pt-1' : 'mb-4 gap-2 px-1',
          collapsed || isRail ? 'justify-center' : ''
        )}
      >
        <div
          className={cn(
            'flex items-center justify-center bg-primary-gradient text-[hsl(var(--primary-foreground))] shadow-[0_12px_28px_var(--nav-brand-shadow)]',
            isRail ? 'h-9 w-9 rounded-[1rem]' : 'h-10 w-10 rounded-2xl'
          )}
        >
          <BarChart3 className={cn(isRail ? 'h-[19px] w-[19px]' : 'h-5 w-5')} />
        </div>
        {!collapsed ? (
          <p className={cn('min-w-0 truncate font-semibold text-foreground', isRail ? 'text-[0.95rem] leading-none' : 'text-sm')}>rjharness</p>
        ) : null}
      </div>

      <nav className={cn('flex flex-col gap-1.5', isRail ? '' : 'flex-1')} aria-label={t('layout.mainNav')}>
        {navItems.map((item) => {
          const { key, labelKey, to, icon: Icon, exact, badge } = item;
          const label = t(labelKey);
          /** NavLink 自身的 isActive 只认 `to`，matchPaths 用来兼容已合并的旧入口。 */
          const resolveActive = (navLinkActive: boolean) => isNavItemActive(item, pathname, navLinkActive);
          return (
          <NavLink
            key={key}
            to={to}
            end={exact}
            onClick={onNavigate}
            aria-label={label}
            className={({ isActive }) =>
              cn(
                itemInteractiveClass,
                resolveActive(isActive) ? itemActiveClass : ''
              )
            }
          >
            {({ isActive }) => (
              <>
                <Icon
                  className={cn(
                    itemIconClass,
                    resolveActive(isActive) ? 'text-[var(--nav-icon-active)]' : 'text-current'
                  )}
                />
                {!collapsed ? <span className={itemLabelClass}>{label}</span> : null}
                {badge === 'completion' && completionBadge ? (
                  <StatusDot
                    tone="info"
                    data-testid="chat-completion-badge"
                    className={cn(
                      'absolute right-3 border-2 border-background shadow-[0_0_10px_var(--nav-indicator-shadow)]',
                      collapsed ? 'right-2 top-2' : ''
                    )}
                    aria-label={t('layout.newChatMessage')}
                  />
                ) : null}
              </>
            )}
          </NavLink>
        );
        })}

        <ThemeToggle
          variant={isRail ? 'rail' : 'nav'}
          collapsed={collapsed}
          wrapperClassName="w-full"
          triggerClassName={itemInteractiveClass}
          triggerActiveClassName={itemActiveClass}
          iconClassName={itemIconClass}
          labelClassName={itemLabelClass}
        />
        <UiLanguageToggle
          variant={isRail ? 'rail' : 'nav'}
          collapsed={collapsed}
          wrapperClassName="w-full"
          triggerClassName={itemInteractiveClass}
          triggerActiveClassName={itemActiveClass}
          iconClassName={itemIconClass}
          labelClassName={itemLabelClass}
        />
      </nav>

      {authEnabled ? (
        <button
          type="button"
          onClick={() => setShowLogoutConfirm(true)}
          className={cn(
            itemInteractiveClass,
            isRail ? 'mt-1.5' : 'mt-5'
          )}
        >
          <LogOut className={itemIconClass} />
          {!collapsed ? <span className={itemLabelClass}>{t('layout.logout')}</span> : null}
        </button>
      ) : null}

      <ConfirmDialog
        isOpen={showLogoutConfirm}
        title={t('layout.logoutTitle')}
        message={t('layout.logoutMessage')}
        confirmText={t('layout.logoutConfirm')}
        cancelText={t('common.cancel')}
        isDanger
        onConfirm={() => {
          setShowLogoutConfirm(false);
          onNavigate?.();
          void logout();
        }}
        onCancel={() => setShowLogoutConfirm(false)}
      />
    </div>
  );
};
