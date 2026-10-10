import type React from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { getParsedApiError, type ParsedApiError } from '../../api/error';
import { stockProfileApi } from '../../api/stockProfile';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiLanguage, UiTextKey, UiTextParams } from '../../i18n/uiText';
import type { DecisionSignalMarket } from '../../types/decisionSignals';
import type { StockProfileResponse, StockProfileStatus } from '../../types/stockProfile';
import { cn } from '../../utils/cn';
import { getDecisionSignalMarketLabel } from '../../utils/decisionSignalLabels';
import { formatDateTime } from '../../utils/format';
import { ApiErrorAlert, Badge, Card, InlineAlert, Loading, StatCard } from '../common';
import { DashboardPanelHeader } from '../dashboard';

type Translator = (key: UiTextKey, params?: UiTextParams) => string;

type StockProfilePanelProps = {
  /** 当前股票上下文里的代码，变化即重新拉取画像。 */
  stockCode: string;
  /** 自动补全已知的名称，加载期间先用它占位，避免头部只有一串代码。 */
  stockName?: string;
};

/**
 * 后端每个 block 的 limitations 是稳定的 reason code；必须逐条翻译成中文/英文，
 * 否则「unavailable」只能渲染成空白卡片，用户无法判断是没覆盖还是查询失败。
 */
const LIMITATION_TEXT_KEYS: Record<string, UiTextKey> = {
  quote_unavailable: 'stockProfile.limitation.quoteUnavailable',
  history_unavailable: 'stockProfile.limitation.historyUnavailable',
  report_list_unavailable: 'stockProfile.limitation.reportListUnavailable',
  no_reports: 'stockProfile.limitation.noReports',
  latest_report_id_unavailable: 'stockProfile.limitation.latestReportIdUnavailable',
  latest_report_detail_unavailable: 'stockProfile.limitation.latestReportDetailUnavailable',
  structured_report_unavailable: 'stockProfile.limitation.structuredReportUnavailable',
  intelligence_query_failed: 'stockProfile.limitation.intelligenceQueryFailed',
  no_symbol_intelligence: 'stockProfile.limitation.noSymbolIntelligence',
  intelligence_alias_query_partial: 'stockProfile.limitation.intelligenceAliasQueryPartial',
  portfolio_relation_unavailable: 'stockProfile.limitation.portfolioRelationUnavailable',
  cached_positions_only: 'stockProfile.limitation.cachedPositionsOnly',
  monitor_query_failed: 'stockProfile.limitation.monitorQueryFailed',
  monitor_alias_query_partial: 'stockProfile.limitation.monitorAliasQueryPartial',
};

/**
 * 「查询动作失败」与「信息池本来就没有覆盖」是两件事：前者要提示用户可能重试，
 * 后者只是事实陈述（个股级资讯大多没有覆盖）。用颜色区分，避免把常态渲染成故障。
 */
const FAILURE_LIMITATION_CODES = new Set<string>([
  'report_list_unavailable',
  'latest_report_detail_unavailable',
  'structured_report_unavailable',
  'intelligence_query_failed',
  'monitor_query_failed',
  'portfolio_relation_unavailable',
]);

const BLOCK_LABEL_KEYS: Record<string, UiTextKey> = {
  quote: 'stockProfile.block.quote',
  history: 'stockProfile.block.history',
  research: 'stockProfile.block.research',
  intelligence: 'stockProfile.block.intelligence',
  portfolio: 'stockProfile.block.portfolio',
  monitors: 'stockProfile.block.monitors',
};

const BLOCK_ORDER = ['quote', 'history', 'research', 'intelligence', 'portfolio', 'monitors'];

const STATUS_TEXT_KEYS: Record<string, UiTextKey> = {
  fresh: 'stockProfile.status.fresh',
  partial: 'stockProfile.status.partial',
  unavailable: 'stockProfile.status.unavailable',
};

function describeLimitations(
  codes: string[],
  fallbackKey: UiTextKey,
  t: Translator,
): string {
  if (codes.length === 0) return t(fallbackKey);
  return codes
    .map((code) => {
      const key = LIMITATION_TEXT_KEYS[code];
      return key ? t(key) : t('stockProfile.limitation.unknown', { code });
    })
    .join(' ');
}

function limitationVariant(codes: string[]): 'info' | 'warning' {
  return codes.some((code) => FAILURE_LIMITATION_CODES.has(code)) ? 'warning' : 'info';
}

/** 后端 market 是 Literal[cn|hk|us|jp|kr|tw]，与决策信号的市场枚举一致，直接复用既有翻译。 */
function marketLabel(market: string, t: Translator): string {
  return getDecisionSignalMarketLabel(market as DecisionSignalMarket, t);
}

function blockLabel(name: string, t: Translator): string {
  const key = BLOCK_LABEL_KEYS[name];
  return key ? t(key) : name;
}

function statusLabel(status: string | undefined, t: Translator): string {
  const key = status ? STATUS_TEXT_KEYS[status] : undefined;
  return key ? t(key) : (status ?? t('stockProfile.status.unavailable'));
}

function statusVariant(status: string | undefined): 'default' | 'success' | 'warning' {
  if (status === 'fresh') return 'success';
  if (status === 'partial') return 'warning';
  return 'default';
}

function formatDecimal(value: number | null | undefined, digits = 2): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return value.toFixed(digits);
}

function formatSignedDecimal(value: number | null | undefined, digits = 2): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}`;
}

function formatSignedPercent(value: number | null | undefined, digits = 2): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}%`;
}

/** 成交量/成交额量级差异极大，用 Intl 紧凑记法（zh-CN 出「万/亿」，en-US 出 K/M/B）。 */
function formatCompact(value: number | null | undefined, language: UiLanguage): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return new Intl.NumberFormat(language === 'zh' ? 'zh-CN' : 'en-US', {
    notation: 'compact',
    maximumFractionDigits: 2,
  }).format(value);
}

function changeTextClass(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value === 0) {
    return 'text-foreground';
  }
  // 沿用本页统计卡片的语义色：上涨 success、下跌 danger。
  return value > 0 ? 'text-success' : 'text-danger';
}

function changeTone(value: number | null | undefined): 'default' | 'success' | 'danger' {
  if (value === null || value === undefined || !Number.isFinite(value) || value === 0) return 'default';
  return value > 0 ? 'success' : 'danger';
}

const StatusBadge: React.FC<{ status?: string }> = ({ status }) => {
  const { t } = useUiLanguage();
  return <Badge variant={statusVariant(status)}>{statusLabel(status, t)}</Badge>;
};

/**
 * 个股信息面板：当前股票被选中后，立刻回答「系统现在到底知道这只股票什么」。
 *
 * 背景：这个页面过去只按股票查询「已记录的 AI 信号」，一只从未分析过的股票
 * （002567 唐人神）会让整页看不到任何与输入相关的内容。该面板把 /stocks/{code}/profile
 * 的行情、日线覆盖、资讯、研报与证据质量如实铺开，并作为最新信号块的补充而非替代。
 */
export const StockProfilePanel: React.FC<StockProfilePanelProps> = ({ stockCode, stockName }) => {
  const { t, language } = useUiLanguage();
  const [profile, setProfile] = useState<StockProfileResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const profileRequestIdRef = useRef(0);

  const loadProfile = useCallback(async () => {
    const code = stockCode.trim();
    if (!code) {
      setProfile(null);
      setError(null);
      setLoading(false);
      return;
    }
    const requestId = profileRequestIdRef.current + 1;
    profileRequestIdRef.current = requestId;
    setLoading(true);
    setError(null);
    // 换股票时先清空上一只股票的画像，否则加载期间会「新股票名 + 旧行情」地误导用户。
    setProfile(null);
    try {
      const response = await stockProfileApi.getProfile(code);
      // 乱序响应保护：只接受最后一次请求的结果，晚到的旧股票数据一律丢弃。
      if (profileRequestIdRef.current !== requestId) return;
      setProfile(response);
    } catch (err) {
      if (profileRequestIdRef.current !== requestId) return;
      setError(getParsedApiError(err));
    } finally {
      if (profileRequestIdRef.current === requestId) {
        setLoading(false);
      }
    }
  }, [stockCode]);

  useEffect(() => {
    void loadProfile();
    return () => {
      // 卸载或换股票时让在途请求失效，避免 setState 落在过期的上下文上。
      profileRequestIdRef.current += 1;
    };
  }, [loadProfile, reloadToken]);

  const quote = profile?.quote.data ?? null;
  const displayName = quote?.stockName || stockName || stockCode;
  const canonicalCode = profile?.canonicalCode || stockCode;
  const bars = profile?.history.data ?? [];
  const latestBar = bars.length > 0 ? bars[bars.length - 1] : null;
  const intelligenceItems = profile?.intelligence.items ?? [];
  const latestReport = profile?.research.data.latestReport ?? null;
  const recentReportCount = profile?.research.data.recentReports.length ?? 0;

  const renderSectionHeader = (title: string, status?: StockProfileStatus) => (
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h4 className="text-sm font-semibold text-foreground">{title}</h4>
      {status ? <StatusBadge status={status} /> : null}
    </div>
  );

  const renderLimitations = (codes: string[], fallbackKey: UiTextKey) => (
    <InlineAlert
      variant={limitationVariant(codes)}
      message={describeLimitations(codes, fallbackKey, t)}
    />
  );

  const quoteDetailCells = quote
    ? [
      { label: t('stockProfile.open'), value: formatDecimal(quote.open) },
      { label: t('stockProfile.high'), value: formatDecimal(quote.high) },
      { label: t('stockProfile.low'), value: formatDecimal(quote.low) },
      { label: t('stockProfile.prevClose'), value: formatDecimal(quote.prevClose) },
      { label: t('stockProfile.volume'), value: formatCompact(quote.volume, language) },
      { label: t('stockProfile.amount'), value: formatCompact(quote.amount, language) },
      {
        label: t('stockProfile.updateTime'),
        value: quote.updateTime ? formatDateTime(quote.updateTime) : null,
      },
    ].filter((cell): cell is { label: string; value: string } => cell.value !== null)
    : [];

  const evidenceBlocks = profile
    ? [
      ...BLOCK_ORDER.filter((name) => profile.evidenceQuality.blocks[name] !== undefined),
      ...Object.keys(profile.evidenceQuality.blocks).filter((name) => !BLOCK_ORDER.includes(name)),
    ]
    : [];

  return (
    <Card padding="md">
      <DashboardPanelHeader
        eyebrow={t('stockProfile.title')}
        title={(
          <span className="flex flex-wrap items-baseline gap-2">
            <span>{displayName}</span>
            <span className="font-mono text-sm font-normal text-secondary-text">{canonicalCode}</span>
          </span>
        )}
        actions={profile ? (
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="info">{marketLabel(profile.market, t)}</Badge>
            <span className="text-xs text-muted-text">
              {t('stockProfile.asOf', { time: formatDateTime(profile.asOf) })}
            </span>
          </div>
        ) : null}
      />
      <p className="mb-4 text-xs leading-5 text-secondary-text">{t('stockProfile.description')}</p>

      {loading ? <Loading label={t('stockProfile.loading')} /> : null}

      {!loading && error ? (
        <ApiErrorAlert
          error={{ ...error, title: t('stockProfile.errorTitle') }}
          actionLabel={t('common.retry')}
          onAction={() => setReloadToken((token) => token + 1)}
        />
      ) : null}

      {!loading && !error && profile ? (
        <div className="space-y-5">
          <section>
            {renderSectionHeader(t('stockProfile.quoteTitle'), profile.quote.status)}
            {quote ? (
              <div>
                <div className="grid gap-3 sm:grid-cols-3">
                  <StatCard
                    label={t('stockProfile.currentPrice')}
                    tone={changeTone(quote.change)}
                    value={(
                      <span className={cn(changeTextClass(quote.change))}>
                        {formatDecimal(quote.currentPrice) ?? '--'}
                      </span>
                    )}
                  />
                  <StatCard
                    label={t('stockProfile.change')}
                    tone={changeTone(quote.change)}
                    value={(
                      <span className={cn(changeTextClass(quote.change))}>
                        {formatSignedDecimal(quote.change) ?? '--'}
                      </span>
                    )}
                  />
                  <StatCard
                    label={t('stockProfile.changePercent')}
                    tone={changeTone(quote.changePercent)}
                    value={(
                      <span className={cn(changeTextClass(quote.changePercent))}>
                        {formatSignedPercent(quote.changePercent) ?? '--'}
                      </span>
                    )}
                  />
                </div>
                {quoteDetailCells.length > 0 ? (
                  <dl className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                    {quoteDetailCells.map((cell) => (
                      <div
                        key={cell.label}
                        className="rounded-xl border border-border/60 bg-elevated/40 px-3 py-2"
                      >
                        <dt className="text-xs text-secondary-text">{cell.label}</dt>
                        <dd className="mt-1 text-sm font-medium text-foreground">{cell.value}</dd>
                      </div>
                    ))}
                  </dl>
                ) : null}
                {profile.quote.limitations.length > 0
                  ? <div className="mt-3">{renderLimitations(profile.quote.limitations, 'stockProfile.limitation.quoteUnavailable')}</div>
                  : null}
              </div>
            ) : renderLimitations(profile.quote.limitations, 'stockProfile.limitation.quoteUnavailable')}
          </section>

          <section>
            {renderSectionHeader(t('stockProfile.historyTitle'), profile.history.status)}
            {bars.length > 0 && latestBar
              ? (
                <p className="text-sm text-secondary-text">
                  {t('stockProfile.historySummary', {
                    count: bars.length,
                    date: latestBar.date,
                  })}
                </p>
              )
              : renderLimitations(profile.history.limitations, 'stockProfile.limitation.historyUnavailable')}
          </section>

          <section>
            {renderSectionHeader(t('stockProfile.intelligenceTitle'), profile.intelligence.status)}
            {intelligenceItems.length > 0
              ? (
                <ul className="space-y-3">
                  {intelligenceItems.map((item) => {
                    const publishedAt = item.publishedAt || item.fetchedAt;
                    const source = item.sourceName || item.source || item.sourceType;
                    return (
                      <li
                        key={item.id}
                        className="rounded-xl border border-border/60 bg-elevated/40 px-3 py-2"
                      >
                        <a
                          href={item.url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-sm font-medium text-foreground transition-colors hover:text-primary"
                        >
                          {item.title}
                        </a>
                        <p className="mt-1 text-xs text-muted-text">
                          {t('stockProfile.intelligenceSource')}: {source}
                          {publishedAt
                            ? ` · ${t('stockProfile.intelligencePublishedAt')}: ${formatDateTime(publishedAt)}`
                            : ''}
                        </p>
                      </li>
                    );
                  })}
                </ul>
              )
              : renderLimitations(profile.intelligence.limitations, 'stockProfile.limitation.noSymbolIntelligence')}
          </section>

          <section>
            {renderSectionHeader(t('stockProfile.researchTitle'), profile.research.status)}
            {latestReport
              ? (
                <div>
                  <dl className="grid gap-3 sm:grid-cols-2">
                    <div className="rounded-xl border border-border/60 bg-elevated/40 px-3 py-2">
                      <dt className="text-xs text-secondary-text">{t('stockProfile.researchLatest')}</dt>
                      <dd className="mt-1 text-sm font-medium text-foreground">
                        #{latestReport.id}{latestReport.stockName ? ` · ${latestReport.stockName}` : ''}
                      </dd>
                    </div>
                    <div className="rounded-xl border border-border/60 bg-elevated/40 px-3 py-2">
                      <dt className="text-xs text-secondary-text">{t('stockProfile.researchAdvice')}</dt>
                      <dd className="mt-1 text-sm font-medium text-foreground">
                        {latestReport.operationAdvice || latestReport.actionLabel || '—'}
                      </dd>
                    </div>
                    <div className="rounded-xl border border-border/60 bg-elevated/40 px-3 py-2">
                      <dt className="text-xs text-secondary-text">{t('stockProfile.researchCreatedAt')}</dt>
                      <dd className="mt-1 text-sm font-medium text-foreground">
                        {formatDateTime(latestReport.createdAt)}
                      </dd>
                    </div>
                  </dl>
                  {latestReport.analysisSummary ? (
                    <p className="mt-3 line-clamp-4 text-sm leading-5 text-secondary-text">
                      {latestReport.analysisSummary}
                    </p>
                  ) : null}
                  {recentReportCount > 1 ? (
                    <p className="mt-2 text-xs text-muted-text">
                      {t('stockProfile.researchRecentCount', { count: recentReportCount - 1 })}
                    </p>
                  ) : null}
                  {profile.research.limitations.length > 0
                    ? <div className="mt-3">{renderLimitations(profile.research.limitations, 'stockProfile.limitation.structuredReportUnavailable')}</div>
                    : null}
                </div>
              )
              : renderLimitations(profile.research.limitations, 'stockProfile.limitation.noReports')}
          </section>

          <section>
            {renderSectionHeader(t('stockProfile.evidenceTitle'), profile.evidenceQuality.status)}
            <p className="mb-3 text-xs leading-5 text-secondary-text">
              {t('stockProfile.evidenceDescription')}
            </p>
            <div className="flex flex-wrap gap-2">
              {evidenceBlocks.map((name) => (
                <span
                  key={name}
                  className="inline-flex items-center gap-2 rounded-full border border-border/60 bg-elevated/40 px-3 py-1 text-xs text-secondary-text"
                >
                  {blockLabel(name, t)}
                  <StatusBadge status={profile.evidenceQuality.blocks[name]} />
                </span>
              ))}
            </div>
          </section>
        </div>
      ) : null}
    </Card>
  );
};
