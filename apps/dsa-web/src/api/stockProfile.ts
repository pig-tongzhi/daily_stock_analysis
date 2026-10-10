import apiClient from './index';
import { toCamelCase } from './utils';
import type { StockProfileResponse } from '../types/stockProfile';

/**
 * 个股画像（行情 + 日线 + 资讯 + 研报 + 证据质量）统一入口。
 *
 * 决策信号页此前只按股票查询「已记录的 AI 信号」，一只从未分析过的股票
 * （例如 002567 唐人神）会让整页与用户的输入无关。该接口把「这只股票现在
 * 能被系统看到什么」一次性返回，且每个 block 自带 status/limitations，
 * 使 UI 可以如实说明缺口而不是留白。
 */
export const stockProfileApi = {
  async getProfile(stockCode: string): Promise<StockProfileResponse> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/stocks/${encodeURIComponent(stockCode)}/profile`,
    );
    return toCamelCase<StockProfileResponse>(response.data);
  },
};
