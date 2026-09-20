# `quant-sdk` profile 素材（规格 FR-GATEWAY-002 / §5.2.2 / §6.3）

量化专属 **SDK JSON-RPC 会话 profile**：把 Harness 变成一个可被平台驱动的**有状态交互式研究会话**，
并把这个会话的工具面收窄到**只读研究**。本目录是**素材**，不是已安装的 profile——按下面
「启用」一节拷进 `$DSH_HOME/profiles/` 才生效。**本次没有写入线上 `~/.dsh/profiles/`**
（理由见 §六），真机验证在**独立 `DSH_HOME`（`/tmp/dsh-quant-sdk-iso`）**里做，详见 §四。

| 文件 | 作用 |
|---|---|
| `package.json` | profile 元数据；`dsh.profile.bundles` = `dsh-base` + `dsh-sdk-app`（都真实存在） |
| `cordis.patch.yml` | 用户 patch 层：JSON-RPC 服务行配置 + 平台 MCP 客户端行 + 本地工具白名单（按行关停）+ 白名单闸门插件 |
| `cordis.yml` | profile 根（空 `[]`，与官方 profile 一致；**不要**编辑它） |
| `pnpm-workspace.yaml` | 工作区设置（`nodeLinker: hoisted` + `autoInstallPeers: false`，与官方三个 profile 逐字一致） |
| `tool-whitelist.json` | **工具白名单单一事实源**（拒绝集 + 关停行 + 机制说明；与 `v3_sdk.py`、插件 JS 三方一致，测试断言） |
| `tool-whitelist/` | 白名单闸门插件（`quant-tool-whitelist`，`ctx.tools.guard` + `ctx.tools.restrict`） |
| `README.md` | 本文件 |

配套实现：`platform/server/v3_sdk.py`（**stdio JSON-RPC 会话客户端** + `/api/v3/sdk/*` 三端点）。

---

## 一、启用

### 方式 A：临时 DSH_HOME（不改线上，推荐先这样试）

```bash
REPO=/home/penn/workspace/dsh-trading-agents
ISO=$(mktemp -d /tmp/dsh-quant-sdk-XXXX)

# 1) profile 目录 + 让 profile 能解析到既有共享包（共享目录是扁平布局）
mkdir -p "$ISO/profiles/quant-sdk" "$ISO/profiles/node_modules"
for e in ~/.dsh/profiles/node_modules/*; do ln -s "$e" "$ISO/profiles/node_modules/$(basename "$e")"; done

# 2) 拷入素材（README.md / tool-whitelist.json / tool-whitelist/ 不进 profile 目录，
#    其中 tool-whitelist/ 由第 3 步以 file: 依赖装进去）
cp "$REPO"/platform/install/quant-sdk/{package.json,cordis.yml,cordis.patch.yml,pnpm-workspace.yaml} \
   "$ISO/profiles/quant-sdk/"

# 3) 装白名单闸门插件（profile 自己的 node_modules；peer 警告可忽略，cordis 从共享目录解析）
DSH_HOME="$ISO" dsh plugin --profile quant-sdk add file:$REPO/platform/install/quant-sdk/tool-whitelist

# 4) 凭据（模型要能鉴权；不拷就只能验证组合，不能真跑）
cp ~/.dsh/.credentials.yaml "$ISO/.credentials.yaml" && chmod 600 "$ISO/.credentials.yaml"
[ -f ~/.dsh/settings.yaml ] && cp ~/.dsh/settings.yaml "$ISO/settings.yaml"

# 5) 先看组合树（不调用模型、不产生副作用）
DSH_HOME="$ISO" dsh --profile quant-sdk --dump-config | grep -nE 'jsonrpc|maxTokensAsSuccess|quant-platform-mcp|quant-sdk-tool-whitelist'

# 6) 真跑一次握手 + 一条最小提示（平台侧同一件事走 POST /api/v3/sdk/prompt）
DSH_HOME="$ISO" dsh --profile quant-sdk    # 然后从 stdin 送 JSON-RPC 帧，见 §四
```

> `profiles/node_modules` 用**软链顶层条目**（而不是整体软链 `profiles/`）：这样
> `dsh plugin add` 若要落包，只会写进临时目录，**绝不碰线上共享目录**。

### 方式 B：装成常驻 profile（要动线上 `~/.dsh`，本次**未做**）

```bash
mkdir -p ~/.dsh/profiles/quant-sdk
cp platform/install/quant-sdk/{package.json,cordis.yml,cordis.patch.yml,pnpm-workspace.yaml} \
   ~/.dsh/profiles/quant-sdk/
dsh plugin --profile quant-sdk add file:$PWD/platform/install/quant-sdk/tool-whitelist
```

### 平台侧启用（**主 agent 的动作**，本目录不碰 `app.py`）

`app.py` 的 V3 子模块自动接线循环里加入 `"v3_sdk"` 即生效（模块暴露
`register(app, v3_run, home)`，与其它 V3 子模块同一约定）：

```python
for _v3_module in ("v3_market", "v3_risk", ..., "v3_sdk"):
```

之后可用端点：

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/api/v3/sdk/status` | **只读**：真实子进程状态（pid/argv/state/exit_code）、握手结果（含协议标识逐字比对）、帧计数、会话概览、stderr 尾部、白名单审计、最近审计。**不会**因为 GET 而起进程 |
| GET | `/api/v3/sdk/sessions?session=&since=&limit=` | **只读**：会话状态 + 事件回放（游标 `since` 之后、最多 `limit` 条） |
| POST | `/api/v3/sdk/prompt` | **写**：`{"prompt": "...", "session_id": "quant-001", "confirmation": "确认下发"}`；`{"action":"start"}` 只做「起进程 + 握手」不下发。**缺口令一律拒绝并落审计** |

环境变量（都有缺省，见 `v3_sdk.runtime_options()`）：
`QUANT_SDK_PROFILE`（默认 `quant-sdk`）、`QUANT_SDK_DSH`（默认 `dsh`）、
`QUANT_SDK_DSH_HOME` 或 `DSH_HOME`、`QUANT_SDK_CWD`、`QUANT_SDK_PROVIDER`
（默认 `deepseek-official`）、`QUANT_SDK_MODEL`（默认 `deepseek-flash`）、
`QUANT_SDK_EFFORT`、`QUANT_SDK_TOOL_DENY`（逗号分隔的运行期加严拒绝）。
审计落 `<home>/v3-sdk-audit.jsonl`（0600，逐行 append；**成功与拒绝都留痕**，只存
`prompt_sha256` / `prompt_preview`(200 字) / 长度 / 会话 / profile / pid / 握手状态 / messageId，
不回显全文）。

---

## 二、真实可用的包与插件（都核实过，没照抄规格）

部署里 `@deepseek-ai/` 共 249 个包（`ls ~/.npm/_npx/1e7f6d9597241db0/node_modules/@deepseek-ai/`），
版本**全部 0.1.5-rc.2**。本 profile 只用到三个：

| 包 | 角色 | 是否已装 |
|---|---|---|
| `@deepseek-ai/dsh-base` | bundle：共享内核 | ✅ 在共享目录 |
| `@deepseek-ai/dsh-sdk-app` | bundle：SDK 应用层（stdio JSON-RPC 生命周期） | ✅ 在共享目录 |
| `@deepseek-ai/dsh-sdk-jsonrpc-server` | 服务端插件（协议实现） | ✅ 在共享目录，**由 `dsh-sdk-app` bundle 自动挂载** |
| `@deepseek-ai/dsh-mcp-client` → 平台 MCP 工具面 | 唯一交互面 | ✅ 在共享目录，**无需额外装包** |

**`@deepseek-ai/dsh-sdk-jsonrpc-server` 没有 `bin` 可执行入口**（`package.json` 里没有 `bin` 字段，
它是个 Loader 插件）；协议入口是 `dsh --profile sdk` 本身，由 `dsh-sdk-app` 的
`cordis.patch.yml` 挂载并接管 stdio。核实命令：

```bash
python3 -c "import json;print(json.load(open('$HOME/.dsh/profiles/node_modules/@deepseek-ai/dsh-sdk-jsonrpc-server/package.json')).get('bin'))"   # → None
grep -n 'sdk-jsonrpc-server' -A3 $HOME/.dsh/profiles/node_modules/@deepseek-ai/dsh-sdk-app/cordis.patch.yml
```

`dsh-sdk-app` 的 patch 里那两行是：

```yaml
    - id: sdk-jsonrpc-server
      name: '@deepseek-ai/dsh-sdk-jsonrpc-server'
      inject: [sdkAppStartup, loader]
```

### ⚠️ 与规格 §6.3 的一处**有意差异**（已实测）

§6.3 的示例是在 `cordis.patch.yml` 里 **insert** 一行 `sdk-jsonrpc-server`。但实测
`dsh --profile sdk --dump-config` 显示该行**已经由 `dsh-sdk-app` bundle 挂上了**：

```
# == @deepseek-ai/dsh-sdk-app
- id: sdk-app-startup
  name: '@deepseek-ai/dsh-sdk-app'
- id: sdk-jsonrpc-server
  name: '@deepseek-ai/dsh-sdk-jsonrpc-server'
```

同一个 id 再 insert 一遍就是**重复行**。因此本 profile 走**按 id 覆盖既有行 config** 的路
（与 `quant-headless` 覆盖 `system-prompt` 同一手法）：

```yaml
- id: sdk-jsonrpc-server
  config:
    maxTokensAsSuccess: false      # §6.3 原文的这一项
```

结果与 §6.3 **等价且不重复挂载**——`--dump-config` 实测（隔离 home，**退出码 0**）：

```
- id: sdk-jsonrpc-server
  name: '@deepseek-ai/dsh-sdk-jsonrpc-server'
  ...
  config:
    maxTokensAsSuccess: false
- id: quant-platform-mcp
  name: '@deepseek-ai/dsh-mcp-client'
- id: quant-sdk-tool-whitelist
  name: quant-tool-whitelist
```

---

## 三、工具白名单：为什么是三层，每层的证据

硬边界（FR-GATEWAY-002）要求「该会话的工具白名单**必须排除全部写/交易工具**」。
`quant-headless` 的既有做法是**按 shipped 行的 `disabled: true` 关停**——本 profile 照抄了
（`disabledRows`，14 行），但**实测这一层挡不住 MCP 工具**：平台工具面是经
`quant-platform-mcp` **一行** `dsh-mcp-client` 进来的，而该包**没有** allow/deny 配置项
（README 配置表只有 `serverName`/`transport`/`url`/`env`/`failOnStartupError`/
`toolCallTimeoutMs`/`reconnect`），`@deepseek-ai/dsh-tools` 的 `Config` 也只有
`mode` / `maxParallelSubCalls`。所以第三层是必须的：

| 层 | 机制 | 关掉了什么 | 关不掉什么 |
|---|---|---|---|
| ① profile 行关停 | `disabled: true`（14 行） | 本地 `tool-bash` / `tool-pwsh` / `tool-jobs` / 委派 / 编排 / goal / 外网直连 | 任何 MCP 工具 |
| ② MCP 客户端行 | 只挂 `quant-platform-mcp`（一行） | ——（桌面唯一入口） | 单行无法按工具收窄 |
| ③ **白名单闸门插件** | `ctx.tools.guard()`（单调拒绝）+ `ctx.tools.restrict({deny})`（按 agent 摘除可见工具） | **`trade_*` / `sim_trade_*` / `plan_execute` / `switch_mode` / `push_*` / `research_tasks_*` / `admin_cancel*` / `admin_prune*` / `v3_credentials` / `v3_oms_sync` / `v3_strategy_run` / `v3_sdk_*`** | —— |

拒绝集的**真值来自平台自己的常量**（`v3_ops.WRITE_TOOLS` +
`mcp_tools.MCP_EXCLUDED_ENDPOINTS − readOnlyExcluded` + `tools/e2e_probe.WRITE_ENDPOINTS`），
在 `tool-whitelist.json` / `v3_sdk.py` / `tool-whitelist/index.js` 三处逐字同源，并被
`platform/tests/test_v3_sdk.py` 三方一致断言 + `policy_gaps` 覆盖断言（平台新增写工具而这里
没跟上 → 测试红）。

### 实测证据（真机，隔离 home）

**① 插件真的摘除了 16 件**（`/status` 的 `stderr_tail` 原文，插件把诊断写 stderr——
stdout 是 JSON-RPC 专线，绝不污染）：

```
[quant-tool-whitelist] agent quant-probe-1: 已从可见工具面摘除 16 件 写/交易工具：mcp__quantwb__switch_mode, mcp__quantwb__plan_execute, mcp__quantwb__trade_place, mcp__quantwb__trade_modify, mcp__quantwb__trade_cancel, mcp__quantwb__trade_max_qty, mcp__quantwb__push_subscribe, mcp__quantwb__push_unsubscribe, mcp__quantwb__research_tasks_claim, mcp__quantwb__research_tasks_report, mcp__quantwb__admin_cancel_run, mcp__quantwb__admin_cancel_stale, mcp__quantwb__admin_prune_runs, mcp__quantwb__v3_credentials, mcp__quantwb__v3_oms_sync, mcp__quantwb__v3_strategy_run
```

**② 模型自己列出的工具面里没有写/交易工具**（提示词：「只列出你当前可用的工具名，不要调用
任何工具……如果能看到 trade_place / trade_modify / trade_cancel / plan_execute / switch_mode
中任何一个，请把它列出来」）。模型的回答（节选，**原文**）：

> **关于写/交易工具：无写/交易工具。**
>
> 我没有看到 `trade_place`、`trade_modify`、`trade_cancel`、`plan_execute`、`switch_mode`
> 中的任何一个，因此在此工具面上我无法下单、改单、撤单、执行计划或切换模式。

**③ 两侧判定逐字一致**（离线交叉核对，97 个真实工具名，含 `mcp__quantwb__*` 形态与
`-`/`_` 两种写法）：`checked 97 names; mismatches: []`，JS 与 Python 各拒 28 件。

> **边界诚实性**：白名单是**纵深的一层，不是交易边界本身**。真正的闸门在服务侧——写端点要
> 人工在 Web 确认（`store_access.request_confirmation`，TTL 120s 超时=拒绝，fail-closed）、
> `confirm-decide` / `rules-decide` / 设置页三端点**有意不进 MCP 工具面**、
> `/api/v3/credentials` 的 `save`/`clear` 在桥内封死（`v3/credentials-web-only`）、
> sim→live 要人在 Web 输口令「确认实盘」。本 profile 只是把「模型能碰到的东西」收窄。
>
> 另：`v3_sdk_*`（本模块自己的三个端点，含 `prompt` 写端点）也在拒绝集里——否则会话能自己
> 给自己下发提示词（递归/自我授权），这条是**有意**的整族拒绝。

---

## 四、真机验证（本次真的跑了，原始输出如下）

环境：隔离 `DSH_HOME=/tmp/dsh-quant-sdk-iso`（`profiles/node_modules` 软链到线上共享目录，
凭据/settings 拷入），`dsh 0.1.5-rc.2`，路由 `deepseek-official` / `deepseek-flash`，
`reasoningEffort=low`。**注意时间戳显示这台机器的日期是 2026-09-20。**

### 4.1 握手（`initialize`）

用**本仓库自己的客户端** `server.v3_sdk.SdkRuntime` 驱动（不是手搓脚本）：

```
INIT 24.2s {"state": "ok",
           "expected": "deepseek-harness-sdk-runtime",
           "observed": "deepseek-harness-sdk-runtime",
           "version": "0.0.1",
           "protocol_match": true,
           "elapsed_ms": 24194,
           "params": {"cwd": "/home/penn/workspace/dsh-trading-agents",
                      "provider": "deepseek-official",
                      "model": "deepseek-flash",
                      "reasoningEffort": "low"},
           "spec": "docs/v3-spec.md FR-GATEWAY-002 / §5.2.2"}
```

原始报文（第一帧，`dsh --profile sdk` 隔离 home，冷启动 **13.2s**）：

```json
{"jsonrpc":"2.0","id":"req_1","result":{"serverInfo":{"name":"deepseek-harness-sdk-runtime","version":"0.0.1"}}}
```

→ **规格里写的协议稳定标识「deepseek-harness-sdk-runtime」逐字命中**。若不符，客户端会如实报
`protocol_match:false` + `observed` 原文，并**拒发提示词**（fail-closed，见
`test_mismatched_handshake_refuses_to_send_prompt`）。

### 4.2 最小真实提示（**真的跑了，真的花了 token**）

提示词：`只回复 OK，不要调用任何工具`（15 字）。客户端侧观测：

```
RECEIPT 1.0s {"ok": true, "session_id": "usage-1", "queue_id": "q_1", "state": "sent",
              "position": 1, "message_id": "664f51f7-4175-4bdd-bfea-fd909894a30c",
              "chars": 15, "sha256": "ad2a676a242003913d98e329cc0273bfa9888ff5855b66e38692c9b0c044ead1"}
TURN    6.1s state=done
```

事件流（`session.event`，`cursor` 即客户端游标；**流式**收到，不是轮询）：

```
permission/preset, sandbox/mode, approval/policy, agent/inbox/spliced, turn/start,
agent/inbox/spliced, step/start, system/message, user/message ×4, request/header,
request/context, session/title, assistant/message, step/end, turn/end
```

`session.status` 转换：`running` → `idle`（`status_events: 2`）。模型的回答是 `OK`；
`assistant/message` 里带着**真实 token 账**：

```json
"usage": {"inputTokens": 5423, "outputTokens": 44, "totalTokens": 30683,
          "cacheReadTokens": 25216, "reasoningTokens": 42}
```

→ **确认消耗 token**：一次最小提示 ≈ 5.4k 输入（其中 25.2k 走缓存读，故 total 30.7k）+ 44 输出。
（提醒：SDK 通道每次 `initialize` 都会让 runtime 起一轮真实模型请求，平台上量前必须按
FR-GATEWAY-004 的外部熔断口径设超时/并发/费用上限。）

### 4.3 会话复用（同一个 `session_id`，同一进程内两条提示）

规格要求「按 session_id 打开/复用会话、把提示词排队」。真机两条提示：

```
INIT 24.6s protocol_match=True
P1 receipt 8a45a63c-6aa1-4e36-804a-332e629bbd0b state=sent
P1 done in 8.0s -> done
P2 receipt 1ebb55db-29f6-41a7-9d01-b8b23f2a8ab1
P2 done in 2.1s -> done
ASSISTANT TEXTS: ["OK", "42"]
SESSION reuse: prompts=2 message_ids=['8a45a63c-…', '1ebb55db-…'] turn_events=2 status=idle
```

提示词 1 是「记住数字 41，只回复 OK」，提示词 2 是「上一个提示里的数字加 1 是多少？只回复数字」，
模型答 **42** —— 会话是**真的有状态**的（同一 Agent、同一份上下文），事件流里能看到
`turn/start → … → assistant/message → turn/end` 出现两次。

### 4.4 收尾与退出码

```
STOP: {"ok": true, "state": "stopped", "exit_code": 0, "forced": false, "shutdown_error": null, ...}
```

`shutdown` 原始报文：`{"jsonrpc":"2.0","id":"req_2","result":{}}` → 进程 **exit 0**，stderr 只有
上面那条白名单诊断（**stdout 全程只有 JSON-RPC 帧**，`malformed_lines: 0`,
`frame_overflows: 0`）。

### 4.5 真机验证没做的事（如实说明）

* **没有下任何真实订单**：本任务全程未调用任何写/交易端点，未调用 `v3_run` 的任何写工具，
  也**没有**去点工作台的确认卡片。
* **没有重启 8397**：平台服务由主 agent 统一重启；本次真机验证只起/停**独立**的
  `dsh --profile quant-sdk` 子进程。
* **没有写入线上 `~/.dsh/profiles/`**：只创建了 `/tmp/dsh-quant-sdk-iso`（临时，可删）。
  线上 `~/.dsh/profiles/` 的 4 个 profile（headless/sdk/web/…）与共享 `node_modules` 未被改动。
* 平台侧 `/api/v3/sdk/*` 端点的**真机**联通要等主 agent 把 `"v3_sdk"` 加进 `app.py` 的接线
  循环后再验；本任务只验证了「模块 + 独立进程 + 协议」这一段。

### 4.6 两条真机踩出来的协议行为（已写进代码与测试）

1. **同一个 `session_id` 不能跨进程复用**。真机上第二次用 `real-probe-1` 起新进程时，
   `session/prompt` 回了 wire 错误：`-32603 session "real-probe-1" already exists`
   （该 `DSH_HOME` 的会话存储里已有同名会话；协议没有 resume/close 方法）。客户端**原样保留**
   这条 wire 文案，并附 `session_exists: true` + `hint`（换 id，或在同一 runtime 进程内复用）——
   见 `explain_rpc_error()` 与 `test_session_already_exists_error_keeps_wire_text_and_adds_hint`。
   **平台侧含义**：`session_id` 要按「一个 runtime 进程内的一段对话」来分配，不能拿它当跨进程主键。
2. **`initialize` 是真正的冷启动边界**：真机冷启动 13.2s / 18.4s / 24.2s / 24.6s（同一台机器，
   取数越多越慢）；握手超时默认 180s。`GET /api/v3/sdk/status` **不会**触发启动，冒烟与巡检
   不会因此产生费用。

---

## 五、这个 profile 与 §7.1 的数据流对齐在哪

```
用户在前端输入分析指令 → gateway-svc 建 session_id → 平台 POST /api/v3/sdk/prompt（口令「确认下发」）
  → SdkRuntime 起/复用 dsh --profile quant-sdk 子进程 → initialize 握手（就绪边界）
  → session/prompt 入队（拿 messageId 入队回执）→ Harness Agent Loop 调 mcp__quantwb__* 只读工具
  → 每个 session.event / session.status 流式回平台（GET /api/v3/sdk/sessions 可回放）
  → 审计落 v3-sdk-audit.jsonl，结论进研究面/决策日志
```

与规格示例（`deepseek-harness-sdk` Python 包）的**唯一差异**：本仓库的 venv 里
`import deepseek_harness` = **False**（包未安装），因此客户端是**自己实现的换行分帧 JSON-RPC**
（协议自描述，三方法四通知全部是普通 JSON）。好处：不新增依赖、可与平台版本独立演进、
离线单测能把分帧/超时/异常退出逐条钉死。协议里有两条硬限制被**如实转述**而不是绕开：

* 没有 per-prompt 结果（`messageId` 只是入队回执）→ 本客户端的「一次下发完成」边界定义在
  **观测到新的 `turn/end`**（`state: done`），等不到就报 `timeout` 并说明「轮次可能仍在进行」；
* 没有 per-session 关闭/取消 → `stop()` 是**进程级**停机（`shutdown` → EOF → terminate/kill）。

---

## 六、为什么默认**不**写进线上 `~/.dsh/profiles/`

1. 本目录是**素材**，装到哪里是部署方的决定；直接写线上会让「仓库里有什么」与「机器上装了什么」
   不再一一对应，手改的 profile 也不会随仓库更新。
2. 线上 `~/.dsh/profiles/` 被官方 `headless`/`sdk`/`web` 共用，其中 `web` 挂着交易插件栈
   （`@bstester/dsh-trading-*`）。在一个共享目录里加 profile 属于**改动生产运行面**。
3. 本 profile 的工具面只依赖**两个官方包**（`dsh-sdk-app` 里带的服务端插件 + 共享目录里的
   `dsh-mcp-client`）加一个本仓库自带的 40 行插件，没有第三方风险；要改的只是配置与白名单，
   属可回滚的小改动。需要常驻就按 §一 方式 B 拷进去。
4. 平台侧还有一个**开关**：`app.py` 的接线循环里没有 `"v3_sdk"` 时，本模块根本不加载——
   也就是说「装不装 profile」与「平台开不开 SDK 通道」是两件独立的事，可以分别上线。

---

## 七、排障（都基于实测）

| 现象 | 真实原因 | 处置 |
|---|---|---|
| `/status` 里 `state: failed` + `exit_code: 1`，`stderr_tail` 有 `ERR_PNPM…` 或 `Cannot find module` | profile 目录缺 `profiles/node_modules` 解析路径，或插件没装 | 按 §一 步骤 1/3 补齐；`dsh plugin --profile quant-sdk add file:…` 只需跑一次 |
| `handshake.state: "timeout"` | 模型路由不可达 / 凭据没拷进隔离 home / 首次冷启动慢 | 看 `stderr_tail`；把 `.credentials.yaml`（0600）拷进 `$DSH_HOME`；握手超时默认 180s，真机冷启动实测 13–24s |
| `protocol_match: false` + `observed: null` | 起的根本不是 SDK profile（例如误用 `--profile web`） | `argv` 与 `profile` 字段会如实显示实际启动的命令；POST 会被 fail-closed 拒绝 |
| 会话工具面里仍能看到 `trade_place` | 插件没装/没挂（`stderr_tail` 里**没有**那句 `[quant-tool-whitelist] … 摘除 N 件`） | 确认 `dsh plugin --profile quant-sdk add file:…` 执行过、patch 里 `quant-sdk-tool-whitelist` 行存在；`tool-whitelist.json` 的 `denyMechanism` 有完整说明 |
| `tests/test_v3_sdk.py::RealMachineTest` skipped | 默认 skip（要真机进程与模型路由） | `QUANT_SDK_REAL=1 QUANT_SDK_REAL_HOME=/tmp/dsh-quant-sdk-iso python -m unittest tests.test_v3_sdk` |
| `session "<id>" already exists`（`-32603`） | 该 `DSH_HOME` 的会话存储里已有同名会话；协议无 resume/close | 换一个 `session_id`，或在**同一 runtime 进程内**复用（§4.3）；平台的 `session_id` 不要当跨进程主键 |
| 改了 `tool-whitelist/index.js` 但白名单行为没变 | `dsh plugin add file:…` 是 **pnpm 拷贝**，不是软链；pnpm 见版本没变会说 `Already up to date` | `rm -rf $DSH_HOME/profiles/quant-sdk/node_modules/quant-tool-whitelist $DSH_HOME/profiles/quant-sdk/pnpm-lock.yaml` 后重跑 `dsh plugin add`（或用 `link:` 装成软链，开发期更省事） |
