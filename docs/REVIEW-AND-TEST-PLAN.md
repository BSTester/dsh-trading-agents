# 复盘 · 工作流 · 重启后全面测试清单

> 生成时间：2026-09-13 19:20 · 对应提交 `4b1e539`
> 本文档面向「重启 Harness → 切换模式 → 全面测试」这一次操作。

---

## 一、复盘：本轮到底改了什么

按主题列，每条都标注了**证据强度**（实测 / 推断）。

### 1. 统一数据层（净删约 460 行重复代码）

新建 `plugins/datasource`（`@bstester/dsh-datasource`，是库不是插件），持有唯一的：

| 模块 | 职责 | 合并前的副本数 |
|---|---|---|
| `futu_mcp.py` | 富途 MCP 客户端（握手/会话/重试/错误归一化/凭证探测） | **4** |
| `market.py` | 行情路由（富途优先，A股长历史走新浪） | **2**（91 行逐字相同） |
| `backtest.py` | 回测核心 | **2**（278 行仅差一行 import） |
| `locate.py` | 跨插件定位兄弟插件脚本 | — |

**为什么值得做**：危害不是重复代码，而是「修一处漏一处」。历史佐证：富途 K 线
`internal error` 排查时根因是参数名写错（`code_list` vs `symbol`），只有一份实现所以改一次就好；
同样的问题若发生在 `fetch_futu` 上要改三处，漏掉的那处会安静地继续坏着。

**加载机制**：安装器把 `python/` 解到 `$DSH_HOME/trading-python/`，并向交易 venv 写入
`dsh-trading-python.pth`，因此插件脚本直接 `import trading_datasource` 即可，无需 `PYTHONPATH`。
包刻意**不 eager 导入**子模块——纯本地路径不该为 `urllib`/`ssl` 付代价，也不该在禁网沙箱里失败。

### 2. 社交渠道改走 API

| 渠道 | 之前 | 现在 |
|---|---|---|
| X | DOM 抓取 40–50 s | `api/graphql` 热 **3.8 s**（社区 `XClientTransaction` 生成反爬头） |
| Reddit | 每次开浏览器 37 s | 纯 HTTP `/search.json` 热 **3.7 s**，**零浏览器** |

关键坑：**queryId 不在首页 HTML 里**，要从 `client-web/main.*.js` 提取；`get_cookies` 曾因
重复定义导致缓存从不落盘（每次重开浏览器）。

### 3. 面板：缓存 + 接口自检 + 三个视图重做

- **两层缓存**（客户端内存 + Host RPC TTL 1–30 分钟），快照兜底轮询 3 s → **60 s**，新增「刷新」按钮；
- **接口自检**：Host 在 snapshot 里声明实际提供的接口，客户端据此跳过缺失接口并给出可读原因
  （取代满屏 `transport failure ... HTTP 404`）；
- **执行页 → 交易概要**：7 条重复 JSON → **12 笔去重订单 + 6 个下单/改单/撤单动作**；
- **组合页 → 券商真实持仓**：按模式取账户，**不跨账户/币种合并**；
- **K 线图回归**：默认日线 + 60m/15m/5m/1m，canvas 自绘。

### 4. 富途 token：两个真 bug

1. **只认一种返回信封**。服务端同时存在 `{"ret_code":0,"data":{…}}`（行情类）与
   `{"s":"ok","d":{…}}`（账户类）。只认前者会把账户类的**成功响应**判成失败——
   `account_authorized_trd_accs` 因此报 `ret=None None`，拿不到 `acc_id`，**持仓必然读不出来**。
2. **access_token 只有 2 小时**（官方 `expires_in=7200`，实测确认）。过期时服务端对所有工具
   返回 `internal error` **而不是 401**，且 `initialize` 仍然成功——旧探测因此长期误报「token 有效」。

### 5. 补齐的两个缺口

- **权益每日盯市**：`positions.py` 为有持仓的账户补取资金，写入
  `~/.dsh/trading-equity-<mode>.json`（每日一条，同日覆盖，保留 400 条）。
  **只从首次取数当日起累积，不回溯伪造历史**——用当前持仓反推过去会得到一个
  从未真实存在过的数字。
- **质量因子**：新增 `quality.py` + `quality` 接口，从 `quote_financials_statements`
  的利润表原文计算毛利率/营业利润率/净利率/研发占比/实际税率/同比。
  **科目名按市场不同**（美股/港股/A股各一套），按候选名依次匹配；缺科目就是缺指标。
  **ROE/ROA**：富途没有资产负债表接口，按渠道优先级落到
  `trading_datasource.fundamentals`——**Yahoo Finance 三市场实测可用**，
  A 股再退 AKShare（东方财富）。返回里带来源与两个报表期，两条都拿不到才标为不可得。
  实测：AAPL ROE 151.91% / 腾讯 19.48% / 阿里 9.76% / 茅台 33.65% / 平安银行 7.73%。

### 6. 修掉五个"从来没成功过"的接口

用户点面板时报出来的：

```
Command failed: .../analytics.py sources
analytics.py: error: argument cmd: invalid choice: 'sources'
```

查下去发现**不止一个**：`analytics.py` 的子命令只有
`{equity, positions, correlation, risk, trades}`，而另外五个 provider 一直在
往它上面调：

| 接口 | 原先调用 | 实际应该是 | 状态 |
|---|---|---|---|
| `sources` | `analytics.py sources` | `sources.py`（选项式，无子命令） | 已修 |
| `events` | `analytics.py events …` | `events.py` | 已修 |
| `factors` | `analytics.py snapshot …` | `factors.py snapshot …` | 已修 |
| `ic` | `analytics.py ic …` | `factors.py ic …` | 已修 |
| `sensitivity` | `analytics.py sensitivity …` | `sensitivity.py` | 已修 |

**这几个接口从加入仓库那天起就没成功过**（`sources` 自 `c082963` 起），
因为路由错误只在真跑一次时才暴露：argparse 报错发生在业务逻辑之前，
而单元测试覆盖的是 python 脚本本身（直接跑 `sources.py` 是正常的），
没有覆盖 JS 这一层的脚本选择。

新增 `tests/analytics-routing.test.mjs`（6 例）把路由钉死：
逐个 provider 断言它执行的脚本与首个参数、断言期望表覆盖了全部 provider
（新增接口必须同步登记）、断言 `analytics.py` 的子命令集合没变、
断言被执行的脚本真实存在、断言选项式脚本不得收到位置参数。

修复后端到端实测：

```
sources      0.4s   events      5.8s   factors    24.3s
ic          10.9s   sensitivity 12.5s   —— 全部返回真实数据
```

### 7. 安全相关：授权范围实测

脚本请求 `quote:read accid:* trade:read`，服务端实际下发
`quote:read quote:write trade:read **trade:write** accid:…`——**多授了交易写权限**。
即**请求的 `scope` 不是权限上限**。执行边界只有仓库内两层：账户模式互斥（默认 sim）
+ Harness 原生实盘审批。**模式守卫是插件级的**：凭据本身允许下单，绕过插件直接走
HTTP/shell 不受其拦截（仓库明令禁止，但那是约定不是技术强制）。

---

## 二、状态矩阵：已验证 vs 未验证

| 能力 | 状态 | 验证方式 |
|---|---|---|
| 富途 MCP **服务端** | ✅ 实测 | A股/港股/美股 × 1m/5m/15m/60m/日线全部拿到真实 K 线 |
| 统一数据层（脚本通道） | ✅ 实测 | 安装态、无 `PYTHONPATH` 直接 import 并取数 |
| 券商真实持仓 | ✅ 实测 | 模拟盘 **16 笔 / 3 个账户**（港股 6、A股 8、美股 2） |
| X / Reddit API 通道 | ✅ 实测 | `x: ok:api/graphql`、`reddit: ok:api/json` |
| 回测 / 风控 / 因子 / 审计 | ✅ 实测 | 离线测试 + 真实数据跑通 |
| **Harness MCP 插件通道**（`mcp__futu__*`） | ⚠️ **未验证** | 本会话不是交易模式，`Tool.listTools` 确认无这些工具 |
| **面板全部页签**（重启后） | ⚠️ **未验证** | 运行进程仍是 18 小时前的旧代码 |
| **保活插件在 Harness 内运行** | ⚠️ **未验证** | 同上 |
| 质量因子（财报计算） | ✅ 实测 | 三市场：AAPL 毛利 50.06% / 腾讯 57.83% / 茅台 63.59% |
| ROE/ROA（备用源） | ✅ 实测 | AAPL 151.91% / 腾讯 19.48% / 阿里 9.76% / 茅台 33.65% |
| 权益每日盯市 | ✅ 实测 | 3 个账户写入当日盯市（市值/现金/总资产） |
| **token 热重载**（recompose） | ⚠️ **未验证** | 依据源码实现（印章 = mtime+size），需交易会话实测 |

> 说明：我本会话所有富途验证走的都是**脚本通道**。两条通道共用同一服务端与同一套工具，
> 但客户端实现完全不同，所以 MCP 通道必须在交易模式会话里单独验证。

---

## 三、重启后才会生效的清单

运行进程 `PID 26228` 已运行 **18 小时 29 分**，以下**一个都没生效**：

- [ ] **面板接口**（`snapshot`/`switch-mode`/`series`/`equity`/`positions`/`correlation`/
      `sensitivity`/`risk`/`trades`/`events`/`factors`/`ic`/`audit`/`sources`/`instrument` 共 15 个）
      ——运行进程中不可用。依据：进程启动于 **00:48:38**，而 workbench 的 Host 半边
      （提供这些路由的 `plugins/workbench/src/index.js`）是 **01:01 之后**才逐批引入的，
      最近一批到 14:16。另：Host 服务目录中查不到 `tradingWorkbench`。
      > 具体缺哪几个我**没能直接探测**（鉴权在路由分发之前，未授权请求一律 401，无法区分
      > 「路由不存在」与「未授权」）。以上为提交时间线 + 服务目录的推断，非实测。
      > 这不影响结论与操作：重启后以面板顶部的自检横幅为准。
- [ ] 面板缓存与接口自检
- [ ] 执行页 → 交易概要
- [ ] 组合页 → 券商真实持仓
- [ ] 行情页 → K 线图
- [ ] `futu-keepalive` 保活行
- [ ] 修正后的 skill 自检（真调工具）
- [ ] `AuditView` 漏传 `rpc` 的修复

---

## 四、工作流（明确）

### 4.1 两条路径，不要混用

| | 研报路径 | 量化快速路径 |
|---|---|---|
| 触发 | 用户明确要求深度研究（"分析一下 X"、"出份研报"） | 只要信号/回测/下单建议 |
| 内容 | skill `trading-agents`：12 角色 × 6 阶段（4 分析师并行 → 辩论 → 交易员 → 风控 → 组合） | skill `quant-trading`：`quant_signal` → 风控参数 → 可选 `quant_backtest` → 下单建议 |
| 禁止 | — | **不得**跑 12 角色流水线 |

### 4.2 数据渠道优先级（固定）

```
1. 富途 MCP（mcp__futu__*）  ← 能取到的一律先走这里
2. X                          ← 必取（舆情）
3. Reddit                     ← 同源登录态
4. AKShare                    ← A股补充
5. Yahoo / 网页搜索            ← 兜底
```

所有数据必须**注明来源与时间**；取不到就明说，不得用占位数据填充。

### 4.3 模式与审批

```
默认 sim ──(用户在工作台输入「确认实盘」)──> live
   │                                          │
   └── 只能用 sim_trade_*，绝不碰 account_*/trading_*
                                              └── 只能用 account_*/trading_*，绝不碰 sim_trade_*
```

- 模式由 `policy.js` 强制；**切换不是授权下单**，每笔订单仍需会话内逐笔复述并确认；
- 切回 sim 可在面板操作，或请求 `quant_switch(mode="sim")`；
- **实盘未达无人值守准入条件**（见 `docs/P4-live-trading.md`）。

### 4.4 显示层边界

工作台**只做展示**：研报、交易概要、券商持仓、量化预览。它没有聊天框、没有下单入口。
研报与下单都在 Harness 对话里完成。

### 4.5 token 生命周期（自动化）

| 通道 | 机制 |
|---|---|
| MCP（`mcp__futu__*`） | `futu-keepalive` 行每 10 分钟检查，剩余 < 30 分钟时续期 |
| 脚本（`trading_datasource`） | 遇 `internal error` 自动续期并重试一次 |

**为什么续期后要"重建组合"**：Authorization 头在**组合挂载时求值一次**，preset 目录
没有 watcher。但 `ensureStanding()` 用 `{mtimeMs, size}` 作印章，印章变了就销毁并重建
共享挂载（重新求值 `!!js`）。触发入口有两个：新会话的 `mount()`，和现有会话的
`agentPresets.recompose()`。保活插件在续期后会 touch 组合文件并调用 `recompose`，
因此**已在运行的会话**也可能直接换上新 token（此路径**尚未运行时验证**，
失败会自动提示新建会话）。

---

## 五、重启与全面测试清单

### 步骤 0：重启

```bash
kill 26228 26227 26175
cd /home/penn/workspace/Harness && npx @deepseek-ai/dsh web
```

**预期**：新 PID、`etime` 归零。

### 步骤 1：面板打开（先别问 AI）

点右下角「交易工作台」。

| 检查项 | 预期 |
|---|---|
| 顶部横幅 | **不应出现**「未加载 N 个工作台接口」 |
| 数据源徽标 | 显示"数据源正常"（或列出待配置项） |
| 切页签 | 不报错；数据来自缓存，切换不重复取数 |

> ❌ 若仍见横幅，说明 Host 仍缺接口 → 把横幅文字发我。

### 步骤 2：新建交易模式会话，第一步验证通道

新建会话（选「交易智囊模式」），然后说：

> 检查一下富途通道是否可用

**预期**：AI **真调一次** `quote_trading_days` 并报回数据，而不是只说"工具列表里有"。

> ❌ 若返回 `internal error` → 几乎可断定 token 过期；跑
> `python scripts/futu_auth.py --refresh` 后**再新建会话**。

### 步骤 3：逐页签验证

| 页签 | 输入 | 预期结果 |
|---|---|---|
| 行情 | `00700.HK` | 标的卡（价格/涨跌/区间 + 跳转富途）+ **K 线图默认日线**；切 5m 应重新取数一次 |
| 组合 | — | **券商持仓**：模拟盘 3 个账户有持仓（港股 6 / A股 8 / 美股 2）共 16 笔；下方**独立**的「本地策略台账」卡 |
| 执行 | — | **交易概要**：12 笔订单 + 6 个动作（下单/改单/撤单），只读查询仅计数不铺开 |
| 研究 | — | 有研报则列表分页；标题可点进详情页 |
| 因子 / 事件 / 风险 / 审计 | — | 各自数据；审计页**不应**报错（此前漏传 `rpc`） |
| 渠道自检 | — | 6 个渠道状态（富途/x/reddit/venv/cache/risk），富途显示**剩余有效期** |

### 步骤 4：两条路径各跑一次

**研报路径**（会跑 12 角色，耗时较长）：

> 分析一下 00700.HK，把完整报告发布到工作台

预期：工作台「研究」页出现该报告，含来源与时间。

**量化快速路径**：

> 看下 600519 的信号，再回测一下双均线

预期：`quant_signal` + `quant_backtest`，结果进「量化预览」；**不应**触发 12 角色流水线。

### 步骤 5：模式切换与隔离

1. 面板切到 live（需输入「确认实盘」）→ 说"查一下账户持仓" → 应走 `account_*`；
2. 试着让它用 `sim_trade_*` → **应被拒绝**（模式互斥生效）；
3. 切回 sim，重复第 2 步反向验证。

> ⚠️ live 下**不要**让我下单，除非你确实要走实盘。`trade:write` 已在凭据里。

### 步骤 6：新功能

| 页签 | 操作 | 预期 |
|---|---|---|
| 因子 | 选中一个有财报的标的（如 `US.AAPL`） | 「质量因子」卡显示毛利率/营业利润率/净利率/研发占比/实际税率/同比，并**明确列出 ROE/ROA 不可得的原因** |
| 组合 | 打开 | 「账户权益（每日盯市）」卡出现当日记录；**注意只有 1 个点**（从今天开始记，这是设计如此） |

### 步骤 7（可选，需等约 90 分钟）：验证保活与热重载

留一个交易会话挂着 **约 90 分钟**（不要关），观察：

- 若日志出现 `futu-keepalive: 已续期…；已热重载组合，本会话的富途工具将使用新 token`，
  则该会话**无需新建**即可继续用富途工具 → 热重载生效；
- 若出现 `…需新建会话`，说明热重载未生效（这是已知未验证项），**新建会话**即可恢复。

> 这个 90 分钟内不会触发，所以**不会影响你前面的测试**。

### 步骤 8（可选，需等 2 小时）：验证保活

留一个会话挂着，约 **90 分钟后**新建会话，检查富途工具是否**仍然可用**——
可用即说明保活生效（旧会话失效属预期）。

---

## 六、已知缺口（诚实清单）

| 缺口 | 影响 | 状态 |
|---|---|---|
| 凭据含 `trade:write` | 模式守卫是插件级，绕过后无技术拦截 | 已记录；可尝试在授权页不勾选交易权限 |
| 长会话 2 小时悬崖 | 已实现热重载（touch 印章 + `recompose`） | **代码已就绪，运行时未验证**；失败自动回退"新建会话" |
| 权益曲线 | 账户权益改为**每日盯市**，从今天开始累积 | ✅ 已做（不回溯伪造历史；不跨账户合计） |
| 质量因子 | 利润率族（富途）+ ROE/ROA（Yahoo 备用源） | ✅ 已做；ROE/ROA 带来源与报表期，全部失败才标不可得 |
| 两套账 | 本地策略台账 ≠ 券商持仓 | 界面上已明确分开标注 |
| P4 实盘 | 未达无人值守准入 | 由你 gate |

---

## 六之二、安装（标准路径实测）

2026-09-13 用临时 `DSH_HOME` 做了一次**真实全新安装**（`git clone` → 建 venv → 安装器）：

| 检查项 | 结果 |
|---|---|
| preset 克隆到 `.agent-presets/dsh-trading-agents` | ✅ |
| 4 个插件装进 profile 的 node_modules | ✅ workbench 0.20.0 / fin-data 0.6.1 / engine 0.4.1 / keepalive 0.2.0 |
| 统一数据层解到 `trading-python/{datasource,fin-data}` | ✅ |
| `.pth` 写入交易 venv，指向 datasource | ✅ |
| preset 三行（fin-data / trading-engine / futu-keepalive）被启用 | ✅ |
| 仅标准库的 venv 能 `import trading_datasource` | ✅ 四个模块全通 |

**发现的一个限制**：只跑 `dsh plugin add` 单独装插件**装不出统一数据层**。
实测：单独装 workbench 后跑它的脚本 → `ModuleNotFoundError: No module named 'trading_datasource'`。
涉及的脚本：workbench **9/9**、engine **2/3**；fin-data **0/5**（它不依赖统一数据层，
所以按预设注释里那条单独安装 fin-data 是可以的）。

为此给安装器加了 `check` 动作：

```bash
python scripts/install_plugins.py check --repo <preset 目录> --dsh-home ~/.dsh
```

它逐项核对 preset 行是否启用、4 个插件是否装上、统一数据层与 `.pth` 是否就位，
发现问题时给出确切的修复命令。已覆盖测试（含"行被禁用""`.pth` 缺失""插件缺失""无 venv"）。

## 六之三、缓存：目录与策略

面板是**查看用途**，不追求实时。缓存分三级，冷启动实测（`factors` 30.6s、
`correlation` 9.7s、`positions` 10.8s、`instrument` 7.1s、`events` 5.7s、`series` 4.9s）：

| 级 | 位置 | 生效范围 | 命中耗时 |
|---|---|---|---|
| 客户端内存 | 浏览器内存（`endpointCache`） | 切页签不重发请求 | 0 ms（连 RPC 都不发） |
| Host 内存 | dsh 进程内 | 同进程内重复请求 | 0–1 ms |
| **Host 磁盘** | **`~/.dsh/trading-workbench-cache/`** | **跨进程/重启** | 0–13 ms |

**为什么加磁盘这一级**：原先只有内存，**进程一重启缓存全丢**——用户每次重启后
第一次点每个页签都要重新等一遍（实测一次冷启动合计 **69.7 秒**）。
加了磁盘后，新进程访问同样数据合计 **0.0 秒**（`positions` 13 ms，其余 0–1 ms）。

TTL（`plugins/workbench/src/rpc.js` 的 `CACHE_TTL_MS`，已按"不追求实时"放宽）：

| 接口 | TTL | 接口 | TTL |
|---|---|---|---|
| `instrument` `series` | 10 分钟 | `correlation` `factors` `ic` | 30 分钟 |
| `equity` `positions` `trades` `sources` | 5 分钟 | `sensitivity` `events` `quality` | 60 分钟 |
| `risk` | 15 分钟 | `audit` | 2 分钟 |

其它磁盘缓存（不属于面板 RPC 缓存，但同属"本地缓存"）：

| 路径 | 内容 | TTL |
|---|---|---|
| `~/.dsh/trading-series/` | K 线序列（按 标的-周期 一个文件） | 由取数逻辑使用，不按 TTL 失效 |
| `~/.dsh/trading-positions-<mode>.json` | 券商持仓 | 5 分钟 |
| `~/.dsh/trading-equity-<mode>.json` | 每日盯市 | 永久累积（保留 400 条） |
| `~/.dsh/trading-workbench.json` | 研报/预览/交易动态 | 持久 |
| `~/.dsh/trading-python/` | 统一数据层（安装产物，不是缓存） | — |

**两点行为变化**：

1. **切换模拟/实盘不再清空缓存**。缓存键里本来就带 `mode`，两套数据天然分开；
   原先清空全部缓存导致每次切模式后所有页签都要重新取数（`factors` 冷启动 30 秒）。
2. 面板顶部显示"本地缓存 N 项"，让"是否在缓存"这件事在界面上可见。
   「刷新」按钮仍会清空客户端缓存，并让随后 5 秒内的请求穿透 Host 缓存。

## 七、回滚方式

```bash
# 回滚到本次复盘对应的提交
git -C /home/penn/workspace/dsh-trading-agents reset --hard 4b1e539
# 重新部署（顺序不能反：先 rsync，再安装器）
DEST=~/.dsh/.agent-presets/dsh-trading-agents
rsync -a --delete --exclude node_modules --exclude .git --exclude __pycache__ \
  --exclude '.install-*' ./ "$DEST/"
python3 scripts/install_plugins.py install --repo "$DEST" --dsh-home ~/.dsh
```

> ⚠️ **顺序很重要**：`rsync` 会用仓库里的 `disabled: true` 覆盖部署副本，
> 必须**在 rsync 之后**跑安装器把 `fin-data` / `trading-engine` / `futu-keepalive` 三行启用。
> 这一步我本轮漏过一次，已修正。
