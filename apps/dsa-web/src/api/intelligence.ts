import apiClient from './index';

/**
 * 资讯池 / 情报源 API 客户端。
 *
 * 对应后端 `api/v1/endpoints/intelligence.py`：
 *   GET  /api/v1/intelligence/items           列出已落库资讯（支持筛选与分页）
 *   GET  /api/v1/intelligence/sources         列出情报源配置
 *   POST /api/v1/intelligence/sources/fetch-enabled   拉取全部启用源
 *
 * 注意：后端字段是 snake_case，这里直接透传，不做 camelCase 转换，
 * 避免与 `total` / `page_size` 等分页字段产生歧义。
 */

export type IntelligenceScopeType = 'market' | 'symbol' | 'sector' | string;
export type IntelligenceSourceType = 'rss' | 'atom' | 'newsnow' | string;

export interface IntelligenceItem {
  id: number;
  source_id: number | null;
  source_name: string | null;
  source_type: IntelligenceSourceType | null;
  title: string;
  summary: string | null;
  url: string | null;
  source: string | null;
  published_at: string | null;
  fetched_at: string | null;
  scope_type: IntelligenceScopeType | null;
  scope_value: string | null;
  market: string | null;
}

export interface IntelligenceSource {
  id: number;
  name: string;
  source_type: IntelligenceSourceType;
  url: string;
  enabled: boolean;
  scope_type: IntelligenceScopeType | null;
  scope_value: string | null;
  market: string | null;
  description: string | null;
  last_status: string | null;
  last_error: string | null;
  last_fetched_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface IntelligenceItemsPage {
  items: IntelligenceItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface IntelligenceSourcesPage {
  items: IntelligenceSource[];
  total: number;
  page: number;
  page_size: number;
}

export interface IntelligenceItemsQuery {
  scope_type?: string;
  scope_value?: string;
  market?: string;
  query?: string;
  days?: number;
  page?: number;
  page_size?: number;
}

export interface IntelligenceFetchResult {
  source_count?: number;
  saved_count?: number;
  errors?: unknown[];
  [key: string]: unknown;
}

export async function fetchIntelligenceItems(
  params: IntelligenceItemsQuery = {},
): Promise<IntelligenceItemsPage> {
  const { data } = await apiClient.get<IntelligenceItemsPage>('/api/v1/intelligence/items', {
    params,
  });
  return data;
}

export async function fetchIntelligenceSources(): Promise<IntelligenceSourcesPage> {
  const { data } = await apiClient.get<IntelligenceSourcesPage>(
    '/api/v1/intelligence/sources',
  );
  return data;
}

export async function fetchEnabledIntelligenceSources(): Promise<IntelligenceFetchResult> {
  const { data } = await apiClient.post<IntelligenceFetchResult>(
    '/api/v1/intelligence/sources/fetch-enabled',
  );
  return data;
}

export async function fetchOneIntelligenceSource(
  sourceId: number,
): Promise<IntelligenceFetchResult> {
  const { data } = await apiClient.post<IntelligenceFetchResult>(
    `/api/v1/intelligence/sources/${sourceId}/fetch`,
  );
  return data;
}
