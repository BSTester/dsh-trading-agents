# `quant-headless` profile 素材（规格 §5.2 / §6）

量化专属一次性（headless）profile：**量化分析师 system prompt + 风控规则 + 工具白名单**。
本目录是**素材**，不是已安装的 profile——按下面「启用」一节拷进 `$DSH_HOME/profiles/` 才生效。
**本次没有写入线上 `~/.dsh/profiles/`**（理由见文末）。

| 文件 | 作用 |
|---|---|
| `package.json` | profile 元数据；`dsh.profile.bundles` 指向**真实存在**的包 |
| `cordis.patch.yml` | 用户 patch 层：角色 + 风控规则 + 工具白名单 + MCP 工具面 + **写/交易工具逐名拒绝**（`quant-headless-tool-deny` 行） |
| `cordis.yml` | profile 根（空 `[]`，与官方 profile 一致；**不要**编辑它） |
| `pnpm-workspace.yaml` | 工作区设置（`nodeLinker: hoisted` + `autoInstallPeers: false`，与官方三个 profile 逐字一致） |
| `hooks.json` | `PreToolUse` 命令钩子：**42 个**写/交易工具名（含 `mcp__quantwb__` 前缀）一律 `exit 2` 阻断 |
| `tool-whitelist.json` | 白名单唯一事实源（关停行 / 拒绝工具名 / matcher / 推导来源）；由 `server/v3_headless.py` 的 `deny_tool_names()` 推导同一份名单 |
| `README.md` | 本文件 |

> **平台侧会校验这份白名单**：`platform/server/v3_headless.py` 的 `verify_whitelist()` 在起
> `dsh` 子进程**之前**检查「危险行都 disabled + 钩子行在 + matcher 覆盖全部写工具名」，
> 任一不成立就不起进程（`outcome=whitelist-unverified`，fail-closed）。所以拷贝 profile 时
> **必须**把 `hooks.json` 与 `tool-whitelist.json` 一起拷进 `$DSH_HOME/profiles/quant-headless/`
> （下面的「方式 A」命令已包含这两个文件）。真机验证见 `docs/e2e-and-data-gaps.md` §19.

---

## 一、启用

### 方式 A：临时 DSH_HOME（不改线上，推荐先这样试）

```bash
# 1) 建临时 home，并让 profile 能解析到既有共享包（共享目录是扁平布局）
TMP=$(mktemp -d /tmp/dsh-quant-XXXX)
mkdir -p "$TMP/profiles/quant-headless" "$TMP/profiles/node_modules"
for e in ~/.dsh/profiles/node_modules/*; do ln -s "$e" "$TMP/profiles/node_modules/$(basename "$e")"; done

# 2) 拷入素材（6 个文件；README.md 不用进 profile 目录）。
#    hooks.json / tool-whitelist.json 是**必须**的：平台起进程前会校验它们（fail-closed）
cp platform/install/quant-headless/{package.json,cordis.yml,cordis.patch.yml,pnpm-workspace.yaml,hooks.json,tool-whitelist.json} \
   "$TMP/profiles/quant-headless/"

# 3) 凭据（模型要能鉴权；不拷就只能验证组合，不能真跑）
cp ~/.dsh/.credentials.yaml "$TMP/.credentials.yaml" && chmod 600 "$TMP/.credentials.yaml"
[ -f ~/.dsh/settings.yaml ] && cp ~/.dsh/settings.yaml "$TMP/settings.yaml"

# 4) 先看组合树（不调用模型、不产生副作用）
DSH_HOME="$TMP" dsh --profile quant-headless --dump-config

# 5) 真跑一个一次性任务
DSH_HOME="$TMP" dsh --profile quant-headless "复盘今日 A 股持仓的行业集中度与回撤，输出研究笔记"
```

> `profiles/node_modules` 用**软链顶层条目**（而不是整体软链 `profiles/`）：这样
> `dsh plugin add` 若要落包，只会写进临时目录，**绝不碰线上共享目录**。

### 方式 B：装成常驻 profile（要动线上 `~/.dsh`，本次未做）

```bash
mkdir -p ~/.dsh/profiles/quant-headless
cp platform/install/quant-headless/{package.json,cordis.yml,cordis.patch.yml,pnpm-workspace.yaml,hooks.json,tool-whitelist.json} \
   ~/.dsh/profiles/quant-headless/
dsh --profile quant-headless "…"
```

### 与 `platform/` 的衔接

* **取数通道（唯一交互面）**：`cordis.patch.yml` 末尾 insert 了官方
  `@deepseek-ai/dsh-mcp-client` 行，指向 `http://127.0.0.1:8397/mcp`。这就是本 profile
  与平台之间的**全部**交互：既有工作台工具 + `/api/v3/*` 路由桥接出的 `v3_*` 工具，
  同一 `/mcp`、同一份服务端实现（见 `docs/v3-integration.md`）。**不挂任何第三方
  adapter/桥接包。** 平台服务没起时 `failOnStartupError: false` 让工具面缺席，
  由模型**如实报告数据源不可达**，而不是让会话崩在启动阶段。
* **工具面模式（规格 FR-TOOLS-003 / §10 决策 3）**：见下方「一之补：工具面模式」。
* **技能目录**：`skill-filesystem` 覆盖为仓库内
  `skills/` 与 `skills/futu-skills/`（绝对路径，换机器要改）。
* **交易边界**：本 profile 只挂 **MCP 客户端**行。写端点仍要求人工在 Web 确认
  （`store_access.request_confirmation`，TTL 120s 超时=拒绝，fail-closed）；
  `confirm-decide` 与设置页两端点**有意不进 MCP 工具面**，模型无法自批实盘单、无法自改凭据；
  sim→live 必须由人在 Web 输口令。**白名单是纵深的一层，不是唯一一层，也不该被当成「所以可以放心」。**
* **监控**：`platform/deploy/monitoring/` 的告警覆盖工作台与富途限流；
  本 profile 的 `quantwb` 工具面调用会计入 `/metrics` 的 `quantwb_mcp_calls_total`。

### 一之补：工具面模式（`QUANT_MCP_SURFACE`，切换只改一个环境变量）

MCP 面上工具越多数，**schema 成本越大**（名称 + 描述 + 完整 inputSchema 每一轮都在
上下文里）。规格 FR-TOOLS-003 要求「暴露一个 `list_tools` 入口和一个 `call_tool` 入口，
让 Harness Agent 通过间接调用发现具体能力，避免上百个工具 schema 撑爆上下文窗口」。
平台因此支持两种模式（实现在 `platform/server/mcp_discovery.py`）：

| 模式 | `/mcp` 的 `tools/list` | 实测成本（2026-09-20 worktree） |
|---|---|---|
| `discovery`（**默认**） | 4 件直连保留（`snapshot` / `admin_status` / `v3_gateway` / `v3_tools`）+ `list_tools` + `call_tool` = **6 件** | **3,248 字符** / ≈812 token（4 字符每 token） |
| `direct`（向后兼容） | 全部工作台工具 + 全部 `v3_*` 桥接件（随 `/api/v3/*` 路由表涨，实测 118 → 125） | **79,624 → 86,930 字符** / ≈19.9k → 21.7k token |

**怎么切（改一个环境变量，然后重启平台服务）**：

```bash
# 平台服务进程读的是这个变量（不是 dsh profile 的变量）；缺省 = discovery
QUANT_MCP_SURFACE=direct scripts/platform_service.sh restart     # 切回全量直暴露
QUANT_MCP_SURFACE=discovery scripts/platform_service.sh restart  # 切回发现代理（默认）

# 或者写进配置文件 <DSH_HOME>/trading-platform.json（部署级缺省，跟随机器）：
#   {"service": {"mcp_surface": "direct"}}
# 优先级：create_app(mcp_surface=...) 显式入参 > service.mcp_surface > QUANT_MCP_SURFACE
#         > 缺省 discovery。取值只能是 discovery|direct，写错直接报错（不静默退回）。
```

切换**不影响** profile 侧任何东西：`cordis.patch.yml` 的 `quant-platform-mcp` 行不用改，
两种模式都是同一个 `/mcp`、同一份服务端实现（代理转发的是注册表里同一个函数对象）。

**选择建议**：

* **默认用 `discovery`**：省掉约 96% 的 `tools/list` 上下文（≈19k token → ≈0.8k），
  代价是「要先 `list_tools` 检索、再 `call_tool` 调用」——多 1~2 次往返。检索页
  （20 张卡片）约 1,694 token，所以**只要一轮里要用的工具少于几十件就是净收益**；
  另外首轮少 19k token 对长会话的上下文压力是持续收益（不是只省一次）。
* **切 `direct` 的场合**：① 模型小/弱、多步工具编排容易走错（直连少一层间接）；
  ② 排查「代理是不是丢了某个能力」（`tools/list` 一次看全）；
  ③ 某次任务确定要连续用几十件不同工具（此时检索页的往返更贵）。
* **不要**把 `direct` 当默认：规格 §10 决策 3 明确要求工具粒度控制，且一百多件 schema
  的约 2 万 token 是**每一轮**的固定开销（不是只花一次）。

> 诚实提醒：`discovery` 省的是 **context**，多的是**往返**（模型要先发现再调用）。
> 这不是「免费优化」；上表两个数字与取舍是本仓库实测+估算，token 是按 4 字符/token 估的
> （真实分词器不在依赖里），字符数是硬数字。复现命令见 `docs/v3-integration.md` §1.5。

---

## 二、真实可用的包与插件（都核实过，没照抄规格）

部署里 `@deepseek-ai/` 共 **249 个包**（`ls ~/.npm/_npx/1e7f6d9597241db0/node_modules/@deepseek-ai/`）；
其中声明 `dsh.bundle`（可作为 `dsh.profile.bundles` 条目）的只有 **6 个**：

| bundle 包 | 用途 |
|---|---|
| `@deepseek-ai/dsh-base` | 共享内核（本 profile 用） |
| `@deepseek-ai/dsh-headless` | 一次性任务模式（本 profile 用） |
| `@deepseek-ai/dsh-web-app` | Web/Host 层 |
| `@deepseek-ai/dsh-sdk-app` / `dsh-sdk-minimal` | SDK 层 |
| `@deepseek-ai/dsh-acp-app` | ACP 层 |

版本：`@deepseek-ai/dsh` 与全部 `@deepseek-ai/dsh-*` 均为 **0.1.5-rc.2**。

### 关于 `@deepseek-ai/dsh-persona`：存在，但**不能**用在这里

包确实存在（0.1.5-rc.2）。但它的自述写明它是**作用域专用（scope-only）**行：

> `dsh-system-prompt` owns the global persona as its own config, and registers that section
> unconditionally — so this row is **scope-only**. Mounted inside an agent preset it shadows the
> deployment persona for that one session … mounted globally it **collides with the registry's own
> registration and fails loud**.

也就是说它是给 **agent preset**（`~/.dsh/.agent-presets/<id>/agent.cordis.yml`，每会话作用域）
用的；本目录是 **profile** 层（进程级全局作用域），挂它会**冲突报错**。
profile 层的正确机制就是 `system-prompt` 行的 `personaPrefix` / `personaSuffix` 配置——
**官方 `@deepseek-ai/dsh-headless` 自己就是这么做的**。本 profile 用的是这条路。

> 若将来要的是「每会话不同人格」，那应该做成 agent preset 而不是 profile。

### 关于工具白名单：没有现成插件，用「按行关停」实现

核实结论（2026-09-20）：`@deepseek-ai/dsh-tools` **没有** allow/deny 列表配置项；
`node_modules/@deepseek-ai/` 下也不存在 `dsh-tool-filter` / `dsh-tool-policy` 之类的过滤插件
（`grep -iE 'allow|deny|filter|policy'` 只命中 sandbox-policy / fs-observation-policy /
spill-policy / timeout-policy 等无关行）。

运行时的唯一细粒度扩展点是 `tools/pre-execute` 这个 waterfall 事件（allow / deny / ask 三态），
但用它就得**自己写一个插件包**——超出「profile 素材」范围。所以这里走
**profile 层唯一真实可用的手段：把不在白名单里的 `tool-*` 行 `disabled: true`**，
并把真实边界交给服务侧（见上「交易边界」）。

---

## 三、实测：工具白名单真的生效了

临时 DSH_HOME 真跑（`dsh --profile quant-headless "只列出你当前可用的工具名…"`，**退出码 0**），
模型报出的工具面：

* **Harness 本地工具（9）**：`read`、`write`、`edit`、`glob`、`grep`、`read_image`、`skill`、
  `todo_write`、`exit_plan_mode`
* **quantwb MCP 工具**：全部 `mcp__quantwb__*`（账户/行情/因子/研究/风控/推送等）。
  这次运行报出的是 **77** 个 —— 那是**桥接落地前**、且表面模式尚未引入时的记录；
  现在同一 `/mcp` 端点的条数取决于 `QUANT_MCP_SURFACE`：
  * 缺省 `discovery` → 只有 6 个 `mcp__quantwb__*`（4 件直连保留 + `list_tools` +
    `call_tool`），其余能力经 `call_tool` 间接可达（本 profile 的会话应当报出 6 个）；
  * `direct` → 全部工作台工具 + 全部 `v3_*` 桥接件（数量随 `/api/v3/*` 路由表**自动**变化，
    路由表是唯一事实来源，测试断言一一对应）。
  profile 侧不用改任何一行——切换只发生在平台服务进程（见「一之补」）。

**被成功关掉的**（组合树里 `disabled: true`，共 16 行；其中 2 行是 bundle 自己关的）：

| 行 | 关掉的工具面 | 关掉的理由（也写在 `cordis.patch.yml` 里） |
|---|---|---|
| `tool-bash` / `tool-pwsh` | 通用 shell | 可绕过工具面直接打任意 HTTP（含交易端点）；取数通道应是 MCP 工具面 |
| `tool-subagent` / `-fork` / `-control` / `-list-agents` | 委派 | 子代理是放大工具面的新会话面 |
| `tool-workflow` / `tool-ralph` | 编排 / 无监督循环 | 一次性任务里不需要 |
| `tool-goal` | 长跑自治目标 | 跨轮自治续跑 + 无人复核 |
| `tool-jobs` | 后台作业 | 上游被关后是死面 |
| `tool-web` / `web` / `web-search-deepseek` / `web-fetch-http` | 外网直连 | 新闻/公告走平台 MCP 工具面（带 source/as_of），避免同一事实两个不可审计来源 |

> 只关**模型可见的 `tool-*` 行**，刻意**不关** `subagent` / `subagent-spawn-in-process` /
> `subagent-fork-in-process` / `workflow-worker-thread` 这些**服务**行——关服务行会让其它行卡在
> `waiting`（"N row(s) did not activate"）。
>
> 注：headless 组合里**没有** `present`（`@deepseek-ai/dsh-tool-present` 未挂载），
> 所以该会话无法用 `present` 声明交付物；需要它就得额外 insert 该行。

---

## 四、临时 `DSH_HOME` 验证过程与结论（可复现）

| 步骤 | 命令 | 结果 |
|---|---|---|
| 1. 组合树 | `DSH_HOME=$TMP dsh --profile quant-headless --dump-config` | **退出码 0**，88 行；`system-prompt` 的 `personaPrefix` 已是量化分析师文本；16 行 `disabled`；`quant-platform-mcp` 已 insert |
| 2. 真实一次性运行 | `DSH_HOME=$TMP dsh --profile quant-headless "…"` | **退出码 0**；模型列出 9 个本地工具 + 77 个 MCP 工具；**无 bash / subagent / web / ralph / goal / jobs** → 白名单生效 |
| 3. 线上未被污染 | `ls ~/.dsh/profiles/node_modules \| wc -l` / `@helibeiqi` / `mtime` | **279 条、无 `@helibeiqi`、mtime 不变**；`dsh --profile headless --dump-config` 仍 **OK** |

两步都只在 `/tmp` 下的临时 `DSH_HOME` 里做；共享包目录用**顶层软链**而非整体软链，
落包只落在临时目录。第 3 步是事后核对，确认线上 `~/.dsh` **未被改动**。

---

## 五、为什么默认**不**写进线上 `~/.dsh/profiles/`

1. 本目录是**素材**，装到哪里是部署方的决定；直接写线上会让「仓库里有什么」与
   「机器上装了什么」不再一一对应，手改的 profile 也不会随仓库更新。
2. 线上 `~/.dsh/profiles/` 被官方 `headless`/`sdk`/`web` 共用，其中 `web` 挂着交易插件栈
   （`@bstester/dsh-trading-*`，`file:` 指向 `~/.dsh/trading-plugin-packages/*.tgz`）。
   在一个共享目录里加 profile 属于**改动生产运行面**，应由运维按变更流程执行。
3. 本 profile 的工具面**不依赖任何第三方插件包**（只 insert 官方
   `@deepseek-ai/dsh-mcp-client` 一行，包已在共享目录里），因此没有「装上就炸」的第三方风险；
   要改的只是角色提示词与工具白名单，属可回滚的小改动。
4. 需要常驻就按 §一 方式 B 拷进去；本 README 与 `deploy/monitoring/` 的记录足以复现。
