import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

/**
 * 侧栏收敛（11 项 → 8 项）的源码级护栏。
 *
 * 与 ui_governance.test.ts 同一套路：渲染断言证明“现在只显示 8 项”，
 * 这里再证明“被隐藏的入口并没有被删掉”——定义、目标路由、页面路由三者都在位，
 * 未来维护者只要把 hiddenByDefault 改成 false 就能恢复，不需要重建菜单项。
 */
const sidebarSource = readFileSync(join(process.cwd(), 'src/components/layout/SidebarNav.tsx'), 'utf8');
const appSource = readFileSync(join(process.cwd(), 'src/App.tsx'), 'utf8');

const HIDDEN_ENTRIES = ['/alerts', '/backtest', '/usage'];
// 被隐藏的入口必须仍然有页面组件与路由：删页面/删路由都算破坏本次契约。
const KEPT_PAGES = ['AlertsPage', 'BacktestPage', 'TokenUsagePage', 'DataInventoryPage'];

describe('sidebar convergence guard', () => {
  it('keeps the hidden entries defined in NAV_ITEMS with a single display switch', () => {
    for (const path of HIDDEN_ENTRIES) {
      expect(sidebarSource).toContain(`to: '${path}'`);
    }
    // 三个隐藏入口共用一个开关，避免出现第二套隐藏机制。
    expect(sidebarSource.match(/hiddenByDefault: true/g) ?? []).toHaveLength(HIDDEN_ENTRIES.length);
    expect(sidebarSource).toContain('shouldShowNavItem');
    // 既有的后端配置开关（showScreeningNav）仍然是同一套过滤机制的一部分。
    expect(sidebarSource).toContain("item.key === 'screening'");
  });

  it('keeps /alerts, /backtest and /usage reachable by URL', () => {
    for (const path of HIDDEN_ENTRIES) {
      expect(appSource).toContain(`path="${path}"`);
    }
    // /usage 由合并后的数据页承载，并预选用量 Tab；不做重定向，旧链接原样可用。
    expect(appSource).toContain('<DataPage initialTab="usage" />');
    expect(appSource).toContain('path="/data"');
  });

  it('does not delete the merged pages or their lazy chunks', () => {
    for (const page of KEPT_PAGES) {
      // 页面组件由数据页按 Tab 静态引入，仍然存在于代码库中。
      expect(readFileSync(join(process.cwd(), `src/pages/${page}.tsx`), 'utf8').length).toBeGreaterThan(0);
    }
  });
});
