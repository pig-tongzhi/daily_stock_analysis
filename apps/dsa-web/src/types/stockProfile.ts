import type { HistoryItem } from './analysis';

/**
 * `/api/v1/stocks/{code}/profile` 的响应类型。
 *
 * 后端把每个数据源包成独立 block，并给出 status + limitations：这样前端可以
 * 如实区分「拿到了」「只拿到一部分」「这个源根本没有」，而不是把「没有覆盖」
 * 渲染成空白或伪装成错误。字段名沿用后端 snake_case，由 toCamelCase 统一转换。
 */
export type StockProfileStatus = 'fresh' | 'partial' | 'unavailable';

export interface StockProfileQuote {
  stockCode: string;
  stockName?: string | null;
  currentPrice: number;
  change?: number | null;
  changePercent?: number | null;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  prevClose?: number | null;
  volume?: number | null;
  amount?: number | null;
  updateTime?: string | null;
}

export interface StockProfileQuoteBlock {
  status: StockProfileStatus;
  data: StockProfileQuote | null;
  limitations: string[];
}

export interface StockProfileDailyBar {
  date: string;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  close?: number | null;
  volume?: number | null;
  amount?: number | null;
  changePercent?: number | null;
}

export interface StockProfileHistoryBlock {
  status: StockProfileStatus;
  period: string;
  data: StockProfileDailyBar[];
  limitations: string[];
}

export interface StockProfileIntelligenceItem {
  id: number;
  sourceId?: number | null;
  sourceName?: string | null;
  sourceType: string;
  title: string;
  summary?: string | null;
  url: string;
  source?: string | null;
  publishedAt?: string | null;
  fetchedAt?: string | null;
  scopeType: string;
  scopeValue?: string | null;
  market: string;
}

export interface StockProfileIntelligenceBlock {
  status: StockProfileStatus;
  items: StockProfileIntelligenceItem[];
  limitations: string[];
}

export interface StockProfileResearchData {
  latestReport: HistoryItem | null;
  recentReports: HistoryItem[];
  structuredReport: unknown;
}

export interface StockProfileResearchBlock {
  status: StockProfileStatus;
  data: StockProfileResearchData;
  limitations: string[];
}

export interface StockProfilePortfolioRelation {
  held: boolean;
  matchedMarkets: string[];
}

export interface StockProfilePortfolioBlock {
  status: StockProfileStatus;
  data: StockProfilePortfolioRelation;
  limitations: string[];
}

export interface StockProfileMonitorData {
  totalRuleCount: number;
  enabledRuleCount: number;
  ruleIds: number[];
}

export interface StockProfileMonitorBlock {
  status: StockProfileStatus;
  data: StockProfileMonitorData;
  limitations: string[];
}

export interface StockProfileEvidenceQuality {
  status: StockProfileStatus;
  /** key 是 block 名（quote / history / research / intelligence / portfolio / monitors）。 */
  blocks: Record<string, StockProfileStatus>;
  limitations: string[];
}

export interface StockProfileResponse {
  requestedCode: string;
  canonicalCode: string;
  market: string;
  asOf: string;
  quote: StockProfileQuoteBlock;
  history: StockProfileHistoryBlock;
  research: StockProfileResearchBlock;
  intelligence: StockProfileIntelligenceBlock;
  portfolio: StockProfilePortfolioBlock;
  monitors: StockProfileMonitorBlock;
  evidenceQuality: StockProfileEvidenceQuality;
}
