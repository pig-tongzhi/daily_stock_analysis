# DSA 全链路技术说明

> 项目：`daily-stock-analysis`（DSA）智能股票分析系统 · 资讯子系统全链路
> 项目路径：`/Users/mac_1/renjiebank/renjie问股/daily_stock_analysis`
> 文档编写时间：2026-10-02
> 用途：**面试讲解用**。所有代码位置均为真实 `文件:行号`，所有数字均为本机实测。
>
> ⚠️ 事实标注约定：
> - 未特别标注 = 本机实测或源码核对确认
> - 标注「未验证」= 会话中未实测，不可当作已验证事实陈述

---

## 目录

- [第 0 部分：系统一句话与总览](#第-0-部分系统一句话与总览)
- [第 1 部分：数据从哪里来（数据源层）](#第-1-部分数据从哪里来数据源层)
- [第 2 部分：资讯怎么写入 DSA（含代码位置）](#第-2-部分资讯怎么写入-dsa含代码位置)
- [第 3 部分：怎么注入到 LLM（重点）](#第-3-部分怎么注入到-llm重点)
- [第 4 部分：生成了哪些文件和调用](#第-4-部分生成了哪些文件和调用)
- [第 5 部分：LLM 与代币（面试必问）](#第-5-部分llm-与代币面试必问)
- [第 6 部分：端到端总览图](#第-6-部分端到端总览图)
- [第 7 部分：面试可讲的技术亮点](#第-7-部分面试可讲的技术亮点)

---

# 第 0 部分：系统一句话与总览

**一句话**：DSA 是一个本地运行的股票智能分析系统，它把「行情数据 + 新闻资讯」两路输入组装成 Prompt 喂给 LLM，产出结构化分析报告并落库，同时通过 FastAPI 提供 Web 界面和 API。

**技术栈**：

| 层 | 技术 |
|---|---|
| 语言 | Python 3.11.15（3.6 万行级后端）+ TypeScript/React（前端 `apps/dsa-web/`）|
| Web 框架 | FastAPI + uvicorn |
| ORM / 存储 | SQLAlchemy + **SQLite**（`data/stock_analysis.db`，无外部数据库依赖）|
| LLM 接入 | **LiteLLM** 统一网关 → DeepSeek |
| 前端 | React + Vite（`apps/dsa-web/`），另有 Electron 桌面端 `apps/dsa-desktop/` |
| 资讯转换层 | RSSHub（自建 :1200）+ NewsNow（自建 :5173）|

> ⚠️ **本文档中的行数 / 文件大小是 2026-10-02 某个时点的快照。** 服务持续运行时会不断写入
> （日志追加、每次分析新增 `analysis_history` 行），所以这些数字**只增不减**，复现时以更高值为正常。
> 表结构、代码位置、逻辑语义**不随时间变化**，可长期引用。

**本机运行实例**：

| 服务 | 端口 | 说明 |
|---|---|---|
| DSA Web / API | `:8010` | 主应用 |
| RSSHub | `:1200` | 资讯转换层（RSS/Atom 输出）|
| NewsNow | `:5173` | 资讯转换层（JSON 输出，仅绑 IPv6）|

---

# 第 1 部分：数据从哪里来（数据源层）

DSA 的输入分两大类：**行情数据**和**资讯数据**。两者来源、机制、落库位置完全不同。

## 1.1 行情数据（免费源，多源 fallback）

`data_provider/` 下有 **13 个 fetcher**，覆盖免费与付费源：

```
akshare_fetcher.py        efinance_fetcher.py      baostock_fetcher.py
tencent_fetcher.py        pytdx_fetcher.py         yfinance_fetcher.py
tushare_fetcher.py        futu_fetcher.py          longbridge_fetcher.py
alphavantage_fetcher.py   finnhub_fetcher.py       tickflow_fetcher.py
tw_institutional_fetcher.py
```

**优先级机制**：`data_provider/base.py:346` 定义了 `priority: int = 99  # 优先级数字越小越优先`，并支持 `*_PRIORITY` 环境变量单项覆盖（`base.py:1965-1966`）。

**本机实测的运行时优先级顺序**（从日志中提取，A 股日线）：

```
EfinanceFetcher(P0)  →  AkshareFetcher(P1)   →  PytdxFetcher(P2)
BaostockFetcher(P3)  →  YfinanceFetcher(P4)  →  TencentFetcher(P5)
```

**免费源覆盖**：

| 源 | 性质 |
|---|---|
| efinance / akshare | 免费（爬取东方财富 / 新浪等公开接口）|
| pytdx | 免费（通达信协议）|
| baostock | 免费（证券宝）|
| tencent / 新浪 | 免费（行情接口）|
| yfinance | 免费（Yahoo Finance）|
| tushare / futu / longbridge / alphavantage / finnhub | 需要账号或付费（可选）|

**⚠️ 实测到的稳定性问题**（真实日志，非理论）：

```
[API错误] 获取 600519 筹码分布失败: ('Connection aborted.', RemoteDisconnected(...))
[efinance] 获取板块排行失败: HTTPConnectionPool(host='push2.eastmoney.com', port=80):
           Max retries exceeded ...
[Akshare] 东财接口获取行业板块排行失败: ... 尝试新浪接口
[概念排行] 所有数据源均失败，最终错误: TencentFetcher返回空结果
```

**说明**：爬取类免费源会间歇性失败（目标站限流 / 连接被断）。系统靠**多源 fallback 链**兜底——单一源失败不会拖垮整个分析流程。这是这套系统的一个真实工程特点，也是面试可以讲的点（见第 7 部分）。

## 1.2 资讯数据（两条独立链路）

**⚠️ 关键概念：DSA 的资讯有两条完全独立的链路，落到两张不同的表。** 这是理解整套系统的前提。

| | **链路 A：资讯源采集** | **链路 B：按需搜索** |
|---|---|---|
| 数据来源 | RSSHub / NewsNow（**我们自建**）| 搜索 API（Bocha / Tavily / Brave / MiniMax / SerpAPI / SearXNG / Anspire）|
| 触发方式 | 按**源**拉取（分析时自动 / 手动调 API）| 按 **query** 实时搜索 |
| 落库表 | **`intelligence_items`** | **`news_intel`** |
| 成本 | **免费无限** | Bocha 免费额度 1000 次 / 3 个月 |
| 时间字段 | 取决于源（RSSHub 有，部分 NewsNow 源无）| ✅ 都有 |
| 正文 | ✅ RSSHub 有（实测 100% 填充）| ✅ 有（Bocha `summary`）|
| 本机当前行数 | **298 条** | 10 条 |

### 1.2.1 链路 A 的转换层

| | **RSSHub** | **NewsNow** |
|---|---|---|
| 端口 | `:1200` | `:5173`（**仅绑 IPv6 `[::1]`**，必须用 `localhost` 访问）|
| 开源许可 | AGPL-3.0 | MIT |
| 路由 / 源数量 | **4,010 个路由 / 1,941 个站点** | **48 个源** |
| 输出格式 | RSS / Atom XML | 私有 JSON |
| 缓存 | 内存，**5 分钟**（实测 `cache-control: max-age=300`）| SQLite，**30 分钟**（`shared/consts.ts:6 TTL = 30*60*1000`）|
| `summary` 正文 | ✅ **有**（会进详情页抓正文，如财联社 3,003 字符）| ⚠️ 参差不齐 |
| 反爬能力 | 动态 header 生成 + 78/4010 路由需浏览器内核 | 固定 UA + 签名算法（注释显示抄自 RSSHub）|

**抓取原理**（以财联社为例，读了两边源码）：

```typescript
// RSSHub lib/routes/cls/utils.ts
params = { appName: 'CailianpressWeb', os: 'web', sv: '8.7.9' }
① searchParams.sort()                    // 参数按 key 排序
② CryptoJS.SHA1(searchParams.toString()) // SHA1
③ CryptoJS.MD5(sha1)                     // 再 MD5
④ append('sign', md5)                    // 作为 sign 参数
```

> 同一个 API，RSSHub 会**再逐条进详情页**用 cheerio 解析 `__NEXT_DATA__` 抠正文（`lib/routes/cls/hot.ts:51-67`），NewsNow 只取列表（`server/sources/cls/index.ts:46-59`）。**这就是数据深度差异的来源。**

### 1.2.2 实测数据（本机，2026-10-02）

**NewsNow 5 个内置源直连实测**：

| 源 | 条数 | 有 title | 有 url | 有发布时间 | 最新一条 |
|---|---|---|---|---|---|
| `cls-hot` | 13 | ✅ | ✅ | ❌ | — |
| `xueqiu-hotstock` | 30 | ✅ | ✅ | ❌ | — |
| `jin10` | 25 | ✅ | ✅ | ✅ | **11 分钟前** |
| `wallstreetcn` | 30 | ✅ | ✅ | ✅ | 46 分钟前 |
| `gelonghui` | 15 | ✅ | ✅ | ✅ | 227 分钟前 |

**RSSHub 财经路由实测**：

| 路由 | 条数 | 最新一条 | 有正文 |
|---|---|---|---|
| `/cls/hot` | 13 | 404 分钟前 | ✅ 3,003 字符 |
| `/cls/depth` | 32 | **84 分钟前** | ✅ |
| `/wallstreetcn/news` | 26 | **45 分钟前** | ✅ |
| `/wallstreetcn/hot` | 10 | 438 分钟前 | ✅ |
| `/gelonghui/home` | 12 | — | ✅ |
| `/gelonghui/hot-article` | 10 | 594 分钟前 | ✅ |
| **`/eastmoney/search/<关键词>`** | **10** | **3.1 小时前**（比亚迪）| ✅ 42~147 字 |
| `/xueqiu/stock_info/<代码>` | 10 | 公告驱动（较旧）| ✅ |
| `/xueqiu/today` | 19 | — | ✅ |
| `/xueqiu/hots` | ❌ 503 | — | 上游 API 失败 |
| `/xueqiu/timeline` | ❌ 503 | — | 需登录 Cookie |

**落库后按源统计（真实链路，一次 fetch-enabled）**：

| 源 | 条数 | **有 summary** | 有时间 | 有 URL |
|---|---|---|---|---|
| 财联社深度（RSSHub）| 41 | ✅ **41（100%）** | 41 | 41 |
| 财联社快讯·见闻资讯（RSSHub）| 26 | ✅ **26（100%）** | 26 | 26 |
| 格隆汇首页（RSSHub）| 12 | ✅ **12（100%）** | 12 | 12 |
| NewsNow 华尔街见闻快讯 | 30 | ❌ **0（0%）** | 30 | 30 |
| NewsNow 金十数据 | 26 | ⚠️ **7（27%）** | 26 | 26 |
| NewsNow 格隆汇事件 | 15 | ✅ 15（100%） | 15 | 15 |

**结论：RSSHub 三个源共 79 条，`summary` 正文填充率 100%；NewsNow 参差不齐。**

### 1.2.3 一个关键发现：`symbol` 级 vs `market` 级

DSA 的资讯源有**作用域（scope）**概念：

- `scope_type = "market"` → 大盘级资讯（对所有股票生效）
- `scope_type = "symbol"` + `scope_value = "600519"` → **个股级资讯**（只对该股票生效）

**内置的 8 个模板全部是 `market` 级**，这意味着：默认情况下，**任何 A 股个股分析拿到的都是同一批大盘快讯**。

**解决方案（实测有效）**：用 RSSHub 的东财搜索路由建 `symbol` 级源：

```
url = http://127.0.0.1:1200/eastmoney/search/贵州茅台
scope_type = symbol
scope_value = 600519
```

实测落库后：

```
scope_type=market   scope_value=__dsa_null_scope__   278 条
scope_type=symbol   scope_value=600519                20 条   ← 个股级
```

**效果对比**（同一只股票，同一套 Prompt 模板）：

| | 只有 `market` 级 | 加入 `symbol` 级后 |
|---|---|---|
| 🚨 风险警报 | 只有技术面（均线/量能）| **多了「09-30 公司公告 i茅台APP及43家自营公司将于10-06暂停营业一天」** |
| 📢 最新动态 | 「近3日**未出现**与贵州茅台直接相关的公司层面新闻」| **「【最新消息】2026-09-30 贵州茅台酒销售有限公司公告…」** |

**这是这套系统最重要的设计点之一**：scope 粒度决定了注入信息的精确度。

---

# 第 2 部分：资讯怎么写入 DSA（含代码位置）

## 2.1 完整调用链

```
① 触发（两种自动入口 + 两种手动入口）
   ├─ 分析时自动：src/core/pipeline.py:683（个股）
   │               src/core/pipeline.py:1550（大盘复盘）
   │               src/market_analyzer.py:2080（大盘复盘）
   │   ⚠️ 问股（Agent 对话）**不在其中**：它走搜索 API，不读资讯池，详见 2.1 更正说明
   ├─ 手动单源：POST /api/v1/intelligence/sources/{id}/fetch
   └─ 手动全部：POST /api/v1/intelligence/sources/fetch-enabled
          ↓
② IntelligenceService.refresh_auto_sources()      ← src/services/intelligence_service.py:307
   三道闸门（详见 2.2）
          ↓
③ ensure_default_sources_enabled()                ← src/services/intelligence_service.py:204
   自动创建 / 启用内置源（详见 2.3）
          ↓
④ fetch_enabled_sources()                         ← src/services/intelligence_service.py:282
   遍历所有 enabled=True 的源
          ↓
⑤ _fetch_feed_entries()                           ← src/services/intelligence_service.py:446
   ├─ source_type == "newsnow" → _fetch_newsnow_entries()   ← :504
   └─ source_type == "rss"/"atom" → 解析 XML（RSS/Atom）
          ↓
⑥ 归一化为 FeedEntry{title, url, summary, published_at, source, ...}
          ↓
⑦ IntelligenceRepository.upsert_items()           ← src/repositories/intelligence_repo.py:108
   去重 + 插入/更新（详见 2.4）
          ↓
⑧ 落库表：intelligence_items
```

### ⚠️ 2.1.1 更正说明：问股（Agent 对话）**不使用**资讯池

**结论（2026-10-02 实测 + 源码核实）**：资讯池只喂给**个股分析**和**大盘复盘**两条分析链路，
**问股（Agent 对话）完全不读资讯池**。

**源码证据** —— 全仓库 `_load_persisted_intelligence_context` 只有两个调用点：

```
src/core/pipeline.py:683     ← 个股分析
src/core/pipeline.py:1550    ← 大盘复盘
```

`src/agent/` 整个目录下：`IntelligenceService`、`intelligence_items`、
`_load_persisted_intelligence_context` 的出现次数均为 **0**。
问股的 `news_context` 由 agent 自己的工具产出（`src/agent/tools/search_tools.py` 的
`search_comprehensive_intel`，底层是**搜索 API**），经
`src/agent/orchestrator.py` 的 `ctx.get_data("news_context")` 读入。

**实测证据**：

| 验证 | 结果 |
| --- | --- |
| 问股期间日志中「本地资讯证据池」出现次数 | **0** |
| 停掉 RSSHub / NewsNow 后问股的回答 | 「新闻与情报检索工具均不可用（**未配置搜索服务**）」——只反映搜索服务状态 |
| 停掉 RSSHub / NewsNow 后个股分析 | 仍能读到库内资讯并注入 ✅ |

**由此得出的两条运维结论：**

1. **停掉 RSSHub / NewsNow 不影响个股分析与大盘复盘** —— 注入读的是本地
   `intelligence_items`，转换层只是"进货渠道"。三层 fail-open 保证拉取失败不阻塞分析。
2. **停掉 RSSHub / NewsNow 会让问股彻底失去资讯能力** —— 问股只依赖搜索 API，
   而搜索 API（Bocha）目前处于注释关闭状态。

**项目自带文档存在不一致**：`docs/intelligence-sources.md` 第 115 行称
「Agent 分析同样通过 `news_context` 注入本地资讯证据，避免 Agent 必须重新搜索才能看到已沉淀新闻」，
与上述代码事实不符。**未确认这是文档写错，还是实现漏接了这条链路**——面试被问到时建议
按「已实测验证的是：个股分析与大盘复盘走资讯池，问股走搜索 API」来回答。

## 2.2 三道闸门（`refresh_auto_sources()`，`intelligence_service.py:307-356`）

```python
def refresh_auto_sources(self, *, force: bool = False) -> Dict[str, Any]:
    """Fail-open runtime refresh for opt-in local intelligence evidence."""

    # ── 闸门 1：总开关 ──────────────────────────────
    if not getattr(self.config, "news_intel_auto_fetch_enabled", False):
        return {"ok": True, "skipped": True, "reason": "disabled"}      # :309-310

    now = datetime.now()
    cls = type(self)
    with cls._auto_fetch_condition:

        # ── 闸门 2：进程内并发锁 ────────────────────
        waited_for_in_progress = False
        while cls._auto_fetch_in_progress:
            waited_for_in_progress = True
            cls._auto_fetch_condition.wait()                            # :316-318

        # ── 闸门 3：60 分钟冷却 ─────────────────────
        if (not force
                and cls._auto_fetch_last_run_at is not None
                and (now - cls._auto_fetch_last_run_at).total_seconds()
                    < _AUTO_FETCH_MIN_INTERVAL_SECONDS):                # :321-326
            return {"ok": True, "skipped": True, "reason": "cooldown"}

        cls._auto_fetch_in_progress = True

    result: Dict[str, Any]
    try:
        bootstrap = self.ensure_default_sources_enabled()               # :331
        fetch = self.fetch_enabled_sources()                            # :332
        result = {"ok": True, "skipped": False, "bootstrap": bootstrap,
                  "fetch": fetch, "saved_count": int(fetch.get("saved_count") or 0)}
    except Exception as exc:
        # ── fail-open：异常不阻塞主流程 ─────────────
        error = self._sanitize_error(exc)
        logger.warning("Intelligence auto fetch failed (fail-open): %s", error)  # :350
        result = {"ok": False, "skipped": False, "error": error}
    finally:
        with cls._auto_fetch_condition:
            cls._auto_fetch_last_run_at = datetime.now()                # :354
            cls._auto_fetch_last_result = dict(result)
            cls._auto_fetch_in_progress = False
```

**冷却常量的定义**（`intelligence_service.py:39`）：

```python
_AUTO_FETCH_MIN_INTERVAL_SECONDS = 60 * 60      # = 3600 秒 = 60 分钟
```

**要点**：
- `_auto_fetch_last_run_at` 是**类变量**，作用域是**进程内**——进程重启即重置。
- 冷却的目的是「**避免每只股票重复请求外部站点**」：一次分析 10 只股票，只会在第一次真正拉取。
- **fail-open** 是刻意的设计：资讯拉取失败绝不阻塞分析主流程。

## 2.3 自动建源（`ensure_default_sources_enabled()`，`intelligence_service.py:204-226`）

```python
def ensure_default_sources_enabled(self) -> Dict[str, Any]:
    """Create missing built-in sources and enable existing built-ins for auto mode."""
    created_count = 0
    enabled_count = 0
    errors = []
    templates = self._builtin_source_templates()
    for template in templates:
        name = str(template["name"])
        try:
            existing = self.repo.get_source_by_name(name)
            if existing is not None:
                if not existing.enabled:
                    self.repo.update_source_enabled(existing.id, True)  # ← 无条件复活
                    enabled_count += 1
                continue
            payload = {key: value for key, value in template.items()
                       if key != "template_id"}
            payload["enabled"] = True
            self.create_source(payload)                                 # ← 自动创建
            created_count += 1
        except Exception as exc:
            errors.append({"source": name, "error": self._sanitize_error(exc)})
    return {"created_count": ..., "enabled_count": ..., "errors": errors}
```

**8 个内置模板**（`_NEWSNOW_DEFAULT_SOURCE_DEFS` + 其他，实测建出的源）：

| 源名 | 类型 | market |
|---|---|---|
| SEC Latest Filings | rss | us |
| HKEX Market News | rss | hk |
| MarketWatch Top Stories | rss | global |
| NewsNow 财联社热门 | newsnow | cn |
| NewsNow 雪球热门股票 | newsnow | cn |
| NewsNow 华尔街见闻快讯 | newsnow | cn |
| NewsNow 金十数据 | newsnow | global |
| NewsNow 格隆汇事件 | newsnow | hk |

> ⚠️ **已知行为缺陷（实测确认，可讲）**：`update_source_enabled(existing.id, True)` 会**无条件复活**所有被手动禁用的内置源。
> 实测：接通时只启用 6 个源，跑过一次分析后变成 **11 个全部启用**，日志证据 `Intelligence auto fetch completed: sources=11 saved=78 created=0 enabled=5 errors=0` 里的 `enabled=5` 就是被复活的 5 个。
> 影响：`SEC` / `HKEX` / `MarketWatch` 这三个美港股源会被强行拉起（实测 HKEX 抓取失败），`财联社热门` / `雪球热门股票` 这两个**无 `published_at`** 的源会破坏时效过滤。

## 2.4 落库去重（`upsert_items()`，`intelligence_repo.py:108-149`）

```python
def upsert_items(self, items: Iterable[Dict[str, Any]]) -> int:
    saved = 0
    with self.db.get_session() as session:
        for fields in items:
            url = (fields.get("url") or "").strip()
            title = (fields.get("title") or "").strip()
            if not url or not title:
                continue                                    # ← 无 url / 无标题直接丢弃
            item_fields = dict(fields)
            scope_value = self._normalize_scope_value(item_fields.get("scope_value"))
            item_fields["scope_value"] = scope_value
            source_id = item_fields.get("source_id")

            # ── 去重键：6 个字段的组合 ──────────────────
            conditions = [
                IntelligenceItem.url         == url,
                IntelligenceItem.source_type == (item_fields.get("source_type") or "rss"),
                IntelligenceItem.scope_type  == (item_fields.get("scope_type") or "market"),
                IntelligenceItem.market      == (item_fields.get("market") or "cn"),
            ]
            if source_id is None:
                conditions.append(IntelligenceItem.source_id.is_(None))
                conditions.append(IntelligenceItem.source_name == item_fields.get("source_name"))
            else:
                conditions.append(IntelligenceItem.source_id == source_id)
            conditions.append(IntelligenceItem.scope_value == scope_value)

            existing = session.execute(
                select(IntelligenceItem).where(and_(*conditions)).limit(1)
            ).scalar_one_or_none()

            if existing is not None:
                # ── 已存在 → 只更新 5 个字段，不计入 saved ──
                existing.summary      = item_fields.get("summary") or existing.summary
                existing.source       = item_fields.get("source") or existing.source
                existing.published_at = item_fields.get("published_at") or existing.published_at
                existing.fetched_at   = item_fields.get("fetched_at") or datetime.now()
                existing.raw_payload  = item_fields.get("raw_payload") or existing.raw_payload
                continue
            try:
                with session.begin_nested():
                    session.add(IntelligenceItem(**item_fields))
                    session.flush()
                saved += 1                                  # ← 只有新增才 +1
            except IntegrityError:
                continue
        session.commit()
```

**去重键（面试可直接背）**：

```
url + source_type + scope_type + market + source_id(或 source_name) + scope_value
```

**这解释了日志里 `saved=78` / `saved=50` 的含义**——那是**新增**数；重复条目走「更新」分支，不计数。**也说明反复调 `fetch-enabled` 不会让数据暴涨**：同一 URL 在同一 scope 下永远只有一行。

## 2.5 落库表 `intelligence_items` 的字段

```sql
intelligence_items (
    id, source_id, source_name, source_type,
    title, summary, url, source,
    published_at, fetched_at,
    scope_type, scope_value, market,
    raw_payload
)
```

| 字段 | 作用 |
|---|---|
| `source_id` / `source_name` / `source_type` | 来源标识（`rss` / `atom` / `newsnow`）|
| `title` / `summary` / `url` / `source` | 内容（**`summary` 决定 LLM 能看到多少**）|
| `published_at` | 发布时间（**时效过滤依赖它**）|
| `fetched_at` | 抓取时间 |
| `scope_type` / `scope_value` | 作用域（`symbol` / `market` + 具体值）|
| `market` | 市场（`cn` / `hk` / `us` / `global`）|
| `raw_payload` | 原始载荷（便于回溯）|

**相关表**：

```sql
intelligence_sources (
    id, name, source_type, url, enabled,
    scope_type, scope_value, market, description,
    last_status, last_error, last_fetched_at,
    created_at, updated_at
)
```

**数据保留策略**（实测：只有 `intelligence_items` 有自动清理）：

| 表 | 自动清理 | 机制 |
|---|---|---|
| `intelligence_items` | ✅ **30 天** | `intelligence_repo.apply_retention()`（由 `intelligence_service.py:263` 调用）|
| `analysis_history` | ❌ 只能手动 | `storage.delete_analysis_history_records(ids)` |
| `news_intel` | ❌ 无 | — |
| `llm_usage` | ❌ 无 | — |
| `logs/` | ✅ 自动轮转 | 常规 `10MB×5` / 调试 `50MB×3`（`src/logging_config.py:139-153`）|

> ⚠️ **命名陷阱**：配置项 `NEWS_INTEL_RETENTION_DAYS=30` 清的是 **`intelligence_items`**（intel**ligence**），**不是** `news_intel`。两个名字高度相似但表不同。

---

# 第 3 部分：怎么注入到 LLM（重点）

## 3.1 入口：两处调用点

```python
# src/core/pipeline.py:683  —— 个股分析
persisted_intelligence_context = self._load_persisted_intelligence_context(
    code=..., stock_name=..., market=...
)

# src/core/pipeline.py:1550 —— 大盘复盘
persisted_intelligence_context = self._load_persisted_intelligence_context(...)
```

## 3.2 核心方法（`pipeline.py:3019-3070+`）

```python
def _load_persisted_intelligence_context(
    self, *, code: str, stock_name: str, market: str, limit: int = 6,
) -> Optional[str]:
    """Load locally persisted intelligence as fail-open evidence context."""
    try:
        service = IntelligenceService(config=self.config)
        service.refresh_auto_sources()                          # ① 先尝试刷新（受三道闸门约束）
        days = max(1, int(self.config.get_effective_news_window_days() or 1))  # ② 时效窗口
        collected: list[Dict[str, Any]] = []
        seen_urls: set[str] = set()

        # ③ 两级 scope 查找：先 symbol（该股票），再 market（大盘）兜底
        symbol_filters = [
            {"scope_type": "symbol", "scope_value": scope_value, "market": market}
            for scope_value in _symbol_scope_lookup_values(code, market)
        ]
        for filters in symbol_filters + [{"scope_type": "market", "market": market}]:
            payload = service.list_items(published_days=days, page=1,
                                        page_size=limit, **filters)
            for item in payload.get("items", []):
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "")
                if url in seen_urls:                            # ④ URL 去重
                    continue
                seen_urls.add(url)
                collected.append(item)
                if len(collected) >= limit:                     # ⑤ 最多 6 条
                    break
            if len(collected) >= limit:
                break

        if not collected:
            return None

        # ⑥ 格式化成 Prompt 段落
        lines = [f"## 本地资讯证据池（{stock_name}/{code}）"]
        for idx, item in enumerate(collected[:limit], 1):
            title     = str(item.get("title") or "未命名资讯").strip()
            summary   = str(item.get("summary") or "").strip()
            source    = str(item.get("source") or item.get("source_name") or "local-intel").strip()
            published = str(item.get("published_at") or "").strip()
            url       = str(item.get("url") or "").strip()
            meta = " / ".join(part for part in (source, published) if part)
            lines.append(f"{idx}. {title}" + (f"（{meta}）" if meta else ""))
            if summary:                                          # ★ 关键：有 summary 才追加
                ...
```

**五个设计要点**：

| # | 要点 | 值 / 说明 |
|---|---|---|
| ① | 先尝试刷新 | 受 §2.2 三道闸门约束（总开关 / 并发锁 / 60 分钟冷却）|
| ② | 时效窗口 | `get_effective_news_window_days()`，默认 `NEWS_MAX_AGE_DAYS=3` |
| ③ | **两级 scope 查找** | **先 `symbol`（该股票），查不到再 `market`（大盘）兜底** |
| ④ | URL 去重 | `seen_urls` set |
| ⑤ | **最多 6 条** | `limit=6` |
| ⑥ | 格式化 | `## 本地资讯证据池（股票名/代码）` |

> ★ **`summary` 字段直接决定 LLM 能看到多少信息**。这就是 RSSHub（正文 100% 填充）与 NewsNow（0%~100%）在 DSA 里的实际差距所在。

## 3.3 与搜索 API 结果的拼接（`pipeline.py:682-754`）

```python
# pipeline.py:682
news_context = None

# pipeline.py:683
persisted_intelligence_context = self._load_persisted_intelligence_context(...)

# ... 中间是链路 B（搜索 API）的调用 ...

# pipeline.py:709
news_context = self.search_service.format_intel_report(intel_results, stock_name)

# ... 中间可能拼接社交情绪 ...

# pipeline.py:750-754  ★ 拼接点：搜索在前，资讯池在后
if persisted_intelligence_context:
    news_context = (
        f"{news_context}\n\n{persisted_intelligence_context}"
        if news_context
        else persisted_intelligence_context
    )
```

**顺序很重要**：**搜索 API 结果在前，本地资讯池在后**。两者拼接成同一个 `news_context` 字符串。

## 3.4 真实 Prompt 原文（从日志捞取，完整贴出）

以下是从 `logs/stock_analysis_debug_20261002.log` 中提取的**真实注入段落**（贵州茅台 600519，2026-10-02）：

````text
## 本地资讯证据池（贵州茅台/600519）
1. 贵州茅台 旗下i 茅台 APP和43家自营公司将于10月6日暂停营业一天（东财个股·贵州茅台 / 2026-09-30T05:53:36）
   摘要：南方财经9月30日电，9月30日， 贵州茅台 酒销售有限公司官微消息，计划于2026年10月6日全天对i茅台APP进行维护升级。在此期间，i茅台APP将暂停使用，需依托i茅台APP办理的线下相关服务同步暂停。此外， 贵州茅台 43家自营公司也将于10月6日暂停营业一天。
   来源：http://finance.eastmoney.com/a/202609303887625272.html
2. 欧股开盘走高，欧洲斯托克50指数上涨0.4%，德国DAX指数上涨0.3%，英国富时100指数上涨0.2%，法国CAC40指数上涨0.2%。（NewsNow 华尔街见闻快讯 / 2026-10-02T07:01:12）
   来源：https://wallstreetcn.com/livenews/3173618
3. 加拿大拟建石油运输管道降低对美依赖（NewsNow 华尔街见闻快讯 / 2026-10-02T06:58:51）
   来源：https://wallstreetcn.com/livenews/3173617
4. 普京：很快所有人都得开中国车（财联社深度 / 2026-10-02T06:49:43）
   摘要：据中国新闻社，10月1日，俄罗斯总统普京在“瓦尔代”国际辩论俱乐部全体会议上表示，由于欧洲汽车市场正陷入深度衰退，很快所有车主都可能换开中国汽车。 普京表示：“欧洲的汽车工业在哪里？企业正在关闭。我们很快都将开上中国汽车。这是好是坏我不知道，但便宜且质量好。” 普京驳斥了西方国家关于中国汽车“产能过剩”的指责，并提醒市场的基本原则——最终结果永远由消费者需求决定。他强调，如今不仅欧洲汽车工业处于深度危机和严重困境，欧洲的冶金、相关行业以
   来源：https://www.cls.cn/detail/2497224
5. 民商火箭位次出炉：SpaceX第一、中科宇航第二（格隆汇首页 / 2026-10-02T06:38:17）
   摘要：国内头部梯队成型 海外航天数据平台Flight Atlas显示，截至9月28日统计，今年全球航天发射已超170次，入轨总质量接近1900吨。 SpaceX星舰第14次任务单次完成约52吨载荷入轨，大运力低成本航天趋势加速显现，猎鹰9号依旧保持全球领先地位。 &nbsp;海外巨头掀起运力竞赛背景下，国内民商航天加速追赶。 中科宇航前三季度入轨载荷约9吨，国内民营火箭排名第一、全球民商火箭第二位。 东方空间、蓝箭航天、星河动力相继实现入轨，
   来源：https://www.gelonghui.com/p/6869920
6. 核心通胀2.8%粘性未消、出口1209亿美元创纪录，韩国央行11月加息预期升温（财联社快讯·见闻资讯 / 2026-10-02T06:35:18）
   摘要：韩国9月整体通胀如
````

**观察**：
- **第 1 条是 `symbol` 级**（东财个股·贵州茅台）——证明两级查找的 symbol 优先确实生效
- 第 2~6 条是 `market` 级——symbol 只有 1 条符合时效窗口，剩余由 market 兜底
- 总数 6 条 —— 与 `limit=6` 一致
- **格式**：`{序号}. {标题}（{来源} / {发布时间}）` + `   摘要：{正文}` + `   来源：{URL}`

## 3.5 注入到 Prompt 模板

**`_format_prompt()` 定义在 `src/analyzer.py:4313`**：

```python
def _format_prompt(
    self,
    context: Dict[str, Any],
    name: str,
    news_context: Optional[str] = None,
    report_language: str = "zh",
    analysis_context_pack_summary: Optional[str] = None,
) -> str:
    """
    格式化分析提示词（决策仪表盘 v2.0）
    包含：技术指标、实时行情（量比/换手率）、筹码分布、趋势分析、新闻
    """
```

**`{news_context}` 的两个注入点**：

| 位置 | 用途 |
|---|---|
| `src/analyzer.py:4904` | 英文模板（`report_language in ("en", "ko")`）|
| `src/analyzer.py:4919` | **中文模板**（默认）|

**中文模板实际片段**（`analyzer.py:4907-4921`）：

```python
elif news_context:
    prompt += f"""
以下是 **{stock_name}({code})** 近{news_window_days}日的新闻搜索结果，请重点提取：
1. 🚨 **风险警报**：减持、处罚、利空
2. 🎯 **利好催化**：业绩、合同、政策
3. 📊 **业绩预期**：年报预告、业绩快报
4. 🕒 **时间规则（强制）**：
   - 输出到 `risk_alerts` / `positive_catalysts` / `latest_news` 的每一条都必须带具体日期（YYYY-MM-DD）
   - 超出近{news_window_days}日窗口的新闻一律忽略
   - 时间未知、无法确定发布日期的新闻一律忽略

```
{news_context}
```
"""
```

**⚠️ 注意**：`{news_context}` 只是**内容占位符**。真正把 `news_context` 送进 LLM 的路径是 `analyzer.py:281`：

```python
# src/analyzer.py:281（_legacy_audit_marker_specs 内）
news_marker = "## 📰 News Intelligence" if english else "## 📰 舆情情报"
add("news_context", news_marker if news_context else None)
```

也就是说：**`news_context` 会以「`## 📰 舆情情报` 标记 + 内容」的形式进入 user 消息**。

## 3.6 ⚠️ 三个容易混淆的字段（面试会被追问）

| 字段 | 类型 | 含义 | 代码位置 |
|---|---|---|---|
| `news_context` | `Optional[str]` | **本次实际传入的消息面文本**。来源可以是搜索 API、**本地资讯池**、社交情绪 | `analyzer.py:252`, `:4317`, `:4151` |
| `search_performed` | `bool` | **是否执行了联网搜索**（只看链路 B）| `analyzer.py:1770`，赋值 `:4236` `result.search_performed = bool(news_context)` |
| `news_evidence_present` | `bool` | **本次收到的 `news_context` 是否非空**（不区分来源）| `analyzer.py:1782` |

**关键区别**（源码注释原文，`analyzer.py:1777-1782`）：

> 本次分析实际收到的消息面证据（news_context）是否非空。
> ……「新闻面证据」——两者是不同命题：`news_context` 还可能来自社交情绪或本地已落库的……

**为什么这个区分重要**：如果本地资讯池提供了证据但**没有走搜索 API**，那么：

```
search_performed      = False    ← 没搜网
news_evidence_present = True     ← 但有证据（来自本地池）
```

**这两个字段在报告里会体现为不同的语义**，不能混用。

---

# 第 4 部分：生成了哪些文件和调用

## 4.1 报告文件

| 文件 | 生成者 | 本机实测大小 |
|---|---|---|
| `reports/report_YYYYMMDD.md` | 个股分析日报（`src/notification.py:2922`）| **6,877 字节** |
| `reports/market_review_YYYYMMDD.md` | 大盘复盘（同上报，`main.py` 另存）| **8,087 字节** |

日志中的确认行：

```
src.notification | src/notification.py:2922 | 日报已保存到: .../reports/report_20261002.md
src.core.pipeline | src/core/pipeline.py:3755 | 决策仪表盘日报已保存: .../reports/report_20261002.md
```

## 4.2 数据库写入

单一 SQLite 文件：`data/stock_analysis.db`（实测 1.28 MB，32 张表）

| 表 | 写入内容 | 本机行数 |
|---|---|---|
| **`intelligence_items`** | 链路 A 的资讯池 | **298** |
| **`news_intel`** | 链路 B 的搜索结果 | 10 |
| **`analysis_history`** | 每次分析的完整结果 | 7 |
| **`llm_usage`** | 每次 LLM 调用的 token 用量（**46 个字段**，含缓存归一化字段）| 25 |
| `stock_daily` | 日线 K 线缓存 | 222 |
| `intelligence_sources` | 资讯源配置 | 13 |
| `conversation_messages` | 问股对话消息 | 10 |

**`analysis_history` 单行约 46 KB**（实测 51 KB/行），构成：

```
raw_result        18,779 字符   ← LLM 原始返回（完整 JSON）
context_snapshot  23,376 字符   ← 分析上下文（K线+指标+新闻）
news_content       3,571 字符   ← 喂进去的新闻原文
analysis_summary     176 字符
```

> ⚠️ **这是数据库增长的主要来源**，且**没有自动清理策略**（只有 `intelligence_items` 有 30 天 retention）。

## 4.3 LLM 调用：三种 `call_type`

`llm_usage.call_type` 有三个取值（实测）：

| `call_type` | 触发场景 | 实测次数 | 说明 |
|---|---|---|---|
| **`agent`** | **Agent 问股**（Web 对话页）| **18** | 多轮对话，每轮携带完整历史 |
| **`analysis`** | **个股分析** | **4** | 单次调用 |
| **`market_review`** | **大盘复盘** | **3** | 单次调用 |

## 4.4 日志文件

| 文件 | 实测大小 | 内容 |
|---|---|---|
| `logs/stock_analysis_YYYYMMDD.log` | **159,185 字节** | 常规分析日志 |
| `logs/stock_analysis_debug_YYYYMMDD.log` | **4,603,335 字节（4.6 MB）** | 调试日志（含完整 Prompt、原始响应）|
| `logs/web_server_YYYYMMDD.log` | **72,479 字节** | Web 服务常规日志 |
| `logs/web_server_debug_YYYYMMDD.log` | **199,582 字节** | Web 服务调试日志 |

**轮转配置**（`src/logging_config.py:139-153`）：

```python
file_handler  = RotatingFileHandler(..., maxBytes=10 * 1024 * 1024, backupCount=5)  # 常规 10MB×5
debug_handler = RotatingFileHandler(..., maxBytes=50 * 1024 * 1024, backupCount=3)  # 调试 50MB×3
```

---

# 第 5 部分：LLM 与代币（面试必问）

## 5.1 用的是什么模型

**DeepSeek**，模型标识 `deepseek/deepseek-chat`（经 LiteLLM 网关调用）。

日志确认：

```
src.analyzer | src/analyzer.py:4217 | [LLM返回] deepseek/deepseek-chat 响应成功, 耗时 19.03s, 响应长度 7119 字符
```

**实测响应耗时**：18.05s ~ 26.58s（多次运行区间）。

## 5.2 什么是代币（token）

> **代币（token）= LLM 的计费单位。** 它不是"字"，而是模型分词器切出来的最小语义单元。中文大约 1 个字 ≈ 0.6~1 个 token，英文大约 4 个字符 ≈ 1 个 token。

代币分两类：

| 类型 | 英文 | 含义 |
|---|---|---|
| **提示令牌** | `prompt_tokens` / input tokens | **你发给模型的内容**（system prompt + 上下文 + 用户问题）|
| **完成令牌** | `completion_tokens` / output tokens | **模型生成的内容**（回答）|

`total_tokens = prompt_tokens + completion_tokens`

**为什么这个区分重要**：两者**计价不同**，而且**输出通常比输入贵数倍**。

## 5.3 Key 从哪来，消耗谁的账号

**Key 来源**：

```
~/.dsh/.credentials.yaml  →  DEEPSEEK_API_KEY
                              ↓ 写入
daily_stock_analysis/.env:176  →  DEEPSEEK_API_KEY=***
```

- `.env` 已被 `.gitignore` 忽略（`.gitignore:4:*.env`），不会进版本库
- **消耗的是用户自己的 DeepSeek 账号**（platform.deepseek.com，**预付费**模式）

## 5.4 实测用量（本机 `llm_usage` 表，2026-10-02）

| `call_type` | 次数 | 提示令牌 | 完成令牌 | **合计** | 占比 |
|---|---|---|---|---|---|
| **`agent`**（问股）| 18 | **248,526** | 11,903 | **260,429** | **82.6%** |
| `analysis`（个股分析）| 4 | 23,913 | 20,353 | **44,266** | 14.0% |
| `market_review`（大盘复盘）| 3 | 3,804 | 6,756 | **10,560** | 3.4% |
| **总计** | **25** | **276,243** | **39,012** | **315,255** | 100% |

**这些数字的 SQL 复现方式**：

```sql
SELECT call_type,
       COUNT(*)                        AS calls,
       SUM(COALESCE(prompt_tokens,0))  AS prompt_tokens,
       SUM(COALESCE(completion_tokens,0)) AS completion_tokens,
       SUM(COALESCE(total_tokens,0))   AS total_tokens
FROM llm_usage
GROUP BY call_type
ORDER BY total_tokens DESC;
```

## 5.5 为什么「问股」占了 83%（面试重点）

**核心原因：多轮对话每轮都携带完整历史上下文，输入 token 快速累积。**

```
第 1 轮：  system + 用户问题           → 输入 ~3,000 tokens
第 2 轮：  system + 第1轮全部 + 新问题  → 输入 ~6,000 tokens
第 3 轮：  system + 前2轮全部 + 新问题  → 输入 ~9,000 tokens
...
第 N 轮：  输入 ≈ N × 单轮增量
```

**输入 token 随轮数近似线性增长（累加），总输入 ≈ O(N²)。**

对比三种调用：

| | 调用模式 | 输入规模 |
|---|---|---|
| `agent` 问股 | **多轮对话**，每轮重发全部历史 | **随轮数累加（平方级）** |
| `analysis` 个股分析 | **单次调用**，Prompt 固定（技术面 + 资讯池）| 固定（实测均 ~6,000 提示令牌/次）|
| `market_review` 大盘复盘 | **单次调用** | 固定（实测均 ~1,268 提示令牌/次）|

**实测印证**：

- `agent`：248,526 ÷ 18 = **均 13,807 提示令牌/次**
- `analysis`：23,913 ÷ 4 = **均 5,978 提示令牌/次**
- `market_review`：3,804 ÷ 3 = **均 1,268 提示令牌/次**

**问股单次输入的均值是个股分析的 2.3 倍**，因为它是多轮累加的结果。

## 5.6 价格

**DeepSeek-V4-Pro 定价**（来源：21 经济网 2026-05-23 报道）：

| 计费项 | 价格（元 / 百万 tokens）|
|---|---|
| 输入 · **缓存命中** | **0.025** |
| 输入 · **缓存未命中** | **3** |
| 输出 | **6** |

**⚠️ 注意**：以上价格来自 21 经济网报道，**本会话未在 DeepSeek 官网直接核实**，属于「引用第三方报道」而非一手确认。面试时如被追问价格来源，应如实说明。

**缓存命中的意义**：命中缓存时输入价格是未命中的 **1/120**（0.025 vs 3）。这对**多轮对话**极其关键——因为问股每轮都在重发相同的历史前缀，缓存命中率高的话成本会大幅下降。

> 这也解释了为什么 `llm_usage` 表有 **46 个字段**，其中大量是缓存归一化字段：
> `normalized_cache_read_tokens`、`normalized_cache_write_tokens`、`normalized_cache_miss_tokens`、
> `normalized_uncached_input_tokens`、`normalized_cache_eligible_input_tokens`、
> `normalized_cache_hit_ratio`、`normalized_cache_write_ratio`、`cache_capability`、
> `cache_eligibility`、`cache_observation`、`provider_reported_cached_tokens` …
>
> **这套字段设计的目的就是精确区分「缓存命中 / 未命中」的输入 token，从而算准成本。** 这是可以在面试里展开讲的工程细节。

## 5.7 用量界面

Web 界面上的「用量」页数据来自 **`llm_usage` 表**，由 `api/v1/endpoints/usage.py` 聚合：

| 端点 | 响应模型 |
|---|---|
| `GET ...`（`usage.py:67`）| `UsageSummaryResponse` |
| `GET ...`（`usage.py:83`）| `UsageDashboardResponse` |

---

# 第 6 部分：端到端总览图

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ ① 外部数据源（公网，不受我们控制）                                              │
│                                                                                │
│  行情：东方财富 / 新浪 / 腾讯 / 通达信 / 证券宝 / Yahoo …                        │
│  资讯：财联社 cls.cn    金十 jin10    雪球 xueqiu    华尔街见闻 wallstreetcn     │
│        格隆汇 gelonghui  东方财富 eastmoney   SEC / HKEX / MarketWatch …        │
└───────────────────────────┬───────────────────────────────────────────────────┘
                            │
        ┌───────────────────┴────────────────────┐
        │                                        │
        ▼ 行情线                                  ▼ 资讯线
┌──────────────────────────┐        ┌────────────────────────────────────────────┐
│ data_provider/ 13 个 fetcher │        │ ② 资讯转换层（自建，可插拔）                │
│ 优先级 P0→P5：              │        │                                            │
│  Efinance(P0)              │        │  ┌─────────────────┐  ┌─────────────────┐  │
│  Akshare(P1)               │        │  │ RSSHub  :1200   │  │ NewsNow :5173   │  │
│  Pytdx(P2)                 │        │  │ 4,010 路由      │  │ 48 个源         │  │
│  Baostock(P3)              │        │  │ RSS/Atom XML    │  │ 私有 JSON       │  │
│  Yfinance(P4)              │        │  │ 缓存 5 分钟      │  │ 缓存 30 分钟    │  │
│  Tencent(P5)               │        │  │ ✅ 有正文        │  │ ⚠️ 正文参差     │  │
│  失败自动降级 → 下一源       │        │  └─────────────────┘  └─────────────────┘  │
└──────────┬───────────────┘        └────────────────────┬───────────────────────┘
           │                                             │
           │                       ┌─────────────────────┴──────────────────────┐
           │                       │ ③ DSA 资讯写入（第 2 部分）                   │
           │                       │                                             │
           │                       │  refresh_auto_sources()   ← 三道闸门         │
           │                       │    ├ 总开关 NEWS_INTEL_AUTO_FETCH_ENABLED   │
           │                       │    ├ 进程内并发锁                            │
           │                       │    └ 60 分钟冷却                             │
           │                       │  ensure_default_sources_enabled() ← 自动建源 │
           │                       │  _fetch_feed_entries()                       │
           │                       │    ├ newsnow → 解析 JSON                     │
           │                       │    └ rss     → 解析 XML                      │
           │                       │  repo.upsert_items()  ← 6 字段去重键          │
           │                       └─────────────────────┬───────────────────────┘
           │                                             │
           │                       ┌─────────────────────▼───────────────────────┐
           │                       │ 链路 A 落库: intelligence_items              │
           │                       │   （298 条 | 30 天 retention）               │
           │                       └─────────────────────┬───────────────────────┘
           │                                             │
           │        ┌────────────────────────────────────┘
           │        │
           ▼        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ ④ Prompt 组装（第 3 部分）                                                      │
│                                                                                │
│  _load_persisted_intelligence_context()   ← pipeline.py:3019                    │
│    ① refresh_auto_sources()               ← 先尝试刷新                          │
│    ② 时效窗口 NEWS_MAX_AGE_DAYS=3                                               │
│    ③ 两级 scope 查找：symbol（该股票）→ market（大盘兜底）                       │
│    ④ URL 去重                                                                   │
│    ⑤ 最多 6 条                                                                  │
│    ⑥ 格式化 → "## 本地资讯证据池（股票名/代码）"                                  │
│                                                                                │
│  链路 B：search_service.format_intel_report()  → news_intel 表                  │
│                                                                                │
│  拼接（pipeline.py:750-754）：                                                  │
│      news_context = 搜索API结果 + "\n\n" + 本地资讯池                            │
│                     ↑ 搜索在前        ↑ 资讯池在后                               │
│                                                                                │
│  _format_prompt()  ← analyzer.py:4313                                          │
│      └ {news_context} 注入点 ← analyzer.py:4904(英) / 4919(中)                   │
│      └ 同时以 "## 📰 舆情情报" 标记进入 user 消息 ← analyzer.py:281               │
└───────────────────────────────┬───────────────────────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ ⑤ LLM 调用（LiteLLM 网关 → DeepSeek）                                          │
│                                                                                │
│   模型：deepseek/deepseek-chat    实测单次 18~27 秒                             │
│   三种 call_type：                                                              │
│     agent          (问股，多轮对话)  → 260,429 令牌 / 18 次  (82.6%)            │
│     analysis       (个股分析，单次)  →  44,266 令牌 /  4 次                     │
│     market_review  (大盘复盘，单次)  →  10,560 令牌 /  3 次                     │
│                                      ─────────────────────────                  │
│                                      合计 315,255 令牌 / 25 次                  │
│                                                                                │
│   token 计费：提示令牌(prompt) + 完成令牌(completion)                            │
│   缓存命中 0.025 元/百万 vs 未命中 3 元/百万（差 120 倍）← 对多轮对话影响巨大      │
└───────────────────────────────┬───────────────────────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
┌────────────────┐   ┌────────────────────┐   ┌──────────────────────┐
│ ⑥ 报告文件      │   │ ⑦ 数据库写入        │   │ ⑧ LLM 用量记录        │
│                │   │                    │   │                      │
│ reports/       │   │ analysis_history   │   │ llm_usage            │
│  report_       │   │  （单行 ~46KB）     │   │  （46 个字段，        │
│   YYYYMMDD.md  │   │ news_intel         │   │    含缓存归一化）      │
│  market_review_│   │ intelligence_items │   │                      │
│   YYYYMMDD.md  │   │ stock_daily        │   │ → api/v1/endpoints/  │
│                │   │ conversation_      │   │    usage.py 聚合      │
│ (6.9KB/8.1KB)  │   │  messages          │   │ → Web「用量」页       │
└────────────────┘   └────────────────────┘   └──────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ ⑨ 呈现层                                                                        │
│   FastAPI (:8010) + React Web UI（首页/问股/持仓/回测/告警/用量/设置）           │
│   日志：logs/stock_analysis_*.log、logs/web_server_*.log（自动轮转）             │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

# 第 7 部分：面试可讲的技术亮点

以下是这个项目**真正值得讲**的技术点——每一条都有实测数据或源码支撑，不是包装。

## 亮点 1：可插拔的多源资讯架构（协议适配 + 双解析器）

**问题**：外部资讯站格式各异——有的是 RSS，有的是私有 JSON API。

**设计**：`_ALLOWED_SOURCE_TYPES = {"rss", "atom", "newsnow"}`，统一抽象成「源」概念，`_fetch_feed_entries()` 按 `source_type` 分派到两个解析器：

```python
def _fetch_feed_entries(self, fields, *, limit):
    if fields["source_type"] == "newsnow":
        return self._fetch_newsnow_entries(fields, limit=limit)   # 解析 JSON
    # else: 解析 XML（RSS / Atom）
```

**价值**：新增一种源类型只需加一个解析器，不动数据模型、不动注入逻辑。**实测同时跑通了 RSSHub（RSS）和 NewsNow（JSON）两种协议**。

---

## 亮点 2：SSRF 防护设计（多层校验，且做成可配置）

**问题**：允许用户自定义资讯源 URL，等于开放了一个"让服务器替你发请求"的入口——典型 SSRF 攻击面。

**设计**（`intelligence_service.py:392-434`, `:560-594`）：

```python
_PRIVATE_HOSTNAMES = {"localhost", "localhost.localdomain"}

# ① 只允许 http/https 绝对 URL
# ② 禁止 URL 携带 username/password
# ③ hostname 黑名单（localhost / .local）
# ④ IP 字面量 → _is_blocked_ip() 校验
# ⑤ 域名 → DNS 解析，所有结果都不能是内网 IP
# ⑥ 必须至少有一个公网地址

def _is_blocked_ip(ip):
    return (not ip.is_global          # ← 最严格
            or ip.is_private          # 含 100.64/10 CGNAT
            or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast)

# ⑦ 二次 DNS 校验（防 DNS rebinding）：运行时 monkey-patch socket.getaddrinfo
def _get_with_validated_dns(self, raw_url, **kwargs):
    def guarded_getaddrinfo(host, port, *args, **inner_kwargs):
        addrinfos = original_getaddrinfo(host, port, *args, **inner_kwargs)
        if self._normalize_hostname(host) == target_hostname:
            self._validate_addrinfos(addrinfos)      # 二次校验
        return addrinfos
```

**额外的细节**：
- **显式禁用环境代理**（`_DISABLE_REQUEST_PROXIES = {"http": None, "https": None}`），防止攻击者通过 `HTTP_PROXY` 绕过校验
- 重定向后的最终 URL **再校验一次**

**我们的实际工作**：为了接本机自托管资讯源，我加了一个**显式 opt-in 开关** `NEWS_INTEL_ALLOW_PRIVATE_HOSTS`（默认 `false`），只在开启时跳过 ③⑤⑦，**① scheme 校验② 凭据校验和禁用代理仍生效**。同步更新了 `.env.example`、`docs/intelligence-sources.md`、`docs/CHANGELOG.md`。

**这个点可以讲的深度**：安全控制不是「有/无」二元，而是**默认严格 + 显式放宽 + 放宽后仍保留其余防线**。这是工程判断力。

---

## 亮点 3：两级 scope 查找（精确优先，宽泛兜底）

**问题**：同一个资讯池里既有"茅台自己的新闻"也有"大盘快讯"，怎么决定给 LLM 喂哪些？

**设计**（`pipeline.py:3034-3051`）：

```python
symbol_filters = [{"scope_type": "symbol", "scope_value": v, "market": market}
                  for v in _symbol_scope_lookup_values(code, market)]

for filters in symbol_filters + [{"scope_type": "market", "market": market}]:
    payload = service.list_items(published_days=days, page=1, page_size=limit, **filters)
    ...
    if len(collected) >= limit:
        break
```

**语义**：先查 `symbol` 级（这只股票自己的资讯）→ 不足 6 条时用 `market` 级（大盘）补齐 → 累计到 6 条即停。

**实测效果**：
- 只有 `market` 级时，报告写「**未出现**与贵州茅台直接相关的公司层面新闻」
- 加入 `symbol` 级后，报告出现「**09-30 公司公告 i茅台APP及43家自营公司将于10-06暂停营业一天**」

**这个设计的可讲性**：它是一个**信息检索的优先级策略**（specific-first, fallback-to-generic），而且**上限（limit=6）同时约束了两级**，避免大盘快讯把个股新闻挤出去。

---

## 亮点 4：基于组合键的幂等 upsert（去重 + 增量更新）

**问题**：同一批资讯会被反复拉取（每 60 分钟冷却后、每次分析时）。如何保证不重复又能更新？

**设计**（`intelligence_repo.py:108-149`）：用 **6 字段组合键**判重：

```
url + source_type + scope_type + market + source_id(或 source_name) + scope_value
```

- **命中** → 只更新 5 个字段（`summary` / `source` / `published_at` / `fetched_at` / `raw_payload`），**不计入新增**
- **未命中** → `INSERT`，`saved++`
- **`IntegrityError`** → 静默跳过（并发/竞态兜底）
- **无 `url` 或无 `title`** → 直接丢弃

**价值**：
1. **幂等**——反复拉取不会产生重复行
2. **增量**——已存在条目的 `summary` 会被补全（即使上游先返回了空 `summary`）
3. **可观测**——`saved_count` 精确表达"净新增"，日志 `saved=78` 有明确含义

**这个点可以讲的**：判重键为什么是 6 个字段而不是只有 `url`？因为**同一个 URL 在不同 scope / market / 源下应当是不同记录**（比如同一条新闻既属于"大盘"又属于某只股票）。

---

## 亮点 5：fail-open 降级链（三层都不阻塞主流程）

**这个系统里 fail-open 出现了三层**：

| 层级 | 代码位置 | 行为 |
|---|---|---|
| **源级** | `_fetch_feed_entries` 每个源独立 try | 单源失败不影响其他源 |
| **自动拉取级** | `intelligence_service.py:348-351` | `except Exception` → `logger.warning("... (fail-open)")` → **返回 `{"ok": False}` 而不是抛出** |
| **资讯池级** | `pipeline.py:3028` `try:` 包住整个 `_load_persisted_intelligence_context` | 拿不到资讯就返回 `None`，分析照常进行 |

**设计意图**（源码注释原文）：`"""Load locally persisted intelligence as fail-open evidence context."""`

**为什么这是对的**：资讯是**增强项**，不是**必需项**。如果因为一个资讯源挂了就整个分析失败，那是本末倒置。**实测印证**：某次运行中 HKEX 源抓取失败，但分析照常完成（`成功: 1, 失败: 0`）。

---

## 亮点 6：缓存与冷却的双层节流（保护上游 + 保护额度）

**两层节流，目的不同**：

| 层 | 位置 | 时长 | 保护对象 |
|---|---|---|---|
| **转换层缓存** | RSSHub `lib/config.ts:822` | **5 分钟** | 保护**上游网站**（不被打爆）|
| | NewsNow `shared/consts.ts:6` | **30 分钟** | 同上 |
| **DSA 拉取冷却** | `intelligence_service.py:39` | **60 分钟** | 保护**外部请求次数**（"为避免每只股票重复请求外部站点"）|

**实测印证**：
- RSSHub 缓存：第 1 次 0.114s → 第 2 次 0.021s（快 5 倍），响应头 `rsshub-cache-status: HIT`、`cache-control: max-age=300`
- DSA 冷却：`_AUTO_FETCH_MIN_INTERVAL_SECONDS = 60 * 60`，且 `_auto_fetch_last_run_at` 是**类变量（进程内）**

**可讲的点**：这两层是**正交**的——转换层缓存的是"上游内容"，DSA 冷却的是"我方请求频率"。而且**冷却在分析多只股票时特别有价值**：一次分析 10 只股票只会真正拉取 1 次。

---

## 亮点 7：Token 成本的可观测性设计（46 字段的 `llm_usage`）

**问题**：LLM 成本是这类系统的核心运营指标，但"多少钱"很难算准——因为**缓存命中与否价格差 120 倍**（0.025 vs 3 元/百万 tokens）。

**设计**：`llm_usage` 表有 **46 个字段**，其中一大类专门做缓存归一化：

```
normalized_prompt_tokens / normalized_completion_tokens / normalized_total_tokens
normalized_cache_read_tokens / normalized_cache_write_tokens / normalized_cache_miss_tokens
normalized_uncached_input_tokens / normalized_cache_eligible_input_tokens
normalized_cache_hit_ratio / normalized_cache_write_ratio
cache_capability / cache_eligibility / cache_observation
provider_reported_prompt_tokens / provider_reported_cached_tokens / provider_min_cache_tokens
estimated_prefix_tokens / tokenizer_name / tokenizer_version
```

**为什么这么复杂**：不同 provider 对"缓存 token"的上报口径不同，所以要**归一化**成统一字段才能跨 provider 算成本。`provider_usage_json` 保留原文，`normalized_*` 是统一口径。

**实测数据支撑**（这是可讲的真实结论）：

| `call_type` | 次数 | 提示令牌 | 完成令牌 | 合计 | 占比 |
|---|---|---|---|---|---|
| `agent`（问股）| 18 | 248,526 | 11,903 | **260,429** | **82.6%** |
| `analysis` | 4 | 23,913 | 20,353 | 44,266 | 14.0% |
| `market_review` | 3 | 3,804 | 6,756 | 10,560 | 3.4% |

**分析结论**：问股占 83%，因为**多轮对话每轮重发完整历史**——输入 token 随轮数累加（总输入近似 O(N²)）。实测 `agent` 均 **13,807** 提示令牌/次，是个股分析（5,978）的 2.3 倍。

**这个点的价值**：它不是"我调了个 LLM"，而是**"我度量了成本、定位了成本大户、并知道优化方向（prompt 缓存命中率）"**。

---

## 亮点 8：多源 fallback + 真实世界容错

**行情数据有 13 个 fetcher，按优先级 P0→P5 链式降级**：

```
Efinance(P0) → Akshare(P1) → Pytdx(P2) → Baostock(P3) → Yfinance(P4) → Tencent(P5)
```

**真实世界的失败是常态**（实测日志）：

```
[API错误] 获取 600519 筹码分布失败: Connection aborted, RemoteDisconnected
[efinance] 获取板块排行失败: Max retries exceeded ...
[Akshare] 东财接口获取行业板块排行失败: ...，尝试新浪接口
[概念排行] 所有数据源均失败，最终错误: TencentFetcher返回空结果
```

**设计哲学**：**部分成功优于完全失败**。报告里会明确标注数据质量，例如：

```
"data_sources": "行情来源：tencent；日线来源：storage.get_analysis_context；
 技术面partial（含intraday_realtime_overlay）；筹码分布missing；基本面partial；
 新闻来源：本地资讯证据池（东财个股、财联社、华尔街见闻、格隆汇）。
 数据质量评分88/100，基于2026-09-30完整交易日数据，分析日期2026-10-02为非交易日。"
```

**这个点特别适合讲**：它体现的是**对爬虫类数据源的现实主义认知**——免费源必然不稳定，所以架构上必须假设"每一个源随时可能挂"，并把这个假设贯彻到 fallback 链、数据质量评分、fail-open 三个层面。

---

## 附：面试时可以主动坦承的已知问题

**诚实是加分项，不要假装系统完美。** 以下是本会话实测确认的缺陷：

| 问题 | 证据 | 影响 |
|---|---|---|
| **自动建源会复活手动禁用的源** | `ensure_default_sources_enabled()` 里 `update_source_enabled(existing.id, True)` 无条件执行；实测从 6 个源变成 11 个全启用 | 无法真正禁用某个源；会拉起用不上的美港股源 |
| **只有 `intelligence_items` 有 retention** | `apply_retention` 只被 `intelligence_service.py:263` 调用 | `analysis_history`（单行 46KB）/ `news_intel` / `llm_usage` 无上限增长 |
| **60 分钟冷却是硬编码常量** | `_AUTO_FETCH_MIN_INTERVAL_SECONDS = 60 * 60` | 无环境变量可调，要改代码 |
| **`symbol` 级源需"一股一源"** | 当前 `symbol/600519` 两个源 | 股票池扩到 50 只就要建 100 个源，**扩展性差** |
| **界面无资讯池页面** | 前端 `pages/` 下无 intelligence 页面 | 只能通过 API / DB / 报告间接观测 |
| **NewsNow 的 `sv` 版本号陈旧** | `7.7.5` vs RSSHub 的 `8.7.9` | 上游改版后可能被拒 |

---

*文档结束。所有代码位置为 2026-10-02 的仓库实际状态；所有数字为本机实测，可在 `data/stock_analysis.db` 与 `logs/` 中复现。*
