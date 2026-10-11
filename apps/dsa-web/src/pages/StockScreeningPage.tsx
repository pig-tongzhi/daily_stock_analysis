import type React from 'react';
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  Bookmark,
  Building2,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Clock3,
  Droplet,
  Factory,
  Gem,
  Landmark,
  Pickaxe,
  Plane,
  Play,
  RefreshCw,
  Search,
  Shield,
  SlidersHorizontal,
  Stethoscope,
  Trees,
  Utensils,
  Wrench,
} from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import {
  screeningApi,
  type ScreeningCandidate,
  type ScreeningHotspotDetail,
  type ScreeningHotspot,
  type ScreeningHotspotsResponse,
  type ScreeningRunSummary,
  type ScreeningScreenResponse,
  type ScreeningScreenTaskStatus,
  type ScreeningStrategy,
  type ScreeningTrackingResponse,
} from '../api/screening';
import { formatParsedApiError, getParsedApiError, toApiErrorMessage, type ParsedApiError } from '../api/error';
import { Badge, Button, InlineAlert, Select } from '../components/common';
// 与首页共用同一套「卡通友好」基元：页面只组合这些组件，不再自己拼一长串
// Tailwind 类名，保证同一个视觉语言只在 components/playful 一处演进。
//
// 注意：状态类胶囊不用 PlayfulChip —— index.css 的 .playful-pill 会覆盖
// Tailwind 的颜色工具类（未分层样式优先于 @layer utilities），tone 实际不生效，
// 会把"可复盘/待行情"这类语义色压成中性灰。语义状态一律走既有 Badge 的
// success/warning/danger 变体。
import {
  PlayfulCard,
  PlayfulEmptyState,
  PlayfulList,
  PlayfulListItem,
  PlayfulMascot,
  PlayfulPage,
  PlayfulSectionTitle,
  PlayfulStat,
  PlayfulStatGrid,
  PlayfulTrack,
  type PlayfulStatTone,
} from '../components/playful';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import {
  benchmarkIndexLabelKey,
  formatRatePercent,
  formatSignedPercent,
  trackingBenchmarkReasonKey,
  trackingReasonKey,
} from './screeningTrackingDisplay';

const MARKETS = [{ id: 'cn', label: 'A 股' }];
// 复盘回看窗口。与后端 SCREENING_PICK_TRACKING_DAYS 的默认值保持一致；
// 这里只是请求参数，真正的口径以后端返回的 windowDays 为准（页面上按返回值展示）。
const TRACKING_WINDOW_DAYS = 60;
const SCREEN_TASK_STORAGE_KEY = 'dsa.screening.activeScreenTask.v1';
const SCREEN_TASK_POLL_INTERVAL_MS = 2000;
const CUSTOM_STRATEGY_OPTION_VALUE = '__custom_strategy__';
const STRATEGY_CATEGORY_LABELS: Record<string, string> = {
  framework: '综合',
  income: '收益',
  momentum: '动量',
  quality: '质量',
  reversal: '反转',
  trend: '趋势',
  value: '价值',
};

const formatStrategyCategory = (value?: string) => {
  const normalized = value?.trim();
  if (!normalized) {
    return '自定义';
  }
  return STRATEGY_CATEGORY_LABELS[normalized.toLowerCase()] || normalized;
};

type PersistedScreenTask = {
  taskId: string;
  runId?: string;
  market: string;
  strategy: string;
  maxResults: number;
};

const formatRunCreatedAt = (value: string | null | undefined) => {
  if (!value) {
    return '时间未知';
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
};

// 历史条目里展示筛选条件：策略 ID → 中文名（找不到时回退到原始 ID）
const formatHistoryStrategyName = (strategyId: string, strategies: ScreeningStrategy[]): string => {
  const matched = strategies.find((item) => item.id === strategyId);
  return matched?.name || matched?.title || strategyId || '未知策略';
};

// 历史条目里展示筛选条件：市场 ID → 中文标签
const formatHistoryMarketLabel = (marketId: string | null | undefined): string =>
  MARKETS.find((item) => item.id === marketId)?.label || marketId || 'cn';

const readPersistedScreenTask = (): PersistedScreenTask | null => {
  if (typeof window === 'undefined') {
    return null;
  }
  try {
    const raw = window.sessionStorage.getItem(SCREEN_TASK_STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw) as Partial<PersistedScreenTask>;
    if (typeof parsed.taskId !== 'string' || !parsed.taskId.trim()) {
      return null;
    }
    const restoredMaxResults = Number(parsed.maxResults);
    return {
      taskId: parsed.taskId,
      runId: typeof parsed.runId === 'string' && parsed.runId.trim() ? parsed.runId : undefined,
      market: typeof parsed.market === 'string' && parsed.market.trim() ? parsed.market : 'cn',
      strategy: typeof parsed.strategy === 'string' && parsed.strategy.trim() ? parsed.strategy : 'dual_low',
      maxResults: Number.isFinite(restoredMaxResults) ? Math.min(100, Math.max(1, restoredMaxResults)) : 3,
    };
  } catch {
    return null;
  }
};

const persistScreenTask = (task: PersistedScreenTask) => {
  try {
    window.sessionStorage.setItem(SCREEN_TASK_STORAGE_KEY, JSON.stringify(task));
  } catch {
    // Session storage is best-effort; polling still works while the page stays mounted.
  }
};

const clearPersistedScreenTask = () => {
  try {
    window.sessionStorage.removeItem(SCREEN_TASK_STORAGE_KEY);
  } catch {
    // Ignore storage cleanup failures.
  }
};

const isUnrecoverableScreenTaskError = (error: ParsedApiError) =>
  error.title === '选股任务不可恢复';

const formatRecoverableScreenTaskPollingError = (error: ParsedApiError) => {
  if (error.category === 'upstream_timeout') {
    return '选股任务仍在后台运行，状态轮询暂时超时，将自动重试。';
  }
  if (error.category === 'upstream_network' || error.category === 'local_connection_failed') {
    return '选股任务仍在后台运行，暂时无法连接本地服务获取状态，将自动重试。';
  }
  return formatParsedApiError(error) || '暂时无法获取选股任务状态，稍后将自动重试。';
};

const formatScore = (score: ScreeningCandidate['score']) => {
  if (score == null || Number.isNaN(Number(score))) {
    return '-';
  }
  return Number(score).toFixed(2);
};

const formatNumber = (value: unknown, digits = 2) => {
  if (value == null || value === '' || Number.isNaN(Number(value))) {
    return '-';
  }
  return Number(value).toFixed(digits);
};

const formatAmount = (value: unknown) => {
  if (value == null || value === '' || Number.isNaN(Number(value))) {
    return '-';
  }
  const amount = Number(value);
  if (Math.abs(amount) >= 100_000_000) {
    return `${(amount / 100_000_000).toFixed(2)} 亿`;
  }
  if (Math.abs(amount) >= 10_000) {
    return `${(amount / 10_000).toFixed(2)} 万`;
  }
  return amount.toFixed(2);
};

const formatPercent = (value: unknown) => {
  if (value == null || value === '' || Number.isNaN(Number(value))) {
    return '-';
  }
  return `${(Number(value) * 100).toFixed(0)}%`;
};

// 复盘收益率已经是百分数（后端返回 +1.23 表示 +1.23%），不能再乘 100。
const formatTrackingPercent = (value: number | null | undefined, digits = 2) => {
  if (value == null || Number.isNaN(Number(value))) {
    return '-';
  }
  const number = Number(value);
  return `${number > 0 ? '+' : ''}${number.toFixed(digits)}%`;
};

/**
 * 复盘收益的涨跌配色，遵循 A 股习惯：红涨绿跌。
 *
 * 为什么不用 text-success / text-danger：那一套是「成功 / 失败」语义
 * （--color-success 是绿色），用在收益率上会把上涨画成绿色，正好和 A 股习惯相反。
 * playful 的 --playful-up / --playful-down 就是为此定义的（见 index.css），
 * 与首页 PlayfulStat 的 tone="up" / tone="down" 是同一组颜色。
 */
const getTrackingReturnClassName = (value: number | null | undefined) => {
  if (value == null || Number.isNaN(Number(value))) {
    return 'text-secondary-text';
  }
  if (Number(value) > 0) {
    return 'text-[var(--playful-up)]';
  }
  if (Number(value) < 0) {
    return 'text-[var(--playful-down)]';
  }
  return 'text-secondary-text';
};

/** PlayfulStat 的 tone 版本：null 与持平都走中性色，不冒充涨也不冒充跌。 */
const getTrackingReturnTone = (value: number | null | undefined): PlayfulStatTone => {
  if (value == null || Number.isNaN(Number(value))) {
    return 'soft';
  }
  if (Number(value) > 0) {
    return 'up';
  }
  if (Number(value) < 0) {
    return 'down';
  }
  return 'soft';
};

const FACTOR_LABELS: Record<string, string> = {
  value: '估值',
  liquidity: '流动性',
  momentum: '动量',
  reversal: '反转',
  activity: '活跃度',
  stability: '稳定性',
  size: '规模',
  theme_heat: '题材热度',
  topic_alignment: '题材匹配',
};

const POST_TAG_LABELS: Record<string, string> = {
  value_quality: '价值质量',
  controlled_reversal: '受控反转',
  momentum: '趋势动量',
  liquidity: '流动性',
};

const HOTSPOT_QUALITY_LABELS: Record<string, string> = {
  available: '可用',
  failed: '不可用',
  partial: '部分可用',
  stale: '缓存',
};

const HOTSPOT_STAGE_LABELS: Record<string, string> = {
  accelerating: '加速主升',
  cooling: '降温退潮',
  diverging: '分歧放量',
  initial: '初次异动',
  persistent_hot: '确认扩散',
  warming: '确认扩散',
  weakening: '降温退潮',
};

const HOTSPOT_ROLE_LABELS: Record<string, string> = {
  core_leader: '核心龙头',
  follower: '助攻',
  laggard: '掉队',
  leader: '核心龙头',
  secondary: '补涨',
};

const HOTSPOT_MISSING_FIELD_LABELS: Record<string, string> = {
  canonical_topic: '标准题材',
  hotspot_constituents: '概念股列表',
  leader_stocks: '核心股',
  live_stocks: '实时概念股行情',
  route: '发酵路径',
  source: '数据来源',
  stocks: '概念股列表',
  timeline: '发酵时间线',
};

const getHotspotStageLabel = (value: unknown) => {
  const text = String(value || '').trim();
  if (!text) {
    return '';
  }
  return HOTSPOT_STAGE_LABELS[text.toLowerCase()] || text;
};

const getHotspotRoleLabel = (value: unknown) => {
  const text = String(value || '').trim();
  if (!text) {
    return '概念股';
  }
  return HOTSPOT_ROLE_LABELS[text.toLowerCase()] || text;
};

const getHotspotQualityLabel = (value: unknown) => {
  const text = String(value || '').trim();
  return HOTSPOT_QUALITY_LABELS[text.toLowerCase()] || '待确认';
};

const getLocalFactorReason = (item: ScreeningCandidate) => {
  const factors = Object.entries(item.factorScores || {})
    .filter(([, value]) => typeof value === 'number')
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, 3)
    .map(([key, value]) => `${FACTOR_LABELS[key] || key} ${Number(value).toFixed(0)}`);
  const tags = (item.postAnalysisTags || [])
    .slice(0, 2)
    .map((tag) => POST_TAG_LABELS[tag] || tag);
  if (factors.length > 0) {
    return `主要优势：${factors.join('、')}${tags.length > 0 ? `；标签：${tags.join('、')}` : ''}`;
  }
  return '';
};

const getCandidateReason = (item: ScreeningCandidate) => {
  if (item.llmThesis || item.llmScore != null) {
    return item.reason || item.llmThesis || 'LLM 已完成相对排序。';
  }
  const localReason = getLocalFactorReason(item);
  if (localReason) {
    return localReason;
  }
  if (item.reason) {
    return item.reason;
  }
  const summaries = item.postAnalysisSummaries || {};
  const summary = Object.values(summaries).find((value) => typeof value === 'string' && value.trim());
  if (typeof summary === 'string') {
    return summary;
  }
  return '暂无摘要，请查看因子和风险信息。';
};

const getSignal = (item: ScreeningCandidate) => {
  const rawSignal = item.raw.action ?? item.raw.signal ?? item.raw.recommendation;
  return typeof rawSignal === 'string' && rawSignal.trim() ? rawSignal : '观察';
};

const getFactorEntries = (item: ScreeningCandidate) =>
  Object.entries(item.factorScores || {})
    .filter(([, value]) => typeof value === 'number')
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, 6);

const toMessageList = (values: string[] | undefined) =>
  Array.isArray(values) ? values.map((value) => String(value).trim()).filter(Boolean) : [];

const KNOWN_SNAPSHOT_SOURCES = new Set(['tushare', 'sina', 'efinance', 'akshare_em', 'em_datacenter', 'baostock']);
const MAX_MESSAGE_DETAIL_LENGTH = 96;

const truncateMessageDetail = (value: string, maxLength = MAX_MESSAGE_DETAIL_LENGTH) => {
  const text = value.replace(/\s+/g, ' ').trim();
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength - 1)}…`;
};

const summarizeScreeningDiagnostic = (detail: string) => {
  if (/no_json_found|invalid_response|coverage below threshold/i.test(detail)) {
    return '模型未返回可用的结构化排序结果';
  }
  if (/call_failed/i.test(detail)) {
    return '模型调用失败';
  }
  if (/trade_cal returned no open trading days/i.test(detail)) {
    return '交易日历暂无可用开市日';
  }
  if (/too many requests|rate limit|http\s*429/i.test(detail)) {
    return '请求过于频繁';
  }
  if (/403 forbidden|forbidden|access denied/i.test(detail)) {
    return '访问被拒绝';
  }
  if (/timeout|timed out/i.test(detail)) {
    return '请求超时';
  }
  if (/RemoteDisconnected|Connection aborted|ProtocolError|ConnectionPool|Max retries exceeded|ProxyError|NameResolutionError/i.test(detail)) {
    return '网络连接中断';
  }
  if (/missing .*api key|GEMINI_API_KEY|GOOGLE_API_KEY|gemini_api_key/i.test(detail)) {
    return '缺少可用 LLM API Key';
  }
  if (/returned no data|empty/i.test(detail)) {
    return '未返回可用数据';
  }

  const withoutUrl = detail
    .replace(/https?:\/\/\S+/gi, 'URL')
    .replace(/\bwith url:\s*\S+/gi, 'with url: URL')
    .replace(/\burl:\s*\S+/gi, 'url: URL');
  return truncateMessageDetail(withoutUrl);
};

const parseSourceDiagnostic = (value: string) => {
  const match = value.match(/^([a-zA-Z0-9_-]+)\s*[:：]\s*(.+)$/);
  if (!match) {
    return null;
  }
  return {
    source: match[1],
    detail: match[2],
  };
};

const normalizeScreenMessageKey = (value: string) => {
  const formatted = formatScreenMessage(value);
  return formatted ? formatted.trim().toLowerCase() : value.trim().toLowerCase();
};

const formatEnrichmentSummary = (value: string) =>
  value
    .replace(/(?:DSA|rjharness)行情\s*[:：]\s*/gi, '行情：')
    .replace(/(?:DSA|rjharness)新闻\s*[:：]\s*/gi, '新闻：')
    .replace(/(?:DSA|rjharness)事件\s*[:：]\s*/gi, '事件：');

const formatScreenMessage = (value: string) => {
  if (/^(?:DSA|rjharness) provider context applied \d+ of \d+ candidates/i.test(value)) {
    return '';
  }
  if (/^LLM ranking skipped:\s*no LLM config/i.test(value)) {
    return '未配置智能重排模型，当前使用确定性因子排序。';
  }
  if (/^LLM ranking failed/i.test(value)) {
    return `未完成智能重排：${summarizeScreeningDiagnostic(value)}；当前结果继续使用确定性因子评分。`;
  }
  if (/no_json_found|invalid_response|coverage below threshold|call_failed/i.test(value)) {
    return `未完成智能重排：${summarizeScreeningDiagnostic(value)}；当前结果继续使用确定性因子评分。`;
  }
  if (/^(?:LLM ranking prompt|LLM context) truncated:/i.test(value)) {
    return '';
  }
  if (/^(?:Remote post-analysis cap|Risk veto excluded|Snapshot hard-filter waterfall|Daily hard-filter waterfall|Daily hard-filter rejections|Candidate context collected rows=)/i.test(value)) {
    return '';
  }
  if (/^Daily K-line (?:enrichment attempted|sources|quality flags|source ordering|source health):?/i.test(value)) {
    return '';
  }
  if (/^Daily K-line enrichment row errors:/i.test(value)) {
    return '部分候选的日线数据未能补齐，结果已按可用数据生成。';
  }
  if (/^Daily K-line enrichment skipped:/i.test(value)) {
    return '可选日线数据补充未完成，结果已按快照数据生成。';
  }
  if (/^Candidate context row errors:/i.test(value)) {
    return '部分候选的辅助数据未能补齐。';
  }
  if (/^Industry\/concepts enrichment:/i.test(value)) {
    return '部分行业或题材信息未能补齐。';
  }
  if (/^(?:DSA|rjharness) deep analysis failed for /i.test(value)) {
    return '部分候选的深度分析未完成。';
  }

  const snapshotFallback = value.match(/^Snapshot source fallback:\s*(.+)$/i);
  if (snapshotFallback) {
    const parsed = parseSourceDiagnostic(snapshotFallback[1]);
    if (parsed) {
      return `数据源降级：${parsed.source}（${summarizeScreeningDiagnostic(parsed.detail)}）`;
    }
    return `数据源降级：${summarizeScreeningDiagnostic(snapshotFallback[1])}`;
  }

  const parsed = parseSourceDiagnostic(value);
  if (parsed && KNOWN_SNAPSHOT_SOURCES.has(parsed.source.toLowerCase())) {
    return `数据源降级：${parsed.source}（${summarizeScreeningDiagnostic(parsed.detail)}）`;
  }
  return truncateMessageDetail(value);
};

const getScreenMessages = (meta: ScreeningScreenResponse | null) => {
  if (!meta) {
    return [];
  }
  const messages: string[] = [];
  const seen = new Set<string>();
  [...toMessageList(meta.warnings), ...toMessageList(meta.sourceErrors), ...toMessageList(meta.llmParseErrors)].forEach(
    (value) => {
      const key = normalizeScreenMessageKey(value);
      if (seen.has(key)) {
        return;
      }
      const message = formatScreenMessage(value);
      if (!message) {
        return;
      }
      seen.add(key);
      messages.push(message);
    },
  );
  return messages;
};

const isRunningScreenTask = (status: string | undefined | null) => status === 'pending' || status === 'processing';

const formatScreenTaskFailure = (value: string | null | undefined) => {
  const text = String(value || '').trim();
  if (!text) {
    return '选股任务失败，请稍后重试。';
  }
  return `选股任务失败：${summarizeScreeningDiagnostic(text)}`;
};

const SCREENING_HOTSPOT_NO_CACHE_HINT = 'No cached Screening hotspot snapshot. Click refresh to fetch live hotspots.';
const SCREENING_HOTSPOT_UNAVAILABLE_CODE = 'eastmoney_hotspot_unavailable';

const formatHotspotEmptyMessage = (result: ScreeningHotspotsResponse) => {
  const message = String(result.message || '').trim();
  const sourceErrors = result.sourceErrors || [];
  if (message && sourceErrors.includes(SCREENING_HOTSPOT_UNAVAILABLE_CODE)) {
    return message;
  }
  if (message === SCREENING_HOTSPOT_NO_CACHE_HINT) {
    return '暂无热点缓存';
  }
  const sourceError = sourceErrors[0];
  if (sourceError) {
    return `热点题材暂未返回数据：${summarizeScreeningDiagnostic(sourceError)}`;
  }
  return '热点题材暂未返回数据';
};

const ScreenAlertMessage: React.FC<{ messages: string[] }> = ({ messages }) => {
  if (messages.length <= 1) {
    return <span>{messages[0]}</span>;
  }
  return (
    <ul className="list-disc space-y-1 pl-4">
      {messages.map((message) => (
        <li key={message}>{message}</li>
      ))}
    </ul>
  );
};

const hasLlmInsight = (item: ScreeningCandidate) =>
  Boolean(
    item.llmThesis ||
      item.llmSector ||
      item.llmTheme ||
      item.llmConfidence != null ||
      item.llmWatchItems?.length ||
      item.llmCatalysts?.length,
  );

const getRiskClassName = (riskLevel: string | undefined) => {
  if (riskLevel === 'high') {
    return 'bg-danger/10 text-danger';
  }
  if (riskLevel === 'medium') {
    return 'bg-warning/10 text-warning';
  }
  if (riskLevel === 'low') {
    return 'bg-success/10 text-success';
  }
  return 'bg-surface text-secondary-text';
};

const getRiskLabel = (riskLevel: string | undefined) => {
  if (riskLevel === 'high') return '高';
  if (riskLevel === 'medium') return '中';
  if (riskLevel === 'low') return '低';
  return '待评估';
};

const getRouteTimeLabel = (item: ScreeningHotspotDetail['route'][number]) => {
  const rawTime = item.publishedAt || item.date || item.time || '';
  if (!rawTime) {
    return '时间待确认';
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(rawTime)) {
    return rawTime;
  }
  const parsed = new Date(rawTime);
  if (!Number.isNaN(parsed.getTime())) {
    return parsed.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  }
  return rawTime;
};

const formatHotspotRouteTitle = (value: string) => {
  const text = String(value || '').trim();
  const normalized = text.toLowerCase();
  if (normalized === 'current fermentation') {
    return '当前发酵';
  }
  if (normalized === 'news catalyst') {
    return '消息催化';
  }
  return text || '热点变化';
};

const formatHotspotRouteDescription = (value: string) => {
  const text = String(value || '').trim();
  if (!text) {
    return '暂无更多说明。';
  }
  const parts = text.split(/\s*;\s*/).map((part) => part.trim()).filter(Boolean);
  if (parts.some((part) => /\b(?:heat|stage|leaders?)\b/i.test(part))) {
    const localized = parts.map((part) => {
      const heat = part.match(/^(.*?)\s+heat\s+(-?\d+(?:\.\d+)?)$/i);
      if (heat) {
        return `${heat[1]}热度 ${formatNumber(heat[2], 1)}`;
      }
      const stage = part.match(/^stage\s+(.+)$/i);
      if (stage) {
        return `阶段 ${getHotspotStageLabel(stage[1])}`;
      }
      const leaders = part.match(/^leaders?\s+(.+)$/i);
      if (leaders) {
        return `核心股 ${leaders[1].split(/\s*,\s*/).filter(Boolean).join('、')}`;
      }
      return part;
    });
    return `${localized.join('，')}。`;
  }
  if (/Dsa[A-Z]|Provider\b|stock_board_|concept_constituents|leader_stocks|last_good_cache/i.test(text)) {
    return '热点数据出现新变化。';
  }
  return text;
};

const getHotspotMissingFieldLabels = (values: string[] | undefined) => {
  const labels = (values || []).map((value) => HOTSPOT_MISSING_FIELD_LABELS[String(value).trim().toLowerCase()] || '部分明细');
  return [...new Set(labels)];
};

const formatHotspotDiagnostic = (value: string) => {
  const text = String(value || '').trim();
  const timeoutSeconds = text.match(/timed out after\s*(\d+(?:\.\d+)?)s/i);
  if (timeoutSeconds) {
    return `热点明细请求超时（${timeoutSeconds[1]} 秒）`;
  }
  if (/timeout|timed out/i.test(text)) {
    return '热点明细请求超时';
  }
  if (/RemoteDisconnected|Connection aborted|ProtocolError|ConnectionPool|Max retries exceeded|ProxyError|NameResolutionError/i.test(text)) {
    return '热点数据源连接中断';
  }
  if (/eastmoney_hotspot_unavailable|returned no data|no live hotspot rows|\bempty\b/i.test(text)) {
    return '热点数据源暂未返回数据';
  }
  if (/rate limit|too many requests|http\s*429/i.test(text)) {
    return '热点数据请求过于频繁';
  }
  if (/Dsa[A-Z]|Provider\b|stock_board_|concept_constituents|leader_stocks|last_good_cache|^[a-z0-9_.:-]+$/i.test(text)) {
    return '部分热点数据暂不可用';
  }
  if (/[\u0080-\uFFFF]/.test(text)) {
    return truncateMessageDetail(text);
  }
  return '部分热点数据暂不可用';
};

const getHotspotDiagnosticMessages = (values: string[] | undefined) =>
  [...new Set((values || []).map(formatHotspotDiagnostic).filter(Boolean))].slice(0, 4);

const hasHotspotDetailDegradation = (detail: ScreeningHotspotDetail) => {
  if ((detail.missingFields || []).length > 0) {
    return true;
  }
  const qualityStatus = String(detail.qualityStatus || '').trim().toLowerCase();
  if (qualityStatus) {
    return qualityStatus !== 'available';
  }
  return (detail.sourceErrors || []).length > 0;
};

const getHotspotFallbackLabel = (detail: ScreeningHotspotDetail) => {
  if (detail.stale || detail.cacheUsed) {
    return detail.staleAgeHours != null
      ? `缓存回退 ${formatNumber(detail.staleAgeHours, 1)}h`
      : '缓存回退';
  }
  return '备用数据源';
};

const getHotspotSummaryText = (detail: ScreeningHotspotDetail, hotspot?: ScreeningHotspot) => {
  const summaryDetail = detail.summaryDetail || {};
  const heatScore = summaryDetail.heatScore ?? summaryDetail.heat_score ?? hotspot?.heatScore;
  const stage = summaryDetail.stage ?? hotspot?.stage ?? hotspot?.state;
  const rawLeaders = summaryDetail.leaders ?? hotspot?.leaders;
  const leaders = Array.isArray(rawLeaders)
    ? rawLeaders.map((value) => String(value).trim()).filter(Boolean).slice(0, 3)
    : [];
  const parts: string[] = [];
  if (heatScore != null && !Number.isNaN(Number(heatScore))) {
    parts.push(`热度 ${formatNumber(heatScore, 1)}`);
  }
  if (stage) {
    parts.push(`阶段 ${getHotspotStageLabel(stage)}`);
  }
  if (leaders.length > 0) {
    parts.push(`核心股 ${leaders.join('、')}`);
  }
  if (parts.length > 0) {
    return `${detail.name || detail.canonicalTopic || detail.topic}：${parts.join('，')}。`;
  }
  const summary = String(detail.summary || '').trim();
  if (summary && !/\b(?:heat|stage|leaders?|quality status|available|partial|stale|failed)\b|Dsa[A-Z]|Provider\b|stock_board_/i.test(summary)) {
    return summary;
  }
  return '已加载热点详情。';
};

const buildHotspotPreviewDetail = (hotspot: ScreeningHotspot): ScreeningHotspotDetail => {
  const leaders = (hotspot.leaders || []).map((value) => String(value).trim()).filter(Boolean);
  const stage = getHotspotStageLabel(hotspot.stage || hotspot.state);
  const descriptionParts = [`${hotspot.name || hotspot.topic}热度 ${formatHotspotMetric(hotspot.heatScore)}`];
  if (stage) {
    descriptionParts.push(`阶段 ${stage}`);
  }
  if (leaders.length > 0) {
    descriptionParts.push(`核心股 ${leaders.slice(0, 3).join('、')}`);
  }
  const stocks = (hotspot.leaderStocks || []).slice(0, 10);
  return {
    enabled: true,
    provider: 'akshare',
    topic: hotspot.topic,
    name: hotspot.name || hotspot.topic,
    canonicalTopic: hotspot.topic,
    summaryDetail: {
      heatScore: hotspot.heatScore,
      stage: hotspot.stage || hotspot.state,
      leaders,
    },
    route: [{ title: '当前发酵', description: `${descriptionParts.join('，')}。` }],
    stocks,
    stockCount: hotspot.sampleStockCount ?? stocks.length,
    sourceErrors: hotspot.sourceErrors,
    qualityStatus: hotspot.qualityStatus,
    missingFields: hotspot.missingFields,
    fallbackUsed: hotspot.fallbackUsed,
    stale: hotspot.stale,
    staleAgeHours: hotspot.staleAgeHours,
    cacheUsed: hotspot.cacheUsed,
    cachedAt: hotspot.cachedAt,
  };
};

const stripHotspotSearchAugmentation = (detail: ScreeningHotspotDetail): ScreeningHotspotDetail => {
  const baseDetail: ScreeningHotspotDetail = {
    ...detail,
    route: (detail.route || []).filter((item) => !item.searchResult),
    ...(detail.timeline
      ? { timeline: detail.timeline.filter((item) => !item.searchResult) }
      : {}),
  };
  delete baseDetail.newsSearchRequested;
  delete baseDetail.newsSearchStatus;
  return baseDetail;
};

const stripHotspotSearchAugmentationByTopic = (
  details: Record<string, ScreeningHotspotDetail>,
) => Object.fromEntries(
  Object.entries(details).map(([topic, detail]) => [topic, stripHotspotSearchAugmentation(detail)]),
) as Record<string, ScreeningHotspotDetail>;

const getHotspotRouteItems = (detail: ScreeningHotspotDetail) => {
  const route = detail.route || [];
  if (route.length > 0) {
    return route;
  }
  return detail.timeline || [];
};

const formatHotspotMetric = (value: unknown, digits = 1) => {
  const formatted = formatNumber(value, digits);
  return formatted === '-' ? '观察中' : formatted;
};

const getHotspotLeadersText = (item: ScreeningHotspot) => {
  const leaders = (item.leaders || []).map((value) => String(value).trim()).filter(Boolean);
  if (leaders.length > 0) {
    return leaders.slice(0, 2).join('、');
  }
  return '观察中';
};

const getHotspotSampleText = (item: ScreeningHotspot) => {
  if (item.sampleStockCount == null || Number.isNaN(Number(item.sampleStockCount))) {
    return '活跃股观察中';
  }
  return `覆盖 ${item.sampleStockCount} 股`;
};

const formatStockChangeText = (value: unknown) => {
  const formatted = formatNumber(value);
  return formatted === '-' ? '暂无行情' : `${formatted}%`;
};

const formatHotspotUpdatedAt = (value: string | null) => {
  if (!value) {
    return '待刷新';
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
};

const getHotspotStrength = (item: ScreeningHotspot, index: number) => {
  const heat = Number(item.heatScore ?? 0);
  const changePct = Number(item.changePct ?? 0);
  if (index === 0 || heat >= 90 || changePct >= 8) {
    return { label: '强势领先', className: 'bg-red-500/10 text-red-500' };
  }
  if (heat >= 80 || changePct >= 5) {
    return { label: '强势', className: 'bg-blue-500/10 text-blue-500' };
  }
  return { label: '较强', className: 'bg-cyan/10 text-cyan' };
};

const HOTSPOT_ICON_RULES: Array<{
  pattern: RegExp;
  icon: React.ComponentType<{ className?: string }>;
  className: string;
}> = [
  { pattern: /金|银|铜|铝|铅|锌|钼|钴|镍|贵金属|矿|有色/, icon: Pickaxe, className: 'bg-orange-500/10 text-orange-500' },
  { pattern: /黄金|珠宝/, icon: Gem, className: 'bg-amber-500/10 text-amber-500' },
  { pattern: /油|气|能源|煤/, icon: Droplet, className: 'bg-yellow-700/10 text-yellow-700' },
  { pattern: /金融|券商|银行|保险|资本/, icon: Landmark, className: 'bg-orange-500/10 text-orange-500' },
  { pattern: /航空|机场|航天|运输/, icon: Plane, className: 'bg-blue-500/10 text-blue-500' },
  { pattern: /林业|农业|种植/, icon: Trees, className: 'bg-emerald-500/10 text-emerald-500' },
  { pattern: /医疗|诊断|卫生|医药/, icon: Stethoscope, className: 'bg-teal-500/10 text-teal-500' },
  { pattern: /食品|餐饮|酒/, icon: Utensils, className: 'bg-violet-500/10 text-violet-500' },
  { pattern: /工业|制造|修理|机械|设备/, icon: Wrench, className: 'bg-blue-500/10 text-blue-500' },
  { pattern: /租赁|地产|建筑/, icon: Building2, className: 'bg-emerald-500/10 text-emerald-500' },
  { pattern: /电|芯片|算力|AI|机器人/, icon: Factory, className: 'bg-indigo-500/10 text-indigo-500' },
  { pattern: /保险|安全/, icon: Shield, className: 'bg-blue-500/10 text-blue-500' },
];

const getHotspotIcon = (topic: string) => {
  const match = HOTSPOT_ICON_RULES.find((rule) => rule.pattern.test(topic));
  return match || { icon: Activity, className: 'bg-cyan/10 text-cyan' };
};

const MiniSparkline: React.FC<{ score?: number | null; selected?: boolean }> = ({ score, selected }) => {
  const normalizedScore = Number.isFinite(Number(score)) ? Math.max(0, Math.min(100, Number(score))) : 65;
  const lift = Math.max(0, Math.min(16, normalizedScore / 7));
  const path = `M2 35 C12 ${32 - lift / 4}, 16 ${34 - lift / 2}, 24 ${28 - lift / 3} S38 ${29 - lift}, 46 ${23 - lift / 2} S62 ${24 - lift}, 72 ${16 - lift / 3} S86 ${15 - lift}, 94 ${7}`;
  return (
    <svg className="h-8 w-20" viewBox="0 0 96 40" aria-hidden="true">
      <path d={`${path} L94 40 L2 40 Z`} fill={selected ? 'rgba(249,115,22,0.14)' : 'rgba(59,130,246,0.12)'} />
      <path d={path} fill="none" stroke={selected ? '#f97316' : '#3b82f6'} strokeLinecap="round" strokeWidth="2" />
    </svg>
  );
};

const StockScreeningPage: React.FC = () => {
  const navigate = useNavigate();
  const { t } = useUiLanguage();
  const [restoredTask] = useState<PersistedScreenTask | null>(() => readPersistedScreenTask());
  const [enabled, setEnabled] = useState(false);
  const [available, setAvailable] = useState(false);
  const [market, setMarket] = useState(restoredTask?.market || 'cn');
  const [strategy, setStrategy] = useState(restoredTask?.strategy || 'dual_low');
  const [strategies, setStrategies] = useState<ScreeningStrategy[]>([]);
  const [maxResults, setMaxResults] = useState(restoredTask?.maxResults || 3);
  const [candidates, setCandidates] = useState<ScreeningCandidate[]>([]);
  const [hotspots, setHotspots] = useState<ScreeningHotspot[]>([]);
  const [hotspotsUpdatedAt, setHotspotsUpdatedAt] = useState<string | null>(null);
  const [hotspotsExpanded, setHotspotsExpanded] = useState(false);
  const [selectedHotspotTopic, setSelectedHotspotTopic] = useState<string | null>(null);
  const selectedHotspotTopicRef = useRef<string | null>(null);
  const hotspotDetailRequestIdRef = useRef(0);
  const hotspotDetailsByTopicRef = useRef<Record<string, ScreeningHotspotDetail>>({});
  const historyRunRequestIdRef = useRef(0);
  const [hotspotDetail, setHotspotDetail] = useState<ScreeningHotspotDetail | null>(null);
  const [loadingHotspotDetail, setLoadingHotspotDetail] = useState(false);
  const [searchingHotspotNews, setSearchingHotspotNews] = useState(false);
  const [hotspotDetailError, setHotspotDetailError] = useState('');
  const [loadingHotspots, setLoadingHotspots] = useState(false);
  const [hotspotError, setHotspotError] = useState('');
  const [screenMeta, setScreenMeta] = useState<ScreeningScreenResponse | null>(null);
  const [expandedCode, setExpandedCode] = useState<string | null>(null);
  const [loading, setLoading] = useState(Boolean(restoredTask?.taskId));
  const [enabling, setEnabling] = useState(false);
  const [historyRuns, setHistoryRuns] = useState<ScreeningRunSummary[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState('');
  const [tracking, setTracking] = useState<ScreeningTrackingResponse | null>(null);
  const [trackingLoading, setTrackingLoading] = useState(false);
  const [trackingError, setTrackingError] = useState('');
  const [restoreResolved, setRestoreResolved] = useState(() => !restoredTask?.runId);
  // 标记当前 strategy 是否来自历史 run（刷新自动恢复或手动历史选择）的上下文同步。
  // 为 true 时 loadStrategies 跳过“不在列表则回退第一项”的归一化，
  // 防止迟到的 /strategies 响应把历史上下文改写回默认策略。
  const historyContextStrategyRef = useRef(false);
  const [loadingStrategies, setLoadingStrategies] = useState(false);
  const [error, setError] = useState('');
  const [strategyLoadError, setStrategyLoadError] = useState('');
  const [activeTaskId, setActiveTaskId] = useState<string | null>(restoredTask?.taskId ?? null);
  const [taskProgress, setTaskProgress] = useState(restoredTask?.taskId ? 10 : 0);
  const [taskMessage, setTaskMessage] = useState(restoredTask?.taskId ? '正在恢复选股任务状态...' : '');

  const selectedStrategy = useMemo(() => strategies.find((item) => item.id === strategy), [strategies, strategy]);
  const selectedStrategyTitle = selectedStrategy?.name || selectedStrategy?.title || '自定义策略';
  const selectedStrategyTag = formatStrategyCategory(
    selectedStrategy?.category || selectedStrategy?.tag || selectedStrategy?.tags?.[0],
  );
  const displayedStrategy = selectedStrategy ? selectedStrategyTitle : `自定义策略 (${strategy})`;
  const screenMessages = useMemo(() => getScreenMessages(screenMeta), [screenMeta]);
  const selectedHotspot = useMemo(
    () => hotspots.find((item) => item.topic === selectedHotspotTopic),
    [hotspots, selectedHotspotTopic],
  );
  const factorRanking = Boolean(screenMeta && (screenMeta.rankingMode === 'factor' || screenMeta.llmRanked === false));
  const llmFailed = Boolean(factorRanking && screenMeta?.llmFailureReason);
  const alertMessages = llmFailed
    ? screenMessages.length > 0
      ? screenMessages
      : ['智能重排未完成，当前候选继续使用确定性因子评分。']
    : screenMessages;
  const isScreeningEnabled = enabled && available;
  // 状态文案也要过 t()：它同时出现在页头状态胶囊和"未开启"提示里，
  // 两处必须同源，否则双语切换时会一处中文一处英文。
  const statusText = isScreeningEnabled
    ? t('screeningPage.status.enabled')
    : t('screeningPage.status.disabled');

  // 复盘区块的展示文案（原因码 → 本地化）。后端只返回机器可读的原因码，
  // 映射集中在 screeningTrackingDisplay，便于用测试钉住契约。
  const trackingReasonLabel = (reason: string | null | undefined): string => t(trackingReasonKey(reason));
  const trackingBenchmarkLabel = (reason: string | null | undefined): string =>
    t(trackingBenchmarkReasonKey(reason));
  const trackingBenchmarkIndexName = (code: string | null | undefined): string => {
    const key = benchmarkIndexLabelKey(code);
    return key ? t(key) : String(code || '').trim();
  };
  // 基准"不可用"的两种来源要分开：
  //  - 功能被关掉（codesConfigured 为空）→ 这是配置问题，即使本地有指数行也不算；
  //  - 探测说没有可用序列 → 用后端给的原因码。
  // 后端 benchmark.available 的语义是"本地有指数序列"，保持不变；这里只是展示层判断。
  const trackingBenchmarkUnavailableReason = tracking
    ? tracking.benchmark.codesConfigured.length === 0
      ? 'benchmark_disabled'
      : tracking.benchmark.available
        ? null
        : tracking.benchmark.reason
    : null;
  const trackingFreshnessText = tracking
    ? tracking.dataFreshness.stockDailyLatestDate
      ? t('screening.tracking.freshness', { date: tracking.dataFreshness.stockDailyLatestDate })
      : t('screening.tracking.freshness.none')
    : '';

  const applyScreenResult = useCallback((result: ScreeningScreenResponse) => {
    const nextCandidates = result.candidates || [];
    setScreenMeta(result);
    setCandidates(nextCandidates);
    setExpandedCode(nextCandidates[0]?.code ?? null);
  }, []);

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    setHistoryError('');
    try {
      const result = await screeningApi.getHistory({ limit: 10 });
      setHistoryRuns(result.runs || []);
    } catch (err) {
      setHistoryError(toApiErrorMessage(err, '历史记录加载失败'));
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  const loadTracking = useCallback(async () => {
    setTrackingLoading(true);
    setTrackingError('');
    try {
      const result = await screeningApi.getTracking({ days: TRACKING_WINDOW_DAYS, limit: 200 });
      setTracking(result);
    } catch (err) {
      setTrackingError(toApiErrorMessage(err, ''));
    } finally {
      setTrackingLoading(false);
    }
  }, []);

  const handleHistoryRunSelect = useCallback(async (runId: string) => {
    // 竞态防护：快速切换历史条目时，只应用最新一次请求的响应
    const requestId = historyRunRequestIdRef.current + 1;
    historyRunRequestIdRef.current = requestId;
    const isCurrentRequest = () => historyRunRequestIdRef.current === requestId;
    // 与运行中的选股任务互斥：手动选择历史记录后，暂停/取消后台任务轮询，
    // 避免任务完成后把当前任务的候选结果回写到历史上下文中。
    setActiveTaskId(null);
    setHistoryError('');
    setLoading(true);
    try {
      const detail = await screeningApi.getRun(runId);
      if (!isCurrentRequest()) {
        return;
      }
      if (detail?.result) {
        applyScreenResult(detail.result);
        historyContextStrategyRef.current = true;
        // 同步持久化恢复指针：刷新后应恢复用户刚选中的历史 run，
        // 而不是停留在更早的 task/run。历史详情不携带 taskId，以 runId
        // 作为占位——正常路径刷新走 getRun(runId) 恢复、不会触发轮询；
        // 若该 run 恢复失败，占位轮询会命中不可恢复错误并清理过期指针。
        persistScreenTask({
          taskId: runId,
          runId,
          market: detail.market || market,
          strategy: detail.strategy || strategy,
          maxResults,
        });
        // 同步历史 run 的策略与市场上下文，确保结果区文案和后续深度分析
        // 使用该历史 run 对应的 strategy/market，而不是当前表单的选择
        if (detail.strategy) {
          setStrategy(detail.strategy);
        }
        if (detail.market) {
          setMarket(detail.market);
        }
        setError('');
        setTaskProgress(100);
        setTaskMessage('已加载历史选股结果');
      } else {
        setError('历史记录中未找到该次运行的结果。');
      }
    } catch (err) {
      if (!isCurrentRequest()) {
        return;
      }
      setError(toApiErrorMessage(err, '历史结果加载失败'));
    } finally {
      if (isCurrentRequest()) {
        setLoading(false);
      }
    }
  }, [applyScreenResult, market, maxResults, strategy]);

  const clearScreeningResults = () => {
    setCandidates([]);
    setScreenMeta(null);
    setExpandedCode(null);
  };

  const loadHotspotDetail = useCallback(async (
    topic: string,
    options: { refresh?: boolean; includeSearch?: boolean } = {},
  ) => {
    if (!topic) {
      return;
    }
    const cachedDetail = !options.refresh && !options.includeSearch
      ? hotspotDetailsByTopicRef.current[topic]
      : null;
    if (cachedDetail) {
      setHotspotDetail(cachedDetail);
      setHotspotDetailError('');
      setLoadingHotspotDetail(false);
      return;
    }
    const requestId = hotspotDetailRequestIdRef.current + 1;
    hotspotDetailRequestIdRef.current = requestId;
    const isCurrentRequest = () => hotspotDetailRequestIdRef.current === requestId;
    const canApplyRequest = () => isCurrentRequest() && selectedHotspotTopicRef.current === topic;
    setLoadingHotspotDetail(!options.includeSearch);
    setSearchingHotspotNews(Boolean(options.includeSearch));
    setHotspotDetail((currentDetail) => (currentDetail?.topic === topic ? currentDetail : null));
    setHotspotDetailError('');
    try {
      const detail = await screeningApi.getHotspotDetail({
        topic,
        provider: 'akshare',
        refresh: options.refresh ?? false,
        ...(options.includeSearch ? { includeSearch: true } : {}),
      });
      if (!canApplyRequest()) {
        return;
      }
      const cacheableDetail = options.includeSearch
        ? hotspotDetailsByTopicRef.current[topic] || stripHotspotSearchAugmentation(detail)
        : stripHotspotSearchAugmentation(detail);
      hotspotDetailsByTopicRef.current = {
        ...hotspotDetailsByTopicRef.current,
        [topic]: cacheableDetail,
      };
      setHotspotDetail(options.includeSearch ? detail : cacheableDetail);
      if (options.includeSearch && detail.newsSearchStatus === 'no_results') {
        setHotspotDetailError('暂未搜到该题材近期的有效消息。');
      } else if (options.includeSearch && detail.newsSearchStatus !== 'available') {
        setHotspotDetailError('消息搜索失败，请稍后重试。');
      }
    } catch (err) {
      if (!canApplyRequest()) {
        return;
      }
      if (!options.includeSearch) {
        setHotspotDetail(null);
      }
      setHotspotDetailError(toApiErrorMessage(
        err,
        options.includeSearch ? '消息搜索失败，请稍后重试。' : '热点题材详情加载失败，请稍后重试。',
      ));
    } finally {
      if (isCurrentRequest()) {
        setLoadingHotspotDetail(false);
        setSearchingHotspotNews(false);
      }
    }
  }, []);

  const loadStrategies = useCallback(async () => {
    setLoadingStrategies(true);
    try {
      setStrategyLoadError('');
      const result = await screeningApi.getStrategies();
      const loadedStrategies = result.strategies || [];
      setStrategies(loadedStrategies);
      // 历史 run 的自定义/下线策略不在当前列表属预期行为，不做“回退第一项”的归一化，
      // 否则迟到的策略列表会把刚恢复好的上下文改写回默认策略。
      if (loadedStrategies.length > 0 && !historyContextStrategyRef.current) {
        setStrategy((currentStrategy) =>
          loadedStrategies.some((item) => item.id === currentStrategy) ? currentStrategy : loadedStrategies[0].id,
        );
      }
    } catch (err) {
      setStrategies([]);
      setStrategyLoadError(err instanceof Error ? err.message : '策略列表加载失败');
    } finally {
      setLoadingStrategies(false);
    }
  }, []);

  const loadHotspots = useCallback(async (refresh = false) => {
    setLoadingHotspots(true);
    setHotspotError('');
    try {
      const result = await screeningApi.getHotspots({ provider: 'akshare', top: 12, refresh });
      const nextHotspots = result.hotspots || [];
      const nextDetails = stripHotspotSearchAugmentationByTopic(result.details || {});
      hotspotDetailsByTopicRef.current = {
        ...hotspotDetailsByTopicRef.current,
        ...nextDetails,
      };
      const currentTopic = selectedHotspotTopicRef.current;
      const retainedTopic = Boolean(currentTopic && nextHotspots.some((item) => item.topic === currentTopic));
      const nextTopic = retainedTopic ? currentTopic : null;
      setHotspots(nextHotspots);
      setHotspotsUpdatedAt(result.cachedAt || (nextHotspots.length > 0 ? new Date().toISOString() : null));
      setSelectedHotspotTopic(nextTopic);
      selectedHotspotTopicRef.current = nextTopic;
      setHotspotDetailError('');
      if (nextTopic && nextDetails[nextTopic]) {
        setHotspotDetail(nextDetails[nextTopic]);
        setLoadingHotspotDetail(false);
      } else if (!retainedTopic) {
        setHotspotDetail(null);
      } else if (refresh && nextTopic) {
        // A refreshed list and a retained detail must describe the same source
        // snapshot. The list endpoint intentionally omits details by default,
        // so explicitly bypass the detail cache for the retained topic.
        await loadHotspotDetail(nextTopic, { refresh: true });
      }
      if (nextHotspots.length === 0) {
        setHotspotError(formatHotspotEmptyMessage(result));
      }
    } catch (err) {
      setHotspotError(toApiErrorMessage(err, '热点题材加载失败，请稍后重试。'));
    } finally {
      setLoadingHotspots(false);
    }
  }, [loadHotspotDetail]);

  const handleHotspotSelect = useCallback((topic: string) => {
    selectedHotspotTopicRef.current = topic;
    setSelectedHotspotTopic(topic);
    const cachedDetail = hotspotDetailsByTopicRef.current[topic];
    if (cachedDetail) {
      setHotspotDetail(cachedDetail);
      setHotspotDetailError('');
      setLoadingHotspotDetail(false);
    } else {
      const preview = hotspots.find((item) => item.topic === topic);
      setHotspotDetail((currentDetail) => (
        currentDetail?.topic === topic ? currentDetail : preview ? buildHotspotPreviewDetail(preview) : null
      ));
    }
  }, [hotspots]);

  const toggleHotspotsExpanded = useCallback(() => {
    setHotspotsExpanded((expanded) => {
      const nextExpanded = !expanded;
      if (!nextExpanded) {
        selectedHotspotTopicRef.current = null;
        setSelectedHotspotTopic(null);
        setHotspotDetail(null);
        setHotspotDetailError('');
      }
      return nextExpanded;
    });
  }, []);

  const handleAnalyzeHotspotStock = useCallback((stock: ScreeningHotspotDetail['stocks'][number]) => {
    const stockCode = String(stock.code || '').trim();
    if (!stockCode) {
      return;
    }
    const stockName = String(stock.name || stockCode).trim();
    navigate('/', {
      state: {
        stockCode,
        stockName,
        autoAnalyze: true,
        selectionSource: 'screening_hotspot',
        skills: ['hot_theme'],
      },
    });
  }, [navigate]);

  const handleAnalyzeCandidate = useCallback((candidate: ScreeningCandidate) => {
    const stockCode = String(candidate.code || '').trim();
    if (!stockCode) {
      return;
    }
    const stockName = String(candidate.name || stockCode).trim();
    const analysisSkills = (selectedStrategy?.analysisSkills || []).filter(Boolean);
    navigate('/', {
      state: {
        stockCode,
        stockName,
        autoAnalyze: true,
        selectionSource: 'screening_result',
        ...(analysisSkills.length > 0 ? { skills: analysisSkills } : {}),
      },
    });
  }, [navigate, selectedStrategy]);

  useEffect(() => {
    selectedHotspotTopicRef.current = selectedHotspotTopic;
  }, [selectedHotspotTopic]);

  useEffect(() => {
    if (!selectedHotspotTopic) {
      return;
    }
    void loadHotspotDetail(selectedHotspotTopic);
  }, [loadHotspotDetail, selectedHotspotTopic]);

  useEffect(() => {
    let active = true;
    screeningApi
      .getStatus()
      .then((status) => {
        if (!active) {
          return;
        }
        setEnabled(status.enabled);
        setAvailable(status.available);
        if (status.enabled && status.available) {
          void loadStrategies();
          void loadHotspots(false);
          void loadHistory();
          void loadTracking();
        }
      })
      .catch(() => {
        if (active) {
          setEnabled(false);
          setAvailable(false);
        }
      });
    return () => {
      active = false;
    };
  }, [loadHotspots, loadStrategies, loadTracking]);

  // 刷新后优先从 history API 按 run_id 恢复结果；恢复失败再回退到 task 轮询
  useEffect(() => {
    const runId = restoredTask?.runId;
    if (!runId) {
      setRestoreResolved(true);
      return undefined;
    }
    let active = true;
    setLoading(true);
    // 记录自动恢复的请求基准：若在自动恢复返回前用户手动点开了历史记录
    // （historyRunRequestIdRef 被 handleHistoryRunSelect 递增），则放弃本次自动恢复响应，
    // 避免较晚返回的自动恢复把页面切回旧 run，覆盖用户最新一次的历史选择。
    const restoreRequestBase = historyRunRequestIdRef.current;
    screeningApi
      .getRun(runId)
      .then((detail) => {
        if (!active || historyRunRequestIdRef.current !== restoreRequestBase) {
          return;
        }
        if (detail?.result) {
          applyScreenResult(detail.result);
          historyContextStrategyRef.current = true;
          // 同步恢复该历史 run 的策略与市场上下文，避免结果区展示和
          // 深度分析沿用当前表单策略（与 handleHistoryRunSelect 一致）
          if (detail.strategy) {
            setStrategy(detail.strategy);
          }
          if (detail.market) {
            setMarket(detail.market);
          }
          setError('');
          setTaskProgress(100);
          setTaskMessage('已从历史记录恢复上次选股结果');
          setActiveTaskId(null);
        }
      })
      .catch(() => {
        // 历史记录恢复失败（run 不存在或服务重启），回退到 task 轮询；
        // 若用户已手动选择了历史记录，则不再回退，保持用户的选择。
        if (active && historyRunRequestIdRef.current === restoreRequestBase) {
          setActiveTaskId(restoredTask?.taskId ?? null);
        }
      })
      .finally(() => {
        // 无论结果是否过期都要解除自动恢复门闩，否则新任务的轮询会被阻塞到旧请求超时。
        if (active) {
          setRestoreResolved(true);
        }
        // 过期的自动恢复不得触碰共享 loading：用户手动选择的历史详情请求可能仍在飞行，
        // 提前清掉会重新放开“运行选股”入口，随后迟到的历史响应会覆盖新任务状态。
        if (active && historyRunRequestIdRef.current === restoreRequestBase) {
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [applyScreenResult, restoredTask]);

  useEffect(() => {
    if (!activeTaskId || !restoreResolved) {
      return undefined;
    }

    const pollingTaskId = activeTaskId;
    let active = true;
    let timer: ReturnType<typeof window.setTimeout> | undefined;

    function finishTask() {
      setActiveTaskId(null);
      setLoading(false);
    }

    function applyTaskStatus(task: ScreeningScreenTaskStatus) {
      const nextProgress = Number(task.progress ?? 0);
      setTaskProgress(Number.isFinite(nextProgress) ? nextProgress : 0);
      setTaskMessage(task.message || '');

      if (task.status === 'completed') {
        if (task.result) {
          applyScreenResult(task.result);
          setError('');
          // 持久化 runId：刷新后优先从 history API 恢复结果，而非依赖内存 task
          const completedRunId = task.result.runId || screenMeta?.runId;
          if (completedRunId) {
            persistScreenTask({
              taskId: pollingTaskId,
              runId: completedRunId,
              market,
              strategy,
              maxResults,
            });
          }
        } else {
          setError('选股任务已完成，但服务端未返回候选结果。');
          setCandidates([]);
          setScreenMeta(null);
        }
        finishTask();
        return;
      }

      if (task.status === 'failed') {
        setCandidates([]);
        setScreenMeta(null);
        setExpandedCode(null);
        setError(formatScreenTaskFailure(task.error || task.message));
        clearPersistedScreenTask();
        finishTask();
        return;
      }

      if (isRunningScreenTask(task.status)) {
        setLoading(true);
        timer = window.setTimeout(pollTask, SCREEN_TASK_POLL_INTERVAL_MS);
        return;
      }

      setError(`选股任务返回未知状态：${task.status || 'unknown'}`);
      clearPersistedScreenTask();
      finishTask();
    }

    async function pollTask() {
      try {
        const task = await screeningApi.getScreenTask(pollingTaskId);
        if (!active) {
          return;
        }
        applyTaskStatus(task);
      } catch (err) {
        if (!active) {
          return;
        }
        const parsedError = getParsedApiError(err);
        if (isUnrecoverableScreenTaskError(parsedError)) {
          setError(formatParsedApiError(parsedError) || '选股任务不可恢复，请重新提交。');
          setCandidates([]);
          setScreenMeta(null);
          clearPersistedScreenTask();
          finishTask();
          return;
        }
        setError(formatRecoverableScreenTaskPollingError(parsedError));
        setLoading(true);
        timer = window.setTimeout(pollTask, SCREEN_TASK_POLL_INTERVAL_MS);
      }
    }

    void pollTask();

    return () => {
      active = false;
      if (timer) {
        window.clearTimeout(timer);
      }
    };
  }, [activeTaskId, applyScreenResult, restoreResolved]);

  const handleEnable = async () => {
    setEnabling(true);
    setError('');
    try {
      await screeningApi.enable();
      setEnabled(true);
      setAvailable(true);
      await loadStrategies();
    } catch (err) {
      try {
        const status = await screeningApi.getStatus();
        setEnabled(status.enabled);
        setAvailable(status.available);
      } catch {
        setEnabled(false);
        setAvailable(false);
      }
      setError(err instanceof Error ? err.message : '开启选股失败');
    } finally {
      setEnabling(false);
    }
  };

  const handleStrategyChange = (nextStrategy: string) => {
    if (nextStrategy !== strategy) {
      clearScreeningResults();
    }
    setStrategy(nextStrategy);
  };

  const handleMarketChange = (nextMarket: string) => {
    if (nextMarket !== market) {
      clearScreeningResults();
    }
    setMarket(nextMarket);
  };

  const handleMaxResultsChange = (nextMaxResults: number) => {
    if (nextMaxResults !== maxResults) {
      clearScreeningResults();
    }
    setMaxResults(nextMaxResults);
  };

  const handleSubmit = async () => {
    // 新任务提交即代表用户放弃当前历史/恢复上下文：
    // 递增请求代号作废飞行中的历史详情与自动恢复响应；
    // 解除自动恢复门闩，保证刚提交的任务轮询立即可启动。
    historyRunRequestIdRef.current += 1;
    setRestoreResolved(true);
    setLoading(true);
    setError('');
    setScreenMeta(null);
    setTaskProgress(0);
    setTaskMessage('正在提交选股任务...');
    try {
      const task = await screeningApi.startScreen({ market, strategy, maxResults });
      persistScreenTask({
        taskId: task.taskId,
        market,
        strategy,
        maxResults,
      });
      setActiveTaskId(task.taskId);
      setTaskProgress(0);
      setTaskMessage(task.message || '选股任务已提交');
    } catch (err) {
      setCandidates([]);
      setLoading(false);
      setError(toApiErrorMessage(err, '选股任务提交失败，请稍后重试。'));
    }
  };

  return (
    <PlayfulPage className="max-w-6xl space-y-6 pb-12 pt-6">
      {/* ---------- 页头：暖色问候条，与首页同一套 hero 卡 ---------- */}
      <PlayfulCard variant="hero" padding="none" className="flex flex-wrap items-center gap-4 px-5 py-4">
        <PlayfulMascot className="h-[72px] w-[104px] shrink-0" />
        <div className="min-w-[12rem] flex-1">
          <h1 className="text-2xl font-extrabold tracking-tight text-foreground">
            {t('screeningPage.page.title')}
          </h1>
          <p className="mt-1 text-xs font-semibold playful-ink-soft">{t('screeningPage.page.subtitle')}</p>
        </div>
        <Badge
          variant={isScreeningEnabled ? 'success' : 'warning'}
          size="md"
          className="shrink-0"
        >
          {statusText}
        </Badge>
      </PlayfulCard>

      {!enabled ? (
        <InlineAlert
          variant="info"
          title={t('screeningPage.disabled.title')}
          message={t('screeningPage.disabled.message')}
          action={
            <Button
              size="sm"
              isLoading={enabling}
              loadingText={t('screeningPage.enable.actionLoading')}
              onClick={() => void handleEnable()}
            >
              {t('screeningPage.enable.action')}
            </Button>
          }
        />
      ) : null}

      {enabled && !available ? (
        <InlineAlert
          variant="warning"
          title={t('screeningPage.unavailable.title')}
          message={t('screeningPage.unavailable.message')}
        />
      ) : null}

      {error ? <InlineAlert variant="danger" title={t('screeningPage.error.title')} message={error} /> : null}

      <PlayfulCard as="section" padding="md">
        <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          {/* 热点是"温度"语义，用暖色火苗做区块标记，不占用语义色 */}
          <PlayfulSectionTitle emoji="🔥" as="h2" title={t('screeningPage.hotspot.title')} />
          <div className="flex flex-col items-start gap-2 lg:items-end">
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="secondary"
                disabled={!isScreeningEnabled}
                onClick={toggleHotspotsExpanded}
              >
                <Bookmark className="h-4 w-4" />
                {hotspotsExpanded ? '收起热点题材' : `展开热点题材${hotspots.length ? `（${hotspots.length}）` : ''}`}
                <ChevronDown className={`h-4 w-4 transition-transform ${hotspotsExpanded ? 'rotate-180' : ''}`} />
              </Button>
              {hotspotsExpanded ? (
              <Button
                size="sm"
                variant="secondary"
                isLoading={loadingHotspots}
                loadingText="刷新中..."
                disabled={!isScreeningEnabled || loadingHotspots}
                onClick={() => void loadHotspots(true)}
              >
                <RefreshCw className="h-4 w-4" />
                刷新热点题材
              </Button>
              ) : null}
            </div>
            {hotspotsUpdatedAt ? (
              <p className="text-xs text-secondary-text">更新于 {formatHotspotUpdatedAt(hotspotsUpdatedAt)}</p>
            ) : null}
          </div>
        </div>

        {hotspotsExpanded && hotspotError ? (
          <p className="mb-3 rounded-xl border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
            {hotspotError}
          </p>
        ) : null}

        {!hotspotsExpanded ? null : hotspots.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border bg-surface/70 px-4 py-6 text-sm text-secondary-text">
            暂无热点数据，点击“刷新热点题材”获取。
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
            {hotspots.map((item, index) => {
              const selected = selectedHotspotTopic === item.topic;
              const strength = getHotspotStrength(item, index);
              const iconMeta = getHotspotIcon(item.name || item.topic);
              const Icon = iconMeta.icon;
              return (
              <button
                key={`${item.topic}-${item.rank ?? ''}`}
                className={`group relative min-h-[116px] overflow-hidden rounded-xl border px-3 py-3 text-left transition-all ${
                  selected
                    ? 'border-orange-400 bg-gradient-to-br from-orange-500/10 via-card to-card shadow-[0_0_0_1px_rgba(249,115,22,0.16),0_18px_44px_rgba(249,115,22,0.14)]'
                    : 'border-border/80 bg-card hover:-translate-y-0.5 hover:border-orange-300/70 hover:shadow-soft-card'
                }`}
                type="button"
                onClick={() => handleHotspotSelect(item.topic)}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex min-w-0 items-start gap-3">
                    <span
                      className={`grid h-6 w-6 shrink-0 place-items-center rounded-full text-xs font-bold ${
                        index < 3 ? 'bg-orange-500 text-white shadow-[0_8px_24px_rgba(249,115,22,0.24)]' : 'bg-surface text-secondary-text'
                      }`}
                    >
                      {index + 1}
                    </span>
                    <span className={`grid h-9 w-9 shrink-0 place-items-center rounded-full ${iconMeta.className}`}>
                      <Icon className="h-5 w-5" />
                    </span>
                    <div className="min-w-0">
                      <p className="truncate text-sm font-bold text-foreground">{item.name || item.topic}</p>
                      <span className={`mt-1 inline-flex rounded-md px-1.5 py-0.5 text-[11px] font-semibold ${strength.className}`}>
                        {strength.label}
                      </span>
                    </div>
                  </div>
                  <span className="shrink-0 text-2xl font-black leading-none text-orange-500">
                    {formatNumber(item.heatScore, 0)}
                  </span>
                </div>
                <div className="mt-4 grid max-w-[72%] gap-1 text-[11px] text-secondary-text">
                  <span>涨跌幅 <strong className="font-semibold text-foreground">{formatHotspotMetric(item.changePct)}%</strong></span>
                  <span>趋势 <strong className="font-semibold text-foreground">{formatHotspotMetric(item.trendScore)}</strong> · 持续 <strong className="font-semibold text-foreground">{formatHotspotMetric(item.persistenceScore)}</strong></span>
                  <span>{getHotspotSampleText(item)} · 龙头 {getHotspotLeadersText(item)}</span>
                </div>
                <div className="absolute bottom-3 right-3 opacity-95 transition-transform group-hover:scale-105">
                  <MiniSparkline score={item.heatScore} selected={selected} />
                </div>
              </button>
              );
            })}
          </div>
        )}

        {hotspotsExpanded && selectedHotspotTopic ? (
          <div className="mt-4 rounded-xl border border-border/80 bg-surface/80 p-4">
            <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h3 className="text-sm font-semibold text-foreground">
                  {hotspotDetail?.name || selectedHotspotTopic}
                </h3>
                <p className="mt-1 text-xs leading-5 text-secondary-text">
                  {hotspotDetail
                    ? getHotspotSummaryText(hotspotDetail, selectedHotspot)
                    : loadingHotspotDetail
                      ? '正在读取发酵路线与概念股...'
                      : '点击题材查看发酵路线与概念股。'}
                </p>
                {hotspotDetail?.canonicalTopic && hotspotDetail.canonicalTopic !== selectedHotspotTopic ? (
                  <p className="mt-1 text-[11px] text-secondary-text">标准题材：{hotspotDetail.canonicalTopic}</p>
                ) : null}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  variant="secondary"
                  isLoading={searchingHotspotNews}
                  loadingText="搜索中..."
                  disabled={loadingHotspotDetail || searchingHotspotNews}
                  onClick={() => void loadHotspotDetail(selectedHotspotTopic, { includeSearch: true })}
                >
                  <Search className="h-3.5 w-3.5" />
                  搜索最新消息
                </Button>
                {loadingHotspotDetail ? (
                  <span className="w-fit rounded-full bg-cyan/10 px-3 py-1 text-xs font-semibold text-cyan">
                    正在补充详情
                  </span>
                ) : null}
                {hotspotDetail?.qualityStatus ? (
                  <span className="w-fit rounded-full bg-warning/10 px-3 py-1 text-xs font-semibold text-warning">
                    质量 {getHotspotQualityLabel(hotspotDetail.qualityStatus)}
                  </span>
                ) : null}
                {hotspotDetail?.fallbackUsed || hotspotDetail?.stale ? (
                  <span className="w-fit rounded-full bg-warning/10 px-3 py-1 text-xs font-semibold text-warning">
                    {getHotspotFallbackLabel(hotspotDetail)}
                  </span>
                ) : null}
                {hotspotDetail?.stockCount != null ? (
                  <span className="w-fit rounded-full bg-orange-500/10 px-3 py-1 text-xs font-semibold text-orange-500">
                    概念股 {hotspotDetail.stockCount}
                  </span>
                ) : null}
              </div>
            </div>

            {hotspotDetailError ? (
              <p className="mb-3 rounded-xl border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
                {hotspotDetailError}
              </p>
            ) : null}

            {hotspotDetail && hasHotspotDetailDegradation(hotspotDetail) ? (
              <details className="mb-3 rounded-xl border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
                <summary className="cursor-pointer font-semibold">详情数据已降级，展开查看原因</summary>
                <div className="mt-2 space-y-1 leading-5">
                  {(hotspotDetail.missingFields || []).length > 0 ? (
                    <p>暂缺：{getHotspotMissingFieldLabels(hotspotDetail.missingFields).join('、')}</p>
                  ) : null}
                  {getHotspotDiagnosticMessages(hotspotDetail.sourceErrors).map((message, index) => (
                    <p key={`${message}-${index}`}>{message}</p>
                  ))}
                </div>
              </details>
            ) : null}

            {hotspotDetail ? (
              <div className="grid gap-4 lg:grid-cols-[1fr_1.3fr]">
                <div>
                  <p className="mb-3 flex items-center gap-1.5 text-xs font-semibold text-secondary-text">
                    <Clock3 className="h-3.5 w-3.5 text-orange-500" />
                    发酵时间线
                  </p>
                  <div className="relative space-y-0 pl-4 before:absolute before:bottom-3 before:left-[5px] before:top-2 before:w-px before:bg-border">
                    {getHotspotRouteItems(hotspotDetail).map((item, index) => (
                      <div key={`${item.title}-${index}`} className="relative pb-4 last:pb-0">
                        <span className="absolute -left-4 top-1 h-2.5 w-2.5 rounded-full border border-orange-400 bg-card" />
                        <div className="rounded-lg border border-border/70 bg-card/80 p-3">
                          <p className="text-[11px] font-semibold text-orange-500">{getRouteTimeLabel(item)}</p>
                          <p className="mt-1 text-xs font-semibold text-foreground">{formatHotspotRouteTitle(item.title)}</p>
                          <p className="mt-1 text-xs leading-5 text-secondary-text">{formatHotspotRouteDescription(item.description)}</p>
                          {item.url ? (
                            <a
                              className="mt-2 inline-flex text-[11px] font-semibold text-cyan hover:text-foreground"
                              href={item.url}
                              target="_blank"
                              rel="noreferrer"
                            >
                              查看消息
                            </a>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
                <div>
                  <p className="mb-2 text-xs font-semibold text-secondary-text">概念股</p>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {(hotspotDetail.stocks || []).slice(0, 10).map((stock) => (
                      <div key={`${stock.code || stock.name}`} className="rounded-lg border border-border/70 bg-card/80 p-3">
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0">
                            <p className="truncate text-xs font-semibold text-foreground">{stock.name || stock.code || '-'}</p>
                            <p className="mt-1 text-[11px] text-secondary-text">{stock.code || '-'}</p>
                          </div>
                          <div className="flex shrink-0 items-center gap-1">
                            <span className="rounded-full bg-cyan/10 px-2 py-1 text-[11px] font-semibold text-cyan">
                              {getHotspotRoleLabel(stock.role)}
                            </span>
                            {stock.code ? (
                              <button
                                type="button"
                                aria-label={`分析 ${stock.name || stock.code}`}
                                className="inline-flex h-7 items-center gap-1 rounded-full border border-cyan/30 bg-cyan/10 px-2 text-[11px] font-semibold text-cyan transition-colors hover:border-cyan hover:bg-cyan/15 hover:text-foreground"
                                onClick={() => handleAnalyzeHotspotStock(stock)}
                              >
                                <Play className="h-3 w-3" />
                                分析
                              </button>
                            ) : null}
                          </div>
                        </div>
                        <p className="mt-2 text-[11px] text-secondary-text">
                          涨跌幅 {formatStockChangeText(stock.changePct)} · 热度 {formatNumber(stock.hotStockScore, 0)}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
      </PlayfulCard>

      <PlayfulCard as="section" padding="md">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <PlayfulSectionTitle
            icon={<SlidersHorizontal className="h-4 w-4" />}
            title={t('screeningPage.form.title')}
          />
          <Badge variant="info">{selectedStrategyTag}</Badge>
        </div>

        <div className="grid gap-4 lg:grid-cols-[1fr_1.2fr_180px_auto] lg:items-end">
          <label className="space-y-2 text-xs font-medium text-secondary-text">
            {t('screeningPage.form.market')}
            <select
              className="h-11 w-full rounded-xl border border-[var(--playful-border)] bg-surface px-3 text-sm text-foreground outline-none transition-colors focus:border-cyan"
              value={market}
              disabled={loading}
              onChange={(event) => handleMarketChange(event.target.value)}
            >
              {MARKETS.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>

          <div className="space-y-2 text-xs font-medium text-secondary-text">
            <label htmlFor="screening-strategy">{t('screeningPage.form.strategy')}</label>
            <Select
              id="screening-strategy"
              value={selectedStrategy ? strategy : CUSTOM_STRATEGY_OPTION_VALUE}
              disabled={loading || loadingStrategies}
              placeholder=""
              options={[
                ...strategies.map((item) => ({
                  value: item.id,
                  label: item.name || item.title || item.id,
                })),
                { value: CUSTOM_STRATEGY_OPTION_VALUE, label: t('screeningPage.form.strategyCustomOption') },
              ]}
              onChange={(value) =>
                handleStrategyChange(
                  value === CUSTOM_STRATEGY_OPTION_VALUE ? '' : value,
                )
              }
            />
          </div>

          {!selectedStrategy && !loadingStrategies ? (
            <label className="space-y-2 text-xs font-medium text-secondary-text lg:col-start-2">
              {t('screeningPage.form.strategyCustomId')}
              <input
                aria-label={t('screeningPage.form.strategyCustomId')}
                className="h-11 w-full rounded-xl border border-[var(--playful-border)] bg-surface px-3 text-sm text-foreground outline-none transition-colors focus:border-cyan"
                value={strategy}
                disabled={loading}
                placeholder={t('screeningPage.form.strategyCustomIdPlaceholder')}
                onChange={(event) => handleStrategyChange(event.target.value)}
              />
            </label>
          ) : null}

          <label className="space-y-2 text-xs font-medium text-secondary-text">
            {t('screeningPage.form.maxResults')}
            <input
              className="h-11 w-full rounded-xl border border-[var(--playful-border)] bg-surface px-3 text-sm text-foreground outline-none transition-colors focus:border-cyan"
              type="number"
              min={1}
              max={100}
              value={maxResults}
              disabled={loading}
              onChange={(event) => handleMaxResultsChange(Number(event.target.value))}
            />
          </label>

          <Button
            className="h-11 min-w-40"
            isLoading={loading}
            loadingText={t('screeningPage.form.submitting')}
            disabled={!isScreeningEnabled || loading || !strategy.trim()}
            onClick={() => void handleSubmit()}
          >
            <Play className="h-4 w-4" />
            {t('screeningPage.form.submit')}
          </Button>
        </div>

        {/* 策略说明条沿用 playful 的暖黄便签：它是补充说明，不该抢表单的注意力 */}
        <div className="playful-note mt-3 rounded-xl px-3 py-2 text-xs leading-5">
          {strategyLoadError
            ? strategyLoadError
            : selectedStrategy?.description || t('screeningPage.form.descriptionFallback')}
        </div>
      </PlayfulCard>

      {loading || screenMeta ? (
        <PlayfulCard as="section" padding="md">
          <div className="flex items-center gap-3">
            <span className={`grid h-9 w-9 shrink-0 place-items-center rounded-full ${loading ? 'bg-[var(--playful-tint-b)] text-[var(--playful-grape)]' : 'bg-success/10 text-success'}`}>
              {loading ? <CircleAlert className="h-5 w-5" /> : <CheckCircle2 className="h-5 w-5" />}
            </span>
            <div>
              <h2 className="text-sm font-bold text-foreground">
                {loading ? t('screeningPage.progress.running') : t('screeningPage.progress.done')}
              </h2>
              <p className="mt-1 text-xs playful-ink-soft">
                {loading
                  ? `${taskMessage || t('screeningPage.progress.message')} · ${taskProgress}%`
                  : `${displayedStrategy} · ${MARKETS.find((item) => item.id === market)?.label}`}
              </p>
            </div>
          </div>

          {/* 运行时画真实进度（后端 progress 字段），不做假动画 */}
          {loading ? (
            <PlayfulTrack
              as="div"
              size="sm"
              tone="grape"
              className="mt-3 w-full"
              value={taskProgress}
            />
          ) : null}

          <details className="mt-3 border-t border-dashed playful-dashed pt-3 text-xs text-secondary-text">
            <summary className="w-fit cursor-pointer font-medium text-secondary-text">
              {t('screeningPage.result.details')}
            </summary>
            <div className="mt-2 grid gap-1">
              <span>{t('screeningPage.result.task', { id: activeTaskId ? activeTaskId.slice(0, 12) : '-' })}</span>
              <span>{t('screeningPage.result.runId', { id: screenMeta?.runId || '-' })}</span>
              <span>
                {t('screeningPage.result.counts', {
                  snapshot: screenMeta?.snapshotCount ?? '-',
                  afterFilter: screenMeta?.afterFilterCount ?? '-',
                  candidates: screenMeta?.candidateCount ?? candidates.length,
                })}
              </span>
              <span>
                {t('screeningPage.result.ranking', {
                  mode: screenMeta?.llmRanked
                    ? t('screeningPage.result.rankingLlm')
                    : screenMeta
                      ? t('screeningPage.result.rankingFactor')
                      : '-',
                })}
                {screenMeta?.llmModelUsed ? ` · ${screenMeta.llmModelUsed}` : ''}
                {screenMeta?.llmCoverage != null
                  ? ` · ${t('screeningPage.result.coverage', { coverage: formatPercent(screenMeta.llmCoverage) })}`
                  : ''}
              </span>
              {screenMeta?.resultVariantPoolSize ? (
                <span>
                  {screenMeta.resultVariantApplied
                    ? t('screeningPage.result.variantApplied', {
                        pool: screenMeta.resultVariantPoolSize,
                        rotated: screenMeta.resultVariantRotatedSlots ?? 0,
                      })
                    : t('screeningPage.result.variantKept', { pool: screenMeta.resultVariantPoolSize })}
                </span>
              ) : null}
              <span>
                {t('screeningPage.result.enrichment', {
                  enriched: screenMeta?.dsaEnrichment?.enrichedCount ?? '-',
                  requested: screenMeta?.dsaEnrichment?.requestedCount ?? '-',
                })}
              </span>
            </div>
          </details>
        </PlayfulCard>
      ) : null}

      {screenMeta && alertMessages.length > 0 ? (
        <InlineAlert
          variant={llmFailed ? 'warning' : 'info'}
          title={llmFailed ? '当前使用因子排序' : '选股提示'}
          message={<ScreenAlertMessage messages={alertMessages} />}
        />
      ) : null}

      {screenMeta ? (
        <PlayfulCard as="section" padding="md">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
            <PlayfulSectionTitle emoji="📋" as="h2" title={t('screeningPage.result.title')} />
            <Badge variant="default">
              <Search className="h-3.5 w-3.5" aria-hidden="true" />
              <span className="tabular-nums">{t('screeningPage.result.count', { count: candidates.length })}</span>
            </Badge>
          </div>

        {candidates.length === 0 ? (
          <PlayfulEmptyState
            icon={<Search className="h-6 w-6" />}
            title={t('screeningPage.result.empty')}
          />
        ) : (
          <div className="overflow-hidden rounded-2xl border border-[var(--playful-border)]">
            <table className="w-full min-w-[860px] border-collapse text-sm">
              <thead className="bg-surface text-left text-xs text-secondary-text">
                <tr>
                  <th className="w-14 px-4 py-3 font-semibold">#</th>
                  <th className="px-4 py-3 font-semibold">代码</th>
                  <th className="px-4 py-3 font-semibold">名称</th>
                  <th className="px-4 py-3 font-semibold">行业</th>
                  <th className="px-4 py-3 font-semibold">价格</th>
                  <th className="px-4 py-3 font-semibold">涨跌幅</th>
                  <th className="px-4 py-3 font-semibold">评分</th>
                  <th className="px-4 py-3 font-semibold">排序依据</th>
                  <th className="px-4 py-3 font-semibold">风险</th>
                  <th className="px-4 py-3 font-semibold">详情</th>
                </tr>
              </thead>
              <tbody>
                {candidates.map((item) => {
                  const expanded = expandedCode === item.code;
                  const factors = getFactorEntries(item);
                  const llmInsightAvailable = hasLlmInsight(item);
                  const dsaWarnings = item.dsaContext?.warnings || [];
                  const dsaNews = item.dsaNews || [];
                  const dsaEvents = item.dsaEvents || [];
                  return (
                    <Fragment key={`${item.rank}-${item.code}`}>
                      <tr className="border-t border-border align-top transition-colors hover:bg-hover/50">
                        <td className="px-4 py-3 text-secondary-text">{item.rank}</td>
                        <td className="px-4 py-3 font-mono font-semibold text-foreground">{item.code}</td>
                        <td className="px-4 py-3 font-semibold text-foreground">{item.name || '-'}</td>
                        <td className="px-4 py-3 text-secondary-text">{item.industry || '-'}</td>
                        <td className="px-4 py-3 text-secondary-text">{formatNumber(item.price)}</td>
                        <td className="px-4 py-3 text-secondary-text">{formatNumber(item.changePct)}%</td>
                        <td className="px-4 py-3 font-bold text-cyan">{formatScore(item.score)}</td>
                        <td className="px-4 py-3 text-secondary-text">{factorRanking ? '因子排序' : formatScore(item.llmScore)}</td>
                        <td className="px-4 py-3">
                          <span className={`rounded-lg px-2.5 py-1 text-xs font-semibold ${getRiskClassName(item.riskLevel)}`}>
                            {getRiskLabel(item.riskLevel)}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <button
                            className="text-sm font-semibold text-cyan transition-colors hover:text-foreground"
                            type="button"
                            onClick={() => setExpandedCode(expanded ? null : item.code)}
                          >
                            {expanded ? '收起' : '展开查看'}
                          </button>
                        </td>
                      </tr>
                      {expanded ? (
                        <tr className="border-t border-border bg-surface/45">
                          <td colSpan={10} className="px-4 py-4">
                            <div className="grid gap-4 lg:grid-cols-[1.1fr_1fr]">
                              <div className="space-y-3">
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">摘要</p>
                                  <p className="mt-1 text-sm leading-6 text-foreground">{getCandidateReason(item)}</p>
                                </div>
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">操作信号</p>
                                  <p className="mt-1 text-sm text-foreground">{getSignal(item)}</p>
                                  <button
                                    className="mt-2 rounded-lg border border-cyan/40 px-3 py-1.5 text-xs font-semibold text-cyan transition-colors hover:bg-cyan/10"
                                    type="button"
                                    onClick={() => handleAnalyzeCandidate(item)}
                                  >
                                    进一步深度分析
                                  </button>
                                </div>
                                {item.dsaAnalysisSummary ? (
                                  <div>
                                    <p className="text-xs font-semibold text-secondary-text">增强摘要</p>
                                    <p className="mt-1 text-sm leading-6 text-foreground">
                                      {formatEnrichmentSummary(item.dsaAnalysisSummary)}
                                    </p>
                                  </div>
                                ) : null}
                                {llmInsightAvailable ? (
                                  <div>
                                    <p className="text-xs font-semibold text-secondary-text">智能判断</p>
                                    <p className="mt-1 text-sm leading-6 text-foreground">{item.llmThesis || item.reason}</p>
                                    <p className="mt-1 text-xs text-secondary-text">
                                      板块 {item.llmSector || '-'} · 主题 {item.llmTheme || '-'} · 置信度 {formatPercent(item.llmConfidence)}
                                    </p>
                                  </div>
                                ) : null}
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">风险标签</p>
                                  <p className="mt-1 text-sm text-foreground">
                                    {[...(item.riskFlags || []), ...(item.llmRisks || [])].length
                                      ? [...(item.riskFlags || []), ...(item.llmRisks || [])].join('，')
                                      : '无'}
                                  </p>
                                </div>
                              </div>
                              <div className="space-y-3">
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">主要因子</p>
                                  <div className="mt-2 grid grid-cols-2 gap-2">
                                    {factors.length > 0 ? (
                                      factors.map(([key, value]) => (
                                        <div key={key} className="rounded-lg border border-border bg-card px-3 py-2">
                                          <span className="block text-xs text-secondary-text">{FACTOR_LABELS[key] || key}</span>
                                          <span className="text-sm font-semibold text-foreground">{formatNumber(value)}</span>
                                        </div>
                                      ))
                                    ) : (
                                      <span className="text-sm text-secondary-text">无因子明细</span>
                                    )}
                                  </div>
                                </div>
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">成交额</p>
                                  <p className="mt-1 text-sm text-foreground">{formatAmount(item.amount)}</p>
                                </div>
                                {item.llmWatchItems?.length ? (
                                  <div>
                                    <p className="text-xs font-semibold text-secondary-text">智能关注项</p>
                                    <p className="mt-1 text-sm text-foreground">{item.llmWatchItems.join('，')}</p>
                                  </div>
                                ) : null}
                                {item.llmCatalysts?.length ? (
                                  <div>
                                    <p className="text-xs font-semibold text-secondary-text">催化因素</p>
                                    <p className="mt-1 text-sm text-foreground">{item.llmCatalysts.join('，')}</p>
                                  </div>
                                ) : null}
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">相关新闻</p>
                                  {dsaNews.length > 0 ? (
                                    <ul className="mt-1 space-y-1 text-sm text-foreground">
                                      {dsaNews.slice(0, 3).map((newsItem, newsIndex) => (
                                        <li key={`${item.code}-dsa-news-${newsIndex}`}>
                                          {newsItem.title || newsItem.snippet || '-'}
                                        </li>
                                      ))}
                                    </ul>
                                  ) : (
                                    <p className="mt-1 text-sm text-secondary-text">无</p>
                                  )}
                                </div>
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">公告与事件</p>
                                  {dsaEvents.length > 0 ? (
                                    <ul className="mt-1 space-y-1 text-sm text-foreground">
                                      {dsaEvents.slice(0, 3).map((eventItem, eventIndex) => (
                                        <li key={`${item.code}-dsa-event-${eventIndex}`}>
                                          {eventItem.title || eventItem.snippet || '-'}
                                        </li>
                                      ))}
                                    </ul>
                                  ) : (
                                    <p className="mt-1 text-sm text-secondary-text">无</p>
                                  )}
                                </div>
                                {dsaWarnings.length > 0 ? (
                                  <div>
                                    <p className="text-xs font-semibold text-secondary-text">数据补充提示</p>
                                    <p className="mt-1 text-sm text-secondary-text">{dsaWarnings.join('，')}</p>
                                  </div>
                                ) : null}
                              </div>
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        </PlayfulCard>
      ) : null}

      <PlayfulCard as="section" padding="md">
        <PlayfulSectionTitle
          emoji="📈"
          as="h2"
          title={t('screening.tracking.title')}
          meta={
            <button
              type="button"
              className="font-semibold text-[var(--playful-grape)] transition-colors hover:text-foreground disabled:opacity-60"
              onClick={() => void loadTracking()}
              disabled={trackingLoading}
            >
              {trackingLoading ? t('common.loading') : t('screening.tracking.refresh')}
            </button>
          }
        />
        <p className="mt-1 text-xs leading-5 playful-ink-soft">
          {t('screening.tracking.subtitle', { days: tracking?.windowDays ?? TRACKING_WINDOW_DAYS })}
          {tracking ? ` · ${trackingFreshnessText}` : ''}
          {tracking && tracking.runWindow.malformedRuns > 0
            ? ` · ${t('screening.tracking.malformedRuns', { count: tracking.runWindow.malformedRuns })}`
            : ''}
        </p>
        {trackingError ? (
          <p className="mt-2 text-xs text-danger">
            {t('screening.tracking.loadError')}
            {trackingError ? `: ${trackingError}` : ''}
          </p>
        ) : null}
        {!tracking && trackingLoading ? (
          <p className="py-3 text-center text-xs playful-ink-soft">{t('screening.tracking.loading')}</p>
        ) : null}
        {!tracking && !trackingLoading && !trackingError ? (
          <p className="py-3 text-center text-xs playful-ink-soft">{t('screening.tracking.empty')}</p>
        ) : null}
        {tracking ? (
          <>
            {/* 七个指标固定一行（窄屏 2 列 / 中屏 4 列 / 宽屏 7 列）：
                用 wrap 会把"跑赢基准"挤到第二行，看起来像漏了一项 */}
            <PlayfulStatGrid className="mt-3 gap-2 md:grid-cols-4 xl:grid-cols-7">
              <PlayfulStat
                size="sm"
                tone="ink"
                label={t('screening.tracking.metric.picks')}
                value={tracking.total.picks}
              />
              <PlayfulStat
                size="sm"
                tone="mint"
                label={t('screening.tracking.metric.trackable')}
                value={tracking.total.trackable}
              />
              <PlayfulStat
                size="sm"
                tone="sun"
                label={t('screening.tracking.metric.untrackable')}
                value={tracking.total.untrackable}
              />
              <PlayfulStat
                size="sm"
                tone={tracking.total.hitRate == null ? 'soft' : 'grape'}
                label={t('screening.tracking.metric.hitRate')}
                value={tracking.total.hitRate == null
                  ? t('screening.tracking.noTrackableValue')
                  : formatTrackingPercent(tracking.total.hitRate * 100, 0)}
              />
              <PlayfulStat
                size="sm"
                tone={getTrackingReturnTone(tracking.total.avgReturnPct)}
                label={t('screening.tracking.metric.avgReturn')}
                value={tracking.total.avgReturnPct == null
                  ? t('screening.tracking.noTrackableValue')
                  : formatTrackingPercent(tracking.total.avgReturnPct)}
              />
              {/* 相对基准的超额：这才是"选股有没有用"的答案。分母为 0 时显式说明，
                  不显示成 +0.00%（那会被读成"与大盘持平"）。 */}
              <PlayfulStat
                size="sm"
                tone={getTrackingReturnTone(tracking.total.avgExcessReturnPct)}
                label={t('screening.tracking.metric.avgExcess')}
                value={tracking.total.avgExcessReturnPct == null
                  ? t('screening.tracking.noTrackableValue')
                  : formatSignedPercent(tracking.total.avgExcessReturnPct)}
              />
              <PlayfulStat
                size="sm"
                tone={tracking.total.beatBenchmarkRate == null ? 'soft' : 'grape'}
                label={t('screening.tracking.metric.beatRate')}
                value={tracking.total.beatBenchmarkRate == null
                  ? t('screening.tracking.noTrackableValue')
                  : formatRatePercent(tracking.total.beatBenchmarkRate, 0)}
              />
            </PlayfulStatGrid>

            {/* 命中率口径常驻可见：分母只含可复盘样本，缺前瞻 K 线的入选既不入分母、
                也不会被当成 0% —— 这条说明不能藏进提示里，否则数字会被误读 */}
            <p className="mt-2 text-[11px] font-semibold leading-5 playful-ink-soft">
              {t('screeningPage.tracking.hitRateBasis', {
                trackable: tracking.total.trackable,
                picks: tracking.total.picks,
              })}
            </p>

            {tracking.total.untrackable > 0 ? (
              <InlineAlert
                className="mt-3"
                variant={tracking.total.trackable === 0 ? 'warning' : 'info'}
                title={tracking.total.trackable === 0 ? t('screening.tracking.untrackableNotice.title') : undefined}
                message={
                  tracking.total.trackable === 0
                    ? t('screening.tracking.untrackableNotice.message', {
                        total: tracking.total.picks,
                        untrackable: tracking.total.untrackable,
                      })
                    : t('screening.tracking.partialNotice.message', {
                        trackable: tracking.total.trackable,
                        untrackable: tracking.total.untrackable,
                      })
                }
              />
            ) : null}

            {tracking.untrackableReasons.length > 0 ? (
              <div className="mt-3 flex flex-wrap items-center gap-1.5">
                <span className="text-[11px] font-semibold playful-ink-soft">
                  {t('screeningPage.tracking.untrackableReasons')}
                </span>
                {tracking.untrackableReasons.map((item) => (
                  <Badge key={item.reason} variant="warning">
                    {trackingReasonLabel(item.reason)} <span className="tabular-nums">{item.count}</span>
                  </Badge>
                ))}
              </div>
            ) : null}

            {tracking.strategies.length > 0 ? (
              <div className="mt-4">
                <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                  <p className="text-xs font-bold text-foreground">
                    {t('screening.tracking.strategyTable.title')}
                  </p>
                  <p className="text-[11px] font-semibold playful-ink-soft">
                    {t('screeningPage.tracking.strategyHint')}
                  </p>
                </div>
                <PlayfulList>
                  {tracking.strategies.map((row) => {
                    const hitRate = row.hitRate;
                    return (
                      <PlayfulListItem
                        key={row.strategy}
                        density="roomy"
                        className="flex-col items-stretch gap-2"
                      >
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                          <span className="min-w-0 flex-1 truncate text-xs font-bold text-foreground">
                            {formatHistoryStrategyName(row.strategy, strategies)}
                          </span>
                          <span className="inline-flex items-baseline gap-1 text-[11px] font-semibold playful-ink-soft">
                            {t('screening.tracking.metric.picks')}
                            <b className="text-xs font-extrabold tabular-nums text-foreground">{row.picks}</b>
                          </span>
                          <span className="inline-flex items-baseline gap-1 text-[11px] font-semibold playful-ink-soft">
                            {t('screening.tracking.metric.trackable')}
                            <b className="text-xs font-extrabold tabular-nums text-foreground">{row.trackable}</b>
                          </span>
                          {/* 没有可复盘样本时命中率必须显示"样本不足"，绝不能拿 0 顶替 */}
                          <span className="inline-flex items-baseline gap-1 text-[11px] font-semibold playful-ink-soft">
                            {t('screening.tracking.metric.hitRate')}
                            <b className={`text-xs font-extrabold tabular-nums ${hitRate == null ? 'text-secondary-text' : 'text-[var(--playful-grape)]'}`}>
                              {hitRate == null
                                ? t('screening.tracking.noTrackableValue')
                                : formatTrackingPercent(hitRate * 100, 0)}
                            </b>
                          </span>
                          <span className="inline-flex items-baseline gap-1 text-[11px] font-semibold playful-ink-soft">
                            {t('screening.tracking.metric.avgReturn')}
                            <b className={`text-xs font-extrabold tabular-nums ${getTrackingReturnClassName(row.avgReturnPct)}`}>
                              {row.avgReturnPct == null ? '-' : formatTrackingPercent(row.avgReturnPct)}
                            </b>
                          </span>
                          <span className="inline-flex items-baseline gap-1 text-[11px] font-semibold playful-ink-soft">
                            {t('screening.tracking.metric.avgExcess')}
                            <b className={`text-xs font-extrabold tabular-nums ${getTrackingReturnClassName(row.avgExcessReturnPct)}`}>
                              {row.avgExcessReturnPct == null ? '-' : formatSignedPercent(row.avgExcessReturnPct)}
                            </b>
                          </span>
                          {/* 跑赢基准恒带分母：口径可见，不能只给一个比率 */}
                          <span className="inline-flex items-baseline gap-1 text-[11px] font-semibold playful-ink-soft">
                            {t('screening.tracking.metric.beatRate')}
                            <b className="text-xs font-extrabold tabular-nums text-foreground">
                              {row.beatBenchmarkRate == null
                                ? '-'
                                : `${formatRatePercent(row.beatBenchmarkRate, 0)} (${row.beatBenchmarkDenominator})`}
                            </b>
                          </span>
                        </div>
                        {/* 条长 = 命中率（0~100%），只在真有可复盘样本时才画：
                            空条形会被读成"0% 命中"，所以样本不足时干脆不画。
                            颜色固定用 grape 点缀色 —— 命中率不是涨跌，不借用红绿语义。 */}
                        {hitRate == null ? null : (
                          <PlayfulTrack
                            as="div"
                            size="sm"
                            tone="grape"
                            className="w-full"
                            value={Math.max(0, Math.min(100, hitRate * 100))}
                          />
                        )}
                      </PlayfulListItem>
                    );
                  })}
                </PlayfulList>
              </div>
            ) : null}

            {tracking.picks.length > 0 ? (
              <div className="mt-4">
                <p className="mb-2 text-xs font-bold text-foreground">
                  {t('screening.tracking.picksTable.title')}
                </p>
                <div className="max-h-80 overflow-y-auto rounded-2xl border border-[var(--playful-border)]">
                  <table className="w-full min-w-[840px] text-left text-xs">
                    <thead className="sticky top-0 bg-card/95 text-secondary-text">
                      <tr>
                        <th className="py-2 pl-3 pr-3 font-medium">{t('screening.tracking.column.name')}</th>
                        <th className="py-2 pr-3 font-medium">{t('screening.tracking.column.pickDate')}</th>
                        <th className="py-2 pr-3 font-medium">{t('screening.tracking.column.pickPrice')}</th>
                        <th className="py-2 pr-3 font-medium">{t('screening.tracking.column.latestClose')}</th>
                        <th className="py-2 pr-3 font-medium">{t('screening.tracking.column.return')}</th>
                        <th className="py-2 pr-3 font-medium">{t('screening.tracking.column.excess')}</th>
                        <th className="py-2 pr-3 font-medium">{t('screening.tracking.column.maxDrawdown')}</th>
                        <th className="py-2 pr-3 font-medium">{t('screening.tracking.column.daysHeld')}</th>
                        <th className="py-2 pr-3 font-medium">{t('screening.tracking.column.status')}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/60">
                      {tracking.picks.map((pick) => (
                        <tr key={`${pick.runId}-${pick.code}`} className="transition-colors hover:bg-hover/40">
                          <td className="py-2 pl-3 pr-3">
                            <span className="font-medium text-foreground">{pick.name || pick.code}</span>
                            <span className="ml-1 text-secondary-text">{pick.code}</span>
                          </td>
                          <td className="py-2 pr-3 text-secondary-text">{pick.pickDate}</td>
                          <td className="py-2 pr-3 text-secondary-text">
                            {pick.pickPrice == null ? '-' : pick.pickPrice.toFixed(2)}
                          </td>
                          <td className="py-2 pr-3 text-secondary-text">
                            {pick.latestClose == null ? '-' : pick.latestClose.toFixed(2)}
                          </td>
                          {/* 不可复盘的入选一律给 '-'，绝不用 0% 冒充"持平" */}
                          <td className={`py-2 pr-3 font-medium ${getTrackingReturnClassName(pick.returnPct)}`}>
                            {pick.trackable ? formatTrackingPercent(pick.returnPct) : '-'}
                          </td>
                          {/* 有基准就显示超额；没有基准但可复盘就显示原因码，两者都没有才是 '-' */}
                          <td className={`py-2 pr-3 font-medium ${getTrackingReturnClassName(pick.excessReturnPct)}`}>
                            {pick.benchmarkAvailable
                              ? formatSignedPercent(pick.excessReturnPct)
                              : pick.trackable
                                ? trackingBenchmarkLabel(pick.benchmarkReason)
                                : '-'}
                          </td>
                          <td className={`py-2 pr-3 ${getTrackingReturnClassName(pick.maxDrawdownPct)}`}>
                            {pick.trackable ? formatTrackingPercent(pick.maxDrawdownPct) : '-'}
                          </td>
                          <td className="py-2 pr-3 text-secondary-text">
                            {pick.daysHeld == null ? '-' : pick.daysHeld}
                          </td>
                          <td className="py-2 pr-3">
                            {pick.trackable ? (
                              <Badge variant="success">{t('screening.tracking.status.trackable')}</Badge>
                            ) : (
                              // 不可复盘必须带上机器原因码对应的人话，而不是只画一个灰点；
                              // 语义色沿用既有 warning 令牌，不用中性 pill 掩盖状态
                              <Badge variant="warning">
                                {t('screening.tracking.status.untrackable')} · {trackingReasonLabel(pick.reason)}
                              </Badge>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : null}

            {trackingBenchmarkUnavailableReason ? (
              // 基准不可用时必须说清"为什么没有"，不能安静地不显示
              <p className="mt-3 rounded-xl border border-warning/30 bg-warning/10 px-3 py-2 text-xs leading-5 text-warning">
                {t('screening.tracking.benchmark.unavailable', {
                  reason: trackingBenchmarkLabel(trackingBenchmarkUnavailableReason),
                })}
              </p>
            ) : null}

            {!trackingBenchmarkUnavailableReason &&
            tracking.benchmark.comparablePicks === 0 ? (
              // 指数有了但一只能比的都没有：这是"有基准、无样本"，不是"跑平基准"。
              <p className="mt-3 rounded-xl border border-[var(--playful-border)] bg-elevated/40 px-3 py-2 text-xs leading-5 text-secondary-text">
                {t('screening.tracking.benchmark.noComparable', {
                  index: trackingBenchmarkIndexName(tracking.benchmark.code),
                })}
              </p>
            ) : null}

            {!trackingBenchmarkUnavailableReason &&
            tracking.benchmark.comparablePicks > 0 ? (
              // 基准条用 playful 的暖黄便签：它是"这批选股到底跑赢没有"的结论行，
              // 但要和涨跌红绿区分开，所以不借用 up/down 色。
              <div className="playful-note mt-3 rounded-2xl px-3 py-2">
                <p className="text-xs font-bold">
                  {t('screening.tracking.benchmark.summary', {
                    index: trackingBenchmarkIndexName(tracking.benchmark.code),
                    benchmarkReturn: formatSignedPercent(tracking.benchmark.avgBenchmarkReturnPct),
                    avgExcess: formatSignedPercent(tracking.benchmark.avgExcessReturnPct),
                    beatRate: formatRatePercent(tracking.benchmark.beatBenchmarkRate, 0),
                  })}
                </p>
                <p className="mt-1 text-[11px] leading-5">
                  {t('screening.tracking.benchmark.summaryBasis', {
                    denominator: tracking.benchmark.beatBenchmarkDenominator,
                  })}
                </p>
                <p className="mt-0.5 text-[11px] leading-5">
                  {t('screening.tracking.benchmark.windowBasis')}
                </p>
                {tracking.benchmark.unavailableReasons.length > 0 ? (
                  <p className="mt-0.5 text-[11px] leading-5">
                    {tracking.benchmark.unavailableReasons
                      .map((item) => `${trackingBenchmarkLabel(item.reason)} ${item.count}`)
                      .join(' · ')}
                  </p>
                ) : null}
              </div>
            ) : null}
          </>
        ) : null}
      </PlayfulCard>

      <PlayfulCard as="section" padding="md">
        <PlayfulSectionTitle
          emoji="🕒"
          as="h2"
          title={t('screeningPage.history.title')}
          meta={
            <button
              type="button"
              className="font-semibold text-[var(--playful-grape)] transition-colors hover:text-foreground disabled:opacity-60"
              onClick={() => void loadHistory()}
              disabled={historyLoading}
            >
              {historyLoading ? t('screeningPage.history.loadingAction') : t('screeningPage.history.refresh')}
            </button>
          }
        />
        {historyError ? (
          <p className="mt-2 text-xs text-danger">{historyError}</p>
        ) : null}
        {historyRuns.length === 0 ? (
          <PlayfulEmptyState
            className="mt-3"
            icon={<Clock3 className="h-6 w-6" />}
            title={historyLoading
              ? t('screeningPage.history.loading')
              : t('screeningPage.history.empty')}
          />
        ) : (
          <PlayfulList className="mt-2">
            {historyRuns.map((run) => (
              <PlayfulListItem key={run.runId} density="compact" className="p-0">
                <button
                  type="button"
                  className="flex w-full items-center justify-between gap-3 rounded-xl px-2 py-2.5 text-left transition-colors hover:bg-hover/40"
                  onClick={() => void handleHistoryRunSelect(run.runId)}
                >
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-semibold text-foreground">
                      {formatHistoryStrategyName(run.strategy, strategies)}
                      <span className="ml-2 text-xs font-normal text-secondary-text">
                        {formatHistoryMarketLabel(run.market)}
                      </span>
                    </span>
                    <span className="mt-0.5 block text-xs text-secondary-text">
                      {t('screeningPage.history.returnCount', { count: run.candidateCount ?? 0 })}
                      {run.snapshotCount != null
                        ? ` · ${t('screeningPage.history.snapshot', { count: run.snapshotCount })}`
                        : ''}
                      {run.llmRanked ? ` · ${t('screeningPage.history.llmRanked')}` : ''}
                    </span>
                  </span>
                  <span className="shrink-0 text-xs text-secondary-text">
                    {formatRunCreatedAt(run.createdAt)}
                  </span>
                </button>
              </PlayfulListItem>
            ))}
          </PlayfulList>
        )}
      </PlayfulCard>
    </PlayfulPage>
  );
};

export default StockScreeningPage;
