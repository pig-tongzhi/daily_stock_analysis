import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Archive,
  Bell,
  Bot,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  CircleDashed,
  Clock,
  Cpu,
  Database,
  Layers3,
  LineChart,
  ListChecks,
  Play,
  Plus,
  RefreshCw,
  Settings2,
  SlidersHorizontal,
  Sparkles,
  Tag,
  Target,
  Trash2,
} from 'lucide-react';
import { useLocation } from 'react-router-dom';
import { useAuth, useDesktopUpdate, useSystemConfig } from '../hooks';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import { createParsedApiError, getParsedApiError, type ParsedApiError } from '../api/error';
import { analysisApi } from '../api/analysis';
import { screeningApi, notifyScreeningConfigChanged, notifySystemConfigChanged } from '../api/screening';
import { systemConfigApi } from '../api/systemConfig';
import { ApiErrorAlert, Button, ConfirmDialog, EmptyState } from '../components/common';
// 卡通友好（playful）基元：与首页 / 组合 / 选股页共用同一套卡片、区块标题与暖色令牌。
import {
  PlayfulCard,
  PlayfulChip,
  PlayfulMascot,
  PlayfulPage,
  PlayfulSection,
  PlayfulSectionTitle,
} from '../components/playful';
import {
  AgentBackendStatusPanel,
  AuthSettingsCard,
  ChangePasswordCard,
  GenerationBackendStatusPanel,
  IntelligentImport,
  LLMChannelEditor,
  NotificationTestPanel,
  SettingsCategoryNav,
  SettingsAlert,
  SettingsField,
  SettingsLoading,
  SettingsPanelErrorBoundary,
} from '../components/settings';
import { WEB_BUILD_INFO } from '../utils/constants';
import { parseStockListValue } from '../utils/stockList';
import { getCategoryDescription, getCategoryTitle } from '../utils/systemConfigI18n';
import type {
  ConfigValidationIssue,
  SchedulerStatusResponse,
  SetupStatusCheck,
  SetupStatusResponse,
  SystemConfigCategory,
  SystemConfigItem,
  SystemConfigUpdateItem,
} from '../types/systemConfig';
import type { UiLanguage, UiTextKey } from '../i18n/uiText';

const LLM_CHANNEL_EDITOR_RUNTIME_KEYS = new Set([
  'LITELLM_MODEL',
  'LITELLM_FALLBACK_MODELS',
  'AGENT_LITELLM_MODEL',
  'VISION_MODEL',
  'LLM_TEMPERATURE',
]);
const GENERATION_BACKEND_STATUS_KEYS = new Set([
  'GENERATION_BACKEND',
  'GENERATION_FALLBACK_BACKEND',
  'GENERATION_BACKEND_TIMEOUT_SECONDS',
  'GENERATION_BACKEND_MAX_OUTPUT_BYTES',
  'GENERATION_BACKEND_MAX_CONCURRENCY',
  'LOCAL_CLI_BACKEND_MAX_CONCURRENCY',
  'OPENCODE_CLI_MODEL',
  'LITELLM_CONFIG',
  'LITELLM_MODEL',
  'LITELLM_FALLBACK_MODELS',
  'GEMINI_API_KEY',
  'GEMINI_API_KEYS',
  'GEMINI_MODEL',
  'GEMINI_MODEL_FALLBACK',
  'GEMINI_TEMPERATURE',
  'ANTHROPIC_API_KEY',
  'ANTHROPIC_API_KEYS',
  'ANTHROPIC_MODEL',
  'ANTHROPIC_TEMPERATURE',
  'ANTHROPIC_MAX_TOKENS',
  'OPENAI_API_KEY',
  'OPENAI_API_KEYS',
  'OPENAI_BASE_URL',
  'OPENAI_MODEL',
  'OPENAI_VISION_MODEL',
  'OPENAI_TEMPERATURE',
  'OLLAMA_API_BASE',
  'OLLAMA_MODEL',
  'DEEPSEEK_API_KEY',
  'DEEPSEEK_API_KEYS',
  'AIHUBMIX_KEY',
  'ANSPIRE_LLM_ENABLED',
  'ANSPIRE_LLM_BASE_URL',
  'ANSPIRE_LLM_MODEL',
  'ANSPIRE_API_KEYS',
]);
const LLM_CHANNEL_STATUS_KEY_PATTERN = /^LLM_[A-Z0-9_]+_(PROTOCOL|API_SURFACE|BASE_URL|API_KEY|API_KEYS|MODELS|EXTRA_HEADERS|ENABLED)$/;
const AGENT_BACKEND_STATUS_KEYS = new Set([
  'AGENT_BACKEND',
  'AGENT_GENERATION_BACKEND',
  'AGENT_LITELLM_MODEL',
  'AGENT_MODE',
  'AGENT_ARCH',
  'AGENT_ORCHESTRATOR_TIMEOUT_S',
]);

function isLlmChannelEditorDraftKey(key: string): boolean {
  const normalized = key.trim().toUpperCase();
  return normalized.startsWith('LLM_') || LLM_CHANNEL_EDITOR_RUNTIME_KEYS.has(normalized);
}

function isGenerationBackendStatusDraftKey(key: string): boolean {
  const normalized = key.trim().toUpperCase();
  return (
    GENERATION_BACKEND_STATUS_KEYS.has(normalized)
    || normalized === 'LLM_CHANNELS'
    || LLM_CHANNEL_STATUS_KEY_PATTERN.test(normalized)
  );
}

function mergeGenerationBackendDraftItems(
  outerItems: SystemConfigUpdateItem[],
  llmChannelItems: SystemConfigUpdateItem[],
): SystemConfigUpdateItem[] {
  const merged = new Map<string, SystemConfigUpdateItem>();
  for (const item of outerItems) {
    const normalizedKey = item.key.trim().toUpperCase();
    if (isGenerationBackendStatusDraftKey(normalizedKey)) {
      merged.set(normalizedKey, item);
    }
  }
  for (const item of llmChannelItems) {
    const normalizedKey = item.key.trim().toUpperCase();
    if (isLlmChannelEditorDraftKey(normalizedKey) && isGenerationBackendStatusDraftKey(normalizedKey)) {
      merged.set(normalizedKey, item);
    }
  }
  return Array.from(merged.values());
}

const PROMPT_CACHE_ADVANCED_SETTING_KEYS = new Set([
  'LLM_PROMPT_CACHE_TELEMETRY_ENABLED',
  'LLM_PROMPT_CACHE_HINTS_ENABLED',
  'LLM_PROMPT_CACHE_DIAGNOSTICS_LEVEL',
]);

function isPromptCacheAdvancedSetting(item: { key: string }) {
  return PROMPT_CACHE_ADVANCED_SETTING_KEYS.has(item.key);
}

function formatEnvBackupFilename(isDesktopRuntime: boolean) {
  const now = new Date();
  const pad = (value: number) => value.toString().padStart(2, '0');
  const date = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`;
  const time = `${pad(now.getHours())}${pad(now.getMinutes())}`;
  return `${isDesktopRuntime ? 'dsa-desktop-env' : 'dsa-env'}_${date}_${time}.env`;
}

const SCHEDULE_TIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
const SCHEDULER_DEFAULT_TIME = '18:00';
const SCHEDULER_SETTING_KEYS = new Set([
  'SCHEDULE_ENABLED',
  'SCHEDULE_TIME',
  'SCHEDULE_TIMES',
  'SCHEDULE_RUN_IMMEDIATELY',
]);

function getConfigItem(items: SystemConfigItem[], key: string) {
  return items.find((item) => item.key === key);
}

function parseSetupStockList(value: unknown) {
  return parseStockListValue(String(value ?? ''));
}

function isEnabledConfigValue(value: unknown) {
  return String(value ?? '').trim().toLowerCase() === 'true';
}

function getSetupCheckIcon(check: SetupStatusCheck) {
  if (check.status === 'configured' || check.status === 'inherited') {
    return <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" aria-hidden="true" />;
  }
  if (check.status === 'needs_action') {
    return <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden="true" />;
  }
  return <CircleDashed className="mt-0.5 h-4 w-4 shrink-0 text-muted-text" aria-hidden="true" />;
}

function getSetupCheckStatusLabel(
  check: SetupStatusCheck,
  t: (key: UiTextKey, params?: Record<string, string | number>) => string,
) {
  if (check.status === 'configured') return t('settings.setupStatusConfigured');
  if (check.status === 'inherited') return t('settings.setupStatusInherited');
  if (check.status === 'needs_action') return t('settings.setupStatusNeedsAction');
  return t('settings.setupStatusOptional');
}

/**
 * 当前分类面板的图标：与左侧分类导航用同一套 lucide 线型。
 *
 * 这里刻意用线型图标而不是 emoji —— 设置页是拿来「读」的，图标只需要帮人
 * 快速定位分组，不参与表达情绪；emoji 只出现在页面级 hero（吉祥物）和状态胶囊上。
 */
const ACTIVE_PANEL_ICONS: Partial<Record<SystemConfigCategory, React.ReactNode>> = {
  system: <Settings2 className="h-4 w-4" />,
  base: <SlidersHorizontal className="h-4 w-4" />,
  data_source: <Database className="h-4 w-4" />,
  ai_model: <Layers3 className="h-4 w-4" />,
  notification: <Bell className="h-4 w-4" />,
  agent: <Bot className="h-4 w-4" />,
  backtest: <LineChart className="h-4 w-4" />,
};

interface SettingsGroupCardProps {
  title: React.ReactNode;
  description?: React.ReactNode;
  /** 分组图标，只挂在区块标题上；字段级一律不加装饰。 */
  icon?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}

/**
 * 页面私有的设置分组卡。
 *
 * 为什么不直接用 `components/settings` 里的 `SettingsSectionCard`：那个组件被
 * 认证 / 密码 / 通知等多个面板共用，而本次任务的文件所有权只允许改本页面，
 * 所以在页面内用 playful 基元重组成一份等价结构 —— 外层依然是
 * 「section + h2 标题 + 说明 + 内容」，`closest('section')`、标题层级和既有
 * 测试断言都不受影响，只是把卡片表面换成暖色描边 + 大圆角 + 贴纸感投影。
 */
const SettingsGroupCard: React.FC<SettingsGroupCardProps> = ({
  title,
  description,
  icon,
  children,
  className = '',
}) => (
  // padding 用 none + 显式 p-4/md:p-5，保持与 SettingsSectionCard 完全一致的内边距，
  // 避免「换了皮肤但表单整体位移」这种与视觉无关的副作用。
  <PlayfulCard as="section" padding="none" className={`p-4 md:p-5 ${className}`}>
    <div className="mb-4 min-w-0 space-y-1">
      <PlayfulSectionTitle as="h2" icon={icon} title={title} />
      {description ? <p className="text-sm leading-6 text-muted-text">{description}</p> : null}
    </div>
    <div className="space-y-4">{children}</div>
  </PlayfulCard>
);

type FirstRunSetupCardProps = {
  status: SetupStatusResponse | null;
  isLoading: boolean;
  error: ParsedApiError | null;
  firstStockCode: string;
  isSaving: boolean;
  isRunningSmoke: boolean;
  smokeError: ParsedApiError | null;
  smokeSuccess: string;
  onRefresh: () => void | Promise<void>;
  onSelectCategory: (category: SystemConfigCategory) => void;
  onRunSmoke: () => void | Promise<void>;
  listSeparator: string;
  t: (key: UiTextKey, params?: Record<string, string | number>) => string;
};

const FirstRunSetupCard: React.FC<FirstRunSetupCardProps> = ({
  status,
  isLoading,
  error,
  firstStockCode,
  isSaving,
  isRunningSmoke,
  smokeError,
  smokeSuccess,
  onRefresh,
  onSelectCategory,
  onRunSmoke,
  listSeparator,
  t,
}) => {
  const [isHidden, setIsHidden] = useState(false);
  const requiredMissing = status?.checks.filter((check) => check.required && check.status === 'needs_action') || [];
  const isComplete = Boolean(status?.isComplete);
  const canRunSmoke = Boolean(status?.readyForSmoke && firstStockCode);
  const summaryTitle = !status
    ? error
      ? t('settings.setupGuideUnknownTitle')
      : t('settings.setupGuideCheckingTitle')
    : isComplete
      ? t('settings.setupGuideCompleteTitle')
      : t('settings.setupGuideIncompleteTitle');
  const summaryMessage = !status
    ? error
      ? t('settings.setupGuideUnknownSummary')
      : t('settings.setupGuideCheckingSummary')
    : requiredMissing.length
      ? t('settings.setupGuideMissingSummary', {
        count: requiredMissing.length,
        labels: requiredMissing.slice(0, 3).map((check) => check.title).join(listSeparator),
      })
      : t('settings.setupGuideReadySummary');

  if (isHidden) {
    return (
      // 折叠态是独立的一条提示带，不再套分组卡，只借用同一套暖色表面。
      <div className="playful-card-soft flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-sm font-semibold text-foreground">{t('settings.setupGuideHiddenTitle')}</p>
          <p className="mt-1 text-xs leading-5 text-muted-text">{t('settings.setupGuideHiddenDescription')}</p>
        </div>
        <Button type="button" variant="settings-secondary" size="sm" onClick={() => setIsHidden(false)}>
          {t('settings.setupGuideOpen')}
        </Button>
      </div>
    );
  }

  return (
    <SettingsGroupCard
      title={t('settings.setupGuideTitle')}
      description={t('settings.setupGuideDescription')}
      icon={<ListChecks className="h-4 w-4" />}
    >
      <div data-testid="first-run-setup-card" className="space-y-4">
        <div className="playful-card-soft flex flex-col gap-3 px-4 py-4 md:flex-row md:items-start md:justify-between">
          <div className="min-w-0">
            <p className="text-sm font-semibold text-foreground">
              {summaryTitle}
            </p>
            <p className="mt-1 text-xs leading-6 text-muted-text">
              {summaryMessage}
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="settings-secondary"
              size="sm"
              disabled={isLoading}
              isLoading={isLoading}
              loadingText={t('settings.setupGuideRefreshing')}
              onClick={() => void onRefresh()}
            >
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
              {t('settings.setupGuideRefresh')}
            </Button>
            <Button type="button" variant="settings-secondary" size="sm" onClick={() => setIsHidden(true)}>
              {t('settings.setupGuideHide')}
            </Button>
          </div>
        </div>

        {error ? <ApiErrorAlert error={error} /> : null}

        {isLoading && !status ? (
          <p className="text-sm text-muted-text">{t('common.loading')}</p>
        ) : null}

        {status ? (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {status.checks.map((check) => (
              <div
                key={check.key}
                className="playful-card-soft px-4 py-3"
              >
                <div className="flex items-start gap-3">
                  {getSetupCheckIcon(check)}
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="text-sm font-semibold text-foreground">{check.title}</p>
                      {/* 状态胶囊沿用 playful 的软色 chip：底色变暖，但状态语义仍由左侧图标承担，
                          不把 success / warning 这类语义色挪到胶囊上。 */}
                      <PlayfulChip tone="soft" size="sm">
                        {getSetupCheckStatusLabel(check, t)}
                      </PlayfulChip>
                    </div>
                    <p className="mt-1 text-xs leading-5 text-muted-text">{check.message}</p>
                    {check.nextStep ? (
                      <p className="mt-2 text-xs leading-5 text-secondary-text">{check.nextStep}</p>
                    ) : null}
                  </div>
                </div>
              </div>
            ))}
          </div>
        ) : null}

        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="settings-secondary" size="sm" onClick={() => onSelectCategory('ai_model')}>
            {t('settings.setupGuideConfigureLlm')}
          </Button>
          <Button type="button" variant="settings-secondary" size="sm" onClick={() => onSelectCategory('base')}>
            {t('settings.setupGuideAddStocks')}
          </Button>
          <Button type="button" variant="settings-secondary" size="sm" onClick={() => onSelectCategory('notification')}>
            {t('settings.setupGuideConfigureNotification')}
          </Button>
          <Button
            type="button"
            variant="settings-primary"
            size="sm"
            disabled={!canRunSmoke || isSaving || isRunningSmoke}
            isLoading={isRunningSmoke}
            loadingText={t('settings.setupGuideSmokeRunning')}
            title={!firstStockCode ? t('settings.setupGuideSmokeNeedsStock') : undefined}
            onClick={() => void onRunSmoke()}
          >
            <Play className="h-4 w-4" aria-hidden="true" />
            {t('settings.setupGuideRunSmoke')}
          </Button>
        </div>

        {!canRunSmoke && status ? (
          <p className="text-xs leading-6 text-muted-text">
            {firstStockCode ? t('settings.setupGuideSmokeNotReady') : t('settings.setupGuideSmokeNeedsStock')}
          </p>
        ) : null}
        {smokeError ? <ApiErrorAlert error={smokeError} /> : null}
        {!smokeError && smokeSuccess ? (
          <SettingsAlert title={t('settings.actionSuccess')} message={smokeSuccess} variant="success" />
        ) : null}
      </div>
    </SettingsGroupCard>
  );
};

function parseScheduleTimes(scheduleTimesValue?: string, fallbackValue?: string) {
  const values = String(scheduleTimesValue ?? '')
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean);

  if (values.length > 0) {
    return values;
  }

  const fallback = String(fallbackValue ?? '').trim();
  return fallback ? [fallback] : [SCHEDULER_DEFAULT_TIME];
}

function serializeScheduleTimes(times: string[]) {
  return times.map((time) => time.trim()).filter(Boolean).join(',');
}

function formatSchedulerTimestamp(value: string | null | undefined, language: UiLanguage) {
  if (!value) {
    return '-';
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat(language === 'en' ? 'en-US' : 'zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

type SchedulerSettingsCardProps = {
  items: SystemConfigItem[];
  disabled: boolean;
  issueByKey: Record<string, ConfigValidationIssue[]>;
  statusRefreshToken: number;
  onChange: (key: string, value: string) => void;
  onSchedulerStateChange?: (payload: {
    runtimeEnabled: boolean | null;
    overrideEnabled: boolean | null;
  }) => void;
  t: (key: UiTextKey, params?: Record<string, string | number>) => string;
  language: UiLanguage;
};

const SchedulerSettingsCard: React.FC<SchedulerSettingsCardProps> = ({
  items,
  disabled,
  issueByKey,
  statusRefreshToken,
  onChange,
  onSchedulerStateChange,
  t,
  language,
}) => {
  const scheduleEnabledItem = getConfigItem(items, 'SCHEDULE_ENABLED');
  const scheduleTimesItem = getConfigItem(items, 'SCHEDULE_TIMES');
  const scheduleTimeItem = getConfigItem(items, 'SCHEDULE_TIME');
  const hasSchedulerSettings = Boolean(scheduleEnabledItem || scheduleTimesItem || scheduleTimeItem);
  const [status, setStatus] = useState<SchedulerStatusResponse | null>(null);
  const [isRefreshingStatus, setIsRefreshingStatus] = useState(false);
  const [isRunningNow, setIsRunningNow] = useState(false);
  const [statusError, setStatusError] = useState<ParsedApiError | null>(null);
  const [runNowError, setRunNowError] = useState<ParsedApiError | null>(null);
  const [runNowSuccess, setRunNowSuccess] = useState('');
  const [scheduleEnabledOverride, setScheduleEnabledOverride] = useState<boolean | null>(null);

  const refreshSchedulerStatus = useCallback(async () => {
    setStatusError(null);
    setIsRefreshingStatus(true);
    try {
      const payload = await systemConfigApi.getSchedulerStatus();
      setStatus(payload);
    } catch (error: unknown) {
      setStatusError(getParsedApiError(error));
    } finally {
      setIsRefreshingStatus(false);
    }
  }, []);

  useEffect(() => {
    if (!hasSchedulerSettings) {
      return;
    }
    void refreshSchedulerStatus();
  }, [hasSchedulerSettings, refreshSchedulerStatus, statusRefreshToken]);

  useEffect(() => {
    if (!onSchedulerStateChange) {
      return;
    }

    const runtimeEnabled = status?.enabled ?? null;
    onSchedulerStateChange({
      runtimeEnabled,
      overrideEnabled: scheduleEnabledOverride,
    });
  }, [onSchedulerStateChange, status?.enabled, scheduleEnabledOverride]);

  if (!hasSchedulerSettings) {
    return null;
  }

  const scheduleEnabled = isEnabledConfigValue(scheduleEnabledItem?.value);
  const scheduleTimes = parseScheduleTimes(
    String(scheduleTimesItem?.value ?? ''),
    String(scheduleTimeItem?.value ?? ''),
  );
  const timeTargetKey = scheduleTimesItem ? 'SCHEDULE_TIMES' : 'SCHEDULE_TIME';
  const statusEnabled = status?.enabled ?? scheduleEnabled;
  const displayedScheduleEnabled = scheduleEnabledOverride ?? statusEnabled;
  const effectiveStatusTimes = status?.scheduleTimes?.length ? status.scheduleTimes : scheduleTimes.filter(Boolean);
  const validationIssues = [
    ...(issueByKey.SCHEDULE_ENABLED || []),
    ...(issueByKey.SCHEDULE_TIMES || []),
    ...(issueByKey.SCHEDULE_TIME || []),
  ];

  const updateScheduleTimes = (nextTimes: string[]) => {
    if (timeTargetKey === 'SCHEDULE_TIME') {
      onChange(timeTargetKey, nextTimes[0] || '');
      return;
    }
    onChange(timeTargetKey, serializeScheduleTimes(nextTimes));
  };

  const runSchedulerNow = async () => {
    setRunNowError(null);
    setRunNowSuccess('');
    setIsRunningNow(true);
    try {
      await systemConfigApi.runSchedulerNow();
      setRunNowSuccess(t('settings.schedulerRunAccepted'));
      await refreshSchedulerStatus();
    } catch (error: unknown) {
      setRunNowError(getParsedApiError(error));
    } finally {
      setIsRunningNow(false);
    }
  };

  return (
    <SettingsGroupCard
      title={t('settings.schedulerTitle')}
      description={t('settings.schedulerDescription')}
      icon={<Clock className="h-4 w-4" />}
    >
      <div data-testid="scheduler-settings-card" className="space-y-4">
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(300px,360px)]">
          <div className="playful-card-soft space-y-4 px-4 py-4">
                <label className="flex items-start gap-3">
                  <input
                    type="checkbox"
                    className="mt-1 h-4 w-4 rounded border-border text-cyan focus:ring-cyan/20"
                    checked={displayedScheduleEnabled}
                    data-testid="scheduler-enabled-checkbox"
                    disabled={disabled || !scheduleEnabledItem?.schema?.isEditable}
                    onChange={(event) => {
                      const nextEnabled = Boolean(event.target.checked);
                      setScheduleEnabledOverride(nextEnabled);
                      onChange('SCHEDULE_ENABLED', nextEnabled ? 'true' : 'false');
                    }}
                  />
              <span>
                <span className="block text-sm font-semibold text-foreground">{t('settings.schedulerEnable')}</span>
                <span className="block text-xs leading-6 text-muted-text">{t('settings.schedulerEnableDescription')}</span>
              </span>
            </label>

            <div className="space-y-3">
              <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
                <Clock className="h-4 w-4" aria-hidden="true" />
                {t('settings.schedulerTimes')}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {scheduleTimes.map((time, index) => (
                  <div
                    key={index}
                    className="playful-card-soft inline-flex h-11 shrink-0 items-center gap-1 p-1"
                  >
                    <input
                      data-testid={`scheduler-time-input-${index}`}
                      type="time"
                      value={SCHEDULE_TIME_PATTERN.test(time) ? time : ''}
                      aria-label={t('settings.schedulerTimeInputAria', { index: index + 1 })}
                      className="h-9 w-[8.75rem] rounded-lg border-none bg-transparent px-2 text-sm font-medium text-foreground outline-none transition focus:bg-background/60 focus:ring-2 focus:ring-cyan/20"
                      disabled={disabled}
                      onChange={(event) => {
                        const nextTimes = scheduleTimes.map((currentTime, currentIndex) => (
                          currentIndex === index ? event.target.value : currentTime
                        ));
                        updateScheduleTimes(nextTimes);
                      }}
                    />
                    {scheduleTimes.length > 1 ? (
                      <Button
                        type="button"
                        variant="settings-secondary"
                        size="sm"
                        className="h-8 w-8 rounded-lg px-0"
                        aria-label={t('settings.schedulerRemoveTime')}
                        title={t('settings.schedulerRemoveTime')}
                        disabled={disabled}
                        onClick={() => {
                          updateScheduleTimes(scheduleTimes.filter((_, currentIndex) => currentIndex !== index));
                        }}
                      >
                        <Trash2 className="h-4 w-4" aria-hidden="true" />
                      </Button>
                    ) : null}
                  </div>
                ))}
                <Button
                  type="button"
                  variant="settings-secondary"
                  size="sm"
                  className="h-11 shrink-0"
                  data-testid="scheduler-add-time-button"
                  disabled={disabled}
                  onClick={() => updateScheduleTimes([...scheduleTimes, SCHEDULER_DEFAULT_TIME])}
                >
                  <Plus className="h-4 w-4" aria-hidden="true" />
                  {t('settings.schedulerAddTime')}
                </Button>
              </div>
            </div>
          </div>

          <div className="playful-card-soft space-y-3 px-4 py-4">
            <div>
              <p className="text-sm font-semibold text-foreground">{t('settings.schedulerStatus')}</p>
              <p className="mt-1 text-xs leading-6 text-muted-text">
                {status?.running
                  ? t('settings.schedulerRunning')
                  : statusEnabled
                    ? t('settings.schedulerEnabled')
                    : t('settings.schedulerDisabled')}
              </p>
            </div>
            <dl className="grid grid-cols-1 gap-2 text-xs">
              <div className="playful-chip px-3 py-2">
                <dt className="text-muted-text">{t('settings.schedulerEffectiveTimes')}</dt>
                <dd className="mt-1 font-medium text-foreground">{effectiveStatusTimes.join(', ') || '-'}</dd>
              </div>
              <div className="playful-chip px-3 py-2">
                <dt className="text-muted-text">{t('settings.schedulerNextRun')}</dt>
                <dd className="mt-1 font-medium text-foreground">
                  {formatSchedulerTimestamp(status?.nextRunAt, language)}
                </dd>
              </div>
              <div className="playful-chip px-3 py-2">
                <dt className="text-muted-text">{t('settings.schedulerLastSuccess')}</dt>
                <dd data-testid="scheduler-last-success" className="mt-1 font-medium text-foreground">
                  {formatSchedulerTimestamp(status?.lastSuccessAt, language)}
                </dd>
              </div>
              {status?.lastError ? (
                <div className="rounded-xl border border-danger/40 bg-danger/10 px-3 py-2">
                  <dt className="text-danger">{t('settings.schedulerLastError')}</dt>
                  <dd data-testid="scheduler-last-error" className="mt-1 break-words text-danger">{status.lastError}</dd>
                </div>
              ) : null}
            </dl>
            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="settings-secondary"
                size="sm"
                data-testid="scheduler-refresh-status-button"
                disabled={disabled || isRefreshingStatus}
                isLoading={isRefreshingStatus}
                loadingText={t('settings.schedulerRefreshing')}
                onClick={() => void refreshSchedulerStatus()}
              >
                <RefreshCw className="h-4 w-4" aria-hidden="true" />
                {t('settings.schedulerRefresh')}
              </Button>
              <Button
                type="button"
                variant="settings-primary"
                size="sm"
                data-testid="scheduler-run-now-button"
                disabled={disabled || isRunningNow}
                isLoading={isRunningNow}
                loadingText={t('settings.schedulerRunningNow')}
                onClick={() => void runSchedulerNow()}
              >
                <Play className="h-4 w-4" aria-hidden="true" />
                {t('settings.schedulerRunNow')}
              </Button>
            </div>
          </div>
        </div>

        {validationIssues.length ? (
          <div className="space-y-1 text-xs text-danger">
            {validationIssues.map((issue) => (
              <p key={`${issue.key}-${issue.code}`}>{issue.message}</p>
            ))}
          </div>
        ) : null}
        {statusError ? <ApiErrorAlert error={statusError} /> : null}
        {runNowError ? <ApiErrorAlert error={runNowError} /> : null}
        {!runNowError && runNowSuccess ? (
          <SettingsAlert title={t('settings.actionSuccess')} message={runNowSuccess} variant="success" />
        ) : null}
      </div>
    </SettingsGroupCard>
  );
};

const SettingsPage: React.FC = () => {
  const { authEnabled, passwordChangeable } = useAuth();
  const location = useLocation();
  const { language: uiLanguage, t } = useUiLanguage();
  const [envBackupActionError, setEnvBackupActionError] = useState<ParsedApiError | null>(null);
  const [envBackupActionSuccess, setEnvBackupActionSuccess] = useState<string>('');
  const [screeningActionError, setScreeningActionError] = useState<ParsedApiError | null>(null);
  const [screeningActionSuccess, setScreeningActionSuccess] = useState<string>('');
  const [isExportingEnv, setIsExportingEnv] = useState(false);
  const [isImportingEnv, setIsImportingEnv] = useState(false);
  const [isUpdatingScreening, setIsUpdatingScreening] = useState(false);
  const [showImportConfirm, setShowImportConfirm] = useState(false);
  const [schedulerStatusRefreshToken, setSchedulerStatusRefreshToken] = useState(0);
  const [schedulerRuntimeEnabled, setSchedulerRuntimeEnabled] = useState<boolean | null>(null);
  const [schedulerOverrideFromUi, setSchedulerOverrideFromUi] = useState<boolean | null>(null);
  const [setupStatus, setSetupStatus] = useState<SetupStatusResponse | null>(null);
  const [isRefreshingSetupStatus, setIsRefreshingSetupStatus] = useState(false);
  const [setupStatusError, setSetupStatusError] = useState<ParsedApiError | null>(null);
  const [isRunningSetupSmoke, setIsRunningSetupSmoke] = useState(false);
  const [setupSmokeError, setSetupSmokeError] = useState<ParsedApiError | null>(null);
  const [setupSmokeSuccess, setSetupSmokeSuccess] = useState('');
  const [llmChannelDraftItems, setLlmChannelDraftItems] = useState<SystemConfigUpdateItem[]>([]);
  const envBackupImportRef = useRef<HTMLInputElement | null>(null);
  const setupStatusRequestIdRef = useRef(0);
  const {
    isDesktopRuntime,
    canCheckDesktopUpdate,
    desktopAppVersion,
    isBusy: isDesktopUpdateBusy,
    isChecking: isCheckingDesktopUpdate,
    notice: desktopUpdateNotice,
    checkForUpdates: handleDesktopUpdateCheck,
    openReleasePage: openDesktopReleasePage,
    installDownloadedUpdate: installDesktopUpdate,
  } = useDesktopUpdate();
  const shouldShowDesktopVersionCard = Boolean(desktopAppVersion);

  // Set page title
  useEffect(() => {
    document.title = t('settings.pageTitleDocument');
  }, [t]);

  const {
    categories,
    itemsByCategory,
    issueByKey,
    activeCategory,
    setActiveCategory,
    hasDirty,
    dirtyCount,
    toast,
    clearToast,
    isLoading,
    isSaving,
    loadError,
    saveError,
    retryAction,
    load,
    retry,
    save,
    resetDraft,
    setDraftValue,
    getChangedItems,
    refreshAfterExternalSave,
    configVersion,
    maskToken,
    llmModelProviders,
  } = useSystemConfig();

  const currentChangedItems = getChangedItems();
  const currentChangedItemsFingerprint = JSON.stringify(currentChangedItems);
  const llmChannelDraftItemsFingerprint = JSON.stringify(llmChannelDraftItems);
  const generationBackendDraftItems = useMemo(
    () => mergeGenerationBackendDraftItems(currentChangedItems, llmChannelDraftItems),
    // Fingerprints keep the status panel from refreshing when parent renders do not change draft content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [currentChangedItemsFingerprint, llmChannelDraftItemsFingerprint],
  );
  const agentBackendDraftItems = useMemo(
    () => {
      const merged = new Map(
        generationBackendDraftItems.map((item) => [item.key.trim().toUpperCase(), item]),
      );
      for (const item of currentChangedItems) {
        const key = item.key.trim().toUpperCase();
        if (AGENT_BACKEND_STATUS_KEYS.has(key)) {
          merged.set(key, item);
        }
      }
      return Array.from(merged.values());
    },
    // The fingerprint changes only when the draft content changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [currentChangedItemsFingerprint, generationBackendDraftItems],
  );
  const handleLlmChannelDraftItemsChange = useCallback((items: Array<{ key: string; value: string }>) => {
    setLlmChannelDraftItems(items);
  }, []);

  const refreshSetupStatus = useCallback(async () => {
    const requestId = setupStatusRequestIdRef.current + 1;
    setupStatusRequestIdRef.current = requestId;
    setSetupStatusError(null);
    setIsRefreshingSetupStatus(true);
    try {
      const status = await systemConfigApi.getSetupStatus();
      if (setupStatusRequestIdRef.current !== requestId) {
        return;
      }
      setSetupStatus(status);
    } catch (error: unknown) {
      if (setupStatusRequestIdRef.current !== requestId) {
        return;
      }
      setSetupStatusError(getParsedApiError(error));
    } finally {
      if (setupStatusRequestIdRef.current === requestId) {
        setIsRefreshingSetupStatus(false);
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const requestedCategory = new URLSearchParams(location.search).get('category');
    if (requestedCategory && categories.some((category) => category.category === requestedCategory)) {
      setActiveCategory(requestedCategory);
    }
  }, [categories, location.search, setActiveCategory]);

  useEffect(() => {
    if (isLoading || activeCategory !== 'system' || location.hash !== '#desktop-version-info') {
      return;
    }

    const node = document.getElementById('desktop-version-info');
    node?.scrollIntoView({ block: 'start' });
  }, [activeCategory, isLoading, location.hash]);

  useEffect(() => {
    void refreshSetupStatus();
  }, [refreshSetupStatus]);

  useEffect(() => {
    if (!toast) {
      return;
    }

    const timer = window.setTimeout(() => {
      clearToast();
    }, 3200);

    return () => {
      window.clearTimeout(timer);
    };
  }, [clearToast, toast]);

  const rawActiveItems = itemsByCategory[activeCategory] || [];
  const rawActiveItemMap = new Map(rawActiveItems.map((item) => [item.key, String(item.value ?? '')]));
  const firstSetupStockCode = parseSetupStockList(getConfigItem(itemsByCategory.base || [], 'STOCK_LIST')?.value)[0] || '';
  const screeningItem = (itemsByCategory.base || []).find((item) => item.key === 'SCREENING_ENABLED');
  const screeningEnabled = String(screeningItem?.value ?? '').trim().toLowerCase() === 'true';
  const shouldShowFirstRunSetup = activeCategory === 'base';
  const shouldShowScreeningSettings = activeCategory === 'base' && Boolean(screeningItem);
  const hasConfiguredChannels = Boolean((rawActiveItemMap.get('LLM_CHANNELS') || '').trim());
  const hasLitellmConfig = Boolean((rawActiveItemMap.get('LITELLM_CONFIG') || '').trim());
  const hasRuntimeSchedulerMismatch =
    schedulerRuntimeEnabled !== null
    && schedulerOverrideFromUi !== null
    && schedulerOverrideFromUi !== schedulerRuntimeEnabled;
  const hasRuntimeSchedulerMismatchInDraft = hasRuntimeSchedulerMismatch
    && !currentChangedItems.some((item) => item.key === 'SCHEDULE_ENABLED');
  const effectiveHasDirty = hasDirty || hasRuntimeSchedulerMismatchInDraft;
  const effectiveDirtyCount = dirtyCount + (hasRuntimeSchedulerMismatchInDraft ? 1 : 0);

  const handleSchedulerRuntimeStateChange = useCallback(({ runtimeEnabled, overrideEnabled }: {
    runtimeEnabled: boolean | null;
    overrideEnabled: boolean | null;
  }) => {
    setSchedulerRuntimeEnabled(runtimeEnabled);
    setSchedulerOverrideFromUi(overrideEnabled);
  }, []);

  // UI rendering rule only: hide channel-managed and legacy provider-specific
  // LLM keys from generic fields when channel mode is active. This does not
  // alter save/refresh payloads or config migration/rollback behavior.
  const LLM_CHANNEL_KEY_RE = /^LLM_[A-Z0-9_]+_(PROTOCOL|API_SURFACE|BASE_URL|API_KEY|API_KEYS|MODELS|EXTRA_HEADERS|ENABLED)$/;
  const AI_MODEL_HIDDEN_KEYS = new Set([
    'LLM_CHANNELS',
    'LLM_TEMPERATURE',
    'LITELLM_MODEL',
    'AGENT_LITELLM_MODEL',
    'LITELLM_FALLBACK_MODELS',
    'AIHUBMIX_KEY',
    'DEEPSEEK_API_KEY',
    'DEEPSEEK_API_KEYS',
    'GEMINI_API_KEY',
    'GEMINI_API_KEYS',
    'GEMINI_MODEL',
    'GEMINI_MODEL_FALLBACK',
    'GEMINI_TEMPERATURE',
    'ANTHROPIC_API_KEY',
    'ANTHROPIC_API_KEYS',
    'ANTHROPIC_MODEL',
    'ANTHROPIC_TEMPERATURE',
    'ANTHROPIC_MAX_TOKENS',
    'OPENAI_API_KEY',
    'OPENAI_API_KEYS',
    'OPENAI_BASE_URL',
    'OPENAI_MODEL',
    'OPENAI_VISION_MODEL',
    'OPENAI_TEMPERATURE',
    'VISION_MODEL',
  ]);
  const SYSTEM_HIDDEN_KEYS = new Set([
    'ADMIN_AUTH_ENABLED',
    ...SCHEDULER_SETTING_KEYS,
  ]);
  const BASE_HIDDEN_KEYS = new Set([
    'SCREENING_ENABLED',
  ]);
  const AGENT_HIDDEN_KEYS = new Set(['AGENT_GENERATION_BACKEND']);
  const activeItems =
    activeCategory === 'base'
      ? rawActiveItems.filter((item) => !BASE_HIDDEN_KEYS.has(item.key))
    : activeCategory === 'ai_model'
      ? rawActiveItems.filter((item) => {
        if (hasConfiguredChannels && LLM_CHANNEL_KEY_RE.test(item.key)) {
          return false;
        }
        if (hasConfiguredChannels && !hasLitellmConfig && AI_MODEL_HIDDEN_KEYS.has(item.key)) {
          return false;
        }
        return true;
      })
      : activeCategory === 'system'
        ? rawActiveItems.filter((item) => !SYSTEM_HIDDEN_KEYS.has(item.key))
      : activeCategory === 'agent'
        ? rawActiveItems.filter((item) => !AGENT_HIDDEN_KEYS.has(item.key))
      : rawActiveItems;
  const promptCacheAdvancedItems = activeCategory === 'ai_model'
    ? activeItems.filter(isPromptCacheAdvancedSetting)
    : [];
  const visibleActiveItems = activeCategory === 'ai_model'
    ? activeItems.filter((item) => !isPromptCacheAdvancedSetting(item))
    : activeItems;
  const hasActiveConfigItems = visibleActiveItems.length > 0 || promptCacheAdvancedItems.length > 0;
  const isEnvBackupAllowed = isDesktopRuntime || authEnabled;
  const envBackupActionDisabled = isLoading || isSaving || isExportingEnv || isImportingEnv || !isEnvBackupAllowed;

  const downloadEnvBackup = async () => {
    setEnvBackupActionError(null);
    setEnvBackupActionSuccess('');
    setIsExportingEnv(true);
    try {
      const payload = await systemConfigApi.exportEnv();
      const blob = new Blob([payload.content], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = formatEnvBackupFilename(isDesktopRuntime);
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(url);
      setEnvBackupActionSuccess(t('settings.envExported'));
    } catch (error: unknown) {
      setEnvBackupActionError(getParsedApiError(error));
    } finally {
      setIsExportingEnv(false);
    }
  };

  const beginEnvBackupImport = () => {
    setEnvBackupActionError(null);
    setEnvBackupActionSuccess('');
    if (hasDirty) {
      setShowImportConfirm(true);
      return;
    }
    envBackupImportRef.current?.click();
  };

  const handleEnvBackupImportFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    setShowImportConfirm(false);
    if (!file) {
      return;
    }

    setEnvBackupActionError(null);
    setEnvBackupActionSuccess('');
    setIsImportingEnv(true);
    try {
      const content = await file.text();
      const importResult = await systemConfigApi.importEnv({
        configVersion,
        content,
        reloadNow: true,
      });
      const reloaded = await load();
      if (!reloaded) {
        setEnvBackupActionError(createParsedApiError({
          title: t('settings.envImportedRefreshFailedTitle'),
          message: t('settings.envImportedRefreshFailedMessage'),
          rawMessage: t('settings.envImportedRefreshFailedRaw'),
          category: 'http_error',
        }));
        return;
      }
      if (importResult.updatedKeys.some((key) => SCHEDULER_SETTING_KEYS.has(key))) {
        setSchedulerStatusRefreshToken((current) => current + 1);
      }
      notifySystemConfigChanged();
      void refreshSetupStatus();
      setEnvBackupActionSuccess(t('settings.envImported'));
    } catch (error: unknown) {
      setEnvBackupActionError(getParsedApiError(error));
    } finally {
      setIsImportingEnv(false);
    }
  };

  const updateScreeningEnabled = async (nextEnabled: boolean) => {
    setScreeningActionError(null);
    setScreeningActionSuccess('');
    setIsUpdatingScreening(true);
    try {
      if (nextEnabled) {
        await screeningApi.enable();
        await refreshAfterExternalSave(['SCREENING_ENABLED']);
        setScreeningActionSuccess(t('settings.enabledScreeningSuccess'));
        return;
      }

      await systemConfigApi.update({
        configVersion,
        maskToken,
        reloadNow: true,
        items: [{ key: 'SCREENING_ENABLED', value: 'false' }],
      });
      notifyScreeningConfigChanged();
      await refreshAfterExternalSave(['SCREENING_ENABLED']);
      setScreeningActionSuccess(t('settings.disabledScreeningSuccess'));
    } catch (error: unknown) {
      setScreeningActionError(getParsedApiError(error));
      await refreshAfterExternalSave(['SCREENING_ENABLED']);
    } finally {
      setIsUpdatingScreening(false);
    }
  };

  const handleSaveConfig = async () => {
    const changedItems = getChangedItems();
    const syncRuntimeSchedulerState =
      schedulerOverrideFromUi !== null
      && schedulerRuntimeEnabled !== null
      && schedulerOverrideFromUi !== schedulerRuntimeEnabled
      && !changedItems.some((item) => item.key === 'SCHEDULE_ENABLED');
    const schedulerSyncItem: SystemConfigUpdateItem[] = syncRuntimeSchedulerState
      ? [{ key: 'SCHEDULE_ENABLED', value: schedulerOverrideFromUi ? 'true' : 'false' }]
      : [];
    const changedItemsToSave = [...changedItems, ...schedulerSyncItem];
    const changedScreeningItem = changedItems.find((item) => item.key === 'SCREENING_ENABLED');
    const changedSchedulerSettings = changedItemsToSave.some((item) => SCHEDULER_SETTING_KEYS.has(item.key));
    const result = await save(changedItemsToSave);
    if (!result.success) {
      return;
    }
    notifySystemConfigChanged();
    if (changedSchedulerSettings) {
      setSchedulerStatusRefreshToken((current) => current + 1);
    }
    void refreshSetupStatus();
    if (!changedScreeningItem) {
      return;
    }

    setScreeningActionError(null);
    setScreeningActionSuccess('');
    try {
      const isScreeningEnabled = changedScreeningItem.value.trim().toLowerCase() === 'true';
      if (isScreeningEnabled) {
        await screeningApi.enable();
        await refreshAfterExternalSave(['SCREENING_ENABLED']);
        setScreeningActionSuccess(t('settings.enabledScreeningSuccess'));
        return;
      }

      notifyScreeningConfigChanged();
      setScreeningActionSuccess(t('settings.disabledScreeningSuccess'));
    } catch (error: unknown) {
      setScreeningActionError(getParsedApiError(error));
      await refreshAfterExternalSave(['SCREENING_ENABLED']);
    }
  };

  const handleRunSetupSmoke = async () => {
    setSetupSmokeError(null);
    setSetupSmokeSuccess('');

    if (!setupStatus?.readyForSmoke) {
      setSetupSmokeError(createParsedApiError({
        title: t('settings.setupGuideSmokeUnavailableTitle'),
        message: t('settings.setupGuideSmokeNotReady'),
        rawMessage: t('settings.setupGuideSmokeNotReady'),
        category: 'missing_params',
      }));
      return;
    }

    if (!firstSetupStockCode) {
      setSetupSmokeError(createParsedApiError({
        title: t('settings.setupGuideSmokeUnavailableTitle'),
        message: t('settings.setupGuideSmokeNeedsStock'),
        rawMessage: t('settings.setupGuideSmokeNeedsStock'),
        category: 'missing_params',
      }));
      return;
    }

    setIsRunningSetupSmoke(true);
    try {
      const result = await analysisApi.analyzeAsync({
        stockCode: firstSetupStockCode,
        reportType: 'brief',
        asyncMode: true,
        notify: false,
        originalQuery: firstSetupStockCode,
        selectionSource: 'manual',
      });
      const taskId = 'taskId' in result ? result.taskId : result.accepted?.[0]?.taskId;
      setSetupSmokeSuccess(
        taskId
          ? t('settings.setupGuideSmokeAcceptedWithTask', { stock: firstSetupStockCode, taskId })
          : t('settings.setupGuideSmokeAccepted', { stock: firstSetupStockCode }),
      );
      void refreshSetupStatus();
    } catch (error: unknown) {
      setSetupSmokeError(getParsedApiError(error));
    } finally {
      setIsRunningSetupSmoke(false);
    }
  };

  const shouldGuardActiveConfigPanel = activeCategory === 'notification' || activeCategory === 'agent';
  const activeConfigPanelErrorTitle = activeCategory === 'agent' ? t('settings.agentSettings') : t('settings.notificationSettings');
  const settingsPanelDiagnosticHint = isDesktopRuntime
    ? uiLanguage === 'en'
      ? <>Check and provide the desktop log <code>desktop.log</code>, plus the release version, Windows version, and trigger path.</>
      : <>请查看并提供桌面端日志 <code>desktop.log</code>，同时补充 release 版本、Windows 版本和触发入口。</>
    : t('settings.diagnosticHintWeb');
  const activeCategoryTitle = getCategoryTitle(activeCategory as SystemConfigCategory, t('settings.activePanelTitle'), uiLanguage);
  const activeCategoryDescription = getCategoryDescription(activeCategory as SystemConfigCategory, '', uiLanguage);
  const selectedAgentBackend = (rawActiveItemMap.get('AGENT_BACKEND') || 'auto').trim().toLowerCase();
  const selectedAgentArch = (rawActiveItemMap.get('AGENT_ARCH') || 'single').trim().toLowerCase();
  const hasCodexArchitectureConflict = selectedAgentBackend === 'codex_app_server' && selectedAgentArch !== 'single';
  const codexArchitectureIssue: ConfigValidationIssue = {
    key: 'AGENT_ARCH',
    code: 'unsupported_agent_arch',
    message: t('settings.agentBackendSingleOnly'),
    severity: 'error',
    expected: 'single',
    actual: selectedAgentArch,
  };
  const activeConfigPanel = hasActiveConfigItems ? (
    <SettingsGroupCard
      title={activeCategoryTitle}
      description={activeCategoryDescription || t('settings.activePanelDescription')}
      icon={ACTIVE_PANEL_ICONS[activeCategory as SystemConfigCategory]}
    >
      {visibleActiveItems.length ? (
        // 字段列表本身是「一张暖色内嵌面板」，行与行之间仍用细分割线：
        // 分组感靠卡片描边，行高一律不动，避免设置页变得难扫。
        <div className="playful-card-soft divide-y divide-[var(--settings-border-soft)] overflow-hidden">
          {visibleActiveItems.map((item) => {
            const fieldIssues = item.key === 'AGENT_ARCH' && hasCodexArchitectureConflict
              ? [...(issueByKey[item.key] || []), codexArchitectureIssue]
              : issueByKey[item.key] || [];
            return (
              <SettingsField
                key={item.key}
                item={item}
                value={item.value}
                disabled={isSaving}
                onChange={setDraftValue}
                issues={fieldIssues}
              />
            );
          })}
        </div>
      ) : null}
      {promptCacheAdvancedItems.length ? (
        <details className="playful-card-soft group/prompt-cache overflow-hidden transition-[filter] duration-200 hover:brightness-105">
          <summary className="flex cursor-pointer list-none items-start justify-between gap-3 px-4 py-4 [&::-webkit-details-marker]:hidden">
            <div className="min-w-0 space-y-1">
              <p className="text-sm font-semibold text-foreground">
                {t('settings.promptCacheAdvancedTitle')}
              </p>
              <p className="text-xs leading-5 text-muted-text">
                {t('settings.promptCacheAdvancedDescription')}
              </p>
            </div>
            <ChevronDown className="mt-0.5 h-4 w-4 shrink-0 text-muted-text transition-transform group-open/prompt-cache:rotate-180" aria-hidden="true" />
          </summary>
          <div className="divide-y divide-[var(--settings-border-soft)] border-t border-[var(--settings-border-soft)]">
            {promptCacheAdvancedItems.map((item) => (
              <SettingsField
                key={item.key}
                item={item}
                value={item.value}
                disabled={isSaving}
                onChange={setDraftValue}
                issues={issueByKey[item.key] || []}
              />
            ))}
          </div>
        </details>
      ) : null}
    </SettingsGroupCard>
  ) : (
    <EmptyState
      title={t('settings.currentCategoryEmptyTitle')}
      description={t('settings.currentCategoryEmptyDescription')}
      className="playful-card-soft"
    />
  );

  return (
    // 保留 settings-page 类名：index.css 里 `.settings-page .input-surface` 的表单观感依赖它，不能丢；
    // 外层换成 PlayfulPage 是为了让设置页和首页 / 组合 / 选股页共享同一块暖色底。
    <PlayfulPage bare className="settings-page min-h-full px-4 pb-6 pt-4 md:px-6">
      {/* 顶部 hero 与其它已改造页面同构（吉祥物 + 标题 + 说明 + 操作区），
          但操作区里的重置 / 保存按钮完全沿用原有 wiring、禁用条件与文案。 */}
      <PlayfulCard
        variant="hero"
        padding="none"
        className="mb-4 flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center md:justify-between"
      >
        <div className="flex min-w-0 items-center gap-3 sm:gap-4">
          <PlayfulMascot className="h-[72px] w-[104px] shrink-0" />
          <div className="min-w-0">
            <h1 className="text-lg font-extrabold tracking-tight text-foreground md:text-xl">{t('settings.pageTitle')}</h1>
            <p className="mt-1 max-w-3xl text-xs font-semibold leading-5 playful-ink-soft sm:text-sm sm:leading-6">
              {t('settings.pageDescription')}
            </p>
          </div>
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {/* 未保存项在标题旁边先给一次提示：设置页分组多，滚到底部才发现「有改动没保存」代价太高。
              这里只是既有 effectiveDirtyCount 的另一种呈现，不参与任何保存逻辑。 */}
          {effectiveHasDirty ? (
            <PlayfulChip tone="sun" className="shrink-0">
              {t('settingsPage.unsavedChanges', { count: effectiveDirtyCount })}
            </PlayfulChip>
          ) : null}
          <Button
            type="button"
            variant="settings-secondary"
            size="sm"
            className="px-2.5"
            onClick={resetDraft}
            disabled={isLoading || isSaving}
          >
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            {t('settings.reset')}
          </Button>
          <Button
            type="button"
            variant="settings-primary"
            size="sm"
            className="px-2.5"
            onClick={() => void handleSaveConfig()}
            disabled={!effectiveHasDirty || isSaving || isLoading}
            isLoading={isSaving}
            loadingText={t('settings.saving')}
          >
            <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
            {isSaving
              ? t('settings.saving')
              : effectiveDirtyCount
                ? t('settings.saveConfigWithCount', { count: effectiveDirtyCount })
                : t('settings.saveConfig')}
          </Button>
        </div>

        {saveError ? (
          <ApiErrorAlert
            className="mt-3"
            error={saveError}
            actionLabel={retryAction === 'save' ? t('settings.saveRetry') : undefined}
            onAction={retryAction === 'save' ? () => void retry() : undefined}
          />
        ) : null}
      </PlayfulCard>

      {loadError ? (
        <ApiErrorAlert
          error={loadError}
          actionLabel={retryAction === 'load' ? t('common.retry') : t('settings.reload')}
          onAction={() => void retry()}
          className="mb-4"
        />
      ) : null}

      {isLoading ? (
        <SettingsLoading />
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
          <aside className="lg:sticky lg:top-4 lg:self-start">
            <SettingsCategoryNav
              categories={categories}
              itemsByCategory={itemsByCategory}
              activeCategory={activeCategory}
              onSelect={setActiveCategory}
            />
          </aside>

          <PlayfulSection>
            {shouldShowFirstRunSetup ? (
              <FirstRunSetupCard
                status={setupStatus}
                isLoading={isRefreshingSetupStatus}
                error={setupStatusError}
                firstStockCode={firstSetupStockCode}
                isSaving={isSaving}
                isRunningSmoke={isRunningSetupSmoke}
                smokeError={setupSmokeError}
                smokeSuccess={setupSmokeSuccess}
                onRefresh={refreshSetupStatus}
                onSelectCategory={setActiveCategory}
                onRunSmoke={handleRunSetupSmoke}
                listSeparator={uiLanguage === 'en' ? ', ' : '、'}
                t={t}
              />
            ) : null}
            {shouldShowScreeningSettings ? (
              <SettingsGroupCard
                title={t('settings.screening')}
                description={t('settings.screeningDescription')}
                icon={<Target className="h-4 w-4" />}
              >
                <div className="playful-card-soft flex flex-col gap-4 px-4 py-4 md:flex-row md:items-center md:justify-between">
                  <div>
                    <p className="text-sm font-semibold text-foreground">
                      {screeningEnabled ? t('settings.screeningEnabled') : t('settings.screeningDisabled')}
                    </p>
                    <p className="mt-1 text-xs leading-6 text-muted-text">
                      {t('settings.screeningSummary')}
                    </p>
                    <p className="mt-2 text-xs leading-6 text-amber-700 dark:text-amber-300">
                      {t('settings.screeningRisk')}
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      type="button"
                      variant={screeningEnabled ? 'settings-secondary' : 'settings-primary'}
                      onClick={() => void updateScreeningEnabled(!screeningEnabled)}
                      disabled={isSaving || isLoading || isUpdatingScreening}
                      isLoading={isUpdatingScreening}
                      loadingText={screeningEnabled ? t('settings.disablingScreening') : t('settings.enablingScreening')}
                    >
                      {screeningEnabled ? t('settings.disableScreening') : t('settings.enableScreening')}
                    </Button>
                  </div>
                </div>
                {screeningActionError ? (
                  <div className="mt-3">
                    <ApiErrorAlert error={screeningActionError} />
                  </div>
                ) : null}
                {!screeningActionError && screeningActionSuccess ? (
                  <div className="mt-3">
                    <SettingsAlert title={t('settings.actionSuccess')} message={screeningActionSuccess} variant="success" />
                  </div>
                ) : null}
              </SettingsGroupCard>
            ) : null}
            {/* 认证 / 修改密码 / 通知测试三个面板来自 components/settings（共享组件，本次不改），
                它们自带 SettingsSectionCard 表面，所以这里保持直接渲染，不额外套卡以免出现双层边框。 */}
            {activeCategory === 'system' ? <AuthSettingsCard /> : null}
            {activeCategory === 'system' ? (
              <SchedulerSettingsCard
                items={rawActiveItems}
                disabled={isSaving || isLoading}
                issueByKey={issueByKey}
                statusRefreshToken={schedulerStatusRefreshToken}
                onSchedulerStateChange={handleSchedulerRuntimeStateChange}
                onChange={setDraftValue}
                t={t}
                language={uiLanguage}
              />
            ) : null}
            {activeCategory === 'system' ? (
              <div id="desktop-version-info">
              <SettingsGroupCard
                title={t('settings.versionInfo')}
                description={t('settings.versionInfoDescription')}
                icon={<Tag className="h-4 w-4" />}
              >
                <div
                  className={`grid grid-cols-1 gap-3 ${shouldShowDesktopVersionCard ? 'md:grid-cols-4' : 'md:grid-cols-3'}`}
                >
                  <div className="playful-card-soft px-4 py-3">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-text">
                      {t('settings.versionWebui')}
                    </p>
                    <p className="mt-2 break-all font-mono text-sm text-foreground">
                      {WEB_BUILD_INFO.version}
                    </p>
                  </div>
                  <div className="playful-card-soft px-4 py-3">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-text">
                      {t('settings.versionRevision')}
                    </p>
                    <p className="mt-2 break-all font-mono text-sm text-foreground">
                      {WEB_BUILD_INFO.revision}
                    </p>
                  </div>
                  <div className="playful-card-soft px-4 py-3">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-text">
                      {t('settings.versionBuildTime')}
                    </p>
                    <p className="mt-2 break-all font-mono text-sm text-foreground">
                      {WEB_BUILD_INFO.buildTime}
                    </p>
                  </div>
                  {shouldShowDesktopVersionCard ? (
                    <div className="playful-card-soft px-4 py-3">
                      <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-text">
                        {t('settings.versionDesktop')}
                      </p>
                      <p className="mt-2 break-all font-mono text-sm text-foreground">
                        {desktopAppVersion}
                      </p>
                    </div>
                  ) : null}
                </div>
                <p className="text-xs leading-6 text-muted-text">
                  {t('settings.updateBuildDescription')}
                </p>
                {canCheckDesktopUpdate ? (
                  <div className="playful-card-soft mt-4 space-y-3 px-4 py-4">
                    <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                      <div>
                        <p className="text-sm font-medium text-foreground">{t('settings.desktopUpdate')}</p>
                        <p className="text-xs leading-6 text-muted-text">
                          {t('settings.desktopUpdateDescription')}
                        </p>
                      </div>
                      <Button
                        type="button"
                        variant="settings-secondary"
                        onClick={() => void handleDesktopUpdateCheck()}
                        disabled={isDesktopUpdateBusy}
                        isLoading={isCheckingDesktopUpdate}
                        loadingText={t('settings.checkingDesktopUpdate')}
                      >
                        {t('settings.checkDesktopUpdate')}
                      </Button>
                    </div>
                    {desktopUpdateNotice ? (
                      <SettingsAlert
                        title={desktopUpdateNotice.title}
                        message={desktopUpdateNotice.message}
                        variant={desktopUpdateNotice.variant}
                        actionLabel={desktopUpdateNotice.actionLabel}
                        onAction={desktopUpdateNotice.actionLabel ? () => {
                          if (desktopUpdateNotice.actionKind === 'install') {
                            void installDesktopUpdate();
                            return;
                          }
                          void openDesktopReleasePage();
                        } : undefined}
                      />
                    ) : (
                      <p className="text-xs leading-6 text-muted-text">
                        {t('settings.desktopCurrentNoStatus')}
                      </p>
                    )}
                  </div>
                ) : null}
                {WEB_BUILD_INFO.isFallbackVersion ? (
                  <p className="text-xs leading-6 text-amber-700 dark:text-amber-300">
                    {t('settings.fallbackVersionWarning')}
                  </p>
                ) : null}
              </SettingsGroupCard>
              </div>
            ) : null}
            {activeCategory === 'system' ? (
              <SettingsGroupCard
                title={t('settings.configBackup')}
                description={t('settings.configBackupDescription')}
                icon={<Archive className="h-4 w-4" />}
              >
                <div className="space-y-4">
                  {!isEnvBackupAllowed ? (
                    <p className="text-xs leading-6 text-amber-700 dark:text-amber-300">
                      {t('settings.disabledAuthBackupWarning')}
                    </p>
                  ) : null}
                  <div className="flex flex-wrap items-center gap-3">
                    <Button
                      type="button"
                      variant="settings-secondary"
                      onClick={() => void downloadEnvBackup()}
                      disabled={envBackupActionDisabled}
                      isLoading={isExportingEnv}
                      loadingText={t('settings.exportingEnv')}
                    >
                      {t('settings.exportEnv')}
                    </Button>
                    <Button
                      type="button"
                      variant="settings-primary"
                      onClick={beginEnvBackupImport}
                      disabled={envBackupActionDisabled}
                      isLoading={isImportingEnv}
                      loadingText={t('settings.importingEnv')}
                    >
                      {t('settings.importEnv')}
                    </Button>
                    <input
                      ref={envBackupImportRef}
                      type="file"
                      accept=".env,.txt"
                      className="hidden"
                      onChange={(event) => {
                        void handleEnvBackupImportFile(event);
                      }}
                    />
                  </div>
                  <p className="text-xs leading-6 text-muted-text">
                    {t('settings.envExportNote')}
                  </p>
                  <p className="text-xs leading-6 text-muted-text">
                    {t('settings.envDockerNote')}
                  </p>
                  {envBackupActionError ? (
                    <ApiErrorAlert
                      error={envBackupActionError}
                      actionLabel={envBackupActionError.status === 409 ? t('settings.reload') : undefined}
                      onAction={envBackupActionError.status === 409 ? () => void load() : undefined}
                    />
                  ) : null}
                  {!envBackupActionError && envBackupActionSuccess ? (
                    <SettingsAlert title={t('settings.actionSuccess')} message={envBackupActionSuccess} variant="success" />
                  ) : null}
                </div>
              </SettingsGroupCard>
            ) : null}
            {activeCategory === 'base' ? (
              <SettingsGroupCard
                title={t('settings.intelligentImport')}
                description={t('settings.intelligentImportDescription')}
                icon={<Sparkles className="h-4 w-4" />}
              >
                <IntelligentImport
                  stockListValue={
                    (activeItems.find((i) => i.key === 'STOCK_LIST')?.value as string) ?? ''
                  }
                  configVersion={configVersion}
                  maskToken={maskToken}
                  onMerged={async () => {
                    await refreshAfterExternalSave(['STOCK_LIST']);
                    void refreshSetupStatus();
                  }}
                  disabled={isSaving || isLoading}
                />
              </SettingsGroupCard>
            ) : null}
            {activeCategory === 'ai_model' ? (
              <SettingsGroupCard
                title={t('settings.llmAccess')}
                description={t('settings.llmAccessDescription')}
                icon={<Cpu className="h-4 w-4" />}
              >
                <GenerationBackendStatusPanel
                  items={generationBackendDraftItems}
                  maskToken={maskToken}
                  disabled={isSaving || isLoading}
                />
                <LLMChannelEditor
                  items={rawActiveItems}
                  configVersion={configVersion}
                  maskToken={maskToken}
                  modelProviderPrefixes={llmModelProviders}
                  onDraftItemsChange={handleLlmChannelDraftItemsChange}
                  onSaved={async (updatedItems) => {
                    setLlmChannelDraftItems([]);
                    await refreshAfterExternalSave(updatedItems.map((item) => item.key));
                    void refreshSetupStatus();
                  }}
                  disabled={isSaving || isLoading}
                />
              </SettingsGroupCard>
            ) : null}
            {activeCategory === 'system' && passwordChangeable ? (
              <ChangePasswordCard />
            ) : null}
            {activeCategory === 'notification' ? (
              <SettingsPanelErrorBoundary
                title={t('settings.notificationTest')}
                resetKey={`notification-test:${configVersion}`}
                diagnosticHint={settingsPanelDiagnosticHint}
              >
                <NotificationTestPanel
                  items={rawActiveItems.map((item) => ({ key: item.key, value: String(item.value ?? '') }))}
                  maskToken={maskToken}
                  disabled={isSaving || isLoading}
                />
              </SettingsPanelErrorBoundary>
            ) : null}
            {activeCategory === 'agent' ? (
              <SettingsPanelErrorBoundary
                title={t('settings.agentBackendStatus')}
                resetKey={`agent-backend:${configVersion}`}
                diagnosticHint={settingsPanelDiagnosticHint}
              >
                <SettingsGroupCard
                  title={t('settings.agentBackendSectionTitle')}
                  description={t('settings.agentBackendSectionDescription')}
                  icon={<Bot className="h-4 w-4" />}
                >
                  <AgentBackendStatusPanel
                    items={agentBackendDraftItems}
                    maskToken={maskToken}
                    selectedBackend={selectedAgentBackend}
                    agentArch={selectedAgentArch}
                    disabled={isSaving || isLoading}
                    onUseSingleAgent={() => setDraftValue('AGENT_ARCH', 'single')}
                    onEnableAgentMode={() => setDraftValue('AGENT_MODE', 'true')}
                  />
                </SettingsGroupCard>
              </SettingsPanelErrorBoundary>
            ) : null}
            {shouldGuardActiveConfigPanel && hasActiveConfigItems ? (
              <SettingsPanelErrorBoundary
                title={activeConfigPanelErrorTitle}
                resetKey={`${activeCategory}:${configVersion}`}
                diagnosticHint={settingsPanelDiagnosticHint}
              >
                {activeConfigPanel}
              </SettingsPanelErrorBoundary>
            ) : activeConfigPanel}
          </PlayfulSection>
        </div>
      )}

      {toast ? (
        <div className="fixed bottom-5 right-5 z-50 w-[320px] max-w-[calc(100vw-24px)]">
          {toast.type === 'success'
            ? (
                <SettingsAlert
                  title={t('settings.actionSuccess')}
                  message={toast.message}
                  variant="success"
                  presentation="toast"
                />
              )
            : <ApiErrorAlert error={toast.error} />}
        </div>
      ) : null}
      <ConfirmDialog
        isOpen={showImportConfirm}
        title={t('settings.importConfirmTitle')}
        message={t('settings.importConfirmMessage')}
        confirmText={t('settings.importConfirmContinue')}
        cancelText={t('common.cancel')}
        onConfirm={() => {
          setShowImportConfirm(false);
          envBackupImportRef.current?.click();
        }}
        onCancel={() => {
          setShowImportConfirm(false);
        }}
      />
    </PlayfulPage>
  );
};

export default SettingsPage;
