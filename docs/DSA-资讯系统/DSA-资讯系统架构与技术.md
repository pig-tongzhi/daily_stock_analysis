# DSA 资讯系统 · 架构与技术文档

> 版本：**v2.0**（链路已接通并实测验证）
> 快照时间：2026-10-02
> 状态：**✅ 链路已打通** —— RSSHub / NewsNow → DSA 资讯池 → LLM 报告，已端到端验证
> 部署形态：**全本地**（DSA:8010 + RSSHub:1200 + NewsNow:5173）
> 用途：作为后续优化的基线。所有数据标注了「实测」或「源码」来源，未验证的明确标出。

---

## 0.0 目录结构

**这三个项目现在统一收在 `/Users/mac_1/renjiebank/renjie问股/`**（`renjiebank` 是用户的正式项目目录，同目录下另有 `p1项目/`、`p2项目/`、`云上项目/`）。

```
/Users/mac_1/renjiebank/
├── renjie问股/                                  ← 本系统：三个服务同目录
│   ├── daily_stock_analysis/      1.3 GB   DSA 主项目（Python）
│   ├── newsnow-verify/            670 MB   NewsNow 转换层（Node）
│   ├── rsshub-verify/             916 MB   RSSHub 转换层（Node）
│   ├── start-local.sh                      三服务统一启动脚本
│   ├── .log-{rsshub,newsnow,dsa}.log       运行日志
│   ├── .pid-{rsshub,newsnow,dsa}           进程号
│   └── daily_stock_analysis/docs/DSA-资讯系统/   ← 本文件所在（含 screenshots/）
├── p1项目/                                     其他活跃项目（OpenStock-Enhanced、dsh-desktop-master）
├── p2项目/                                     暂停项目
└── 云上项目/                                   已部署到云机器的项目
```

> 📌 目录沿革：`/Users/mac_1/Documents/renjieskill/` 是**专门存放 skill 的目录**，不是项目目录。这三个项目先被误放到 `renjieskill/renjieMoney/`，后按用户要求迁到 `/Users/mac_1/renjiebank/`；2026-10-08 先按活跃度归入 `p1项目/`，同日再统一收进 `renjiebank/renjie问股/`，启动脚本随之下沉到该目录、原来的 `~/renjiebank/docs/`（含 7 张截图）也挪进了 `daily_stock_analysis/docs/DSA-资讯系统/`。

**为什么叫 `-verify`**：这两个目录最初是为了"实测对比哪个资讯源更好"而克隆的。它们现在是正式组件，不只是验证副本。

### 迁移记录（重要：换机器 / 换路径时会再遇到）

**每次移动这三个项目，都有两处绝对路径会坏**（已踩过三次，2026-10-08 第三次移动时重新实测）：

| 问题 | 现象 | 修法 |
|---|---|---|
| **Python venv shebang** | `.venv/bin/` 下 **47 个文件**写死了旧 venv 绝对路径。2026-10-08 实测：搬之前它们还指着**两代以前**的 `~/renjiebank/daily_stock_analysis`，即 `pip`/`pytest` 早已是坏的（直接执行报 `bad interpreter`），只是没人调用所以没暴露 | `perl -pi -e 's\|旧路径\|新路径\|g'` 批量替换含旧路径的文件 |
| **node_modules/.bin** | 2026-10-08 实测：**rsshub 198 处 / newsnow 120 处**路径、合计 **33 + 20 个文件**含旧路径（不是早先记的 3 个） | 在新路径下重跑 `pnpm install --frozen-lockfile --prefer-offline`（store 有缓存，**约 3 秒**）|
| **pyvenv.cfg** | `command =` 行还写着 venv 最初创建时的路径（纯记录，不影响运行） | 顺手一起替换，避免下次误判 |

> ⚠️ 最容易被漏掉的一点：`.venv/bin/python` 本身是软链（指向基础 Python），所以坏掉的只是**控制台脚本的 shebang**。`start-local.sh` 走的是 `.venv/bin/python`，因此**服务照样能起来**——故障会潜伏到某天直接调用 `pip` / `pytest` 时才爆。

**✅ 安全项**：每个 git 仓库都只有 **1 个 worktree**，移动不会破坏 worktree 引用。
**✅ 无需处理**：`logs/*.log`（历史日志）和 `newsnow-verify/dist/`（构建产物）里的旧路径无害，后者重启时自动重建。

> ⚠️ 容易误判的一点：`.venv/bin/python3.11` 是指向 `/usr/local/opt/python@3.11/bin/python3.11` 的符号链接（指向**基础 Python**，不是 venv 内部），所以移动后 `python` 本身**仍能用**——只有 console scripts 的 shebang 会坏。别以为"venv 整个坏了"就去重建。

### 服务启动命令

推荐直接用启动脚本（已固化 NO_PROXY 修复与 node 版本）：

```bash
cd /Users/mac_1/renjiebank/renjie问股
./start-local.sh          # 启动全部
./start-local.sh status   # 查看状态
./start-local.sh stop     # 停止全部
```

手动启动（等价）：

```bash
# ① DSA Web（必须用 shell 环境变量传端口，原因见 §2.3）
cd /Users/mac_1/renjiebank/renjie问股/daily_stock_analysis
WEBUI_HOST=127.0.0.1 WEBUI_PORT=8010 .venv/bin/python webui.py

# ② RSSHub
cd /Users/mac_1/renjiebank/renjie问股/rsshub-verify
export PATH=/Users/mac_1/.nvm/versions/node/v22.22.3/bin:$PATH
pnpm run dev                                    # :1200

# ③ NewsNow（只绑 IPv6，必须用 localhost 访问，不能用 127.0.0.1）
cd /Users/mac_1/renjiebank/renjie问股/newsnow-verify
export PATH=/Users/mac_1/.nvm/versions/node/v22.22.3/bin:$PATH
pnpm run dev                                    # :5173
```

> ⚠️ **pnpm 必须用 nvm node22 下那个**。DSH 自带的 pnpm 用的是 Electron 的 node v24.18.1，编译原生模块必失败（见 §2.3）。
> 安装：`export PATH=/Users/mac_1/.nvm/versions/node/v22.22.3/bin:$PATH && npm install -g pnpm@<版本>`

---

## 0. 一句话概括

```
外部财经网站  →  资讯转换层（RSSHub / NewsNow）  →  DSA 资讯池  →  LLM 分析
                                                        ↑
                                        搜索 API（Bocha）走另一条独立线
```

**核心设计**：DSA 不直接爬财经网站。它通过两个可插拔的"资讯源类型"（`rss` / `newsnow`）拉取，落进自己的 SQLite，再注入 LLM Prompt。

---

## 1. 分层架构

```
┌──────────────────────────────────────────────────────────────────────┐
│ L1 · 数据源层（外部，不受我们控制）                                    │
│                                                                       │
│   财联社 cls.cn        金十数据 jin10.com      雪球 xueqiu.com         │
│   华尔街见闻 wallstreetcn.com   格隆汇 gelonghui.com                   │
│   东方财富 / 新浪财经 / 财新 / 第一财经 / 证券时报 / 同花顺 / 和讯 …    │
│                                                                       │
│   抓取方式：HTTP + 伪装 UA + 逆向 sign 签名（部分站点需浏览器内核）      │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ L2 · 资讯转换层（我们自托管，可插拔）                                   │
│                                                                       │
│   ┌─────────────────────┐        ┌─────────────────────┐             │
│   │ RSSHub  :1200       │        │ NewsNow  :5173      │             │
│   │ 46,393★  AGPL-3.0   │        │ 21,919★  MIT        │             │
│   ├─────────────────────┤        ├─────────────────────┤             │
│   │ 4,010 路由 / 1,941站│        │ 48 个源             │             │
│   │ 输出：RSS / Atom XML│        │ 输出：私有 JSON     │             │
│   │ 缓存：内存 5 分钟    │        │ 缓存：SQLite 30 分钟│             │
│   │ 正文：✅ 3,000 字   │        │ 正文：❌ 只有标题   │             │
│   │ 反爬：动态header+内核│        │ 反爬：固定UA+抄签名 │             │
│   └─────────────────────┘        └─────────────────────┘             │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                DSA 按 source_type 分别解析：
                  rss     → 解析 XML
                  newsnow → 解析 JSON
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ L3 · DSA 资讯池（唯一的长期存储）                                      │
│                                                                       │
│   SQLite: data/stock_analysis.db                                      │
│   ├── intelligence_items    ← 本条链路的落地点                        │
│   ├── intelligence_sources  ← 资讯源配置                              │
│   └── news_intel            ← 搜索 API 的落地点（另一条线）            │
│                                                                       │
│   写入入口：IntelligenceService.refresh_auto_sources()                 │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ L4 · 分析层                                                           │
│                                                                       │
│   个股报告  /  大盘复盘  /  问股                                       │
│        ↑                                                              │
│   注入方式：_load_persisted_intelligence_context()                     │
│             → 格式化成「## 本地资讯证据池」段落 → 进 LLM Prompt        │
│                                                                       │
│   LLM: DeepSeek (deepseek-chat)，实测单次 18~27 秒                    │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. 组件清单

### 2.1 本机运行实例（实测）

| 组件 | 端口 | 路径 | 状态 |
|---|---|---|---|
| DSA Web/API | `:8010` | `~/renjiebank/renjie问股/daily_stock_analysis` | ✅ 运行中 |
| NewsNow（dev）| `:5173` | `~/renjiebank/renjie问股/newsnow-verify` | ✅ 运行中 |
| RSSHub（dev）| `:1200` | `~/renjiebank/renjie问股/rsshub-verify` | ✅ 运行中 |

**注意**：NewsNow 只绑 **IPv6 `[::1]`**，所以 `127.0.0.1:5173` 不通，必须用 `localhost:5173`。
RSSHub 绑 IPv4+IPv6，`127.0.0.1:1200` 和 `localhost:1200` 都通。

### 2.2 版本与依赖（实测）

| | DSA | RSSHub | NewsNow |
|---|---|---|---|
| 语言 | Python 3.11.15 | Node 22.22.3 | Node 22.22.3 |
| 包管理 | pip / venv | pnpm 10.34.5 | pnpm 10.30.3 |
| 代码规模 | 341,726 行 Python / 635 文件 | — | — |
| 依赖体积 | `.venv` 809 MB | `node_modules` 875 MB | `node_modules` 663 MB |
| 内存（实测）| **662 MB**（Web 服务常驻）| 374 MB（dev）| 604 MB（dev）|
| 许可 | MIT | **AGPL-3.0** | **MIT** |

### 2.3 环境陷阱（已踩过，记录备查）

| 坑 | 现象 | 解法 |
|---|---|---|
| **Python 架构** | `/usr/local/bin/python3.11` 是 **x86_64**，机器是 arm64 | 用 `/usr/local/bin/python3.11`（就是这个 x86_64 的）|
| **cryptography 编译失败** | 49.x 起 macOS 无 x86_64 轮子 → 源码编译 → 缺 Rust target | **`cryptography<49`**（48.0.1 有 universal2 轮子）|
| **DSH 的 pnpm 用错 node** | DSH runtime 的 pnpm 用 Electron 的 node v24.18.1 | **`npm install -g pnpm@x` 装到 nvm node22 下** |
| **DSA 端口不生效** | `webui.py` 先读 `WEBUI_PORT` 再 `setup_env()` | 必须用 **shell 环境变量**传：`WEBUI_PORT=8010 .venv/bin/python webui.py` |
| **8000 端口被占** | 用户自己的 helpcat 服务（uvicorn） | DSA 改用 **8010** |

---

## 3. 数据流：两条独立链路

**重要：DSA 有两条完全独立的资讯链路，不要混淆。**

```
链路 A：资讯源采集（本文档重点）
   RSSHub / NewsNow
        ↓  pull（按源，定时/分析时触发）
   intelligence_items 表
        ↓  _load_persisted_intelligence_context()
        ↓  按 scope 两级查找 + 最多 6 条
   LLM Prompt

链路 B：按需搜索
   搜索 API（Bocha / Tavily / …）
        ↓  search（按 query，实时）
   news_intel 表
        ↓  按 dimension 分类（earnings / risk_check / latest_news / …）
   LLM Prompt
```

| | 链路 A | 链路 B |
|---|---|---|
| 表 | `intelligence_items` | `news_intel` |
| 触发 | 按源拉取 | 按 query 搜索 |
| 成本 | **免费无限** | Bocha 1000次/3个月 |
| 当前状态 | **0 行** | **10 行** |
| 时间字段 | 取决于源 | ✅ 都有 |
| 正文 | ✅ RSSHub 有 | ✅ Bocha summary |

---

## 4. 存储结构

### 4.1 三个库，互不共享（实测）

| 系统 | 存储介质 | 位置 | 当前大小 |
|---|---|---|---|
| **RSSHub** | **内存**（默认） | 无磁盘文件 | 0 |
| **NewsNow** | SQLite | `newsnow-verify/.data/db.sqlite3` | 40 KB |
| **DSA** | SQLite | `daily_stock_analysis/data/stock_analysis.db` | 1.28 MB |

### 4.2 NewsNow 缓存表（实测内容）

```sql
CREATE TABLE cache (id TEXT PRIMARY KEY, updated INTEGER, data TEXT);
-- 一个源一行，INSERT OR REPLACE 覆盖写 → 不会无限增长

gelonghui          更新于 10-02 03:12:23   2310 字节
wallstreetcn       更新于 10-02 03:12:23   4904 字节
jin10              更新于 10-02 03:12:23   5034 字节
xueqiu-hotstock    更新于 10-02 03:12:22   2935 字节
cls-hot            更新于 10-02 03:12:16   1236 字节
```

### 4.3 DSA 的两张资讯表（源码字段）

**`intelligence_items`**（链路 A，当前 0 行）
```
id / source_id / source_name / source_type
title / summary / url / source
published_at / fetched_at
scope_type / scope_value / market
raw_payload
```

**`news_intel`**（链路 B，当前 10 行）
```
id / query_id / code / name / dimension / query / provider
title / snippet / url / source / published_date / fetched_at
query_source / requester_platform / requester_user_id / requester_user_name
```

**`intelligence_sources`**（源配置，当前 0 行）
```
id / name / source_type / url / enabled
scope_type / scope_value / market / description
last_status / last_error / last_fetched_at
created_at / updated_at
```

### 4.4 数据保留策略（实测：只有一张表有自动清理）

| 表 | 自动清理 | 机制 |
|---|---|---|
| `intelligence_items` | ✅ **30 天** | `intelligence_repo.apply_retention()` |
| `analysis_history` | ❌ 只能手动 | `delete_analysis_history_records(ids)` |
| `news_intel` | ❌ 无 | — |
| `llm_usage` | ❌ 无 | — |
| `logs/` | ✅ **自动轮转** | 常规 10MB×5 / 调试 50MB×3（封顶约 260MB）|

> ⚠️ **命名陷阱**：`NEWS_INTEL_RETENTION_DAYS` 清的是 **`intelligence_items`**（intel**ligence**），
> **不是** `news_intel`。两个名字高度相似但是不同表。

---

## 5. 关键机制详解

### 5.1 同步时机与频率

**触发点**（源码核实）：`refresh_auto_sources()` **只有两处调用**

- `src/core/pipeline.py:3030` ← 由 `_load_persisted_intelligence_context()` 调用
  - 该函数被 `pipeline.py:683`（个股分析）与 `pipeline.py:1550`（大盘复盘）调用
- `src/market_analyzer.py:2080` —— 大盘复盘（另一条入口）

> ⚠️ **重要更正（2026-10-02 实测）**：**问股（Agent 对话）不使用资讯池。**
> `src/agent/` 目录下**零调用** `IntelligenceService` / `intelligence_items` / `_load_persisted_intelligence_context`；
> 问股的 `news_context` 来自 agent 自己的工具 `src/agent/tools/search_tools.py`（即**搜索 API**，如 Bocha），
> 走 `src/agent/orchestrator.py` 的 `ctx.get_data("news_context")`。
>
> 实测证据：问股期间日志中「本地资讯证据池」出现 **0 次**；问股回答「新闻与情报检索工具均不可用（未配置搜索服务）」。
>
> 项目自带文档 `docs/intelligence-sources.md` 第 115 行称「Agent 分析同样通过 news_context 注入本地资讯证据」，
> **与代码不符**（属文档与实现不一致，未确认是文档错还是实现漏接）。

**三道闸门**（源码 `intelligence_service.py:307-356`）：

```python
def refresh_auto_sources(self, *, force=False):
    # 闸门① 总开关
    if not config.news_intel_auto_fetch_enabled:      # 默认 False
        return {"skipped": True, "reason": "disabled"}

    # 闸门② 进程内串行（并发保护）
    while cls._auto_fetch_in_progress: wait()

    # 闸门③ 冷却（NEWS_INTEL_AUTO_FETCH_MIN_INTERVAL_SECONDS，默认 1200 秒，clamp 60~86400）
    cooldown_seconds = config.news_intel_auto_fetch_min_interval_seconds or 1200
    if not force and (now - last_run_at) < cooldown_seconds:
        return {"skipped": True, "reason": "cooldown"}

    # 执行
    bootstrap = self.ensure_default_sources_enabled()   # 自动建/启用默认源
    fetch = self.fetch_enabled_sources()                # 拉取全部启用的源
    # fail-open：异常不阻塞主流程
```

**有效同步频率 = 四层延迟里最慢的一环：**

| 环节 | 数值 | 来源 |
|---|---|---|
| 上游内容更新 | jin10 ≈11 分钟 / 热榜类 4~10 小时 | 实测 pubDate |
| RSSHub 缓存 | **5 分钟** | 实测 `cache-control: max-age=300` |
| NewsNow 缓存 | **10~30 分钟** | 源码 `TTL=30min`、`Interval=10min`（源分档 2/5/30 分钟）|
| DSA 拉取冷却 | **20 分钟**（`60` ~ `86400` 秒可配）| 源码 `news_intel_auto_fetch_min_interval_seconds`（默认 `1200`）/ 环境变量 `NEWS_INTEL_AUTO_FETCH_MIN_INTERVAL_SECONDS` |
| **分析触发频率** | **每天 1 次**（默认 18:00）| `SCHEDULE_TIME` |

**结论**：默认用法下，**DSA 每天同步 1 次**。20 分钟冷却只防"同一冷却窗口内重复分析"。

### 5.2 注入 Prompt 的机制

源码 `_load_persisted_intelligence_context()`（`pipeline.py:3019`）：

```python
① service.refresh_auto_sources()          # 先尝试刷新（受三道闸门约束）
② days = 有效新闻窗口                      # NEWS_MAX_AGE_DAYS，默认 3 天
③ 两级 scope 查找：
     symbol 级（该股票的） → 找不到再兜底
     market 级（大盘的）
④ URL 去重，最多取 6 条（limit=6）
⑤ 格式化成：
     ## 本地资讯证据池（{股票名}/{代码}）
     1. {title}（{source} / {published_at}）
        {summary}          ← ★ 只有 summary 非空才追加
```

> ★ **`summary` 字段直接决定 LLM 能看到多少信息** —— 这是 RSSHub 与 NewsNow 在 DSA 里的实际差距所在。

### 5.3 抓取原理（逆向签名）

**财联社 API 需要 `sign` 参数**，两边实现相同（NewsNow 的代码注释直接引用 RSSHub）：

```typescript
params = { appName: 'CailianpressWeb', os: 'web', sv: '8.7.9' }
① searchParams.sort()                        // 参数按 key 排序
② SHA1(searchParams.toString())              // SHA1
③ MD5(sha1)                                  // 再 MD5
④ append('sign', md5)                        // 作为 sign 参数
```

**关键差异**：

| | RSSHub | NewsNow |
|---|---|---|
| `sv` 版本号 | **`8.7.9`** | **`7.7.5`**（旧两代）|
| 加密实现 | `crypto-js`（Node）| Web Crypto API（Workers 兼容）|
| 请求头 | `header-generator` **动态生成** | **固定** Chrome 130 UA |
| 重试 | 8 种状态码 + 代理池 + TLS/HTTP2 控制 | 10 秒超时 + 重试 3 次 |
| 浏览器内核 | 78/4010 路由需要 Playwright | ❌ 无 |

### 5.4 安全设计（DSA 侧，源码 `docs/intelligence-sources.md`）

DSA 对自定义 URL 做了 **SSRF 防护**：

- 只允许绝对 `http` / `https` URL
- 禁止 URL 携带 username/password
- 禁止 `localhost` / `.local` / 回环 / 内网 / 链路本地 / 保留 / 共享 / 组播地址
- **显式禁用环境代理**（`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`），避免代理绕过校验
- **二次校验 DNS 解析结果**（防 DNS rebinding）
- **重定向后的最终 URL 再校验**
- 错误消息脱敏 `token` / `key` / `secret`

**明确非目标**：不做反爬、模拟登录、Cookie 抓取或非授权门户直抓。

---

## 6. 实测数据基线（2026-10-02 03:18）

### 6.1 NewsNow 5 个内置源

| 源 | 条数 | 有 title | 有 url | **有发布时间** | 最新一条 |
|---|---|---|---|---|---|
| `cls-hot` | 13 | ✅ | ✅ | ❌ | — |
| `xueqiu-hotstock` | 30 | ✅ | ✅ | ❌ | — |
| `jin10` | 25 | ✅ | ✅ | ✅ | **11 分钟前** |
| `wallstreetcn` | 30 | ✅ | ✅ | ✅ | 46 分钟前 |
| `gelonghui` | 15 | ✅ | ✅ | ✅ | 227 分钟前 |

**API 契约**：`GET /api/s?id=<source_id>` → `{status, id, updatedTime, items[]}`
✅ 与 DSA 要求的 `dict + items list` **完全兼容**。

### 6.2 RSSHub 财经路由

| 路由 | 条数 | 最新一条 | 有正文 |
|---|---|---|---|
| `/cls/hot` | 13 | 404 分钟前 | ✅ 3,003 字符 |
| `/cls/depth` | 32 | **84 分钟前** | ✅ |
| `/wallstreetcn/hot` | 10 | 438 分钟前 | ✅ |
| `/wallstreetcn/news` | 26 | **45 分钟前** | ✅ |
| `/gelonghui/home` | 12 | — | ✅ |
| `/gelonghui/hot-article` | 10 | 594 分钟前 | ✅ |
| `/jin10/category/1` | 1 | — | ✅ |
| `/eastmoney/search/<kw>` | 1 | — | ✅ |
| `/xueqiu/hots` | ❌ 503 | — | 需 Playwright |
| `/xueqiu/today` | ❌ 503 | — | 需 Playwright |
| `/xueqiu/timeline` | ❌ 503 | — | 需登录 Cookie |

### 6.3 字段深度对比（同一财联社源）

| 字段 | RSSHub `/cls/hot` | NewsNow `cls-hot` |
|---|---|---|
| title | 32 字符 | 45 字符 |
| link/url | ✅ | ✅ |
| **pubDate** | ✅ `Thu, 01 Oct 2026 12:34:23 GMT` | ❌ **无** |
| **author** | ✅ `财联社 黄君芝` | ❌ |
| **description** | ✅ **3,003 字符正文** | ❌ **无** |

### 6.4 缓存实测

**RSSHub**：
```
第 1 次 0.114s  →  第 2 次 0.021s  →  第 3 次 0.019s   （快 5 倍）
响应头：cache-control: public, max-age=300
        rsshub-cache-status: HIT
```

**NewsNow**：`items` 内容指纹一致（缓存），仅 `updatedTime` 每次变化（请求时间戳）。

---

## 7. 已知问题与优化点（后续优化的输入）

### 7.1 阻塞项（接通前必须解决）

| # | 问题 | 影响 | 方向 |
|---|---|---|---|
| B1 | `NEWS_INTEL_AUTO_FETCH_ENABLED` 默认 false | 完全不拉取 | 开启前先配好 URL |
| B2 | ~~`NEWSNOW_BASE_URL` 默认指向已 403 的公开实例~~ | **已于 v1.1 修复** → 改为 `http://localhost:5173` | ✅ 已处理 |
| B3 | `intelligence_sources` 0 行 | 无源可拉 | 建源后再开总闸 |
| B4 | `SCHEDULE_ENABLED=false` | 不自动跑 | 链路验证通过后再开 |
| **B5** | **🔴 DSA 拒绝内网地址 —— 本机服务无法作为资讯源** | **链路完全接不通** | **见 §7.1.1** |

### 7.1.1 🔴 核心阻塞：SSRF 防护 vs 本地部署（2026-10-02 实测发现）

**DSA 硬编码了 SSRF 防护，不接受私网 / 回环地址作为资讯源 URL。** 实测：

| 源 URL | 结果 |
|---|---|
| `http://localhost:5173`（本机 NewsNow）| 🚫 `source url host is not allowed` |
| `http://127.0.0.1:1200/cls/depth`（本机 RSSHub）| 🚫 `source url must not target private or local network addresses` |
| `https://www.zhitongcaijing.com/rss.xml` | 校验通过（但上游 500）|
| `https://feeds.a.dj.com/rss/RSSMarketsMain.xml` | ✅ **通过** |

**校验规则**（`src/services/intelligence_service.py`）：

```python
_PRIVATE_HOSTNAMES = {"localhost", "localhost.localdomain"}   # line 32

# line 396-426  _validate_url
① scheme 必须 http/https
② 不能有 username/password
③ hostname 不在 _PRIVATE_HOSTNAMES、不以 .local 结尾
④ 若是 IP 字面量 → _is_blocked_ip() 必须通过
⑤ 若是域名 → DNS 解析，【所有】结果都不能是内网 IP
⑥ 必须至少有一个公网地址

# line 432-440  _is_blocked_ip
return (not ip.is_global      # ← 最严格：必须"全局可路由"
        or ip.is_private      # 10/8, 172.16/12, 192.168/16, 100.64/10(CGNAT)
        or ip.is_loopback     # 127/8
        or ip.is_link_local   # 169.254/16
        or ip.is_reserved
        or ip.is_multicast)

# line 556-567  _get_with_validated_dns —— DNS 二次校验（防 rebinding）
```

**⚠️ 没有任何配置开关可以放行。** 搜遍代码，无 `allow_private` / `skip_validate` 之类的开关，校验是硬编码的。
**⚠️ 连 Tailscale 也不行** —— `100.64.0.0/10` (CGNAT) 会被 `ip.is_private` 拦掉。

**这意味着**：DSA 的设计假设是**资讯源跑在公网**。所有"全本地部署"方案都会撞上这道墙。

**三条出路：**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| **A. 改代码放行内网** | 加 env 开关，在 `_PRIVATE_HOSTNAMES` / `_is_blocked_ip` / `_get_with_validated_dns` 三处加 bypass | 唯一纯本地方案；约 25 行，集中在一个文件 | **削弱 SSRF 防护**；改动第三方项目 |
| **B. cloudflared quick tunnel** | `brew install cloudflared` → `cloudflared tunnel --url http://localhost:5173` | 不动第三方代码；免注册 | 域名每次重启会变（要改 DSA 配置）；多一个常驻进程；流量绕公网；本地服务暴露到公网 |
| **C. 转换层上云** | 把 RSSHub/NewsNow 部署到公网服务器 | **最符合 DSA 设计**；地址固定稳定 | 与"暂定本地"冲突；需要云主机；云 IP 可能被财经站限流 |

> **决策记录**：⏳ **待定**（需用户拍板）

### 7.2 机制缺陷 / 待优化

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| O1 | **自动建源会"复活"被禁用的源** | `ensure_default_sources_enabled()` 里 `update_source_enabled(existing.id, True)` | 手动关闭某个源无效，下次自动拉取又开 |
| O2 | **只有 `intelligence_items` 有 retention** | 源码 `apply_retention` 只被 `intelligence_service` 调用 | `analysis_history` / `news_intel` / `llm_usage` 无上限增长 |
| O3 | **来源选择未经优化** | 我推荐的 `/cls/hot` 实测 404 分钟前（热榜类） | 应改选 `/cls/depth`(84min) / `/wallstreetcn/news`(45min) / NewsNow `jin10`(11min) |
| O4 | **NewsNow `sv` 版本号陈旧** | `7.7.5` vs RSSHub `8.7.9` | 上游改版后可能被拒 |
| O5 | **NewsNow 只绑 IPv6** | 实测只监听 `[::1]:5173` | DSA 用 `127.0.0.1` 会连不上，必须 `localhost` |
| O6 | **`summary` 字段差异未利用** | RSSHub 有 3000 字正文，NewsNow 无 | 未接通前无法评估对报告质量的实际影响 |
| O7 | **RSSHub 缓存无法按源粒度配置** | `CACHE_EXPIRE` 是全局 | 想给不同源设不同 TTL 做不到 |

### 7.3 待验证项（接通后才能测）

- [ ] `intelligence_items` 落库后的真实字段填充率（`summary` / `published_at` 是否为空）
- [ ] 报告里「风险警报 / 利好催化 / 最新动态」是否真的用上了资讯
- [ ] RSSHub 有正文 vs NewsNow 只有标题，对报告质量的**实际**差异
- [ ] 冷却窗口（默认 1200 秒）在一次多股分析中的实际表现
- [ ] 资讯池能否降低 Bocha 用量（两条链路是否真的互补）
- [ ] `scope_type: symbol` 的源怎么建（当前内置模板都是 market 级）

---

## 8. 技术选型记录（决策与理由）

| 决策 | 选择 | 理由 |
|---|---|---|
| LLM | **DeepSeek** (`deepseek-chat`) | 已有 key，实测 18~27 秒/次，成本低 |
| 搜索 API | **Bocha** | 中文财经覆盖最好；1000 次/3 个月免费额度 |
| 资讯源（正文） | **RSSHub** | 唯一有 3,000 字正文的；缓存最短（5 分钟）|
| 资讯源（补充） | **NewsNow** | MIT；可 CF Pages 免费托管；雪球反而可用 |
| 数据库 | **SQLite**（DSA 内置）| 无需额外服务；单库 GB 级无压力 |
| DSA 端口 | **8010** | 8000 被用户的 helpcat 占用 |

**两者关系：不是替代，是互补**

| | RSSHub | NewsNow |
|---|---|---|
| 强项 | 正文深度、覆盖广度、反爬能力 | 部署省事、雪球可用、MIT |
| 弱项 | 需自托管、AGPL、部分源需浏览器 | 只有标题、无时间字段、sv 陈旧 |
| 定位 | **主力**（要深度） | **补充**（要省事/补漏） |

---

## 9. 接通顺序（本次执行）

严格按此顺序，**顺序错会导致不可用的源被自动创建**：

```bash
# ① 先配 URL（顺序不能反！）
NEWSNOW_BASE_URL=http://localhost:5173

# ② 建源
POST /api/v1/intelligence/sources/defaults        # NewsNow 5 个
POST /api/v1/intelligence/sources                 # RSSHub 手工建（挑快讯类）

# ③ 手动验证（不落库）
POST /api/v1/intelligence/sources/test

# ④ 拉取一次
POST /api/v1/intelligence/sources/fetch-enabled
GET  /api/v1/intelligence/items

# ⑤ 全部成功后再开总闸
NEWS_INTEL_AUTO_FETCH_ENABLED=true

# ⑥ 最后才开定时
SCHEDULE_ENABLED=true
SCHEDULE_TIME=18:00
```

**推荐源清单（按实测新鲜度排序）**

| 优先级 | 源 | 类型 | 新鲜度 | 正文 |
|---|---|---|---|---|
| P0 | NewsNow `jin10` | newsnow | **11 分钟** | ❌ |
| P0 | RSSHub `/wallstreetcn/news` | rss | **45 分钟** | ✅ |
| P1 | RSSHub `/cls/depth` | rss | **84 分钟** | ✅ |
| P2 | RSSHub `/gelonghui/home` | rss | — | ✅ |
| P3 | NewsNow `wallstreetcn` / `gelonghui` | newsnow | 46 / 227 分钟 | ❌ |

---

## 10. 关键路径速查

> 下表相对路径均以系统根 `~/renjiebank/renjie问股/` 为基准。

| 用途 | 路径 / 命令 |
|---|---|
| DSA 库 | `daily_stock_analysis/data/stock_analysis.db` |
| DSA 配置 | `daily_stock_analysis/.env`（gitignored）|
| NewsNow 缓存 | `newsnow-verify/.data/db.sqlite3` |
| 启动 DSA Web | `cd daily_stock_analysis && WEBUI_PORT=8010 .venv/bin/python webui.py` |
| 启动 NewsNow | `cd newsnow-verify && pnpm run dev`（:5173，仅 IPv6）|
| 启动 RSSHub | `cd rsshub-verify && pnpm run dev`（:1200）|
| 查资讯池 | `sqlite3 data/stock_analysis.db "SELECT * FROM intelligence_items LIMIT 5"` |
| 查源配置 | `sqlite3 data/stock_analysis.db "SELECT id,name,source_type,url,enabled FROM intelligence_sources"` |

---

*本文档为 v1 基线。接通后应补充第 7.3 节「待验证项」的实测结果，形成 v2。*

---

# v2.0 · 接通实测结果（2026-10-02）

## 11. 做了什么改动

### 11.1 DSA 代码改动（放行内网地址，解决 B5）

| 文件 | 改动 |
|---|---|
| `src/config.py` | 新增字段 `news_intel_allow_private_hosts: bool = False`（line 999）+ env 解析（line 1919）|
| `src/services/intelligence_service.py` | `_validate_url`（line ~404）与 `_get_with_validated_dns`（line ~562）各加一处显式 opt-in bypass |
| `.env.example` | 新增 `NEWS_INTEL_ALLOW_PRIVATE_HOSTS` 说明（含风险警告）|
| `docs/intelligence-sources.md` | 「配置项」与「安全边界」补充该开关语义 |
| `docs/CHANGELOG.md` | `[Unreleased]` 新增 `- [新功能] ...` 条目（扁平格式）|

**bypass 的边界**（重要）：开启后**只**跳过 hostname 黑名单与 DNS 解析结果校验；`http(s)` scheme 校验、凭据校验、显式禁用环境代理**仍然生效**。

### 11.2 `.env` 配置

```env
NEWSNOW_BASE_URL=http://localhost:5173
NEWS_INTEL_ALLOW_PRIVATE_HOSTS=true
NEWS_INTEL_AUTO_FETCH_ENABLED=true
MARKET_REVIEW_ENABLED=false      # 省 Bocha 额度
SCHEDULE_ENABLED=false           # 暂不定时
# BOCHA_API_KEYS 仍注释（链路验证期间关闭，做单变量测试）
```

## 12. 🔴 新发现的严重问题

### 12.1 `NO_PROXY` 含 `[::1]` 会让 LLM 调用全部失败（排查耗时最长的一个）

**现象**：分析报 `成功: 0, 失败: 1`，日志里：

```
litellm.APIConnectionError: DeepseekException - Invalid port: ':1]'
```

**根因**：环境变量 `NO_PROXY` / `no_proxy` 的值里含畸形条目 `[::1]`：

```
NO_PROXY=192.168.1.203,.local,localhost,127.0.0.1,::1,[::1]
                                                  ^^^^^ 这个
```

LiteLLM / httpx 解析该值时把 `[::1]` 当 `host:port` 处理 → `Invalid port: ':1]'` → 所有 LLM 请求失败。

**修法**：清掉 IPv6 那两项，只留合法条目：

```bash
export NO_PROXY="localhost,127.0.0.1,.local,192.168.1.203"
export no_proxy="localhost,127.0.0.1,.local,192.168.1.203"
```

**⚠️ 这个问题与资讯链路无关**，是环境变量污染。但它会让整个分析链路挂掉，且报错信息完全指不到真实原因（指向 `:1]` 而不是 `NO_PROXY`）。**后续任何"LLM 突然连不上"的情况，先查这个。**

### 12.2 O1 缺陷已被实测确认：自动拉取会复活所有被禁用的源

接通时**刻意只启用了 6 个源**（3 个 RSSHub + 3 个带时间戳的 NewsNow），把不需要的关掉。跑过一次分析后：

```
intelligence_sources: 11 行
其中启用: 11          ← 全部被打开
```

**日志证据**：`Intelligence auto fetch completed: sources=11 saved=78 created=0 enabled=5 errors=0`
（`enabled=5` 就是被复活的 5 个：SEC / HKEX / MarketWatch / 财联社热门 / 雪球热门股票）

**代码位置**：`src/services/intelligence_service.py:204-226`

```python
def ensure_default_sources_enabled(self):
    for template in self._builtin_source_templates():
        existing = self.repo.get_source_by_name(name)
        if existing is not None:
            if not existing.enabled:
                self.repo.update_source_enabled(existing.id, True)   # ← 无条件复活
            continue
```

**影响**：手动关源无效；每次开 `NEWS_INTEL_AUTO_FETCH_ENABLED` 都会把 8 个内置模板全部拉起，包括：
- `SEC Latest Filings` / `HKEX Market News` / `MarketWatch Top Stories`（美港股，A 股分析用不上，且实测 HKEX 抓取失败）
- `财联社热门` / `雪球热门股票`（热榜类，**无 `published_at`**，破坏时效过滤）

**可能的修法**（待优化）：
- 改 `ensure_default_sources_enabled` 只创建不启用；
- 或引入"用户手动禁用"标记，与"默认未启用"区分；
- 或删掉不需要内置模板的 URL 让 `get_source_by_name` 匹配不到（当前唯一的绕过手段）

### 12.3 默认源 URL 文档与实际不符

`.env.example` 与 `docs/intelligence-sources.md` 里 NewsNow 官方仓库指向 `https://github.com/qqhann/newsnow`，**该地址已失效（404）**。真实仓库是 `https://github.com/newsnext/newsnow`（21,919★，MIT）。

## 13. ✅ 端到端验证结果

### 13.1 拉取结果（6 个源，一次 fetch-enabled）

```
HTTP 200，用时 7.1s，处理源数 6，新增条目 150
```

### 13.2 落库数据质量（**验证了 §6.3 的预测**）

| 源 | 条数 | **有 summary** | 有时间 | 有 URL |
|---|---|---|---|---|
| 财联社深度（RSSHub）| 41 | ✅ **41（100%）** | 41 | 41 |
| 财联社快讯·见闻资讯（RSSHub）| 26 | ✅ **26（100%）** | 26 | 26 |
| 格隆汇首页（RSSHub）| 12 | ✅ **12（100%）** | 12 | 12 |
| NewsNow 华尔街见闻快讯 | 30 | ❌ **0（0%）** | 30 | 30 |
| NewsNow 金十数据 | 26 | ⚠️ **7（27%）** | 26 | 26 |
| NewsNow 格隆汇事件 | 15 | ✅ 15（100%） | 15 | 15 |

**结论：RSSHub 三个源共 79 条，`summary` 正文填充率 100%；NewsNow 参差不齐（0%~100%）。**
§6.3 关于「RSSHub 有正文、NewsNow 只有标题」的预测在真实链路中得到确认。

### 13.3 LLM 确实读到了资讯池（**决定性证据**）

在 **Bocha 完全关闭**的情况下跑分析，报告里出现：

> **📢 最新动态**：近3日检索到的信息**均为海外宏观/全球市场类快讯（欧股开盘、韩国通胀、普京谈汽车等）**，未出现与贵州茅台直接相关的公司层面新闻。
>
> **💭 舆情情绪**：舆情以**海外宏观信息为主**，缺乏公司直接催化，情绪中性偏淡。

其中「**普京谈汽车**」正是数据库里 `财联社深度` 的那条（"普京：很快所有人都得开中国车"）；「**韩国通胀**」是 `财联社快讯·见闻资讯` 的"核心通胀2.8%粘性未消…韩国央行11月加息预期升温"。

**这证明**：RSSHub / NewsNow → `intelligence_items` → LLM Prompt 整条链路**真实生效**，且 LLM 没有编造公司层面利好（诚实输出"未检索到"）。

### 13.4 ⚠️ 但暴露了一个关键局限：全是 market 级，没有 symbol 级

```
scope_type=market  scope_value=__dsa_null_scope__  market=cn      109 条
scope_type=market  scope_value=__dsa_null_scope__  market=global   26 条
scope_type=market  scope_value=__dsa_null_scope__  market=hk       15 条
```

**11 个源全部是 `market` 级**（内置模板也只提供 market 级）。按 `_load_persisted_intelligence_context` 的两级查找（先 `symbol` 再 `market` 兜底），结果是：

> **任何 A 股个股分析拿到的都是同一批大盘/宏观快讯**，而不是这只股票自己的新闻。

这解释了报告里那句"未出现与贵州茅台直接相关的公司层面新闻"——**不是源不够好，是 scope 粒度不够细**。

**这是后续优化最重要的方向**（见 §15）。

## 14. 数据规模现状

| 表 | 行数 | 说明 |
|---|---|---|
| `intelligence_sources` | 11 | 全部被 O1 缺陷复活为启用 |
| `intelligence_items` | **278** | 时间跨度 `2026-01-02` ~ `2026-10-02` |
| `news_intel` | 10 | 之前 Bocha 那次留下的 |

> ⚠️ `intelligence_items` 里最早的数据是 **2026-01-02**（9 个月前），来自 SEC/MarketWatch 之类的长历史 feed。查询时有 `NEWS_MAX_AGE_DAYS=3` 兜底过滤，30 天 retention 也会清理，但**存量数据会一直躺到过期**。

## 15. 后续优化清单（按优先级）

| P | 项 | 依据 |
|---|---|---|
| **P0** | **建 `symbol` 级源**，让个股拿到自己的新闻 | §13.4 —— 当前最大的能力缺口 |
| **P0** | 修 O1：`ensure_default_sources_enabled` 不应无条件复活手动禁用的源 | §12.2 |
| **P1** | 清理不需要的内置源（SEC/HKEX/MarketWatch），它们拖慢拉取且实测失败 | §12.2 |
| **P1** | 停用无 `published_at` 的源（`cls-hot` / `xueqiu-hotstock`），它们破坏时效过滤 | §13.2 |
| **P2** | 把 `NO_PROXY` 修复合入启动脚本，避免复发 | §12.1 |
| **P3** | 评估是否需要 `SCHEDULE_ENABLED=true` 定时 | — |

## 16. 本次踩坑速查（下次直接照做）

### 16.1 推荐：用启动脚本（已固化所有环境修复）

```bash
cd /Users/mac_1/renjiebank/renjie问股
./start-local.sh            # 启动全部三个服务
./start-local.sh status     # 查看状态
./start-local.sh stop       # 停止全部
./start-local.sh dsa        # 只启动 DSA
```

脚本 `start-local.sh` 已固化三件事：
1. **强制覆盖 `NO_PROXY` 为合法值**（修 §12.1 的 LLM 全挂问题）
2. **PATH 指向 nvm node22**（避免 DSH 自带 pnpm 用 Electron node 编译失败）
3. 代理只留给 LLM/搜索，国内资讯源直连

日志输出到 `renjie问股/.log-{rsshub,newsnow,dsa}.log`。

### 16.2 手动启动（等价命令）

```bash
# ① 启动三个服务
cd /Users/mac_1/renjiebank/renjie问股/rsshub-verify && pnpm run dev    # :1200
cd /Users/mac_1/renjiebank/renjie问股/newsnow-verify && pnpm run dev   # :5173 (仅 IPv6)
cd /Users/mac_1/renjiebank/renjie问股/daily_stock_analysis
WEBUI_HOST=127.0.0.1 WEBUI_PORT=8010 .venv/bin/python webui.py                    # :8010

# ② 跑分析前必须清理 NO_PROXY（否则 LLM 全挂）
export NO_PROXY="localhost,127.0.0.1,.local"
export no_proxy="localhost,127.0.0.1,.local"

# ③ 手动拉取一次
curl -X POST http://127.0.0.1:8010/api/v1/intelligence/sources/fetch-enabled

# ④ 看数据
sqlite3 data/stock_analysis.db \
  "SELECT source_name, COUNT(*), SUM(summary IS NOT NULL AND summary<>'') FROM intelligence_items GROUP BY 1"
```

---

*本文档为 v2.0，记录链路接通后的实测结果。§15 是后续优化的输入。*

