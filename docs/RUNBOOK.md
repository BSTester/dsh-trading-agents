# 运维 Runbook（WP5）

> **状态标注：** 本文涉及 daemon、指令目录、心跳文件、工作台页面的步骤按 WP4 计划规格撰写，**以 WP4 合并后实测为准**；「演练记录」小节**待 WP4 daemon 合并后执行**回填。
> kill switch 演练内核（`scripts/drills.sh`）与风控联动已落地（WP5 任务 1），可先行执行。
> **WP7（2026-09-16）**：daemon 常驻循环由**平台服务内调度器**承担（随服务进程存活，
> daemon CLI 保留为手动入口），场景 1 的「daemon 崩溃重启」对应平台服务进程的重启；
> 其余协议（心跳/指令目录/kill/告警）不变。

恢复原则（规格 §8.4，P4 延续）：daemon 崩溃 systemd 重启；执行中断订单留 `unknown` 由对账兜底；**先查券商再动手，不自动清除未知在途状态，不自动平仓**。


> **首启必做（否则平台在跑但什么都没发生）**：配置关注池——`~/.dsh/trading-venv/bin/python -m trading_core watchlist-init --from-index SH.000300`（需 universe 表已有该指数成分快照；缺它是全部数据作业静默跳过的原因，流程页会显示「关注池未配置」提示）。**完整三步自举（`universe` → `watchlist-init` → 三市场 `calendar`，以及日历由 18:50 `sync_calendar` 自维护、首启当天要手工跑一次）见 [../README.md](../README.md)「装完之后」**。

### 订单终态人工对齐（`oms-align`，最后手段）

**优先顺序**：① 先跑 `reconcile-daily`（对账会按官方模拟交易状态码自动收敛「券商已撤、
本地仍在途」的行）；② 对账覆盖不到时才用人工入口（例如 S2 修复之前留下的历史行）：

```bash
~/.dsh/trading-venv/bin/python -m trading_core oms-align \
  --broker-order-id 7148788 --status cancelled \
  --reason "券商订单已撤且 cum_qty=0（E2E 探针遗留，对账未覆盖）" --operator <你的标识>
```

纪律：只允许 `cancelled`/`rejected`；必须命中本地订单行（不伪造）；必须走合法状态机路径
（**已成交的单不可能被降级**）；`reason` 必填；成功写一条 `warn` 告警（含券商单号、
from→to、原因、操作者）；**绝不触达券商**。用一个受约束入口而不是手改 SQLite——后者
不可审计。


## 0. 前置与路径速查

前置：`~/.dsh/trading-venv` 存在，且执行过 `install_plugins.py install` 把 `trading_core`/`trading_datasource` 解出到 `~/.dsh/trading-python/`（`link` 写 venv 的 `.pth` 指向这些副本）。

**改了量化侧代码之后跑 `refresh`，不是 `link`**（2026-09-17 实机部署缺口）：`link` 只重写
`.pth`，副本内容不动 —— 无仓库的生产机上子进程读的仍是旧副本。`refresh` 重新解出
core/datasource/fin-data 三个副本并顺带重写 `.pth`，且**不碰 profile / preset / pnpm**，
比 `install` 快且无 Web 副作用，可反复重跑。有仓库的开发机上子进程会优先仓库代码
（`trading_datasource.repo_paths`），所以本地开发不刷新也能跑；**生产必须刷新**。
升级/部署的完整五层流程见 [../README.md](../README.md)「更新到最新版本」。

| 对象 | 路径 / 键 | 说明 |
|---|---|---|
| daemon 心跳 | `~/.dsh/trading-daemon.json` | `heartbeat` 字段；> 5 分钟未刷新工作台标红；`critical: true` 为常驻红点标志位 |
| 指令目录 | `~/.dsh/trading-commands/{pending,processed}/` | 工作台 → daemon **唯一通道**；白名单仅 5 种（execute_plan/cancel_plan/kill/unkill/run_job）；nonce + processed/ 去重 |
| kill 文件 | `~/.dsh/trading-kill` | 存在即风控规则 1 拒绝一切订单；`unkill` = 删除该文件 |
| 熔断 halt | SQLite kv 键 `halt:active` | 库默认 `$DSH_HOME/trading-data/trading.sqlite`；`store.is_halted/clear_halt` 读写 |
| 告警 | `alerts` 表（info/warn/critical） | critical 同时置心跳标志位（WP4 任务 3 落库） |
| 富途凭证 | `~/.dsh/futu-token`（access）/ `~/.dsh/futu-refresh`（续期） | access 约 2 小时过期，过期特征是工具批量返回 internal error |
| 数据依赖核验 | `scripts/verify-data-deps.py` | 约定：富途侧发版异常（internal error 面扩大）时**先跑本脚本**定位漂移面 |
| kill 演练 | `scripts/drills.sh` | 建 kill → 断言风控拒单 → 清除，输出 JSON，无残留 |

## 平台服务（FastAPI 单进程，WP6；WP7 独立量化平台）

工作台独立服务：**一个进程**承载 HTTP API（`POST /api/wb/<endpoint>`）、MCP
（`/mcp`，streamable-http，**77 工具** = 82 端点中转发 72 + 5 个维护工具，Harness 侧
工具名 `mcp__quantwb__*`）与前端静态托管
（`platform/web/dist`）。默认 `127.0.0.1:8397`，loopback 绑定，可选静态 token。

### 依赖安装（一次性，需联网）

```bash
~/.dsh/trading-venv/bin/pip install -r platform/requirements.txt   # fastapi/uvicorn/mcp/httpx
```

前端构建产物由服务托管，改动前端后必须重新构建：

```bash
npm --prefix platform/web install
npm --prefix platform/web run build     # 输出 platform/web/dist
```

### 启动

```bash
cd platform && ~/.dsh/trading-venv/bin/python -m server.run
# 或：~/.dsh/trading-venv/bin/python platform/server/run.py
```

- **不能用 `python -m platform.server.run`**：标准库 `platform` 模块遮蔽同名包，
  `-m` 会解析到标准库而失败（`ModuleNotFoundError: 'platform' is not a package`）。
- 服务应在 `~/.dsh/trading-venv` 内启动：`compute.PYTHON` 取 `sys.executable`，
  用系统 Python 启动会让分析/核心子进程改用系统解释器；`run.py` 在解释器与 venv
  不一致时向 stderr 打一行告警 JSON（不硬失败）。
- 启动成功打印**单行 JSON**：`{"ok": true, "service": "quant-platform", "url": ...,
  "mcp": ".../mcp", "tools": 77, "auth": "loopback-only"}`。
  工具数不是常量直觉：它是 `82 端点 − 10 排除 + 5 维护` 的推导结果，改了端点表要同步
  `platform/server/mcp_tools.py` 的 `TOOL_COUNT` 与本节（不变式：`forwarded == endpoints − excluded`）。

### 服务启停（推荐方式：`scripts/platform_service.sh`）

上面的前台命令**只适合临时排障**：前台进程随终端结束而死；`setsid nohup ... &` 在部分
环境下也会被回收（本仓库实测：会话结束时进程消失、端口释放）。**长期运行请用脚本**——
它用与 `platform-autostart` 插件**完全相同的语义**拉起（node `spawn({detached:true,
stdio:[ignore,log,log]}) + unref()`，node 不可用时退回 `setsid` 兜底），因此进程随会话结束
仍存活。

```bash
scripts/platform_service.sh start      # 未运行才拉起（已在运行则打印现状，不重复拉起）
scripts/platform_service.sh stop       # 优雅 TERM → 等端口释放 → 超时才 KILL
scripts/platform_service.sh status     # 端口/健康/PID/日志尾部/端点与工具面计数
scripts/platform_service.sh restart    # stop + start
scripts/platform_service.sh refresh    # 刷新安装副本（core/datasource/fin-data）；改量化侧代码后跑
```

- **改了 core/datasource/fin-data 的代码 → 先 `refresh` 再（按需）`restart`**：生产环境
  没有仓库可优先，子进程经 venv 的 `.pth` 加载 `~/.dsh/trading-python/*` 这份**安装副本**；
  不刷新就是「代码改了、行为没变」。`refresh` 只重解这三份副本并重写 `.pth`，**不碰
  web profile、不跑 pnpm、不重启服务**（子进程下次拉起即用新代码；服务进程自身仍需
  `restart` 才换代码）。幂等，可随时重跑。原理见 `HANDOVER.md`「代码解析路径」。

- **地址来自配置**：`<home>/trading-platform.json` 的 `service.host` / `service.port`
  （脚本不硬编码 8397；坏 JSON 回落默认）。
- **与 autostart 的关系**：插件在**会话开始时**探活并按需拉起，脚本是**显式**入口
  （运维/演练/CI）。二者共享日志文件与仓库定位规则（`DSH_TRADING_REPO` > 标记文件
  `~/.dsh/trading-platform-repo` > 脚本上一级目录）；重复拉起会被「已在运行」拦下，
  端口绑定本身也保证最终只有一个监听者。
- **日志**：`~/.dsh/trading-platform-service.log`（与插件同文件，append）。
- **退出码**：`0` 成功；`1` 未就绪/未停止；`2` 前置缺失（仓库/venv/入口），且
  `2` 会打印创建 venv 的完整命令。
- **端口被非本服务占用**时脚本**不会强杀**，只报告占用 PID 并建议改端口——避免误杀
  别人的进程。

### 会话自动拉起（platform-autostart 插件）

preset 行 `platform-autostart` 已默认启用：**会话启动时自动 `GET /healthz` 检测**本服务。
已在跑则什么都不做；未启动且仓库/venv 就绪时，以**分离进程**（detached + unref，随会话
结束仍存活）执行 `cd <repo>/platform && <venv-python> -m server.run`；venv/仓库未安装时
不强行启动，仅打一行日志给安装指引（按 install/HARNESS_SETUP.md 安装）。

- **日志**：`~/.dsh/trading-platform-service.log` —— 被拉起服务的 stdout/stderr 与插件的
  运行记录（`{"event":"platform-autostart","action":"ok|started|not-installed|spawn-failed",...}`）
  都在这里；排障先看本文件，再看 Harness 控制台的同款 JSON 行。
- **仓库定位**：环境变量 `DSH_TRADING_REPO` > 标记文件 `~/.dsh/trading-platform-repo`
  （`install_plugins.py` 的 install/update 与 `install_platform.py` 的 service 步写入，
  内容为仓库根路径）> 未找到 → `not-installed` 提示。
- **并发会话竞态是安全的**：每个会话启动都以 `/healthz` 为闸门——后启动者探测到服务
  已在跑即收手（`action:"ok"`）；极端情况下多个会话同时探测失败、各自 spawn，也只有
  一个进程能绑定端口，其余按 `run.py` 的既定行为打印 `{"ok":false}` 后**绑定失败自退**，
  不会出现双监听。
- **端口**：读 `~/.dsh/trading-platform.json` 的 `service.port`（默认 8397，坏 JSON 回落
  默认）；插件只负责"把进程拉起来"，服务自身的 token 认证、sim/live 闸门、交易确认
  全部留在服务侧，不受影响。
- 插件自身失败绝不影响会话：检测/拉起全程捕获，每个会话最多尝试启动一次。

### 配置

`~/.dsh/trading-platform.json`（文件缺失或字段缺省即取默认；`DSH_HOME` 可换根目录）：

```json
{ "service": { "port": 8397, "host": "127.0.0.1", "token": null } }
```

`TRADING_SERVICE_PORT` 环境变量最后覆盖端口（`0` = 由内核分配临时端口，供测试用）；
`token` 非空时 `/api/*` 与 `/mcp` 均要求 `Authorization: Bearer <token>`，
`/healthz` 与静态前端豁免。

### 健康检查与冒烟

```bash
curl -s http://127.0.0.1:8397/healthz            # {"ok":true,"mode":"sim","scheduler":{"alive":true,"last_error":null}}
curl -s -X POST http://127.0.0.1:8397/api/wb/snapshot \
  -H 'content-type: application/json' -d '{}'    # envelope：{"ok":true,"value":{...}}
curl -s -D - -o /dev/null http://127.0.0.1:8397/  # 200 + text/html; charset=utf-8（dist/index.html）
~/.dsh/trading-venv/bin/python -B -m unittest tests.test_wp6_mcp -v   # MCP 协议冒烟 S1–S4
```

（静态路径只认 GET：`curl -I` 发的是 HEAD，会得到 405「仅 GET」信封——这是有意行为，不是故障。）

排障要点：`/healthz` 不通 → 看进程是否在、端口是否被占（`ss -ltnp | grep 8397`）；
`GET /` 404 → `platform/web/dist` 未构建；Harness 里看不到 `mcp__quantwb__*` →
确认 preset 的 `quant-platform-mcp` 行已启用、服务已起、新会话已新建
（`failOnStartupError: false`，服务未起时安静降级）；取数类工具全报
`trading/*-unavailable` → 多因服务不是用 venv 解释器启动。

### 服务内调度器（WP7）

调度器随服务 lifespan 启停（服务停则调度停，`systemd` unit 只需管服务进程一个）：
`create_app` 缺省装配 `Scheduler(build_tick(home), interval=60)`——daemon 线程每 60s 一轮
tick，协议原样复用 `trading_core.daemon.tick`/`JOBS_DEFAULT`/心跳路径/告警落库；
daemon CLI（`python -m trading_core daemon`）保留为手动入口。

- **tick-first 与补跑**：启动即先跑一轮再等间隔——当日已到期的作业立即补跑，不空等下一个周期。与手动 daemon 共享 kv `daemon:state` 的 ran 标记（键 `市场:作业名:日期`），**同日作业不重复执行**（服务与手动 daemon 先后跑同一天也只执行一次）。
- **心跳/告警协议不变**：`~/.dsh/trading-daemon.json` 的 `heartbeat`（> 5 分钟未刷新工作台标红）；告警仍分级落 `alerts` 表。
- **`/healthz` 的 `scheduler` 字段**：`{"alive": bool, "last_error": str|null}`。`last_error` 保留**最近一次** tick 异常（300 字符截断）、**成功不自动清除**——它表示「最近一次出错记录」，不是「上一轮是否出错」；确认已恢复看 `alive: true` 与此后新日期作业是否正常留痕，需要抹掉旧记录只能重启服务。
- **tick 异常不杀线程**：单轮异常记录进 `last_error` 后继续下一轮；连续多轮同一 `last_error` → 查对应 `trading_core` 子命令与 SQLite 库（`trading-data/trading.sqlite`）。

### factors-history 查询（WP7）

因子快照由调度器每个交易日收盘后自动落 `factor_snapshots` 表（作业链末尾的
`factors_snapshot`：SH 16:15 / HK 16:35 / US 05:35，`--tickers @watchlist`；
非交易日或日历未同步不跑，失败落 `alerts`）。三条等价查询路径：

```bash
# HTTP（TTL 5m 缓存；limit 1..120，默认 30）
curl -s -X POST http://127.0.0.1:8397/api/wb/factors-history \
  -H 'content-type: application/json' -d '{"limit": 10}'
# CLI（与 HTTP/MCP 同源，JSON 输出）
~/.dsh/trading-venv/bin/python -B -m trading_core factors-history --limit 10
# MCP：Harness 会话内调用 mcp__quantwb__factors_history {limit}
```

缺当日快照先查调度器：`/healthz` 的 `scheduler` 字段 + `alerts` 表该日 `factors_snapshot`
告警；再查 `factor_snapshots` 表确认落库情况。

### 交易闸门排障（WP7）

写路径 `trade_place/trade_modify/trade_cancel` 全链留痕（改单=撤旧重下），排查顺序：

1. **看拒绝原因**：响应 `error.message` 信封化（绝不 500），错误码族
   `trading/order-rejected`（参数/模式/风控/确认/查重）、`trading/broker-unavailable`
   （券商通道异常/未接入）、`trading/invalid-operation`（载荷非法）。两张表对账：
   ```bash
   sqlite3 ~/.dsh/trading-data/trading.sqlite \
     "SELECT client_order_id,symbol,side,qty,status,mode,updated_at FROM orders ORDER BY updated_at DESC LIMIT 10;"
   sqlite3 ~/.dsh/trading-data/trading.sqlite \
     "SELECT rule,allowed,reason,created_at FROM risk_checks ORDER BY id DESC LIMIT 10;"
   ```
   （风控 8 规则拒绝时 `risk_checks.allowed=0`，`reason` 即中文原因；`orders` 状态机迁移
   白名单见场景 2。）
2. **kill 文件**：`~/.dsh/trading-kill` 存在即规则 1 拒绝一切订单；`unkill` = 人工确认后删除。
3. **业务确认 TTL 120s**：live 写在 Web 确认卡片等待用户作答，TTL 到期由服务按**拒绝**收尾
   （fail-closed，订单迁 `cancelled`）；preset 行 `toolCallTimeoutMs: 180000` = TTL 120s +
   作答与子进程取数余量——模型侧超时不代表服务端放弃，最终以闸门信封为准。
4. **live 写 = OpenAPI 执行协议（WP8）**：live 下 `trade_*` 经 OpenAPI place/modify/cancel/
   order-confirm（协议已接入，**未经真实 live 下单验证**——sim→live 冒烟属 P4 人工准入，
   见 `docs/P4-live-trading.md` 第 11 项）；未配置 OpenAPI 凭据时确认前即拒，
   `error.code=trading/openapi-unavailable`（见下节「OpenAPI 凭据与通道（WP8）」）。
5. **订单 `unknown`（提交超时）**：铁律只查询不重放，走场景 2 对账兜底。

### 人工下单时段闸门（WP19）

**它是什么**：`trade_place` / `trade_modify` 与工作台「执行计划」（`plan-execute` 人工触发）
在**平台层**（与字段校验同层、风控之前）先判「现在是不是可委托时段」。不在 → 业务失败信封
`trading/order-rejected` + `message` 以「**时段闸门拒绝：**」开头。**零券商调用、零确认消耗、
不落 OMS 行、不落 `risk_checks` 行、不写指令文件**（与既有的本地前置拒绝同一口径）。
**撤单（`trade_cancel`）与 `plan-execute` 的 cancel 动作一律放行**——减少敞口不新增风险。

**被拒时怎么确认是「时段」而不是别的**（按顺序看三个字段即可定性）：

1. `error.message` 前缀是「时段闸门拒绝：」→ 时段；若是「风控规则 N：」→ 风控（8 规则）；
   「参数非法：」→ 字段校验；「券商通道异常：/券商拒单：」→ 通道/券商；「业务确认未通过：」
   → 确认卡片。**只有时段闸门的消息里同时含**市场、**当前市场本地时间**与**当日可委托时段**；
2. `error.code`：`trading/order-rejected`（时段/风控/确认/券商拒单同族）→ 用消息前缀区分；
3. 两张表应当**没有**这一笔（本地前置拒绝不落痕迹）：
   ```bash
   sqlite3 ~/.dsh/trading-data/trading.sqlite \
     "SELECT COUNT(*) FROM orders WHERE created_at LIKE '$(date +%F)%';" \
     "SELECT rule,reason FROM risk_checks ORDER BY id DESC LIMIT 3;"
   ```

**逐市场可委托窗口**（本地时间；口径「宁可放过、不可错杀」——只挡明确闭市）：

| 市场 | 窗口 | 说明 |
|---|---|---|
| SH / SZ / BJ | 09:15–15:00 | 含开盘集合竞价与**午间报单**（午休刻意算在内：券商普遍接受午间报单并排队） |
| HK | 09:00–16:10 | 含 09:00 开市前竞价与 16:00–16:10 收市竞价 |
| US | 09:30–16:00（常规/`RTH`/缺省）<br>04:00–20:00（`session` 请求 `RTH+Pre/Post-Mkt` / `OVERNIGHT` / `ALL_DAY`） | 扩展时段取官方四个取值的并集（本仓库没有夜盘时刻表的权威来源，宁可用并集放过） |

**半日/提前收盘**：窗口上界取**真实收盘**（港股半日 → 12:00、美股半日 → 13:00），依据是
日历当日行的 `trade_second`（`calendar` 表；半日 = 开盘 + `trade_second`，**单段不含午休**）。
怎么核当天是不是半日：

```bash
sqlite3 ~/.dsh/trading-data/trading.sqlite \
  "SELECT market,day,trade_date_type,trade_second FROM calendar \
    WHERE day >= date('now','-1 day') ORDER BY market,day LIMIT 12;"
# 对照全天秒数：SH/SZ/BJ=14400、HK=19800、US=23400；小于即视为提前收盘
```

**注意三件事**（避免误判为故障）：

- 闸门**只判钟点，不判交易日**：周末/节假日的「窗口内」时刻仍会放行到下一步，由风控
  **规则 3**（`is_trading_day`，日粒度）逐单拒绝，文案是「非交易日：不提交订单」——
  两者分工不同，别把规则 3 当成钟点守卫；
- 日历**缺当日行**（未同步/休市日）→ 闸门按**全天窗口**判时刻（不 fail-closed），
  非交易日同样落到规则 3；
- 闸门**不覆盖自动执行链**：`auto_execute` → 指令轮询 → `execute.run` 不经 `TradeGate`，
  它的时段约束是**守卫 9 的执行窗口**（`exec_window_minutes`，见「窗口超时 / kill /
  熔断」一节）；因此「自动补跑为什么没跑」要去查执行窗口告警，而不是这条闸门。

**演练/测试注入时钟**：闸门时钟是 `TradeGate(now=...)`（零参 callable 或
`YYYY-MM-DD HH:MM:SS` 北京时间字符串），缺省 `_now()`（东八区）。测试必须注入固定时刻，
否则用例会随真实运行时刻飘。

### OpenAPI 凭据与通道（WP8）

- **凭据配置**：`~/.dsh/futu-openapi.json`（appkey 模式样例）：
  ```json
  { "mode": "appkey", "app_key_id": "<AppKeyID>", "private_key_pem": "<Ed25519/RSA PEM 原文>" }
  ```
  OAuth 模式先跑授权流程（浏览器 OAuth，落盘时私钥/凭据文件权限 0600）：
  ```bash
  ~/.dsh/trading-venv/bin/python scripts/futu_auth.py --openapi
  ```
  凭据文件权限必须 0600；文件缺失/不可读时 openapi 通道如实降级（见下）。
- **通道选择**：`~/.dsh/trading-platform.json` 顶层 `futu_channel: "openapi"|"mcp"`
  （默认 `mcp`，行为与 WP7 完全一致；改后重启服务生效）。`openapi` 通道在凭据
  缺失/无效时相关工具返回 `trading/openapi-unavailable`（如实拒绝，不静默回落 mcp）。
- **连通性自检**：
  ```bash
  ~/.dsh/trading-venv/bin/python scripts/futu_openapi_check.py --app-key <AppKeyID>
  ```
  实调 GET/POST 各一次，期望 200 / `ret_code 0`。
- **推送排障（WS）**：`curl -s http://127.0.0.1:8397/healthz` 的 `push` 字段
  （`{enabled, started, reason, last_error, quote, trade}`；quote/trade 各含
  `connected/authenticated/最后消息时间/reconnects`）。`reconnects` 持续增长 →
  查网络与服务端断连原因；`last_error` 为鉴权失败 → 重跑 `futu_auth.py --openapi`；
  客户端按 5/10 分钟周期发 refresh 帧保活（超 10 分钟服务端主动断开）。
  **断线期间事件不补发**——推送仅作加速，重连后靠对账兜底轮询（60s）收敛，
  事实来源始终是 REST 查询/对账。
- **数据层副本**：`plugins/datasource`、`plugins/core` 新增 Python 模块后，Harness 内
  fin-data/engine 插件仍用 venv 里的副本，须重跑
  ```bash
  ~/.dsh/trading-venv/bin/python scripts/install_plugins.py install   # 或 update/link
  ```
  平台服务进程已优先仓库路径（WP8 起服务侧不再受副本滞后影响），但 Harness 侧插件不会自动感知新模块。

### systemd unit 样例

```ini
# ~/.config/systemd/user/quant-platform.service
# 路径按实际安装位置替换（preset 安装在 ~/.dsh/.agent-presets/dsh-trading-agents/platform）。
[Unit]
Description=quant platform service (workbench HTTP API + MCP + web)
[Service]
WorkingDirectory=%h/dsh-trading-agents/platform
ExecStart=%h/.dsh/trading-venv/bin/python -m server.run
Restart=on-failure
[Install]
WantedBy=default.target
```

`WorkingDirectory` 必须指向 `platform/`（对应 `python -m server.run`）；
`ExecStart` 用 venv 内的 python 绝对路径。

## 场景 1：daemon 崩溃（kill -9）→ systemd 重启 → 心跳恢复 → 指令去重

**症状：** daemon 进程消失/无响应；心跳文件 `heartbeat` 停止刷新（> 5 分钟工作台标红）。

1. 记录基线（崩溃前）：
   ```bash
   cat ~/.dsh/trading-daemon.json                            # 记下 heartbeat / last_job
   ls ~/.dsh/trading-commands/processed/ | wc -l             # 已处理指令数
   ```
   ⚠️ 若 `last_job` 显示执行类作业正在进行，先走场景 2 的在途核对，不要在本场景强杀。
2. 模拟崩溃：
   ```bash
   kill -9 "$(pgrep -f trading_core.daemon)"
   ```
3. 确认 systemd 拉起（unit 名以实际部署为准，随 WP4 部署落位）：
   ```bash
   systemctl --user status trading-daemon
   systemctl --user restart trading-daemon    # 未自动重启时手动拉起
   ```
4. 心跳恢复：等待一个轮询周期（60s），`cat ~/.dsh/trading-daemon.json` —— `heartbeat` 时间刷新、`critical` 为 `false`。
5. 指令去重验证：重启前已处理指令都在 `processed/`（以 nonce 命名），daemon 不会重复执行；将 `processed/` 中任一 nonce 重写回 `pending/` 再观察，daemon 按 processed 去重丢弃、业务效果不重复（execute_plan 只提交一次、kill 只建一次文件）。
6. 清理测试指令，恢复 `processed/` 计数基线。

**预期：** 崩溃 → systemd 自动重启 ≤ 1 个轮询周期；心跳刷新；指令零重复执行。

## 场景 2：执行中断 → 订单 unknown → 对账兜底 → 状态机迁移

**症状：** 执行中进程中断/提交超时，订单停在 `unknown`（适配器铁律：超时一律 `unknown`，绝不重放）。

1. 识别在途 unknown：
   ```bash
   sqlite3 ~/.dsh/trading-data/trading.sqlite \
     "SELECT client_order_id,symbol,side,qty,updated_at FROM orders WHERE status='unknown';"
   ```
2. **铁律：只查询，不重放。**
3. 跑对账作业拿差异输出，并人工核对券商（App/成交回报）该指令的真实状态：
   ```bash
   ~/.dsh/trading-venv/bin/python -B -m trading_core reconcile-diff \
     --local local.json --broker broker.json
   ```
   （daemon 常驻时由 reconcile 作业定期输出，结论同口径。）
4. 按查询结果迁移状态机（白名单：`unknown → submitted/partial/filled/cancelled`）：
   ```bash
   ~/.dsh/trading-venv/bin/python -B -c "from trading_core import oms, store; \
     oms.transition(store.connect(), '<client_order_id>', 'submitted', broker_order_id='<券商单号>')"
   ```
   券商无此单 → 迁 `cancelled`；已成交 → `filled`（并补 fills）。
5. 复核：重跑 `reconcile-diff` 应零差异；非法迁移（如 `unknown → draft`）会抛 `ValueError`，属预期保护。

**预期：** unknown 只经查询结果迁出；台账与券商一致；无任何重复下单。

## 场景 3：对账差异 → critical 告警 + halt → 人工核对 → 恢复

**症状：** `alerts` 表 critical 记录 + 心跳标志位 `critical: true`（工作台常驻红点）+ 熔断 `halt:active`；差异期间执行暂停。

1. 看告警与熔断状态：
   ```bash
   sqlite3 ~/.dsh/trading-data/trading.sqlite \
     "SELECT level,title,detail,created_at FROM alerts ORDER BY id DESC LIMIT 5;"
   ~/.dsh/trading-venv/bin/python -B -c "from trading_core import store; \
     print(store.is_halted(store.connect()))"
   ```
2. 处置原则：**先查券商再动手**；平台只告警 + 暂停，**不自动平仓**，差异处置永远由人决定。
3. 人工核对券商持仓 vs 本地台账（场景 2 的 `reconcile-diff` 或券商 App），定位差异原因（常见：在途 unknown 未迁移、成交回写缺失、重复记账）。
4. **先让对账收敛，再清 halt**（差异没消除就清 halt，下一轮对账会原地再置位）：
   * `missing_in_oms`（券商有单、本地无档）→ 重跑对账即自动**收编**（见「`missing_in_oms`
     差异已能自动收敛」小节；历史日期的遗留单要带 `--today <对账日>`），跑完确认
     `diffs: []`；
   * 券商已撤而本地仍在途 → `reconcile-daily` 按官方状态码自动收敛；对账覆盖不到的
     历史行才用 `oms-align` 人工留痕（见本文开头「订单终态人工对齐」）；
   * **清库重建后的存量持仓不该熔断**（2026-09-19 实机，见 HANDOVER §8.12）：本地库被清空
     或换库后，券商侧的存量老仓在本地**没有任何建仓成交**——按持仓口径它们属于
     `untracked`（如实列出、**不计差异**），**不是** `missing_side`。判据就在重跑输出里：
     `diffs: []`、`untracked` 里能看到这些标的、`halted: false`（本次 run 无差异）。
     若这些标的仍报 `missing_side`，先查**是不是有零成交的本地订单行被算成了持仓知识**
     （`draft`/`frozen`、本地作废的 `cancelled`/`rejected`、只有卖出腿的成交）——那是口径
     缺陷，不是真差异，别用「人工对齐」硬压。
     同理，本地只有**减仓腿**（回填出来的卖出成交、没有建仓成交）的标的也不计差异：
     它们出现在 `reconcile:latest.local_unbacked`（本地账本不完整，如实列出）。
   * 真差异长什么样（**必须继续 critical + halt**）：本地**有买入成交**却与券商对不上——
     「本地认为已平仓、券商仍持有」（本地净持仓 0 vs 券商有量）或数量不一致
     （本地 100 vs 券商 12490）。这类不要当成口径噪音。
5. 修正本地台账、迁移完在途单后，清 halt：
   ```bash
   ~/.dsh/trading-venv/bin/python -B -c "from trading_core import store; \
     store.clear_halt(store.connect())"
   ```
   **清 halt 必须留理由**：`clear_halt` 不带 reason 参数、平台也没有 ack 入口，所以清完要
   紧接着写一条留痕告警（否则后人只看到历史 critical，答不出「为什么不用继续 halt」）：
   ```bash
   ~/.dsh/trading-venv/bin/python -B -c "from trading_core import alerts, store; \
     alerts.emit(store.connect(), home='$HOME/.dsh', level='warn', title='熔断解除', \
     detail='<逐条写明差异成因 + 重跑证据 diffs=0>')"
   ```
   理由要能回答「不是让它闭嘴」：差异**逐条**查明（哪几条是演练/清库产物、哪几条已收敛），
   并附上一次 `diffs: []` 的重跑证据。
6. 若当时上了 kill switch：人工确认后 `unkill`（删除 `~/.dsh/trading-kill`）。
7. 次日恢复验证：daemon 到点正常 `build_plan` 产出冻结计划；工作台红点消失；`is_halted` 为 `False`。

**预期：** 差异 → critical + halt 暂停执行；人工核对修正后恢复；次日计划照常生成。

## 场景 4：富途 token 过期（internal error 特征）

**症状：** 富途工具批量返回 internal error（服务端**不**返回 401，勿等 401）。

1. 先跑聚合核验定位漂移面（约定见 §0）：
   ```bash
   ~/.dsh/trading-venv/bin/python -B scripts/verify-data-deps.py
   ```
2. 第一道防线：`futu_mcp.call_tool` 内置自动续期——判定为鉴权失败时用 `~/.dsh/futu-refresh` 换新 access_token 并重试一次，大多数情况自愈。
3. 仍失败：手动续期（免浏览器授权）：
   ```bash
   ~/.dsh/trading-venv/bin/python scripts/futu_auth.py --refresh
   ```
4. 再失败（refresh_token 已失效）：完整重新授权（浏览器 OAuth；需要交易写权限加 `--write`）：
   ```bash
   ~/.dsh/trading-venv/bin/python scripts/futu_auth.py
   ```
5. 验证：重跑 `scripts/verify-data-deps.py`，全部 `OK`。

**预期：** 过期 → 自动续期自愈；续期失效 → `--refresh` 兜底；两者皆失效 → 完整授权，全程不猜字段不硬编码。

## 工作台页面变空 / 少了几张卡：先看是不是市场筛选（WP23，2026-09-18）

页头右上角的「市场筛选」是**全局**的（选择存在浏览器 localStorage，切页不丢）。它只做
**客户端展示层过滤**：不改请求参数、不改账户配置、不影响任何作业或订单。所以「页面少了
东西」的最常见原因是它被切到了某个具体市场。

| 现象 | 原因 | 处置 |
|---|---|---|
| 表格空白，只有一句「当前筛选：港股 HK —— 本页无港股数据（共 N 行，均属其他市场）。」 | 数据在，但当前市场没有该页数据（这是**有意的明确空态**，替代了过去看不出原因的「暂无数据」） | 页头选择器切回「全部市场」，或换一个市场 |
| 「另有 N 条时间线记录的标的没有交易所前缀（如 '00981'），无法判定市场，未计入上表」 | 审计时间线的 ticker 来自券商回报，是**裸代码**（无 `SH./HK./US.` 前缀），无法判定市场；按「不猜」纪律排除并报数 | 切到「全部市场」核对；不要把这条当成数据丢失 |
| 计划页显示「当前计划（PLN-…）属于「A股 SH」，不在当前筛选「港股 HK」内」 | 执行入口**恒指向端点返回的最新计划**（提交它的内容哈希），不随展示筛选改变——这是防止「切了市场后按钮执行另一个计划」 | 想执行就核对计划号；想只看某市场的计划列表就切筛选，两者互不影响 |
| 调度页作业表只有本市场 + `GLOBAL:*` 行 | `GLOBAL` 作业不属于任何单个市场（对账/入队/日历同步），按市场藏掉等于让它们从页面消失 | 预期行为；要全量看就切「全部市场」 |
| 行情 / 事件 / 资金页出现黄条「标的 XX 属于「A股 SH」，与当前筛选「港股 HK」不一致」 | 这三页**一次只查一个标的**，不按市场过滤（否则与刚输入的标的冲突），只做提示 | 忽略提示，或把选择器切到一致的市场 |
| 「全部市场」下表格多出一列「市场」 | 跨市场混排时用来分辨每行属于哪个市场（显示值与筛选口径同源） | 预期行为 |

排查时不必重启服务、不必清缓存：选择器状态在浏览器里，换个市场即可复现/消除。

## 「某页某字段显示不对」怎么复现与定位（字段级审计，2026-09-19）

**症状**：页面上某个数字/百分比/时间看着不对——该有值却是 `—`、明明是比例却没显示 `%`、
时间戳带着 `T`、或者汇总与明细对不上。

**一条命令复现该页**（只读，不点任何提交类控件；用法与判定码详见
[E2E-ACCEPTANCE.md](E2E-ACCEPTANCE.md) 的「页面字段级审计」一节）：

```bash
# 单页复验（最快的定位入口；退出码 0=该页无缺陷、1=有缺陷、2=服务未就绪/中断）
node scripts/audit_page_fields.mjs --pages capital
# 标的类页面（market/capital/events）用你实际在看的标的
node scripts/audit_page_fields.mjs --pages market --symbol HK.09961
# 期权页必须用 HK/US 标的（A 股会被上游直接拒：option chain only supports HK / US / JP）
# 衍生品卡还要一个**期权合约代码**；不给就自动从 option_screen 真机结果里挑第一条
node scripts/audit_page_fields.mjs --pages options --symbol HK.09961 \
  --option-code US.SPY260918C760000
```

工具把「**后端真实载荷**（同一入口 `POST /api/wb/<endpoint>`）」与「**浏览器里真实渲染的
文本**（真 Chromium + CDP，抓 Statistic / Descriptions / 表格单元格）」逐字段比对，输出一行
一条：`页面 | 字段 | 后端值 | 渲染值 | 判定 | 说明`。产物在
`~/.dsh/logs/page-fields-<ts>/report.json`（每条缺陷都带 backend/rendered/note 三件套）。

**判定码怎么读**（工具的口径，见 `scripts/audit_page_fields.mjs` 文件头）：

| 判定 | 含义 | 是不是缺陷 |
|---|---|---|
| `MISSING_WHEN_DATA` | 后端有非空值，渲染全是 `—` | **是**（最常见：候选键写错/路径取错） |
| `FORMAT` | 两端都有值但对不上（无千分位、比例没 ×100、时间戳带 `T`、原始毫秒时间戳） | **是** |
| `FABRICATED` | 后端缺失/为空，渲染出 `0`/`0.00`/`NaN`/`[object Object]` | **是** |
| `T_TIMESTAMP` | 内容区出现带 `T` 的 ISO 时间戳（应过 `stampOf`） | **是**（全文扫雷命中） |
| `EMPTY` | 两端都空 | 否——**数据确实没有时显示 `—` 是正确的**，别改 |
| `UNJUDGED` | 端点取数失败（无权限/无凭据/参数不被接受） | 否，但要看清端点错误原文 |
| `NOT_RENDERED` | 该标签在当前 DOM 里没出现（条件渲染/需要先输入标的/需要点查询） | 需人工判断 |

**定位三步**（照抄即可）：

1. 看缺陷行的 `endpoint` + `path`：那是工具**期望**的取值路径。若与页面源码里的 `pick(row,[...])`
   候选键不一致，先怀疑**字段名写错**（本项目已发生两次：`capital_flow` 的四档键是
   `super_in_flow` 而不是 `super_inflow`；`option_chain` 的到期日是 `strike_time` 而不是
   `expiration_date`）。
2. 手工核对后端原文（排除是数据问题）：
   ```bash
   curl -s -X POST http://127.0.0.1:8397/api/wb/capital_flow \
     -H 'Content-Type: application/json' -d '{"code":"SH.600000"}' | head -c 800
   ```
   （页面上的「原始返回（核对用）」折叠块是同一份事实。）
3. 改完页面**必须重建静态产物**，否则浏览器加载的还是旧代码：
   ```bash
   # V3 工作台是 Ant Design Pro 单页（platform/web-pro），**有构建步骤**：
   # 改完 src/** 必须重新构建，否则浏览器加载的还是旧产物（服务不用重启，FastAPI 直接读 dist/）
   cd platform/web-pro && flock /tmp/probuild.lock npm run build   # 串行构建，避免与他人的构建互踩
   node scripts/audit_page_fields.mjs --pages <页>  # 复跑该页，退出码 0 即闭环
   ```

**常见误判（先排除，别急着改页面）**：

- **页头市场筛选**把行筛掉了 → 表格空了但不是字段错（见上一节）。
- **标的没输入/没回车** → 卡片根本没渲染（`NOT_RENDERED`），不是字段错。工具会自动在标的
  输入框里填入关注池第一只并回车，所以它跑出来是 `NOT_RENDERED` 就该看是不是别的前置条件。
- **端点本身失败**（权限/凭据/参数）→ `UNJUDGED`，此时页面显示「读取失败：<原文>」是**诚实**
  行为；例如 A 股 `rt_quote` 恒为 `errcode=-9 realtime quote permission required`。
- **`0` 不等于缺失**：后端给 0 时页面显示 `0` 是对的；工具只在后端**没有值**而渲染出数字时
  才判 `FABRICATED`。

### 页面显示 `—` 到底是「没数据」还是「取数层级不对」：先造数据再判（2026-09-19）

`EMPTY`（两端都空）只说明**现在**没数据，**不等于格式验证过**。要判「有数据时显示得对不对」，
得先按平台自己的写入路径把数据造出来，再复跑该页。完整命令表与「生成前 → 生成后」证据见
[E2E-ACCEPTANCE.md](E2E-ACCEPTANCE.md) 的「空态字段怎么变成可判定字段」。要点：

| 想要的数据 | 命令（都在仓库根） | 前提 / 副作用 |
|---|---|---|
| 信号页 `snapshot.previews` | `node scripts/workbench_admin.mjs seed-preview --ticker SH.600000 --strategy rsi` | 跑的是 `engine.py signal`（与 `quant_signal` 工具同一条命令），只写 sim |
| 研究页 runs / 已发布研报 | `node scripts/workbench_admin.mjs seed-research --ticker SH.600000 --report-file <md> --sources-file <json>` | 只调 `beginResearch`/`publishResearch`；**内容真实与否由调用方负责** |
| 执行页 `fills` / 审计页 `diffs` | `PYTHONPATH=plugins/core/python ~/.dsh/trading-venv/bin/python -B -m trading_core reconcile-daily --today <对账日>` | 回填只认**券商订单历史**：券商当天没有成交单时 fills 仍是 0（先 `--today <有成交的那天>` 让对账收编券商单）；**有差异会 `set_halt`**（见场景 3） |
| 组合页权益 / 执行页「本地台账」 | `DSH_HOME=~/.dsh ~/.dsh/trading-venv/bin/python -B plugins/engine/python/engine.py decide --ticker <标的> --strategy rsi --apply` | 本地模拟器**只在 sim** 下可用；只在真有 BUY 信号时才产单（`HOLD` 时 `order=null`，别以为命令没跑） |
| 计划页「计划列表」卡 | `PYTHONPATH=plugins/core/python ~/.dsh/trading-venv/bin/python -B -m trading_core plan-build --mode SIM --strategy <策略> --target '{"SH.600000":0.03}' --prices '{"SH.600000":9.07}' --as-of <交易日>` | `origin=manual`，**不会被自动执行**；`plans.length > 1` 才渲染列表卡 |
| 财报卡（因子页） | 数据本来就在；缺的是取证方式 → 审计工具的 `quality` phase 会只填 1 只标的 | 卡片要求 `tickers.length === 1`，而 ic 要 3..8 只，同一次输入喂不出两种 |

**几条容易踩的**：

- `equity` 曲线要 **≥2 个交易日**的台账成交才会画。同一天造的成交只出 1 个点，
  页面会显示「权益序列仅 1 个点，不足以绘制」——这是**如实空态**，不是缺陷；
  等第二个交易日再跑一次 `decide --apply` 就有了。
- `reconcile-daily` 的订单匹配按**对账日**取窗（`_match_orders` 用 `created_at LIKE <today>%`），
  所以补跑历史日期必须显式 `--today`，否则窗口里没有订单、`fills` 回填为 0。
- 造完数据**别忘了一件**：`--today <今天>` 跑对账若产生差异会把 halt 立起来，
  自动执行随之暂停；不需要这个副作用就别跑当天那一轮（或按场景 3 处置）。

## 标的输入框下拉没有关注池候选（WP24，2026-09-18）

标的输入框（行情/资金/事件/执行/研究/期权/因子的标的字段）的联想候选来自两处：
`snapshot` 载荷的 `watchlist`（**平台关注池**，`trading-platform.json` 顶层 `watchlist`）
与 `positions` 端点（**当前模式的持仓**）。候选是**帮忙，不是白名单**——下拉里没有的代码
照常可以手打、照常提交。

| 现象 | 原因 | 处置 |
|---|---|---|
| 下拉里完全没有候选 | `snapshot` 响应里**没有** `watchlist` 键（服务进程是 WP24 之前的代码） | 重启服务后刷新页面：`scripts/platform_service.sh restart`（后端改了必须重启才换代码） |
| 只有带名称的持仓候选、没有关注池那批 | `watchlist` 是空数组 | 关注池缺失/为空/配置损坏都按空池（`daemon.platform_config` 既有口径）；查 `trading-platform.json` 顶层 `watchlist`。空池本身是合法状态（数据作业也会跳过） |
| 只有关注池候选、没有持仓那批 | `positions` 取不到（券商不可达/无权限/该模式无持仓） | 页面自己的持仓视图会如实报错；这里退化为只有关注池，不重复弹错 |
| 下拉里的东西不随页头「市场筛选」变 | **有意**：输入框问的是「某一个标的」，与页面视角无关 | 预期行为；候选上的「A股 SH / 港股 HK / 美股 US」标签只做标注 |
| 切了 sim/live 后下拉还是老持仓 | 候选按首次取数时的模式缓存（不新增轮询） | 刷新页面 |

排查用一句：`curl -s -X POST http://127.0.0.1:8397/api/wb/snapshot -H 'Content-Type: application/json' -d '{}'`
→ 看 `value.watchlist`；同时 `value.endpoints` 仍是 82 项（WP24 没有新增端点）。

## last30days 社媒研究技能排障（可选组件）

- 行为异常先自检：`~/.dsh/trading-venv/bin/python ~/.dsh/last30days-skill/skills/last30days/scripts/last30days.py --preflight`（不读 Cookie 不写文件）；报「目录/引擎不存在」先跑 `python3 scripts/install_last30days.py`，装完新建会话才会挂载。
- 密钥缺失时的降级面：免密钥来源（Reddit/HN/Polymarket/GitHub/StockTwits）照常可用；X、YouTube、TikTok/Instagram/Threads/Pinterest/LinkedIn、小红书、Perplexity、Brave 未按上游 README 配置密钥/会话时这些来源缺席（简报如实标注），属预期行为而非故障。

## 自动流水线（WP9）

> 前提：`trading-platform.json` 已开 `auto_pipeline.enabled=true`（配置样例见
> [FEATURES.md](FEATURES.md)「WP9：自动流水线」）。关掉开关即回到全人工，任何演练都不需要改代码。

### 设置页保存被拒：先看错误信息里的「合法取值」（WP22，2026-09-18）

设置页（`#/settings` → 「自动流水线」）保存时，服务端会校验**策略名**；非法值一律
拒绝并**逐个列出合法取值**，例如：

```
auto_pipeline.strategies 策略名不可用：'wathclist_rsi'
（合法取值：ma_cross / momentum_value_top5 / rsi / watchlist_rsi；
策略名须为内置策略 id，或研究页「已批准」（status=enabled）的规则 id——其它状态的规则不会被消费）
```

| 报错片段 | 原因 | 处置 |
|---|---|---|
| `策略名不可用：'xxx'` | 名字既不是内置策略 id，也不是 `status='enabled'` 的 `rule_id`（拼错、或规则尚未批准/已停用） | 按错误信息里的合法取值改；规则要先在研究页「批准」（`passed` → `enabled`） |
| `规则库不可读` | `rules` 表打不开（DB 文件损坏/权限），无法确认「已批准」→ fail-closed 拒绝保存**非内置**策略名 | 修复 `~/.dsh/trading-data/trading.sqlite` 后重试；内置策略名不受影响（不读 DB，随时可保存） |
| 保存成功但卡片里有黄条「当前配置里的策略无法被自动流水线消费」 | 历史配置里已有坏名字（写入侧校验是本轮新加的，不追溯拒绝旧配置），或选中的是单标的策略（`rsi`/`ma_cross`，缺 `target_weights`） | 换成组合策略（`watchlist_rsi`/`momentum_value_top5`）或重新批准规则后保存 |
| `规则清单读取失败：…` | `rules` 端点不可达（服务陈旧/未起） | 下拉暂时只列内置策略；服务恢复后刷新页面 |

**分工**（为什么保存被拒但作业只是「没动静」）：写入侧（Web 设置页）fail-closed 拦新错误；
调度侧读取保持原样——历史配置里的坏策略名仍由 `plan_auto` 软跳过，并在告警表留一条
warn「策略未注册」（`snapshot-schedule` 可见），不会因为本轮校验而追着旧配置报错。

### 假时钟演练（`DSH_FAKE_NOW`，测试/演练专用）

```bash
export DSH_HOME=~/.dsh
export DSH_FAKE_NOW="2026-09-16 16:20:00"      # 必填格式 YYYY-MM-DD HH:MM:SS
~/.dsh/trading-venv/bin/python -m trading_core plan-auto --market SH
~/.dsh/trading-venv/bin/python -m trading_core snapshot-schedule   # 看 ran 标记与告警
export DSH_FAKE_NOW="2026-09-17 09:35:00"      # 次日执行窗口内
~/.dsh/trading-venv/bin/python -m trading_core auto-execute --market SH
~/.dsh/trading-venv/bin/python -m trading_core snapshot-plan       # 看计划→订单状态
unset DSH_FAKE_NOW                              # 演练结束必须清理
```

- **预期**：`plan-auto` 产出 `origin=auto/market=SH` 的 frozen 计划；`auto-execute` 在
  窗口内写出 `execute_plan` 指令（`~/.dsh/trading-commands/pending/`）并返回 nonce；
  `snapshot-plan` 可见订单状态推进。
- **必查**：`snapshot-schedule` 的告警列表里有且仅有一条 warn「假时钟生效」——
  它同时是「环境变量还挂着」的提醒；`unset` 后重启服务或等下一轮 tick，告警不再新增。
- **格式写错**（如 `2026/09/16 16:20`）：命令**直接报错退出**，不会静默按真实时间跑。

### 窗口超时 / kill / 熔断：三条「应该不执行」的排查

| 现象（告警标题） | 含义 | 处置 |
|---|---|---|
| `已超执行窗口` | 当前时刻超出 `exec_at + exec_window_minutes`（如服务在收盘后才启动补跑） | 属预期：计划留待人工在工作台执行；要当日自动执行就调整 `exec_at`/窗口或重启服务在窗口内 |
| `kill switch 生效` | `~/.dsh/trading-kill` 存在 | 确认是否人为放的总闸；恢复即删除该文件（工作台一键清除） |
| `熔断生效` | 对账差异或日内亏损触发 halt（**差异只暂停不平仓**） | 先按「场景 3」人工核对券商事实，再 `clear_halt` 恢复 |
| `非交易日` | `auto_execute` 的**纵深防御**守卫（守卫 4b）：作业体内判 `is_trading_day` 为假（真实休市） | **正常**，info 级、零指令。线上调度器本就跳过非交易日的市场链；这条只在 CLI/MCP/E2E 直调时出现 |

### 交易日历：`日历覆盖不足` / `日历已用尽`（`sync_calendar` 作业，WP18）

**背景**：`calendar` 表是**交易日白名单**，`store.is_trading_day` 在日期**超出
`max(day)`** 时返回 **False 而不报错**——日历用尽后市场链被静默跳过，流程页只显示
「市场天天休市」，**零告警**（实测：日历只到 2026-09-30 时跑 2026-10-12，只有 2 条
全局作业、0 条市场作业、0 条 warn）。所以 GLOBAL 链（不查市场日历，假日/周末/用尽都
照常跑）链首新增 `sync_calendar`（18:50，早于对账 19:00 / 入队 19:05）。

| 现象（告警标题，均 warn） | 含义 | 处置 |
|---|---|---|
| `日历覆盖不足` | 某市场 `max(day) < today + 60 天`（覆盖进入预警区）。**链照常跑**，只是快到期 | 查 `sync_calendar` 作业是否连续失败（`alerts` 里的 `作业失败` + detail 尾部）；先手工补：`python -m trading_core calendar-sync --market SH` |
| `日历已用尽` | `today > max(day)`：该市场链**今日整条被跳过**（此前完全静默） | 同上；补齐当日即可恢复（作业在 18:50，当晚同一轮 tick 的市场链就会看到新日历） |
| `日历未同步` | 该市场**零行**（与上面两条互斥：零行走这条） | 首次部署就该跑一次全量：`python -m trading_core calendar --market SH --start 2026-01-01 --end 2027-12-31` |

两条告警都**每市场每日至多一条**（kv 去重，不随 60 秒 tick 刷屏）；判断口径互斥：
`today > max(day)` 只发「已用尽」，覆盖范围内但不在白名单（**真实休市**）**保持静默**
——真实休市不是故障（与 `plan_auto` 的软跳过同一语义）。

只读核对（不改库）：

```bash
DB=~/.dsh/trading-data/trading.sqlite
sqlite3 "$DB" "SELECT market, COUNT(*), MIN(day), MAX(day) FROM calendar GROUP BY market;"
# 或走 CLI（逐市场自节流；覆盖充足时零网络调用、退出 0）：
~/.dsh/trading-venv/bin/python -m trading_core calendar-sync --market SH,HK,US --db "$DB"
```

**手工同步的等价命令**（`--market` 单值、start/end 必传，与 `calendar-sync` 的自动窗口
不同——它是原始抓取口径）：

```bash
~/.dsh/trading-venv/bin/python -m trading_core calendar --market SH \
  --start 2026-01-01 --end 2027-12-31 --db "$DB"
```

> ⚠️ **开发机上手工跑 `calendar-sync` 的两个坑**（2026-09-18 实机实测）：
> ① 安装副本 `~/.dsh/trading-python/core` 不含新子命令——`python -m trading_core
> calendar-sync` 会报 `invalid choice: 'calendar-sync'`，先
> `scripts/platform_service.sh refresh`；只想临时验证可前置仓库层：
> `PYTHONPATH=<repo>/plugins/datasource/python:<repo>/plugins/core/python python -m trading_core calendar-sync …`。
> ② 调度器起的作业子进程**自己会前置仓库层**（`daemon._subprocess_runner` →
> `repo_paths`），所以作业链跑的是仓库代码；但**已在运行的服务进程**在启动时就把
> `trading_core` 载入内存了，**改完代码必须 `refresh && restart`** 才生效（只 `refresh`
> 不重启，服务内调度仍是旧作业表）。

### 作业失败 / 作业异常：先查作业定义与 `@` 占位符（WP20）

**背景（2026-09-18 全新安装演练实测的两个缺陷）**：`quality` 作业只传 `--market SH`，
而 CLI 的 quality **必填** `--symbols/--start/--end` → argparse 退出 2，
detail 形如 `job=quality market=SH exit=2`（**detail 里没有原因**——argparse 的用法说明
走 stderr，而失败告警的 detail 只引用 stdout 尾部）；`@latest-quarter` 从未实现，
字面量被当报告期传给 `akshare.stock_yjbb_em` → 上游报
`TypeError: 'NoneType' object is not subscriptable`，看起来像我们自己的下标 bug。

**排障顺序**：

1. **看作业定义实际传给 CLI 的参数**（占位符先展开再交给 CLI）：
   ```bash
   PYTHONPATH=<repo>/plugins/datasource/python:<repo>/plugins/core/python python - <<'PY'
   from trading_core import daemon
   for mkt, chain in daemon.JOBS_DEFAULT.items():
       for job in chain:
           if "cmd" in job:
               print(mkt, job["name"], job["at"], "→", daemon.resolve_command(job["cmd"], "~/.dsh"))
   PY
   ```
   手工单跑该命令即可复现告警（`~/.dsh/trading-venv/bin/python -B -m trading_core <解析后的参数>`）。
2. **`@` 占位符只有四个**：`@watchlist`（关注池，空则跳过该作业）、
   `@latest-quarter`（今天之前最近一个**已结束**季度末，`YYYYMMDD`；正好落在季度末当天
   取上一季）、`@today` / `@today-<N>d`（`YYYY-MM-DD`，`N` 为自然日数）。
   **任何其它以 `@` 开头的参数 → 显式 `ValueError`**：
   `未知占位符：@lastest-quarter（已知：@watchlist/@latest-quarter/@today/@today-Nd）`，
   由调度层转成 **`作业异常`** 告警（可见）。这是刻意的：此前静默传字面量时，
   故障表现为上游一个读不懂的 TypeError。**改成别的写法不会被猜**（大小写敏感，
   `@Today`/`@today-180D` 同样报错）。
3. **`merge_announcements` 的取数失败**会写明是谁坏了：
   `公告合并失败：上游 akshare/eastmoney 接口异常（stock_yjbb_em date=20260630）：TypeError: …`
   ——**它不软跳过**：`announced_at` 是 PIT 的关键字段，缺数据必须可见（宁缺毋假）。
   `df is None/空` 是上游正常答「本期没有报表」，仍是软返回 `{"matched":0,"rows":0}`。
4. **`quality` 作业的语义**（不要凭告警里的数字猜）：`--symbols @watchlist` 是标的来源；
   `--start/--end` 是**日历区间**（`store.trading_days(market, start, end)` 取区间内的
   交易日集合，quality 内部**没有**回看常量），作业取 `@today-180d → @today`（≈123 交易日，
   覆盖 `rule_engine.IC_WINDOW_DAYS=120` 的 IC 加权窗口）；`--market` **只决定按哪张日历
   取交易日**、不筛标的。**只有 SH 链挂 quality**（刻意的）：`announced_at` 覆盖率是 A 股
   PIT 缺口①的口径，港股/美股 fundamentals 走 futu/statements 本就没有公告日。
5. detail 被截断到 300 字符、只取 stdout 最后 5 行：**CLI 侧必须给失败信封**
   （`{"ok": false, "error": …}` + 非零退出），裸 traceback 走 stderr → 告警里看不到原因。

### 计划「生成了但买不动 / 一笔单都没有」怎么读（WP17）

自动计划的**买入量按可用现金封顶**（现金取券商事实：sim `max_power_long` → `balance`；
live `power` → `available_funds` → `cash`；**绝不用权益冒充现金**）：

| 告警标题（warn） | 含义 | 处置 |
|---|---|---|
| `计划预警：现金不可得` | 券商资金响应无现金字段 → 本次**不生成买单**（卖单照常），`plan.cash.unavailable=true` | 查券商资金接口/通道；字段口径见 `docs/TOOL-LIMITS.md` §九 |
| `计划预警：现金封顶` | 买入量被可用现金压低（`plan.cash.capped` 列出标的；多买单按计划顺序**共享**一笔现金） | 正常约束，不是故障；想多买先减仓/加资金——属交易决策 |
| `计划预警：持仓数超限` | 持仓数 ≥ `max_positions` 且计划含新建仓 → 执行时会被风控规则 6 拦 | 调整 `max_positions` 或先减仓（交易决策） |
| `计划跳过：无可执行订单` | 计划已冻结但零订单（目标与持仓一致 / 全被现金或风控约束） | 流程页按 `skipped` 显示，**不是「已完成」**；看上面几条预警定位 |

计划返回体（`snapshot-plan`）带 `warnings` 与 `cash`，页面与 CLI 口径一致。**警告不改作业
状态**：作业确实跑完并冻结了计划，只是数字没买到「想买的量」。

### 目标外持仓清出没生效 / 清了但买不进（`exit_outside_target`）

**开关**：`~/.dsh/trading-platform.json` **顶层**键 `exit_outside_target`，**默认 `false`**
（缺失即关闭）。只接受真布尔：写 `"true"`/`1` 会被判非法 → 当日计划 fail-closed 软跳过 +
warn「`exit_outside_target` 配置非法」（不改配置就当它没写，绝不会「看起来开着其实没跑」）。
改完配置下一轮 tick 生效（配置每轮现读）；本键只在**自动计划路径**生效，手工 `plan-build`
不受影响。

| 告警标题（warn） | 含义 | 处置 |
|---|---|---|
| `计划预警：目标外持仓清出` | 本次把「券商持仓 − 当日 target 键」按目标权重 0 清出；detail 写明清出只数、价格来源（本地收盘/券商标记价各几只）、未生成订单的标的与原因 | 正常产出。`snapshot-plan` 的 `converge` 里有逐标的 `prices[{price,source,field}]` 与 `skipped` |
| `计划预警：目标为空未清出` | 策略当日 `target` 为空 → **硬守卫拦下清出**（否则等于清空全部持仓） | 属预期保护；先查策略为何选不出标的（数据/信号），不要为此关守卫 |
| `exit_outside_target 配置非法` | 开关值不是布尔 → 当日不生成计划（fail-closed） | 改成 `true`/`false`（真布尔，无引号） |
| `计划跳过：无可执行订单` | 想清的标的**全部**没清成（无价 / T+N 不可卖）且无其他订单 | 看它 detail 里的跳过原因：无价 → 查行情/券商标记价字段；`T+N不可卖` → 等可卖日 |

三件容易误判的事：

1. **清仓价来自哪里**：本地 `bars` 有该标的最近收盘就用它；没有（实机那 8 只确实 0 行）
   才回退券商标记价（sim `cur_price` / live `nominal_price`），返回体与告警里都标
   `broker_mark`——**不是本地收盘价**。两处都没有 → `SYM(无价,无法清出)`，**不猜价、不产单**。
2. **T+N 不可卖**：券商给了可用数量（sim `qty_avbl` / live `can_sell_qty`）时只卖可卖部分，
   `available=0` → `SYM(T+N不可卖)`。这是「不生成注定被拒的单」，不是故障。
3. **同一份计划里「卖了但没买进」是预期**：执行侧 `ctx` 是**静态持仓快照**，卖出不会让
   同批买入提前通过规则 6（持仓数仍按执行时的快照算）。当日先把账户清到策略组合，
   **下个交易日**快照回落后新计划自然能建仓（两步生效，勿据此怀疑清仓没执行；
   也**不要**为绕过规则 6 去改持仓事实）。

### `missing_in_oms` 差异已能自动收敛（WP17）

券商有单、本地 OMS 无对应行（历史遗留 / 收编前下的单 / 探针单）过去**没有收敛入口**
（`oms-align` 要求本地已存在该 `broker_order_id` → 拒绝），只能人工 `clear_halt`，次日
对账再次 critical + halt。

现在 `reconcile-daily` 在差异判定前**收编**这类订单：`plan_id=reconcile-import`、`err`
前缀 `reconcile-import:` 带券商单号与原始状态码，warn 告警 `订单导入：券商独有`。
纪律：只认**已发布**状态枚举，表外码/无单号的券商行**不导入**、照旧暴露为差异（不猜、
不静默吞）；`client_order_id` 由券商单号派生 → 幂等；**只读券商**。

**重放历史日期**（收敛昨天的遗留单——订单匹配按对账日取窗，必须显式给日期）：

```bash
cd <repo>   # 仓库在场时务必显式前置数据层，否则跑的是 ~/.dsh/trading-python 的副本
PYTHONPATH=plugins/core/python:plugins/datasource/python \
  ~/.dsh/trading-venv/bin/python -B -m trading_core reconcile-daily --today <对账日 YYYY-MM-DD>
```

**判定收敛成功的三条**：输出 `digest.orders_imported ≥ 1` 且 `diffs: []`、`halted: false`；
OMS 里 `SELECT plan_id,err FROM orders WHERE plan_id='reconcile-import'` 能对上券商单号；
`reconcile:latest` 的 `diffs` 为空。**收敛 ≠ 抹掉事实**：券商持仓里没有成交足迹的历史存量
仍如实列在 `untracked`（不计差异）；收编单**确有买入成交**时经 fills 回填照常比对持仓
（2026-09-19 起：只有**卖出**成交的标的算「本地账本不完整」，落 `untracked` +
`reconcile:latest.local_unbacked`，不计差异——见 HANDOVER §8.12）。

收敛确认后按场景 3 第 4 步 `clear_halt`；留存的历史 critical 告警是**记录**（平台没有 ack
入口），随对账不再重现而自然过期。

排查入口：`snapshot-schedule`（ran 标记/心跳/告警）、`snapshot-reconcile`（差异/TCA/
链路）、`snapshot-plan`（计划→订单→风控预检）。

## 情绪采集（`sentiment_snapshot`，最慢的基础链作业）

> 定位：**攒 PIT 历史**（三源原始事实落 `sentiment_snapshots`，供 250 交易日后的因子
> 检验）。它不属于交易链、不受 `auto_pipeline` 开关控制，因此**采不完不是交易安全问题**，
> 但「一行都没采到」会让演进条款的计时地基空转。

**为什么需要专门照看**（2026-09-17 实机）：20 标的 × 三源实测 **>7 分钟**——每源都要起
浏览器/网络重试；期间**没有输出**（对外像挂死，实际在推进）；而服务对单个作业有 **900s
上限**，触顶即被外部杀掉 → 一天的数据一行都留不下，且情绪阶段每天显示失败。

**三条保证（已实现，配置见 [FEATURES.md](FEATURES.md)「情绪采集的两个预算键」）**：

| 保证 | 语义 | 运维可见信号 |
|---|---|---|
| 总预算 `sentiment_budget_seconds`（600） | **硬上界**：每次调用前按剩余预算裁剪单次超时，剩余 ≤ 0 不再发起新调用；耗尽即停剩余标的，**退出 0** | warn 告警 `情绪快照预算耗尽`（detail 带 `market=SH` 与 `已处理 N/M`），流程页情绪阶段 `skipped` + 摘要「当日未采完：预算耗尽」 |
| 逐标的超时 `sentiment_symbol_timeout_seconds`（90） | 该标的判失败、**继续下一个**（不再一个卡住拖死整轮） | 该标的进结果 `failed`（原因含「超时」）；全部超时 → warn `情绪快照全部失败` |
| 子进程回收 | 超时/中断都整组 `SIGKILL` | 无孤儿（下面「孤儿检查」应为空） |

**信号语义（为什么这么实现，2026-09-17 实机验证）**：`SIGTERM` 的**默认动作直接终止
进程，`finally` 不会执行** → 子进程组（fin_sentiment 及其拉起的浏览器）被留下来，这正是
实机观察到孤儿的原因。因此真实子进程一律 `start_new_session=True` 建独立进程组，并在三条
路径上回收：① 超时分支 `os.killpg(SIGKILL)`；② `finally`（异常/`KeyboardInterrupt`）；
③ **SIGTERM 守卫**（先回收进程组，再恢复默认动作重发信号，保持「被信号杀死」的语义与退出码）。
`SIGINT` 不必拦（Python 抛 `KeyboardInterrupt`，走 `finally`）。

**一个不可拦截的例外（如实登记）**：`SIGKILL` 无法捕获——作业在 **900s 上限被外部
`SIGKILL`** 时，本进程内的所有清理代码都不会执行，其子进程会变孤儿（已实测复现）。
人工中断作业后请**照下面的「孤儿检查」看一眼并手工清理**；长期解法（`PR_SET_PDEATHSIG`
或让外层按进程组杀）已登记为遗留项，见 `docs/HANDOVER.md`。

**孤儿检查与手工清理**（超时/中断/kill 之后都应跑一眼）：

```bash
# 1) 查（无输出=干净）
pgrep -af "python.*(fin_sentiment|fin_news|last30days|x_search|reddit)" | grep -v pgrep
# 2) 清理：按进程组杀（fin_sentiment 自建进程组，浏览器等子进程同组）
for pid in $(pgrep -f "python.*fin_sentiment\.py"); do
  kill -KILL -"$(ps -o pgid= -p "$pid" | tr -d ' ')" 2>/dev/null || kill -KILL "$pid"
done
```

**耗时上界**：整轮 ≤ 预算 + 1s（每次调用前按剩余预算裁剪超时，见上表；`max(1, …)`
的下限就是那 1s）。另有一条**更保守的冗余校验**：`预算 + 3 × 单标的超时 < 900s`
（默认 870 ✓），它守的是「裁剪逻辑退化」时的兜底；调大预算触发它时会 fail-closed
报错并提示调整方向（刻意：预算被静默忽略就等于没修）。

**分批人工跑**（一次别贪多，每批 ≤5 只标的）：

```bash
# 只跑前 3 只、总预算 180s；进度逐标的打到 stderr，摘要 JSON 打到 stdout
~/.dsh/trading-venv/bin/python -m trading_core sentiment-snapshot \
  --market SH --limit 3 --budget 180
# 指定标的（补采/复核某几只）
~/.dsh/trading-venv/bin/python -m trading_core sentiment-snapshot \
  --market SH --symbols SH.600519,SH.600000 --budget 180
```

**排查表**：

| 现象 | 含义 | 处置 |
|---|---|---|
| 告警 `情绪快照预算耗尽`（`已处理 N/M`） | 本轮没采完，**数据已落已采部分**，作业退出 0 | 属预期（关注池越大越常见）；次日会重新采；急要历史就按上面分批补采 |
| 阶段 `skipped` + 摘要「当日未采完：预算耗尽」 | 同上，流程页如实呈现 | 同上；不要当故障处理 |
| 告警 `情绪源不可用`（单源全标的失败） | 该源通道坏了（如 X 浏览器登录失效），其余源照常 | 按 `docs/TOOL-LIMITS.md` 的渠道口径排查；`last30days` 未安装属正常缺席 |
| 告警 `情绪快照全部失败` | 在场源无一成功 | 先看 detail 的首次错误；多半是 fin-data 脚本环境（浏览器/venv）问题 |
| 想确认「有没有留下孤儿进程」 | 超时/中断后不应残留；**SIGKILL（900s 上限）例外** | 用上文「孤儿检查与手工清理」的两条命令 |

**为什么进度行写 stderr**：作业摘要（stdout 的 JSON）由服务用
`daemon._last_json_object` 解析后进告警 detail；进度若混进 stdout 会干扰该解析。stderr
同样被服务捕获转写进日志，运维照样看得见（`~/.dsh/logs/` 与服务日志）。

## 值班研究员（L3 定时研究任务）

> 定位：**研究侧**的定时执行体。它只消费任务队列产简报/巡检/提案，**永不直接下单、
> 永不直接启用策略**；交易侧自动执行是另一条链（见「自动流水线（WP9）」），两者互不调用。
> 队列与「会话打开补跑」共用同一状态机——定时器没跑成也不丢任务（详见下）。

### 组件与路径

| 对象 | 路径 / 键 | 说明 |
|---|---|---|
| 唤醒脚本 | `scripts/research_duty.sh` | 探活服务 → 拼提示词 → `dsh --profile headless` 单次运行 → 落日志 |
| systemd 单元 | `install/research-duty.{service,timer}` | `OnCalendar=Mon..Fri 19:20`（= 对账 19:00 → 入队 19:05 之后；`Persistent=true` 补跑错过的触发） |
| 任务队列 | SQLite 表 `research_tasks` | 状态机 `pending/running/done/failed`；`TASK_MAX_ATTEMPTS=3`、`TASK_TIMEOUT_MINUTES=30` |
| 任务种类 | `daily_brief` / `factor_patrol` / `mining_round` | 白名单；载荷只含结构化引用（`TASK_PAYLOAD_KEYS`），无自由文本 |
| 入队作业 | `enqueue_research` | 基础链尾（digest 之后）自动入队，**零 LLM**；研究与交易开关解耦 |
| 领取/回报 | `mcp__quantwb__research_tasks_claim` / `..._report` | 执行体（Harness 会话或 headless）经 MCP 调用；`research-tasks-list` 只给 Web |
| 日志 | `~/.dsh/logs/research-duty-<时间戳>.log` | 每次唤醒一份；脚本 stdout/stderr 全文 |

### 安装与启停（systemd user）

```bash
mkdir -p ~/.config/systemd/user
cp install/research-duty.service install/research-duty.timer ~/.config/systemd/user/
# 路径按实际安装位置替换（单元内 %h/dsh-trading-agents 为示例）
systemctl --user daemon-reload
systemctl --user enable --now research-duty.timer
systemctl --user list-timers research-duty.timer     # 看 NEXT
systemctl --user status research-duty.timer
journalctl --user -u research-duty.service -n 50     # 单次运行记录（与脚本日志互为印证）
```

- **停用**：`systemctl --user disable --now research-duty.timer`。停用不影响队列——
  任务照常入队，只在**会话首次交互**时由 Harness 按启动纪律补跑（见下节）。
- **cron 等价**（不想用 systemd 时；同样的行也写在 `install/research-duty.timer` 注释里）：
  ```
  20 19 * * 1-5 /home/<user>/dsh-trading-agents/scripts/research_duty.sh >> ~/.dsh/logs/research-duty-cron.log 2>&1
  ```
- **时刻依据**（一条链，按时间顺序）：`reconcile` 19:00（auto_pipeline 开启时）写当日
  `digest` → **基础链 `enqueue_research` 19:05** 入队 → 本定时器 **19:20** 唤醒消费。
  入队必须在 digest 之后，否则 `daily_brief` 的 `digest_ref` 指向前一日摘要（审查 A-2）。
  19:05 时三市场的观测日与其 digest 的 `as_of` 天然同日：SH/HK = 北京日 D；US = 会话本地日
  D-1（digest(D-1) 正是昨天 19:00 写下的那份）。
  前提是**平台服务在 19:05 前已运行**；服务若更晚才起，tick-first 会在启动时补跑
  `reconcile` → `enqueue_research`（GLOBAL 链按时刻排序保证顺序），任务留在队列等下一次
  唤醒或会话兜底——**不丢、只延后**。
- **手动演练**（不装定时器也能跑）：
  ```bash
  ~/.dsh/trading-venv/bin/python -m trading_core enqueue-research --market SH,HK,US  # 手工入队
  scripts/research_duty.sh                                                          # 单次唤醒
  ```

### 与「会话首次交互补跑」的关系（兜底语义）

两条路径消费**同一张表**：定时器唤醒 headless 会话（**自动**），或你在会话开始/恢复后的
**首次交互**里由 Harness 按 `AGENTS.md` 的启动纪律消费积压（**非自动**：没有任何 turn 的
会话不会消费队列，任务只延迟不丢）。互斥靠状态迁移（`claim_task` 只把 `pending` 迁
`running`），不会重复执行。定时器没装、机器关机、headless 被杀，都只是**延后**：超时的
`running` 在下次领取时被回收（`reclaim_tasks`，幂等）。

**两种失败分开计数（R2，2026-09-16 审查）**：`attempts` = 执行体真的试过并回报
`ok=false` 的次数（达 3 次 failed）；`timeouts` = 领取后没回来的次数（连续 3 次才 failed，
`err` 写「执行体未回报」）。因此「被打断三次」不会把一条从未尝试过的任务判失败——
排查时按 `err` 文案区分两者，别把超时当成任务本身的失败。

### 排查表

| 现象 | 含义 | 处置 |
|---|---|---|
| 脚本退出码 127，提示「找不到 dsh」 | `dsh` 不在 PATH | 用 `DSH_BIN=/abs/path/to/dsh` 指定，或修好 PATH 后重跑 |
| 脚本退出码 2，提示「平台服务不可达」 | 队列端点由服务提供，服务没起 | 启动服务或确认 platform-autostart；脚本**不会**自己拉起服务 |
| 脚本退出码 124，提示「超过上限已中止」 | 单次值班超 `DSH_DUTY_TIMEOUT`（默认 1800s） | 未完成任务留在队列，下次唤醒/会话首次交互继续；持续超时先看日志卡在哪一步 |
| 唤醒时队列为空，且服务是刚启动的 | 服务在 19:05 之后才起，入队晚于 19:20 唤醒 | 确认服务在 19:05 前运行；本次任务等下一次唤醒或会话兜底消费（不丢、只延后） |
| 队列一直 `pending` | 定时器没跑或服务没起 | 查 `systemctl --user list-timers research-duty.timer` 与 `~/.dsh/logs/research-duty-*.log` |
| 任务长期 `running` | 执行体死在半路（headless 被杀/会话中断） | 下次 `claim` 自动回收；急用可手工触发一次唤醒让 `reclaim` 生效 |
| 任务被拒领 + critical 告警 | 载荷被直改库塞进了自由文本（队列即攻击面） | **队列暂停在队首**（fail-closed）。人工核对 `research_tasks.payload`，修正或删除该行后才继续 |
| `attempts=3` 转 `failed` | 真失败不无限重试（防烧额度） | 看 `err` 字段定位（执行体自报原因）；确认是任务本身问题再人工重新入队 |
| `timeouts=3` 转 `failed` | 执行体**连续三次没回报**（≠ 任务本身失败） | 先修执行体（定时器/会话/超时上限），再重新入队；`err` 文案为「执行体未回报（连续 N 次超时…）」 |
| 重复回报后状态不变 | 终态幂等（R1）：`done`/`failed` 不被后到的回报推翻 | 属预期，不是故障；重试回报不必先查状态 |
| 日志为空但退出码 0 | 队列本来为空（非交易日/已消费完） | 属预期；要验证链路就手工 `enqueue-research` 再来一次 |

排查入口：

```bash
sqlite3 ~/.dsh/trading-data/trading.sqlite \
  "SELECT task_id,kind,status,attempts,timeouts,created_at,err FROM research_tasks ORDER BY created_at DESC LIMIT 10;"
```

## 演练记录（待 WP4 daemon 合并后执行）

> **可重复的端到端验收**（两个 harness + 缺陷清单 + 闭环判据）见
> [E2E-ACCEPTANCE.md](E2E-ACCEPTANCE.md)：后端 `scripts/e2e_workbench.py`（含 `--read-only`）、
> 前端 `node scripts/e2e_web.mjs`（含 `--routes/--no-screenshot/--fail-on-soft`）。

> 以下四场景须在 WP4 daemon 合并、`install_plugins.py link` 同步后按上文步骤实际执行，输出原文（JSON/命令回显）粘贴到对应条目，并回填至 `docs/superpowers/plans/2026-09-14-wp5-ops-acceptance.md` 的 WP5 验收记录。

- **场景 1（daemon 崩溃/重启/心跳/指令去重）：** 待执行
- **场景 2（执行中断 → unknown → 对账兜底）：** 待执行
- **场景 3（对账差异 → 告警/halt → 恢复）：** 待执行
- **场景 4（token 过期 → 续期链）：** 待执行
- **内核预检（已执行，WP5 任务 1）：** `scripts/drills.sh` → `{"drill": "kill_switch", "rejected": true, "rule": 1, "cleared": true, "ok": true}`（2026-09-15，feat/wp5 worktree）
