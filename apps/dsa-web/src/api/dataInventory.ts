import apiClient from './index';
import { toCamelCase } from './utils';

/**
 * 单列填充度。
 * pct 为后端直接算好的百分比，前端不再重算，避免与后端统计口径不一致。
 */
export type DataInventoryFill = {
  column: string;
  filled: number;
  pct: number;
  /** 整列从未写入过值：这是本页最需要暴露的信号 */
  empty: boolean;
};

export type DataInventoryTable = {
  table: string;
  label: string;
  note: string;
  rows: number;
  stocks: number;
  exists: boolean;
  /** 可能为 null：表里没有任何时间戳可推断新鲜度 */
  latest: string | null;
  /**
   * 后端当前会返回 fresh / aging / stale / empty / unknown / missing，
   * 实测还存在 ok；这里保留 string，避免后端新增档位时前端类型说谎。
   */
  freshness: string;
  ageHours: number | null;
  columns: string[];
  fill: DataInventoryFill[];
};

export type DataInventoryGroup = {
  name: string;
  tables: DataInventoryTable[];
};

/**
 * issue 取值：empty_columns | empty_table | table_missing | database_missing
 * 同样保留 string，未知取值只影响展示映射，不应导致页面崩溃。
 */
export type DataInventoryGap = {
  table: string;
  issue: string;
  detail: string;
};

export type DataInventoryDatabase = {
  path: string;
  exists: boolean;
  sizeMb: number;
};

export type DataInventory = {
  generatedAt: string;
  database: DataInventoryDatabase;
  groups: DataInventoryGroup[];
  gaps: DataInventoryGap[];
};

export const dataInventoryApi = {
  getInventory: async (): Promise<DataInventory> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/data/inventory');
    return toCamelCase<DataInventory>(response.data);
  },
};
