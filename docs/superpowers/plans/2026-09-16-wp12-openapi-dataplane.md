# WP12 富途 OpenAPI 数据面补全 实现计划

> **面向 AI 代理的工作者：** 必需子技能：superpowers:subagent-driven-development（推荐）逐任务实现，两阶段审查。步骤 `- [ ]` 跟踪。
> **规格**：`docs/superpowers/specs/2026-09-16-wp9-15-autopipeline-research-institute-design.md` §七 + 附录 A（逐端点清单与官方文档 URL）。全局约定见索引（§2.5 依赖锁定协议）。
> 三套测试保持全绿；提交 `feat(datasource|platform): 摘要`。

**目标**：附录 A.3 全部缺口端点接入权威 OpenAPI 通道：传输层方法组 → 服务端点面 1:1 → MCP 工具面分档（预算 ≤80）→ F10/做空/板块 PIT 落库 + 每日快照作业。

**架构**：传输层新增 8 个方法组类（挂 `OpenApiClient`，`_RestValidators` 同构：签名即白名单、枚举/区间本地校验、`parse_envelope` 复用）；服务端点面全量 1:1；工具面「直通 12 + 聚合 2（f10_detail/derivative_detail）+ HTTP-only」三档；`research_snapshot` 作业把 F10 关键 section 与做空数据按交易日落 PIT 表。

---

### 任务 1：官方文档路径锁定（依赖锁定表）

**文件**：新建 `docs/superpowers/plans/wp12-endpoint-lock.md`；测试 `tests/test_wp12_locks.py`。

- [ ] **步骤 1：逐端点取官方文档**（`web_fetch` 官方 md，已验证的照抄，未验证的现场核对）：方法、路径、必填参数、响应字段、错误码（`no_data`/`unsupported` 语义）、限频。**已验证**：stock-screen（POST /api/v1.0/quote/stock-screen，screen_queries 11 选 1/retrieve_queries 9 选 1/sort(s)/next_key/limit≤300/user_stock_list_mode）、plate-list（GET /api/v1.0/quote/plate-list?market&plate_class，REGION 仅 SH/SZ）、short daily-volume（GET /api/v1.0/quote/{symbol}/short/daily-volume?count≤90，HK 成交/US 持仓维度，-10=no_data）。**待核对**（F10 族真实路径在 `/api/quote/financials/*`——llms.txt 的 /f10/*.md 全 404）：23 个 F10 端点、economic-calendar×2、search、owner-plate、rehab、plate-stock、warrant-screen、future-info、reference-future、option-volatility、option-exercise-probability、ipo-list、watchlist×3、sim-trade×9。
- [ ] **步骤 2：锁定表落盘**：每行 `端点名 | 方法+路径 | 关键参数 | 响应摘要 | 错误码语义 | 核对日期`；核对失败的（文档缺失/路径再漂移）→ 在表中标注 `BLOCKED` 并从本 WP 移除（登记规格 §十一.4），**不猜**。
- [ ] **步骤 3：锁定测试**：`test_wp12_locks.py` 断言传输层方法注册表与锁定表逐行一致（路径/参数名常量比对）。
- [ ] **步骤 4：三套绿**。
- [ ] **步骤 5：Commit**：`git commit -m "docs(wp12): 富途数据面端点依赖锁定表+锁定测试"`

### 任务 2：传输层方法组（筛选/板块/做空/基础数据/IPO/自选/衍生品）

**文件**：修改 `plugins/datasource/python/trading_datasource/futu_openapi.py`；测试 `tests/test_wp12_transport.py`（mock http，零网络）。

- [ ] **步骤 1：失败测试**（每组 2–4 个用例：参数校验拒绝 + 成功映射 + 错误码分派）：

```python
def test_stock_screen_validates_and_maps(self):
    c = client_with(mock_http(ok_envelope({...})))
    out = c.screen(market="HK", screen_queries=[...], retrieve_queries=[...], limit=300)
    mock_http.last_request["path"] == "/api/v1.0/quote/stock-screen"
    with self.assertRaises(ValueError): c.screen(market="HK", screen_queries=[], limit=301)

def test_short_daily_volume_no_data_is_empty(self):
    # errcode=-10 → {"items": [], "no_data": True}（空而非错——规格 §7.4）
    ...
def test_plate_list_region_guard(self):
    # plate_class=REGION & market=HK → 本地拒绝（-8 unsupported 语义前置，零网络）
    ...
```

- [ ] **步骤 2：验证失败** → **步骤 3：实现**：`OpenApiScreen/Plate/Short/BasicData/Ipo/Watchlist/Derivatives` 七组（类定义与 `OpenApiTrade` 同构：常量枚举 frozenset + 方法签名白名单；`OpenApiClient` 组合挂载）。全部方法**只做校验+一次 REST+envelope 解析**，不做业务聚合。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(datasource): OpenAPI 数据面传输方法组七类（锁定表对齐）"`

### 任务 3：OpenApiF10 方法组（23 端点）

**文件**：同上；测试 `tests/test_wp12_f10_transport.py`。

- [ ] **步骤 1：失败测试**：23 端点各一个「路径+关键参数+响应 passthrough」用例（表驱动：`[(section, method_name, path, params), ...]` 与锁定表逐行生成）；`announced_at`/`period_end` 类响应字段**原样透传**（解析留到落库层）。
- [ ] **步骤 2：验证失败** → **步骤 3：实现** `OpenApiF10`：统一 `f10(symbol, section, **section_params)` 分派 + 每 section 专用方法（`analyst_consensus(symbol)` 等，路径常量来自锁定表）。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(datasource): OpenApiF10 23 端点方法组（financials 族）"`

### 任务 4：服务端点面 + 工具面分档

**文件**：修改 `platform/server/futu_data.py`（FUTU_TOOLS 扩展/新端点路由）、`compute.py`、`app.py`（载荷白名单/EMPTY_PAYLOAD）、`mcp_tools.py`（TOOLS 清单+TOOL_COUNT 预算断言）、`caches.py`（TTL：F10/板块 30m–6h、做空 1h、实时族 0）、`store_access.py`（endpoints 声明）；测试 `tests/test_wp12_surface.py` + 更新 `tests/test_wp8_*` 锁定测试。

- [ ] **步骤 1：失败测试**：
  ① 直通 11 工具注册（stock_screen/plate_list/plate_stock/short_daily_volume/short_interest/ipo_list/economic_calendar_hot/economic_calendar_search/info_owner_plate/watchlist_list/watchlist_groups），名称/描述/字段与锁定表一致；
  ② 聚合工具 `f10_detail(symbol, section)`：section 枚举=锁定表 §C.5 的 26 项白名单；`derivative_detail(symbol, section)` 同理（4 值）；未知 section → `trading/invalid-operation`；
  ③ `TOOL_COUNT <= 80` 断言（基线 **61**=WP8 末 59 + WP10 `pipeline` + WP11 `sentiment_history`；加 12 直通 + 2 聚合 = **75**）；
  ④ `modify_user_security` 在 HTTP 端点面、**不在** `TOOL_NAMES`；HTTP-only 族（future_info 等）同理；
  ⑤ 缓存 TTL 登记与既有 `CACHE_TTL_MS` 表一致。
- [ ] **步骤 2：验证失败** → **步骤 3：实现**：路由模式照抄 WP8 任务 2（`futu_channel` 选择：openapi 有凭据走 REST，否则 `trading/openapi-unavailable` 如实）；载荷白名单逐端点（screen 类放行 `screen_queries/retrieve_queries/sort/sorts/next_key/limit`；f10_detail 仅 `symbol/section`）。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(platform): 数据面端点/工具面三档接入（直通12+聚合2，预算75）"`

### 任务 5：F10/做空/板块 PIT 落库 + research_snapshot 作业

> **锁定表发现（`docs/superpowers/plans/wp12-endpoint-lock.md` §D，任务 1，2026-09-16）**：
> ① 官方「搜索」= find-news/find-community（**不存在证券搜索端点**）→ 原计划的 `info_search_stock`
> 已删除，直通工具 12→11；② F10 族真实路径分 7 命名空间（financials/research/valuation/
> corporate-actions/shareholders/company/top-brokers），llms.txt 的 /f10/*.md 全 404；
> ③ llms.txt 漏列 `valuation/index-stocks`、`valuation/index-stock-plates`（补入）；
> ④ 经纪商拆为 top-brokers（实时）+ top-brokers-history（历史，`days_before` 必填）；
> ⑤ sim `total_asset` 与 live `total_assets` 字段不同名，勿混用；
> ⑥ `option-exercise-probability` 可能返回 -9（无期权数据权限）→ 如实拒绝不重试。
> **实现一律以锁定表为准**（路径/参数/错误码），本计划的枚举数字以锁定表为最终口径。

**文件**：修改 `store.py`（三新表）、新建 `plugins/core/python/trading_core/research_sync.py`、`cli.py` 子命令 `research-snapshot`、`daemon.py` build_jobs 追加；测试 `tests/test_wp12_pit.py`。

- [ ] **步骤 1：失败测试**：

```python
def test_f10_snapshot_pit_keys(self):
    # insert_f10(conn, symbol, section, period_end, announced_at, payload, fetched_at)
    # 财报类 section：announced_at 必填；快照类（机构持仓/评级等）：数据日期字段缺失时
    # announced_at=NULL 且 payload 附 {"_observed_note": "观测时点非数据时点"}（规格 §7.3）
    # read_f10(conn, symbol, section, as_of) → 只返 announced_at<=as_of 或（announced_at IS NULL 且 fetched_at<=as_of 当日）的行
def test_short_and_plate_snapshots(self):
    # short_snapshots(symbol,date,payload,fetched_at) 主键 (symbol,date)
    # plate_snapshots(date,market,plate_class,payload,fetched_at) 主键 (date,market,plate_class)
def test_research_snapshot_job(self):
    # run(home, market, conn, client=None)：关注池逐标的抓关键 section
    # （analyst_consensus/rating_summary/institutional/insider_trades/holding_changes）+ 做空2项
    # → 落三表；单端点失败 absent+warn 不阻塞；no_data → 空落库标记（当日幂等 REPLACE）
    ...
```

- [ ] **步骤 2：验证失败** → **步骤 3：实现**：三表进 `_SCHEMA`；`research_sync.run` 注入 client（缺省真实 OpenApiClient；无凭据 → 跳过 + info 告警「数据面未配置」）；限速串行（沿用 futu_mcp 退避口径）；作业入链 `research_snapshot`（factors_snapshot 之后，`sentiment_snapshot` 之前）。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): F10/做空/板块 PIT 三表+research_snapshot 每日作业"`

### 任务 6：UI 展示与文档

**文件**：`platform/web/src/pages/research.jsx`（F10 概览卡：分析师共识/评级/做空摘要，数据经 `f10_detail` 端点）、`pages/options.jsx`（衍生品 section 接入 derivative_detail 四项）、`services/api.js`/`endpoints.js` 更新；测试 web endpoints 追加。

- [ ] **步骤 1：web 测试**：端点清单含全部新端点；研究页渲染纯函数（`f10Summary(payload)` → 三行摘要：共识评级/目标价区间、评级分布、做空占比）失败先红。
- [ ] **步骤 2：实现**：研究页新「深度数据」折叠区（输入标的→f10_detail 三 section 并发→摘要卡；as_of/来源标注）；期权页补波动率/行权概率两个折叠卡。
- [ ] **步骤 3：`cd platform/web && npm test` + 三套绿**；UI 文案 grep 无违规。
- [ ] **步骤 4：文档**：`docs/TOOL-LIMITS.md` 新端点实测口径（每端点一行：参数坑/错误码/限频）；`docs/architecture.md` 端点表补全 + 工具面三档说明；`docs/OPENAPI-FEASIBILITY.md` 并入附录 A 摘要。
- [ ] **步骤 5：Commit**：`git commit -m "feat(platform): 研究页/期权页深度数据展示+数据面文档"`

### WP12 验收（对照规格 §7.4）

- [ ] 逐端点形状测试 + 锁定表一致；工具面 75 项 ≤80；写端点不进 MCP；
- [ ] PIT 三表按交易日累积、announced_at 覆盖率可统计（CLI `research-snapshot --stats` 输出）；
- [ ] no_data/unsupported 空而非错；单端点失败不阻塞链；三套全绿。
