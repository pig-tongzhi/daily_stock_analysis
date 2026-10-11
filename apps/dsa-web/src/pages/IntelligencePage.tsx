import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Newspaper, RefreshCw, Rss } from 'lucide-react';
import {
  fetchEnabledIntelligenceSources,
  fetchIntelligenceItems,
  fetchIntelligenceSources,
  type IntelligenceItem,
  type IntelligenceSource,
} from '../api/intelligence';
import type { ParsedApiError } from '../api/error';
import { ApiErrorAlert, Badge, Loading, Select, Tooltip } from '../components/common';
// 资讯页 playful 改造：容器/卡片/指标/列表全部走首页抽出的同一套基元，
// 不再手写 Tailwind 类名串，保证与首页/持仓/选股页观感一致。
import {
  PlayfulCard,
  PlayfulChip,
  PlayfulEmptyState,
  PlayfulList,
  PlayfulListItem,
  PlayfulMascot,
  PlayfulPage,
  PlayfulRankBadge,
  PlayfulSection,
  PlayfulSectionTitle,
  PlayfulStat,
  PlayfulStatGrid,
} from '../components/playful';
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

  // 市场覆盖只统计「当前页返回的条目」里出现过的 market，不做全库推断，
  // 因此文案必须写明「本页涉及市场」，避免被读成全量口径。
  const pageMarkets = useMemo(
    () => [...new Set(items.map((i) => i.market).filter((m): m is string => Boolean(m)))],
    [items],
  );

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  // 序号在整份列表里的真实位置（跨页连续），不是每页从 1 重新数。
  const rankOffset = (page - 1) * PAGE_SIZE;

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
    <PlayfulPage className="max-w-6xl space-y-4 pb-12 pt-4">
      <PlayfulSection>
        {/* 页头沿用首页/持仓/选股同一套 hero 卡：吉祥物 + 标题 + 指标 chip + 动作按钮 */}
        <PlayfulCard
          variant="hero"
          padding="none"
          className="flex flex-wrap items-center gap-4 px-5 py-4"
          data-testid="playful-intelligence-hero"
        >
          <PlayfulMascot className="h-[72px] w-[104px] shrink-0" />
          <div className="min-w-[12rem] flex-1">
            <h1 className="text-2xl font-extrabold tracking-tight text-foreground">
              {t('intelligence.title')}
            </h1>
            <p className="mt-1 text-xs font-semibold playful-ink-soft">
              {t('intelligence.description')}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {/* 资讯总数直接取接口 total，不做本地累加，保证翻页后仍是全量口径 */}
            <PlayfulStat label={t('intelligence.stat.total')} value={total} tone="grape" />
            <button
              type="button"
              onClick={() => void loadItems()}
              className="inline-flex items-center gap-2 rounded-xl border border-[var(--playful-border)] bg-[var(--playful-surface)] px-3 py-2 text-sm font-semibold playful-ink-soft shadow-[var(--playful-shadow-soft)] transition-colors hover:bg-[var(--playful-chip-bg)]"
            >
              <RefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} />
              {t('intelligence.action.refresh')}
            </button>
            <button
              type="button"
              onClick={() => void handleFetchNow()}
              disabled={fetching}
              className="inline-flex items-center gap-2 rounded-xl bg-[hsl(var(--primary))] px-3.5 py-2 text-sm font-medium text-[hsl(var(--primary-foreground))] transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              <Rss className={cn('h-4 w-4', fetching && 'animate-pulse')} />
              {fetching ? t('intelligence.action.fetching') : t('intelligence.action.fetch')}
            </button>
          </div>
        </PlayfulCard>

        {error ? <ApiErrorAlert error={error} /> : null}
        {notice ? (
          <div className="rounded-2xl border border-success/25 bg-success/8 px-4 py-3 text-sm text-success">
            {notice}
          </div>
        ) : null}

        {/* 概览：原来 4 张 StatCard 换成 playful 指标块，语义仍是「接口返回多少就显示多少」 */}
        <PlayfulCard as="section" padding="md" data-testid="playful-intelligence-overview">
          <PlayfulSectionTitle
            emoji="📊"
            title={t('newsPage.overview.title')}
            meta={t('intelligence.list.summary', {
              total: String(total),
              page: String(page),
              pages: String(totalPages),
            })}
          />
          <PlayfulStatGrid className="mt-3 sm:grid-cols-4 xl:grid-cols-4">
            <PlayfulStat
              size="sm"
              label={t('intelligence.stat.total')}
              value={total}
              tone="grape"
            />
            <PlayfulStat
              size="sm"
              label={t('intelligence.stat.sources')}
              value={`${enabledSourceCount}/${sources.length}`}
              tone="mint"
            />
            <PlayfulStat
              size="sm"
              label={t('intelligence.stat.latest')}
              value={relativeAge(latestPublished, language)}
              tone="ink"
            />
            <PlayfulStat
              size="sm"
              label={t('intelligence.filter.market')}
              value={pageMarkets.length}
              tone="sun"
            />
          </PlayfulStatGrid>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] font-semibold playful-ink-soft">
            <PlayfulChip size="sm">
              {t('intelligence.stat.sourcesHint', {
                total: String(sources.length),
                enabled: String(enabledSourceCount),
              })}
            </PlayfulChip>
            <PlayfulChip size="sm" tone="soft">
              {t('intelligence.stat.latest')} · {formatDateTime(latestPublished, language)}
            </PlayfulChip>
            {/* 市场数只覆盖本页条目，文案明确写「本页」，不冒充全库覆盖 */}
            <span>
              {t('newsPage.overview.marketsNote', {
                markets: pageMarkets.join(' · ') || '—',
              })}
            </span>
          </div>
        </PlayfulCard>

        {/* 筛选栏：交互与参数完全不变，只是换到 playful 卡片里 */}
        <PlayfulCard as="section" padding="md" data-testid="playful-intelligence-filters">
          <PlayfulSectionTitle emoji="🎛️" title={t('newsPage.filter.title')} />
          <div className="mt-3 grid grid-cols-1 gap-4 md:grid-cols-4">
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
                className="h-10 rounded-xl border border-border/60 bg-[var(--bg-card)] px-3 text-sm text-foreground outline-none transition-colors placeholder:text-muted-text focus:border-[hsl(var(--primary)/0.5)]"
              />
            </div>
          </div>
        </PlayfulCard>

        {/* 资讯列表：视觉主体，用 roomy 行 + 序号徽标，行高比原来的 px-5 py-4 更紧，长列表更耐扫 */}
        <PlayfulCard as="section" padding="none" data-testid="playful-intelligence-list">
          <div className="px-5 pb-3 pt-5">
            <PlayfulSectionTitle
              emoji="📰"
              title={t('intelligence.title')}
              meta={t('intelligence.list.summary', {
                total: String(total),
                page: String(page),
                pages: String(totalPages),
              })}
            />
          </div>
          <div className="border-t border-dashed playful-dashed" />
          {loading ? (
            <div className="flex justify-center py-16">
              <Loading />
            </div>
          ) : items.length === 0 ? (
            <div className="px-5 py-6">
              <PlayfulEmptyState
                icon={<Newspaper className="h-6 w-6" />}
                title={t('intelligence.empty.title')}
                description={t('intelligence.empty.description')}
              />
            </div>
          ) : (
            <PlayfulList>
              {items.map((item, index) => {
                const isOpen = expanded.has(item.id);
                const hasSummary = Boolean(item.summary && item.summary.trim());
                const rank = rankOffset + index + 1;
                return (
                  <PlayfulListItem
                    key={item.id}
                    density="roomy"
                    className="items-start px-5 py-3 transition-colors hover:bg-[var(--bg-hover)]/40"
                    data-testid="playful-intelligence-item"
                  >
                    <PlayfulRankBadge aria-label={t('newsPage.list.rankLabel', { index: rank })}>
                      {rank}
                    </PlayfulRankBadge>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2 text-[11px] font-medium playful-ink-soft">
                        <span className="tabular-nums">
                          {formatDateTime(item.published_at ?? item.fetched_at, language)}
                        </span>
                        <span aria-hidden="true">·</span>
                        <span>{relativeAge(item.published_at ?? item.fetched_at, language)}</span>
                        {/* 作用域不是好坏语义，用 playful 点缀色区分，不占用 success/warning/danger */}
                        <PlayfulChip size="sm" tone={isSymbolScope(item) ? 'grape' : 'soft'}>
                          {isSymbolScope(item)
                            ? `${item.scope_type}/${item.scope_value ?? '-'}`
                            : `market/${item.market ?? '-'}`}
                        </PlayfulChip>
                        <span className="min-w-0 truncate">
                          {item.source_name ?? item.source ?? '—'}
                        </span>
                      </div>

                      <div className="mt-1 text-sm font-semibold leading-relaxed text-foreground">
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
                              'mt-1 text-sm leading-relaxed text-secondary-text',
                              !isOpen && 'line-clamp-2',
                            )}
                          >
                            {item.summary}
                          </p>
                          <button
                            type="button"
                            onClick={() => toggleSummary(item.id)}
                            className="mt-1 text-xs font-semibold text-[hsl(var(--primary))] transition-opacity hover:opacity-80"
                          >
                            {isOpen
                              ? t('intelligence.hideSummary')
                              : t('intelligence.showSummary')}
                          </button>
                        </>
                      ) : (
                        <p className="mt-1 text-xs text-muted-text">
                          {t('intelligence.summary.none')}
                        </p>
                      )}
                    </div>
                  </PlayfulListItem>
                );
              })}
            </PlayfulList>
          )}

          {totalPages > 1 ? (
            <div className="flex items-center justify-between border-t border-dashed playful-dashed px-5 py-3 text-sm">
              <span className="playful-ink-soft">
                {page} / {totalPages}
              </span>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={page <= 1}
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  className="rounded-xl border border-border/60 px-3 py-1.5 font-semibold text-secondary-text transition-colors hover:bg-[var(--bg-hover)] disabled:opacity-40"
                >
                  {t('intelligence.page.prev')}
                </button>
                <button
                  type="button"
                  disabled={page >= totalPages}
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  className="rounded-xl border border-border/60 px-3 py-1.5 font-semibold text-secondary-text transition-colors hover:bg-[var(--bg-hover)] disabled:opacity-40"
                >
                  {t('intelligence.page.next')}
                </button>
              </div>
            </div>
          ) : null}
        </PlayfulCard>

        {/* 情报源状态：语义色沿用 Badge 的 success/default，错误仍走 danger + Tooltip */}
        <PlayfulCard as="section" padding="none" data-testid="playful-intelligence-sources">
          <div className="px-5 pb-3 pt-5">
            <PlayfulSectionTitle
              emoji="🛰️"
              title={t('intelligence.sources.title')}
              meta={t('intelligence.stat.sourcesHint', {
                total: String(sources.length),
                enabled: String(enabledSourceCount),
              })}
            />
          </div>
          <div className="border-t border-dashed playful-dashed" />
          <PlayfulList className="px-5">
            {sources.map((s) => (
              <PlayfulListItem key={s.id} className="flex-wrap py-2.5 text-xs">
                <span className="font-mono text-xs playful-ink-soft">#{s.id}</span>
                <Badge variant={s.enabled ? 'success' : 'default'} size="sm">
                  {s.enabled ? t('intelligence.sources.enabled') : t('intelligence.sources.disabled')}
                </Badge>
                <span className="font-bold text-foreground">{s.name}</span>
                <span className="playful-ink-soft">{s.source_type}</span>
                <span className="playful-ink-soft">
                  {s.scope_type}/{s.scope_value ?? '-'} · {s.market ?? '-'}
                </span>
                <span className="ml-auto playful-ink-soft">
                  {s.last_fetched_at ? relativeAge(s.last_fetched_at, language) : '—'}
                </span>
                {s.last_error ? (
                  <Tooltip content={s.last_error} contentClassName="max-w-sm whitespace-normal">
                    <span className="w-full truncate text-xs text-danger">{s.last_error}</span>
                  </Tooltip>
                ) : null}
              </PlayfulListItem>
            ))}
          </PlayfulList>
        </PlayfulCard>
      </PlayfulSection>
    </PlayfulPage>
  );
};

export default IntelligencePage;
