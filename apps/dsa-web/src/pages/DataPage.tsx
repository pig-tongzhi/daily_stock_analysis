import type React from 'react';
import { Database, Gauge } from 'lucide-react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { AppPage } from '../components/common';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { UiTextKey } from '../i18n/uiText';
import DataInventoryPage from './DataInventoryPage';
import TokenUsagePage from './TokenUsagePage';

/**
 * 「数据」页：把原本两个侧栏入口合并成一个页面里的两个 Tab。
 *
 * 为什么合并：数据观测（数据库实际内容/列填充/缺口）和 Token 用量
 * 都是系统可观测面，并排占两个侧栏入口是冗余的。合并后侧栏只留「数据」，
 * 但 /usage 路由继续保留（本文件用 initialTab='usage' 预选第二个 Tab），
 * 旧书签、外部链接和既有测试都不会失效。
 *
 * URL 约定：
 * - /data            → 数据观测 Tab
 * - /data?tab=usage  → 用量 Tab
 * - /usage           → 直接渲染本页并预选用量 Tab（不重定向，保持旧 URL 可用）
 */

/** Tab 的稳定标识，同时也是 URL 查询参数 `tab` 的取值。 */
export type ObservabilityTab = 'inventory' | 'usage';

const TAB_QUERY_KEY = 'tab';

type TabDefinition = {
  id: ObservabilityTab;
  /** 复用侧栏的入口文案，保证 Tab 名和菜单名完全一致。 */
  labelKey: UiTextKey;
  icon: React.ComponentType<{ className?: string }>;
};

const TABS: TabDefinition[] = [
  { id: 'inventory', labelKey: 'layout.nav.data', icon: Database },
  { id: 'usage', labelKey: 'layout.nav.usage', icon: Gauge },
];

function isObservabilityTab(value: string | null): value is ObservabilityTab {
  return value === 'inventory' || value === 'usage';
}

export interface DataPageProps {
  /** 进入本页时没有显式 `?tab=` 时使用的 Tab；/usage 旧路由传 'usage'。 */
  initialTab?: ObservabilityTab;
}

const DataPage: React.FC<DataPageProps> = ({ initialTab = 'inventory' }) => {
  const { t } = useUiLanguage();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const paramTab = searchParams.get(TAB_QUERY_KEY);
  // URL 参数优先于路由预设，这样 /data?tab=usage 可以被直接分享。
  const activeTab: ObservabilityTab = isObservabilityTab(paramTab) ? paramTab : initialTab;

  const selectTab = (tab: ObservabilityTab) => {
    // 切换 Tab 后统一落到规范 URL：/data 与 /data?tab=usage。
    navigate(tab === 'usage' ? `/data?${TAB_QUERY_KEY}=usage` : '/data');
  };

  return (
    <AppPage>
      <div role="tablist" aria-label={t('dataPage.tabsLabel')} className="playful-tabs mb-4">
        {TABS.map(({ id, labelKey, icon: Icon }) => {
          const selected = activeTab === id;
          return (
            <button
              key={id}
              type="button"
              role="tab"
              id={`data-tab-${id}`}
              aria-selected={selected}
              aria-controls={`data-tabpanel-${id}`}
              tabIndex={selected ? 0 : -1}
              className="playful-tab inline-flex items-center gap-1.5"
              onClick={() => selectTab(id)}
            >
              <Icon className="h-4 w-4" aria-hidden="true" />
              {t(labelKey)}
            </button>
          );
        })}
      </div>
      <div
        role="tabpanel"
        id={`data-tabpanel-${activeTab}`}
        aria-labelledby={`data-tab-${activeTab}`}
      >
        {/* embedded：两个子页面都不再各自渲染 AppPage，避免 <main> 嵌套与双份内边距。 */}
        {activeTab === 'usage' ? <TokenUsagePage embedded /> : <DataInventoryPage embedded />}
      </div>
    </AppPage>
  );
};

export default DataPage;
