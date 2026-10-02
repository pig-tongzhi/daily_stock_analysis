import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Clock3,
  Database,
  Globe2,
  Newspaper,
  RefreshCw,
  Rss,
} from 'lucide-react';
import {
  fetchEnabledIntelligenceSources,
  fetchIntelligenceItems,
  fetchIntelligenceSources,
  type IntelligenceItem,
  type IntelligenceSource,
} from '../api/intelligence';
import type { ParsedApiError } from '../api/error';
import {
  ApiErrorAlert,
  AppPage,
  Badge,
  Card,
  EmptyState,
  Loading,
  PageHeader,
  Select,
  StatCard,
  Tooltip,
} from '../components/common';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { UiLanguage } from '../i18n/uiText';
import { cn } from '../utils/cn';

const DAYS_OPTIONS = [1, 3, 7, 30] as const;
const PAGE_SIZE = 20;

function formatDateTime(value: string | null | undefined, language: UiLanguage): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  const locale = language === 'en' ? 'en-US' : 'zh-CN';
  return new Intl.DateTimeFormat(locale, {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(d);
}

function relativeAge(value: string | null | undefined, language: UiLanguage): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '—';
  const mins = Math.max(0, Math.round((Date.now() - d.getTime()) / 60000));
  if (mins < 60) return language === 'en' ? `${mins}m ago` : `${mins} 分钟前`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return language === 'en' ? `${hours}h ago` : `${hours} 小时前`;
  const days = Math.round(hours / 24);
  return language === 'en' ? `${days}d ago` : `${days} 天前`;
}

/** 判断条目属于个股级（symbol）还是大盘级（market）。 */
function isSymbolScope(item: IntelligenceItem): boolean {
  return item.scope_type === 'symbol';
}

const IntelligencePage: React.FC = () => {
  const { t, language } = useUiLanguage();

  const [items, setItems] = useState<IntelligenceItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [sources, setSources] = useState<IntelligenceSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [fetching, setFetching] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  // 筛选条件
  const [market, setMarket] = useState('all');
  const [scope, setScope] = useState('all');
  const [days, setDays] = useState<number>(3);
  const [keyword, setKeyword] = useState('');
  const [keywordDraft, setKeywordDraft] = useState('');

  const loadItems = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchIntelligenceItems({
        market: market === 'all' ? undefined : market,
        scope_type: scope === 'all' ? undefined : scope,
        days,
        query: keyword || undefined,
        page,
        page_size: PAGE_SIZE,
      });
      setItems(data.items ?? []);
      setTotal(data.total ?? 0);
    } catch (err) {
      setError(err as ParsedApiError);
    } finally {
      setLoading(false);
    }
  }, [market, scope, days, keyword, page]);

  const loadSources = useCallback(async () => {
    try {
      const data = await fetchIntelligenceSources();
      setSources(data.items ?? []);
    } catch {
      // 源列表失败不影响主列表
    }
  }, []);

  useEffect(() => {
    void loadItems();
  }, [loadItems]);

  useEffect(() => {
    void loadSources();
  }, [loadSources]);

  const handleFetchNow = useCallback(async () => {
    setFetching(true);
    setNotice(null);
    setError(null);
    try {
      const res = await fetchEnabledIntelligenceSources();
      setNotice(
        t('intelligence.action.fetchOk', {
          sources: String(res.source_count ?? 0),
          saved: String(res.saved_count ?? 0),
        }),
      );
      await Promise.all([loadItems(), loadSources()]);
    } catch (err) {
      setError(err as ParsedApiError);
    } finally {
      setFetching(false);
    }
  }, [t, loadItems, loadSources]);

  const toggleSummary = useCallback((id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const enabledSourceCount = useMemo(
    () => sources.filter((s) => s.enabled).length,
    [sources],
  );

  const latestPublished = useMemo(() => {
    // 部分源（如 NewsNow 热榜类）没有 published_at，回退到 fetched_at
    const dated = items
      .map((i) => i.published_at ?? i.fetched_at)
      .filter((v): v is string => Boolean(v));
    if (!dated.length) return null;
    return dated.sort().at(-1) ?? null;
  }, [items]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const marketOptions = useMemo(
    () => [
      { value: 'all', label: t('intelligence.filter.all') },
      { value: 'cn', label: 'CN' },
      { value: 'hk', label: 'HK' },
      { value: 'us', label: 'US' },
      { value: 'global', label: 'Global' },
    ],
    [t],
  );

  const scopeOptions = useMemo(
    () => [
      { value: 'all', label: t('intelligence.filter.all') },
      { value: 'symbol', label: 'symbol' },
      { value: 'market', label: 'market' },
    ],
    [t],
  );

  const daysOptions = useMemo(
    () =>
      DAYS_OPTIONS.map((d) => ({
        value: String(d),
        label: t(`intelligence.days.${d}` as never),
      })),
    [t],
  );

  return (
    <AppPage>
      <PageHeader
        eyebrow="Intelligence"
        title={t('intelligence.title')}
        description={t('intelligence.description')}
        actions={
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void loadItems()}
              className="inline-flex items-center gap-2 rounded-lg border border-border/60 px-3 py-2 text-sm text-secondary-text transition-colors hover:bg-[var(--bg-hover)] hover:text-foreground"
            >
              <RefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} />
              {t('intelligence.action.refresh')}
            </button>
            <button
              type="button"
              onClick={() => void handleFetchNow()}
              disabled={fetching}
              className="inline-flex items-center gap-2 rounded-lg bg-[hsl(var(--primary))] px-3.5 py-2 text-sm font-medium text-[hsl(var(--primary-foreground))] transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              <Rss className={cn('h-4 w-4', fetching && 'animate-pulse')} />
              {fetching ? t('intelligence.action.fetching') : t('intelligence.action.fetch')}
            </button>
          </div>
        }
      />

      {error ? <ApiErrorAlert error={error} className="mb-4" /> : null}
      {notice ? (
        <div className="mb-4 rounded-xl border border-success/25 bg-success/8 px-4 py-3 text-sm text-success">
          {notice}
        </div>
      ) : null}

      {/* 概览 */}
      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label={t('intelligence.stat.total')}
          value={total}
          icon={<Database className="h-4 w-4" />}
          tone="primary"
        />
        <StatCard
          label={t('intelligence.stat.sources')}
          value={`${enabledSourceCount}/${sources.length}`}
          hint={t('intelligence.stat.sourcesHint', {
            total: String(sources.length),
            enabled: String(enabledSourceCount),
          })}
          icon={<Rss className="h-4 w-4" />}
        />
        <StatCard
          label={t('intelligence.stat.latest')}
          value={relativeAge(latestPublished, language)}
          hint={formatDateTime(latestPublished, language)}
          icon={<Clock3 className="h-4 w-4" />}
        />
        <StatCard
          label={t('intelligence.filter.market')}
          value={new Set(items.map((i) => i.market).filter(Boolean)).size}
          hint={[...new Set(items.map((i) => i.market).filter(Boolean))].join(' · ') || '—'}
          icon={<Globe2 className="h-4 w-4" />}
        />
      </div>

      {/* 筛选栏 */}
      <Card className="mb-6" padding="md">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <Select
            label={t('intelligence.filter.market')}
            value={market}
            onChange={(v) => {
              setMarket(v);
              setPage(1);
            }}
            options={marketOptions}
          />
          <Select
            label={t('intelligence.filter.scope')}
            value={scope}
            onChange={(v) => {
              setScope(v);
              setPage(1);
            }}
            options={scopeOptions}
          />
          <Select
            label={t('intelligence.filter.days')}
            value={String(days)}
            onChange={(v) => {
              setDays(Number(v));
              setPage(1);
            }}
            options={daysOptions}
          />
          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="intelligence-keyword"
              className="text-xs font-medium text-muted-text"
            >
              {t('intelligence.filter.keyword')}
            </label>
            <input
              id="intelligence-keyword"
              value={keywordDraft}
              onChange={(e) => setKeywordDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  setKeyword(keywordDraft.trim());
                  setPage(1);
                }
              }}
              placeholder={t('intelligence.filter.keywordPlaceholder')}
              className="h-10 rounded-lg border border-border/60 bg-[var(--bg-card)] px-3 text-sm text-foreground outline-none transition-colors placeholder:text-muted-text focus:border-[hsl(var(--primary)/0.5)]"
            />
          </div>
        </div>
      </Card>

      {/* 资讯列表 */}
      <Card padding="none">
        <div className="flex flex-wrap items-center justify-between gap-2 px-5 pb-3 pt-5">
          <div>
            <h3 className="text-base font-semibold text-foreground">
              {t('intelligence.title')}
            </h3>
            <p className="mt-0.5 text-xs text-muted-text">
              {t('intelligence.list.summary', {
                total: String(total),
                page: String(page),
                pages: String(totalPages),
              })}
            </p>
          </div>
        </div>
        <div className="border-t border-border/40" />
        {loading ? (
          <div className="flex justify-center py-16">
            <Loading />
          </div>
        ) : items.length === 0 ? (
          <div className="py-8">
            <EmptyState
              icon={<Newspaper className="h-6 w-6" />}
              title={t('intelligence.empty.title')}
              description={t('intelligence.empty.description')}
            />
          </div>
        ) : (
          <ul className="divide-y divide-border/40">
            {items.map((item) => {
              const isOpen = expanded.has(item.id);
              const hasSummary = Boolean(item.summary && item.summary.trim());
              return (
                <li key={item.id} className="px-5 py-4 transition-colors hover:bg-[var(--bg-hover)]/40">
                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted-text">
                    <span>{formatDateTime(item.published_at ?? item.fetched_at, language)}</span>
                    <span className="text-border">·</span>
                    <span>{relativeAge(item.published_at ?? item.fetched_at, language)}</span>
                    <Badge
                      variant={isSymbolScope(item) ? 'info' : 'default'}
                      size="sm"
                    >
                      {isSymbolScope(item)
                        ? `${item.scope_type}/${item.scope_value ?? '-'}`
                        : `market/${item.market ?? '-'}`}
                    </Badge>
                    <span className="text-secondary-text">{item.source_name ?? item.source ?? '—'}</span>
                  </div>

                  <div className="mt-2 text-sm font-medium leading-relaxed text-foreground">
                    {item.url ? (
                      <a
                        href={item.url}
                        target="_blank"
                        rel="noreferrer"
                        className="transition-colors hover:text-[hsl(var(--primary))]"
                      >
                        {item.title}
                      </a>
                    ) : (
                      item.title
                    )}
                  </div>

                  {hasSummary ? (
                    <>
                      <p
                        className={cn(
                          'mt-1.5 text-sm leading-relaxed text-secondary-text',
                          !isOpen && 'line-clamp-2',
                        )}
                      >
                        {item.summary}
                      </p>
                      <button
                        type="button"
                        onClick={() => toggleSummary(item.id)}
                        className="mt-1 text-xs text-[hsl(var(--primary))] transition-opacity hover:opacity-80"
                      >
                        {isOpen ? t('intelligence.hideSummary') : t('intelligence.showSummary')}
                      </button>
                    </>
                  ) : (
                    <p className="mt-1.5 text-xs text-muted-text">{t('intelligence.summary.none')}</p>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        {totalPages > 1 ? (
          <div className="flex items-center justify-between border-t border-border/40 px-5 py-3 text-sm">
            <span className="text-muted-text">
              {page} / {totalPages}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className="rounded-lg border border-border/60 px-3 py-1.5 text-secondary-text transition-colors hover:bg-[var(--bg-hover)] disabled:opacity-40"
              >
                {t('intelligence.page.prev')}
              </button>
              <button
                type="button"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                className="rounded-lg border border-border/60 px-3 py-1.5 text-secondary-text transition-colors hover:bg-[var(--bg-hover)] disabled:opacity-40"
              >
                {t('intelligence.page.next')}
              </button>
            </div>
          </div>
        ) : null}
      </Card>

      {/* 情报源状态 */}
      <div className="mt-6">
        <Card padding="none">
          <div className="flex flex-wrap items-center justify-between gap-2 px-5 pb-3 pt-5">
            <div>
              <h3 className="text-base font-semibold text-foreground">
                {t('intelligence.sources.title')}
              </h3>
              <p className="mt-0.5 text-xs text-muted-text">
                {t('intelligence.stat.sourcesHint', {
                  total: String(sources.length),
                  enabled: String(enabledSourceCount),
                })}
              </p>
            </div>
          </div>
          <div className="border-t border-border/40" />
          <ul className="divide-y divide-border/40">
            {sources.map((s) => (
              <li key={s.id} className="flex flex-wrap items-center gap-2 px-5 py-3 text-sm">
                <span className="font-mono text-xs text-muted-text">#{s.id}</span>
                <Badge variant={s.enabled ? 'success' : 'default'} size="sm">
                  {s.enabled ? t('intelligence.sources.enabled') : t('intelligence.sources.disabled')}
                </Badge>
                <span className="font-medium text-foreground">{s.name}</span>
                <span className="text-xs text-muted-text">{s.source_type}</span>
                <span className="text-xs text-muted-text">
                  {s.scope_type}/{s.scope_value ?? '-'} · {s.market ?? '-'}
                </span>
                <span className="ml-auto text-xs text-muted-text">
                  {s.last_fetched_at ? relativeAge(s.last_fetched_at, language) : '—'}
                </span>
                {s.last_error ? (
                  <Tooltip content={s.last_error} contentClassName="max-w-sm whitespace-normal">
                    <span className="w-full truncate text-xs text-danger">{s.last_error}</span>
                  </Tooltip>
                ) : null}
              </li>
            ))}
          </ul>
        </Card>
      </div>
    </AppPage>
  );
};

export default IntelligencePage;
