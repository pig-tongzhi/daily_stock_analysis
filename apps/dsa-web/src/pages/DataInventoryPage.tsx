import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, Database, RefreshCw, Table2 } from 'lucide-react';
import {
  dataInventoryApi,
  type DataInventory,
  type DataInventoryGap,
  type DataInventoryTable,
} from '../api/dataInventory';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import {
  ApiErrorAlert,
  AppPage,
  Badge,
  Button,
  Card,
  ColumnFillIndicator,
  EmptyState,
  InlineAlert,
  Loading,
  PageHeader,
  SectionCard,
  StatCard,
} from '../components/common';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { UiLanguage, UiTextKey, UiTextParams } from '../i18n/uiText';
import { cn } from '../utils/cn';

type Translate = (key: UiTextKey, params?: UiTextParams) => string;
type BadgeTone = 'default' | 'success' | 'warning' | 'danger' | 'info';

const FRESHNESS_META: Record<string, { labelKey: UiTextKey; tone: BadgeTone }> = {
  fresh: { labelKey: 'dataInventory.freshness.fresh', tone: 'success' },
  // 后端实测会返回 ok，语义等同 fresh，不能因为文档没写就退化成「未知」
  ok: { labelKey: 'dataInventory.freshness.ok', tone: 'success' },
  aging: { labelKey: 'dataInventory.freshness.aging', tone: 'warning' },
  stale: { labelKey: 'dataInventory.freshness.stale', tone: 'danger' },
  empty: { labelKey: 'dataInventory.freshness.empty', tone: 'danger' },
  missing: { labelKey: 'dataInventory.freshness.missing', tone: 'danger' },
  unknown: { labelKey: 'dataInventory.freshness.unknown', tone: 'default' },
};

const ISSUE_META: Record<string, { labelKey: UiTextKey; tone: BadgeTone; severe: boolean }> = {
  // 数据在，但这一列从未写入：是漏写字段，不是表坏了
  empty_columns: { labelKey: 'dataInventory.gap.issue.emptyColumns', tone: 'warning', severe: false },
  empty_table: { labelKey: 'dataInventory.gap.issue.emptyTable', tone: 'danger', severe: true },
  table_missing: { labelKey: 'dataInventory.gap.issue.tableMissing', tone: 'danger', severe: true },
  database_missing: { labelKey: 'dataInventory.gap.issue.databaseMissing', tone: 'danger', severe: true },
};

const FALLBACK_ISSUE_META = { labelKey: 'dataInventory.gap.issue.unknown' as UiTextKey, tone: 'default' as BadgeTone, severe: false };

function getLocale(language: UiLanguage): string {
  return language === 'en' ? 'en-US' : 'zh-CN';
}

/**
 * 计数一律不加千分位：这是观测页，`5565` 必须能被直接读到、搜到、复制，
 * 而不是被格式化成 `5,565` 而无法和数据库原始值对账。
 */
function formatCount(value: number | null | undefined): string {
  return String(value ?? 0);
}

function formatDateTime(value: string, language: UiLanguage): string {
  if (!value) {
    return '-';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(getLocale(language), {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

function formatAge(ageHours: number | null, t: Translate): string | null {
  if (ageHours === null || Number.isNaN(ageHours)) {
    return null;
  }
  if (ageHours >= 48) {
    return t('dataInventory.ageDays', { days: Math.round(ageHours / 24) });
  }
  return t('dataInventory.ageHours', { hours: Math.round(ageHours) });
}

const FreshnessBadge: React.FC<{ freshness: string; exists: boolean; t: Translate }> = ({ freshness, exists, t }) => {
  if (!exists) {
    return <Badge variant="danger">{t('dataInventory.table.missingTable')}</Badge>;
  }
  const meta = FRESHNESS_META[freshness] ?? FRESHNESS_META.unknown;
  return <Badge variant={meta.tone}>{t(meta.labelKey)}</Badge>;
};

const GapCard: React.FC<{ gap: DataInventoryGap; tableLabel: string; index: number; t: Translate }> = ({ gap, tableLabel, index, t }) => {
  const meta = ISSUE_META[gap.issue] ?? FALLBACK_ISSUE_META;
  return (
    <div
      data-testid={`data-gap-${gap.table}-${gap.issue}-${index}`}
      className={cn(
        'rounded-xl border px-4 py-3',
        meta.severe ? 'border-danger/30 bg-danger/5' : 'border-warning/30 bg-warning/5'
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <AlertTriangle className={cn('h-4 w-4 shrink-0', meta.severe ? 'text-danger' : 'text-warning')} />
        <span className="font-medium text-foreground">{tableLabel}</span>
        {gap.table ? <code className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-secondary-text">{gap.table}</code> : null}
        <Badge variant={meta.tone}>{t(meta.labelKey)}</Badge>
      </div>
      <p className="mt-2 text-sm text-secondary-text">{gap.detail}</p>
      <p className="mt-1 text-xs text-secondary-text">
        {meta.severe ? t('dataInventory.gap.severeNote') : t('dataInventory.gap.emptyColumnsNote')}
      </p>
    </div>
  );
};

const DataInventoryPage: React.FC = () => {
  const { language, t } = useUiLanguage();
  const [inventory, setInventory] = useState<DataInventory | null>(null);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const requestSeqRef = useRef(0);

  const loadInventory = useCallback(async () => {
    const requestSeq = requestSeqRef.current + 1;
    requestSeqRef.current = requestSeq;
    setLoading(true);
    setError(null);
    try {
      const data = await dataInventoryApi.getInventory();
      if (requestSeq !== requestSeqRef.current) {
        return;
      }
      setInventory(data);
    } catch (err) {
      if (requestSeq !== requestSeqRef.current) {
        return;
      }
      // 保留后端原始信息，标题在渲染时用当前语言覆盖，避免切换语言时重新请求
      setError(getParsedApiError(err));
    } finally {
      if (requestSeq === requestSeqRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    document.title = t('dataInventory.documentTitle');
  }, [t]);

  useEffect(() => {
    void loadInventory();
    return () => {
      requestSeqRef.current += 1;
    };
  }, [loadInventory]);

  const allTables = useMemo(
    () => inventory?.groups.flatMap((group) => group.tables) ?? [],
    [inventory]
  );

  const tablesWithData = useMemo(
    () => allTables.filter((table) => table.exists && table.rows > 0).length,
    [allTables]
  );

  const tableLabels = useMemo(() => {
    const map = new Map<string, DataInventoryTable>();
    allTables.forEach((table) => map.set(table.table, table));
    return map;
  }, [allTables]);

  const gaps = inventory?.gaps ?? [];

  // 共享错误解析层只给中文标题/兜底文案，这里统一换成界面语言，
  // 原始诊断信息保留在 rawMessage 里，交给 ApiErrorAlert 的「查看详情」展示
  const errorAlert: ParsedApiError | null = error
    ? {
      ...error,
      title: t('dataInventory.error.title'),
      message: t('dataInventory.error.message'),
      rawMessage: error.rawMessage || error.message,
    }
    : null;

  return (
    <AppPage>
      <div className="space-y-5">
        <PageHeader
          eyebrow={t('dataInventory.eyebrow')}
          title={t('dataInventory.title')}
          description={t('dataInventory.description')}
          actions={(
            <Button variant="secondary" isLoading={loading} loadingText={t('dataInventory.loading')} onClick={() => void loadInventory()}>
              <RefreshCw className={cn('h-4 w-4', loading ? 'animate-spin' : '')} />
              {t('dataInventory.refresh')}
            </Button>
          )}
        />

        {errorAlert ? (
          <ApiErrorAlert
            error={errorAlert}
            actionLabel={t('common.retry')}
            onAction={() => void loadInventory()}
          />
        ) : null}

        {loading && !inventory ? <Loading label={t('dataInventory.loading')} /> : null}

        {inventory && !inventory.database.exists ? (
          <InlineAlert
            variant="danger"
            title={t('dataInventory.database.missing')}
            message={inventory.database.path}
          />
        ) : null}

        {inventory ? (
          <>
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
              <StatCard
                label={t('dataInventory.stat.tables')}
                value={formatCount(allTables.length)}
                hint={t('dataInventory.stat.tablesHint')}
                icon={<Table2 className="h-5 w-5" />}
                tone="primary"
              />
              <StatCard
                label={t('dataInventory.stat.withData')}
                value={formatCount(tablesWithData)}
                hint={t('dataInventory.stat.withDataHint', { count: allTables.length - tablesWithData })}
                icon={<CheckCircle2 className="h-5 w-5" />}
                tone="success"
              />
              <StatCard
                label={t('dataInventory.stat.gaps')}
                value={formatCount(gaps.length)}
                hint={gaps.length ? t('dataInventory.stat.gapsHint') : t('dataInventory.stat.gapsHintNone')}
                icon={<AlertTriangle className="h-5 w-5" />}
                tone={gaps.length ? 'danger' : 'default'}
              />
              <StatCard
                label={t('dataInventory.stat.size')}
                value={`${inventory.database.sizeMb} MB`}
                hint={t('dataInventory.stat.sizeHint', { time: formatDateTime(inventory.generatedAt, language) })}
                icon={<Database className="h-5 w-5" />}
              />
            </div>

            <Card title={t('dataInventory.overview.title')} subtitle={t('dataInventory.overview.subtitle')}>
              <dl className="grid gap-3 text-sm md:grid-cols-2">
                <div className="md:col-span-2">
                  <dt className="text-xs uppercase tracking-[0.18em] text-secondary-text">{t('dataInventory.database.path')}</dt>
                  <dd className="mt-1 break-all font-mono text-xs text-foreground">{inventory.database.path}</dd>
                </div>
                <div>
                  <dt className="text-xs uppercase tracking-[0.18em] text-secondary-text">{t('dataInventory.database.size')}</dt>
                  <dd className="mt-1 text-foreground">{inventory.database.sizeMb} MB</dd>
                </div>
                <div>
                  <dt className="text-xs uppercase tracking-[0.18em] text-secondary-text">{t('dataInventory.database.generatedAt')}</dt>
                  <dd className="mt-1 text-foreground">{formatDateTime(inventory.generatedAt, language)}</dd>
                </div>
              </dl>
            </Card>

            <SectionCard
              title={t('dataInventory.gaps.title')}
              subtitle={t('dataInventory.gaps.subtitle')}
              actions={<Badge variant={gaps.length ? 'danger' : 'success'} size="md">{t('dataInventory.gaps.count', { count: gaps.length })}</Badge>}
            >
              {gaps.length ? (
                <div className="space-y-3">
                  {gaps.map((gap, index) => (
                    <GapCard
                      key={`${gap.table}-${gap.issue}-${index}`}
                      gap={gap}
                      tableLabel={tableLabels.get(gap.table)?.label ?? gap.table}
                      index={index}
                      t={t}
                    />
                  ))}
                </div>
              ) : (
                <InlineAlert
                  variant="success"
                  title={t('dataInventory.gaps.none')}
                  message={t('dataInventory.gaps.noneDescription')}
                />
              )}
            </SectionCard>

            {allTables.length ? (
              inventory.groups.map((group) => (
                <SectionCard
                  key={group.name}
                  title={group.name}
                  subtitle={t('dataInventory.groups.subtitle')}
                  actions={<span className="text-xs text-secondary-text">{t('dataInventory.groups.tableCount', { count: group.tables.length })}</span>}
                >
                  <div className="overflow-x-auto">
                    <table className="min-w-full divide-y divide-border/70 text-sm">
                      <thead className="text-left text-xs uppercase tracking-[0.16em] text-secondary-text">
                        <tr>
                          <th className="px-3 py-2 font-medium">{t('dataInventory.table.name')}</th>
                          <th className="px-3 py-2 text-right font-medium">{t('dataInventory.table.rows')}</th>
                          <th className="px-3 py-2 text-right font-medium">{t('dataInventory.table.stocks')}</th>
                          <th className="px-3 py-2 font-medium">{t('dataInventory.table.latest')}</th>
                          <th className="px-3 py-2 font-medium">{t('dataInventory.table.freshness')}</th>
                          <th className="min-w-[260px] px-3 py-2 font-medium">{t('dataInventory.table.fill')}</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border/60">
                        {group.tables.map((table) => {
                          const age = formatAge(table.ageHours, t);
                          return (
                            <tr key={table.table} data-testid={`data-table-${table.table}`} className="align-top hover:bg-hover/60">
                              <td className="px-3 py-3">
                                <div className="font-medium text-foreground">{table.label}</div>
                                {table.note ? <div className="mt-0.5 text-xs text-secondary-text">{table.note}</div> : null}
                                <code className="mt-1 inline-block rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-secondary-text">{table.table}</code>
                              </td>
                              <td className="whitespace-nowrap px-3 py-3 text-right text-foreground">{formatCount(table.rows)}</td>
                              <td className="whitespace-nowrap px-3 py-3 text-right text-secondary-text">{formatCount(table.stocks)}</td>
                              <td className="whitespace-nowrap px-3 py-3 text-secondary-text">
                                <div>{table.latest ?? '-'}</div>
                                {age ? <div className="mt-0.5 text-xs text-secondary-text">{age}</div> : null}
                              </td>
                              <td className="whitespace-nowrap px-3 py-3">
                                <FreshnessBadge freshness={table.freshness} exists={table.exists} t={t} />
                              </td>
                              <td className="px-3 py-3">
                                <ColumnFillIndicator segments={table.fill} />
                                <div className="mt-1 text-[11px] text-secondary-text">{t('dataInventory.table.columns', { count: table.columns.length })}</div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </SectionCard>
              ))
            ) : (
              <EmptyState title={t('dataInventory.emptyTitle')} description={t('dataInventory.emptyDescription')} />
            )}
          </>
        ) : null}
      </div>
    </AppPage>
  );
};

export default DataInventoryPage;
