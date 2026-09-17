# 运维 Runbook（WP5）

> **状态标注：** 本文涉及 daemon、指令目录、心跳文件、工作台页面的步骤按 WP4 计划规格撰写，**以 WP4 合并后实测为准**；「演练记录」小节**待 WP4 daemon 合并后执行**回填。
> kill switch 演练内核（`scripts/drills.sh`）与风控联动已落地（WP5 任务 1），可先行执行。
> **WP7（2026-09-16）**：daemon 常驻循环由**平台服务内调度器**承担（随服务进程存活，
> daemon CLI 保留为手动入口），场景 1 的「daemon 崩溃重启」对应平台服务进程的重启；
> 其余协议（心跳/指令目录/kill/告警）不变。

恢复原则（规格 §8.4，P4 延续）：daemon 崩溃 systemd 重启；执行中断订单留 `unknown` 由对账兜底；**先查券商再动手，不自动清除未知在途状态，不自动平仓**。

## 0. 前置与路径速查

前置：`~/.dsh/trading-venv` 存在，且执行过 `install_plugins.py link` 把 `trading_core`/`trading_datasource` 同步进 `~/.dsh/trading-python/`（**每次合并 WP 分支后重新 link 一次**，否则 venv 里的 core 是旧副本）。

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
（`/mcp`，streamable-http，33 工具，Harness 侧工具名 `mcp__quantwb__*`）与前端静态托管
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
  "mcp": ".../mcp", "tools": 33, "auth": "loopback-only"}`。

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
4. 修正本地台账、迁移完在途单后，清 halt：
   ```bash
   ~/.dsh/trading-venv/bin/python -B -c "from trading_core import store; \
     store.clear_halt(store.connect())"
   ```
5. 若当时上了 kill switch：人工确认后 `unkill`（删除 `~/.dsh/trading-kill`）。
6. 次日恢复验证：daemon 到点正常 `build_plan` 产出冻结计划；工作台红点消失；`is_halted` 为 `False`。

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

## last30days 社媒研究技能排障（可选组件）

- 行为异常先自检：`~/.dsh/trading-venv/bin/python ~/.dsh/last30days-skill/skills/last30days/scripts/last30days.py --preflight`（不读 Cookie 不写文件）；报「目录/引擎不存在」先跑 `python3 scripts/install_last30days.py`，装完新建会话才会挂载。
- 密钥缺失时的降级面：免密钥来源（Reddit/HN/Polymarket/GitHub/StockTwits）照常可用；X、YouTube、TikTok/Instagram/Threads/Pinterest/LinkedIn、小红书、Perplexity、Brave 未按上游 README 配置密钥/会话时这些来源缺席（简报如实标注），属预期行为而非故障。

## 自动流水线（WP9）

> 前提：`trading-platform.json` 已开 `auto_pipeline.enabled=true`（配置样例见 README
> 「WP9：自动流水线」）。关掉开关即回到全人工，任何演练都不需要改代码。

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

排查入口：`snapshot-schedule`（ran 标记/心跳/告警）、`snapshot-reconcile`（差异/TCA/
链路）、`snapshot-plan`（计划→订单→风控预检）。

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
`running` 在下次领取时被回收（`reclaim_tasks`，幂等），`attempts` 达 3 次才判 `failed`。

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
| `attempts=3` 转 `failed` | 坏任务不无限重试（防烧额度） | 看 `err` 字段定位；确认是任务本身问题再人工重新入队 |
| 日志为空但退出码 0 | 队列本来为空（非交易日/已消费完） | 属预期；要验证链路就手工 `enqueue-research` 再来一次 |

排查入口：

```bash
sqlite3 ~/.dsh/trading-data/trading.sqlite \
  "SELECT task_id,kind,status,attempts,created_at,err FROM research_tasks ORDER BY created_at DESC LIMIT 10;"
```

## 演练记录（待 WP4 daemon 合并后执行）

> 以下四场景须在 WP4 daemon 合并、`install_plugins.py link` 同步后按上文步骤实际执行，输出原文（JSON/命令回显）粘贴到对应条目，并回填至 `docs/superpowers/plans/2026-09-14-wp5-ops-acceptance.md` 的 WP5 验收记录。

- **场景 1（daemon 崩溃/重启/心跳/指令去重）：** 待执行
- **场景 2（执行中断 → unknown → 对账兜底）：** 待执行
- **场景 3（对账差异 → 告警/halt → 恢复）：** 待执行
- **场景 4（token 过期 → 续期链）：** 待执行
- **内核预检（已执行，WP5 任务 1）：** `scripts/drills.sh` → `{"drill": "kill_switch", "rejected": true, "rule": 1, "cleared": true, "ok": true}`（2026-09-15，feat/wp5 worktree）
