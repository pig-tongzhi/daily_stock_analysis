import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { SidebarNav } from '../SidebarNav';

const mockLogout = vi.fn().mockResolvedValue(undefined);
const mockGetScreeningStatus = vi.fn().mockResolvedValue({ enabled: false, available: false });
const mockThemeToggle = vi.fn(({ collapsed }: { collapsed?: boolean }) => (
  <button type="button">{collapsed ? '切换主题(折叠)' : '切换主题'}</button>
));

const completionBadgeState = { value: true };

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({
    authEnabled: true,
    logout: mockLogout,
  }),
}));

vi.mock('../../../stores/agentChatStore', () => ({
  useAgentChatStore: (selector: (state: { completionBadge: boolean }) => unknown) =>
    selector({ completionBadge: completionBadgeState.value }),
}));

vi.mock('../../../api/screening', () => ({
  SCREENING_CONFIG_CHANGED_EVENT: 'screening-config-changed',
  SYSTEM_CONFIG_CHANGED_EVENT: 'dsa-system-config-changed',
  screeningApi: {
    getStatus: () => mockGetScreeningStatus(),
  },
}));

vi.mock('../../theme/ThemeToggle', () => ({
  ThemeToggle: (props: { collapsed?: boolean }) => mockThemeToggle(props),
}));

describe('SidebarNav', () => {
  it('hides the screening navigation item while Screening is disabled', () => {
    mockGetScreeningStatus.mockResolvedValueOnce({ enabled: false, available: true });

    render(
      <MemoryRouter initialEntries={['/']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.queryByRole('link', { name: '选股' })).not.toBeInTheDocument();
  });

  it('shows screening directly after chat when Screening is enabled', async () => {
    mockGetScreeningStatus.mockResolvedValueOnce({ enabled: true, available: true });

    render(
      <MemoryRouter initialEntries={['/']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(await screen.findByRole('link', { name: '选股' })).toHaveAttribute('href', '/screening');
    const hrefs = screen.getAllByRole('link').map((link) => link.getAttribute('href'));
    // 侧栏收敛为 8 项后的目标顺序：AI 建议 在 资讯 之前。
    expect(hrefs).toEqual([
      '/',
      '/chat',
      '/screening',
      '/portfolio',
      '/decision-signals',
      '/intelligence',
      '/data',
      '/settings',
    ]);
  });

  it('refreshes the controlled screening entry after config changes', async () => {
    mockGetScreeningStatus
      .mockResolvedValueOnce({ enabled: false, available: true })
      .mockResolvedValueOnce({ enabled: true, available: true });

    render(
      <MemoryRouter initialEntries={['/']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.queryByRole('link', { name: '选股' })).not.toBeInTheDocument();
    window.dispatchEvent(new Event('screening-config-changed'));

    expect(await screen.findByRole('link', { name: '选股' })).toHaveAttribute('href', '/screening');
    await waitFor(() => expect(mockGetScreeningStatus.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it('shows the shared completion badge only when chat completion is pending', () => {
    completionBadgeState.value = true;

    const { rerender } = render(
      <MemoryRouter initialEntries={['/chat']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.getByTestId('chat-completion-badge')).toBeInTheDocument();
    expect(screen.getByLabelText('问股有新消息')).toBeInTheDocument();

    completionBadgeState.value = false;
    rerender(
      <MemoryRouter initialEntries={['/chat']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.queryByTestId('chat-completion-badge')).not.toBeInTheDocument();
  });

  it('renders the collapsed theme toggle variant when the sidebar is collapsed', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <SidebarNav collapsed />
      </MemoryRouter>,
    );

    expect(mockThemeToggle).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'nav', collapsed: true }),
    );
    expect(screen.getByRole('button', { name: '切换主题(折叠)' })).toBeInTheDocument();
  });

  it('不再把告警 / 回测 / 用量 渲染成侧栏入口', async () => {
    mockGetScreeningStatus.mockResolvedValueOnce({ enabled: true, available: true });
    render(
      <MemoryRouter initialEntries={['/alerts']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    // 告警功能完全未使用（四张告警表全空）、回测表同样为空，用量已并入「数据」页，
    // 三者都从侧栏下线；它们的目标路由仍然保留（见下一个用例）。
    await screen.findByRole('link', { name: '数据' });
    for (const label of ['告警', '回测', '用量']) {
      expect(screen.queryByRole('link', { name: label })).not.toBeInTheDocument();
    }
    expect(screen.getAllByRole('link')).toHaveLength(8);
  });

  it('从旧的 /usage 链接进入时仍然把「数据」标为当前项', async () => {
    render(
      <MemoryRouter initialEntries={['/usage']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    const dataLink = await screen.findByRole('link', { name: '数据' });
    expect(dataLink.className).toContain('nav-active-border');
  });

  it('renders the AI signals navigation item and marks it active', () => {
    render(
      <MemoryRouter initialEntries={['/decision-signals']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    const signalsLink = screen.getByRole('link', { name: 'AI 建议' });
    expect(signalsLink).toHaveAttribute('href', '/decision-signals');
    expect(signalsLink).toHaveClass('font-medium');
  });

  it('opens the logout confirmation and confirms logout', async () => {
    render(
      <MemoryRouter initialEntries={['/chat']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole('button', { name: '退出' }));

    expect(await screen.findByRole('heading', { name: '退出登录' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确认退出' }));
    expect(mockLogout).toHaveBeenCalled();
  });
});
