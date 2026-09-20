# `quant-headless` profile 素材（规格 §5.2 / §6）

量化专属一次性（headless）profile：**量化分析师 system prompt + 风控规则 + 工具白名单**。
本目录是**素材**，不是已安装的 profile——按下面「启用」一节拷进 `$DSH_HOME/profiles/` 才生效。
**本次没有写入线上 `~/.dsh/profiles/`**（理由见文末）。

> **2026-09-21 更新**：线上 `~/.dsh/profiles/quant-headless` **已按 §一「方式 B」装成常驻 profile
> 并真机跑通**——安装清单、既有 profile 未变的逐文件证据、`--dump-config` 关键行、线上唤醒的
> stdout/stderr/exit_code/duration、`/api/v3/headless/log` 新落库行原文、以及白名单「真拦一次」的
> 实测结果（**含一个未解决项**：默认 `discovery` 工具面下 MCP 写工具拦不住）全部在 **§六**。

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
  `@deepseek-ai/dsh-mcp-client` 行，指向 **`http://127.0.0.1:8397/mcp/ro`（只读面）**。
  这就是本 profile 与平台之间的**全部**交互：既有工作台工具 + `/api/v3/*` 路由桥接出的
  `v3_*` 工具，同一份服务端实现（见 `docs/v3-integration.md`）。**不挂任何第三方
  adapter/桥接包。** 平台服务没起时 `failOnStartupError: false` 让工具面缺席，
  由模型**如实报告数据源不可达**，而不是让会话崩在启动阶段。
  为什么是 `/mcp/ro` 而不是 `/mcp`：见下方「一之补二：只读面 `/mcp/ro`」。
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

切换**不影响** profile 侧任何东西：`cordis.patch.yml` 的 `quant-platform-mcp` 行不用改
（它连的是 `/mcp/ro`，恒为 discovery 形态、与此开关无关），两种模式下同一份服务端实现
（代理转发的是注册表里同一个函数对象）。

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

### 一之补二：只读面 `/mcp/ro`（为什么本 profile 连的是它，不是 `/mcp`）

**漏洞（2026-09-21 真机确认，见 §6.6 与 `docs/e2e-and-data-gaps.md` §22.6）**：缺省
`discovery` 面下，`/mcp` 的 `tools/list` 只有 6 件，写/交易工具（`trade_place` /
`research_tasks_claim` / `admin_prune_runs` / `v3_oms_sync` / …）**不以自己的名字出现在
工具面上**，唯一到达路径是转发器 `call_tool`。而本 profile 的白名单钩子（`hooks.json` 的
42 项 matcher）按**工具名**匹配——`mcp__quantwb__call_tool` 既不在名单里，钩子也**看不到
被转发的内层名字**。后果已经真实发生过：一次线上决策唤醒经 `call_tool` 误领了 2 条值班
队列任务。

**为什么不能把 `call_tool` 一禁了之**：官方 `headless` profile（`research_duty.sh` 值班链）
经全局挂载的 `/mcp` 用 `research_tasks_claim` / `research_tasks_report`——全局禁止会弄断
值班纪律的正规路径。

**修法（服务侧硬边界）**：平台新增第二个 MCP 端点 **`/mcp/ro`**（`platform/server/app.py`
挂载，实现同在 `platform/server/mcp_discovery.py`）：与 `/mcp` **同一实现、同一目录**，
仅两点不同——

1. `call_tool` 转发**之前**按注册表 annotations 判定（`readOnlyHint=true` 的内层工具 +
   4 件直连保留件才放行；**不维护第二份名单**）。写类与非只读标注的工具一律返回
   `mcp/denied-by-policy`（"该工具不是只读，只读面 /mcp/ro 不放行；需要写操作请由人在
   工作台完成"），拒绝发生在实参校验与 handler **之前**（测试断言 handler 零调用）；
2. `list_tools` 照常检索全目录（「看到」不等于「能调」），但非放行工具的卡片标注
   `roCallable=false` + 「只读面不可调用」，避免模型对着目录反复尝试注定被拒的调用。

这条边界在**服务侧**，不依赖任何 Harness 侧钩子——钩子看不到内层名也没关系，平台自己
按内层名拒绝。它与本 profile 的 42 项 hooks 白名单互为纵深（一层拦本地与已知写名，
一层在服务侧拦所有经代理的写转发）。

**代价（如实说明）**：基础面工具在 MCP 上**本来就不发布 readOnlyHint**（注册时不带
annotations），所以 `series` 这类**只读**工作台工具在 `/mcp/ro` 上也调不了（fail-closed：
分不清就拒绝）。决策取数走带只读标注的 `v3_*` 桥接件：K 线用 `v3_market`、资讯用
`v3_news`、公告用 `v3_events`、持仓/成交用 `v3_execution`、情绪用 `v3_sentiment` 等；
`list_tools` 的 `roCallable` 字段就是「这个面上能不能调」的权威判据。值班链用的官方
`headless` profile 仍走全局 `/mcp`，不受任何影响。

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

> §五 是**素材方**的默认立场（写于 2026-09-20）。2026-09-21 运维按 §一「方式 B」把它装成了常驻
> profile——那次动作与原始输出见 **§六**；本节保留原理由，不改写历史。

1. 本目录是**素材**，装到哪里是部署方的决定；直接写线上会让「仓库里有什么」与
   「机器上装了什么」不再一一对应，手改的 profile 也不会随仓库更新。
2. 线上 `~/.dsh/profiles/` 被官方 `headless`/`sdk`/`web` 共用，其中 `web` 挂着交易插件栈
   （`@bstester/dsh-trading-*`，`file:` 指向 `~/.dsh/trading-plugin-packages/*.tgz`）。
   在一个共享目录里加 profile 属于**改动生产运行面**，应由运维按变更流程执行。
3. 本 profile 的工具面**不依赖任何第三方插件包**（只 insert 官方
   `@deepseek-ai/dsh-mcp-client` 一行，包已在共享目录里），因此没有「装上就炸」的第三方风险；
   要改的只是角色提示词与工具白名单，属可回滚的小改动。
4. 需要常驻就按 §一 方式 B 拷进去；本 README 与 `deploy/monitoring/` 的记录足以复现。

---

## 六、线上安装与真机验证（2026-09-21 实测）

**装之前**：线上 `/api/v3/headless/log` 的 6 条记录**全部**是
`outcome=profile-missing`（`error.code=headless/profile-missing`）——触发循环在跑、唤醒全部落空，
记录是诚实的；`GET /api/v3/headless/schedule` 的 `profileWhitelist.verified=false`、`detail=null`。
**装之后**：同一条通道真跑通，`verified=true`，且**线上服务自己的触发循环**在 16:00:36Z 真的
唤醒了 3 个会话（见 6.5）。时间戳说明：这台机器 `date -u` 是 `2026-09-20T16:0xZ`（CST 2026-09-21 00:0x）。

### 6.1 安装动作（加法式；6 个文件）

```console
$ mkdir -p ~/.dsh/profiles/quant-headless
$ cp platform/install/quant-headless/{package.json,cordis.yml,cordis.patch.yml,pnpm-workspace.yaml,hooks.json,tool-whitelist.json} \
     ~/.dsh/profiles/quant-headless/
$ sha1sum ~/.dsh/profiles/quant-headless/*
2f4d87e37861e1f492a82872368c61d0cb663262  cordis.patch.yml
e63968eb42fef4ebee81f811f400da71ad9ba9e5  cordis.yml
098e89b85f2c011fbb621a3eddf1b8c453cf2994  hooks.json
033f1a62465901a18568b82a87e5585dfdc2aae8  package.json
18648b5a9842129ae17a6aae012e7f4436267d28  pnpm-workspace.yaml
d75675263c398b540817641f53116a6dee2654f0  tool-whitelist.json
```

六个文件的 sha1 与仓库素材**逐文件相同**（`diff` 无输出）。`hooks.json` / `tool-whitelist.json`
**必须**一起拷：`platform/server/v3_headless.py` 的 `verify_whitelist()` 在起进程**之前**要用它们，
缺了就是 `outcome=whitelist-unverified`（fail-closed）。

### 6.2 既有 profile 未被改动（安装前 / 安装后 / 全部验证之后 三次快照对比）

快照口径：`find <profile> \( -type f -o -type l \) -printf '%p\t%s\t%T@\n' | LC_ALL=C sort`
的**逐文件清单整体 sha1**（含 symbol link、含 size 与 mtime 纳秒），外加子目录 mtime 清单 sha1。

| 目录 | 文件数（前/后） | 逐文件清单 sha1（前 → 后） | 结论 |
|---|---|---|---|
| `profiles/headless` | 5 / 5 | `c3f47dec7d82960a13a84933cf4740ea89fe3101` → **相同** | 未变 |
| `profiles/sdk` | 4 / 4 | `107169d648ddf635cf70de6bd13e23f408f6d484` → **相同** | 未变 |
| `profiles/web` | 45 / 45 | `8b631f182c36fc2f59fe8244b05e473da77321ca` → **相同** | 未变 |
| `profiles/node_modules`（共享包） | 608 / 608 | `37f136a0a8ef332b4e3a89c39d2cdf22d06c467e` → **相同** | 未变 |
| `headless`/`sdk`/`web` 子目录 mtime 清单 | — | `77914c108d5499bba226092852768f7b3bdfaaf4` → **相同** | 未变 |

目录自身 mtime（`stat -c '%Y'`）也逐字相同：`headless 1789733566`、`sdk 1789811944`、
`web 1789718918`、`node_modules 1789813003`。共享包目录条目数仍是 **279**。

> 唯一新增的是 `profiles/quant-headless/`（新目录）与 `profiles/quant-sdk/`（见另一份 README）。
> `dsh` 首次 boot 新 profile 时会在**新目录内**自建 `.dsh-module-fallback/` 与 `node_modules/`
> （见 6.1 之后的 `find` 结果），不触碰共享目录。

### 6.3 `dsh --profile quant-headless --dump-config`

```console
$ dsh --profile quant-headless --dump-config > /tmp/dump.txt 2>/tmp/err.txt; echo "exit_code=$?"
exit_code=0
$ wc -l < /tmp/dump.txt
424
$ grep -c 'disabled: true' /tmp/dump.txt
16
```

* **`quant-headless-tool-deny` 行在**（`dump.txt:419-423`）：

  ```yaml
  - id: quant-headless-tool-deny
    name: '@deepseek-ai/dsh-hooks-claude-code'
    config:
      configPath: >-
        /home/penn/workspace/dsh-trading-agents/platform/install/quant-headless/hooks.json
  ```

  （`configPath` 指向仓库里那份 `hooks.json`——**换机器要改这一行**，或让校验回退到
  profile 目录下的同名文件；`verify_whitelist()` 两种都认，见其 `candidate` 回退逻辑。）
* **16 行 `disabled: true`**，id 逐个是：
  `hmr, tool-bash, tool-pwsh, tool-jobs, skill-badge, tool-subagent-control,
  tool-subagent-list-agents, tool-subagent, tool-subagent-fork, tool-workflow, tool-goal,
  tool-ralph, web, web-search-deepseek, web-fetch-http, tool-web`
  （其中 `hmr` / `skill-badge` 是 bundle 自己关的，其余 14 行来自本目录的 patch `disabledRows`）。
* **`quant-platform-mcp` 行在**（`dump.txt:411-418`）：`serverName: quantwb` /
  `transport: streamable-http` / `url: http://127.0.0.1:8397/mcp` / `failOnStartupError: false` /
  `toolCallTimeoutMs: 180000`。
* **人格前缀已生效**（`dump.txt:355`）：`personaPrefix: >-` 下是量化分析师文本 +
  「HARD RISK RULES（2% / 20% / 15%）」；`dump.txt:389` 仍是 `personaSuffix: Your working directory is {{cwd}}.`

### 6.4 真机唤醒（**线上 home + 线上台账**，平台自己的 Runner 代码路径）

用的是本仓库自己的探针（`HeadlessRunner.execute` → 同一张 `headless_log` 表），
`--dsh-home` 与 `--home` 都指向线上 `~/.dsh`：

```console
$ cd platform
$ ~/.dsh/trading-venv/bin/python -u -B tools/v3_headless_probe.py \
    --dsh-home "$HOME/.dsh" --home "$HOME/.dsh" --profile quant-headless \
    --prompt "只回复 OK，不要调用任何工具" --timeout 240
[probe] platform home = /home/penn/.dsh
[probe] DSH_HOME      = /home/penn/.dsh
[probe] dsh           = /home/penn/.npm/_npx/1e7f6d9597241db0/node_modules/.bin/dsh (PATH)
[probe] profile dir   = /home/penn/.dsh/profiles/quant-headless
[probe] whitelist     = True
[probe] ---- call record ----
[probe] outcome = 'completed'
[probe] success = 1
[probe] exit_code = 0
[probe] kill_reason = None
[probe] killed = 0
[probe] signal = None
[probe] duration_ms = 43712.415
[probe] tokens_estimate = 14
[probe] stdout = 'OK\n'
[probe] stderr = ''
[probe] error = None
[probe] streams_separated = 1
```

落库行（`headless_log`）关键字段（原文，`started_at` / `finished_at` 是真实时间）：

```json
{"outcome": "completed", "success": 1, "exit_code": 0, "killed": 0, "signal": null,
 "duration_ms": 43712.415, "duration_wall_ms": 43723.646,
 "stdout": "OK\n", "stderr": "", "stdout_chars": 3, "stderr_chars": 0,
 "streams_separated": 1, "tokens_estimate": 14, "tool_deny_count": 42,
 "profile": "quant-headless", "task_type": "manual", "trigger": "manual",
 "dsh_home": "/home/penn/.dsh", "dsh_bin_source": "PATH",
 "started_at": "2026-09-20T16:02:36.279034+00:00",
 "finished_at": "2026-09-20T16:03:20.002625+00:00",
 "error": null}
```

`exit 0` 只证明 Agent 轮次完成（`outcome=completed`），不证明外部效果——这条契约注记在
`contract_note` 里，一并落库。

### 6.5 线上服务**自己的**触发循环真跑通了（最强证据）

安装完成后，线上 8397 进程（`pid 305473`，`python -m server.run`，`DSH_HOME=/home/penn/.dsh`）
在 **16:00:36Z** 的 tick 上判出 `position_move` / `risk_breach` / `pre_rebalance_confirm`
三条 `status=fire`，投递了 3 个 ticket（**没有重启服务**，profile 是运行时发现的）。

`GET /api/v3/headless/log`（线上端点，安装后）：

| started_at | trigger | outcome | exit_code | duration_ms | stdout_chars |
|---|---|---|---|---|---|
| `2026-09-20T16:02:36.279034+00:00` | manual（6.4 的探针） | `completed` | 0 | 43712 | 3 |
| `2026-09-20T16:00:44.939559+00:00` | risk_breach | **`completed`** | **0** | 182071 | 8047 |
| `2026-09-20T16:00:45.248332+00:00` | pre_rebalance_confirm | `timeout` | −9 | 300151 | 0 |
| `2026-09-20T16:00:44.626702+00:00` | position_move | `timeout` | −9 | 300185 | 0 |
| `2026-09-20T15:00:18.39…` / `15:00:18.07…` / `15:00:17.41…` | pre_rebalance_confirm / risk_breach / position_move | `profile-missing` | — | 0 | — |
| `2026-09-20T14:41:50.79…` / `.49…` / `.15…` | 同上三条 | `profile-missing` | — | 0 | — |

* **`outcome` 不再是 `profile-missing`**：新记录是 `completed`（risk_breach）与 `timeout`。
* `timeout` 两条是**外部熔断真的生效**（`error={"code":"headless/timeout","message":"超时，已 kill（timeout_s=300.0）"}`，
  `signal=9`、`killed=1`、`stdout_chars=0`、`stderr_chars` 15796/17389——stderr 里是
  `dsh: reasoning:` 轨迹，说明会话**真的起来了、在思考**，只是没在 300 s 内收尾）。
  这是「通道已通、预算需要标定」的事实，**不是** profile 问题（与 15:00 那批 0 ms 的
  `profile-missing` 是两类不同事实）。
* 安装后线上 `GET /api/v3/headless/schedule` 的 `profileWhitelist`：

  ```json
  {"profile": "quant-headless", "profileDir": "/home/penn/.dsh/profiles/quant-headless",
   "verified": true, "denyToolCount": 42,
   "detail": {"ok": true, "error": null, "missingDisabledRows": [], "uncoveredTools": [],
              "hooksPath": "/home/penn/workspace/dsh-trading-agents/platform/install/quant-headless/hooks.json"}}
  ```

  （安装前同字段是 `"verified": false, "detail": null`。）

### 6.6 白名单「真拦一次」的实测结果：**本地工具拦住了，MCP 写工具没拦住** ⚠️

**（a）本地工具面确实被收窄了。** 真机会话自述的本地工具只有 9 件：
`edit, exit_plan_mode, glob, grep, read, read_image, skill, todo_write, write`
——**没有** `bash` / `subagent*` / `web*` / `ralph` / `goal` / `jobs`。这与 6.3 的 16 行
`disabled` 一致。

**（b）让模型尝试写/交易工具（真机，`trade_place`，载荷为空 ⇒ 必然过不了必填校验 ⇒ 零副作用）：**

```console
$ cd platform && ~/.dsh/trading-venv/bin/python -u -B tools/v3_headless_probe.py \
    --dsh-home "$HOME/.dsh" --home /tmp/v3-deny-probe-home --profile quant-headless \
    --prompt "<逐步执行：list_tools(trade_place) → call_tool(trade_place, {}) → 报告有无钩子字样>" \
    --timeout 240
[probe] whitelist     = True
[probe] outcome = 'completed'   exit_code = 0   duration_ms = 46052.974
```

模型逐字给出的两条原始返回（**原文**）：

```json
// 1) list_tools(keyword="trade_place", limit=10) → 写工具目录里确实有它
{"ok":true,"total":1,"count":1,"catalog_total":125,
 "cards":[{"name":"trade_place","purpose":"受约束下单（临时订单）：过完整闸门链（模式文件→风控 8 规则→kill→业务确认）后提交券商",
           "required":["symbol","side","qty"],"domain":"execution","prefix":"trade_","readOnly":null,"source":"workbench"}]}

// 2) call_tool(name="trade_place", arguments={})
{"ok":false,"error":{"code":"mcp/bad-arguments",
  "message":"call_tool('trade_place') 的实参不合法：缺必填参数 ['qty', 'side', 'symbol']。…"}}
```

模型的第 4 步结论（原文节选）：

> 对上面两段原始返回逐字检索：**无**。（……）我这条执行链本身**没有任何 hook / PreToolUse 层**……

→ 也就是说：这次调用**到达了平台的 `trade_place` 实现**（被业务参数校验拒绝），
**完全没有**出现钩子信息。`trade_place` 没有被下单（空载荷连闸门链都没进），
但**拦截来自服务侧参数校验，不是来自白名单钩子**。

**（c）钩子机制本身是活的**（诊断用 overlay，**不修改**已安装 profile）。
把 deny 行的 `configPath` 临时指向一份 matcher 改成 `mcp__quantwb__call_tool` 的副本，
再让模型调一次**只读**的 `call_tool(name="snapshot")`：

```console
$ dsh --profile quant-headless --patch /tmp/deny-overlay.yml \
      '请调用 call_tool：name="snapshot"，arguments={}。把原始返回逐字贴出…'
```

模型贴出的工具返回（**钩子 stderr 原文**，由 `exit 2` 的 stderr 转成给模型的理由）：

```
Error: headless 白名单：写/交易工具已从工具面移除（额度：只读研究）。该动作只能由人在工作台 Web 确认后执行。
```

→ 钩子行、`@deepseek-ai/dsh-hooks-claude-code` 包、`exit 2 = 阻断` 的语义**都工作正常**。

**（d）根因：默认 `discovery` 工具面下，写工具的名字根本不在钩子能看到的名单里。**

* 线上 `/mcp` 的 `tools/list` **实测只有 6 件**：`snapshot`、`admin_status`、`v3_gateway`、
  `v3_tools`、`list_tools`、`call_tool`（缺省 `QUANT_MCP_SURFACE=discovery`，见「一之补」）。
* 所有写工具（含 `trade_place`、`research_tasks_claim`）都只能经 **`mcp__quantwb__call_tool`**
  转发（`platform/server/mcp_discovery.py`：转发的是注册表里**同一个函数对象**，服务侧**不按名拒绝**）。
* `hooks.json` 的 matcher 列的是**工具名**（含 `mcp__quantwb__trade_place` 等 42 个写工具名），
  **没有** `mcp__quantwb__call_tool`；`dsh-tools` 的 `tools/pre-execute` 只在
  `createExecution()` 返回 `ready`（工具名已注册、实参合法）时才跑（`dsh-tools/lib/index.js:3106`），
  所以这个代理入口既不会被逐名拒绝，也**看不到**被转发的内层名字。

**（e）这个缺口有真实后果，不是理论风险。** 16:00:44Z 那次线上 `risk_review` 唤醒的 stderr 里写着
「I've claimed a research task for HK factor patrol…」，平台库（`~/.dsh/trading-data/trading.sqlite`
的 `research_tasks` 表）里对应地出现了**真的被领走**的任务：

```
RT-20260918-HK-factor_patrol-EAFE8E  status=running  started_at=2026-09-21 00:01:29  timeouts=1
RT-20260918-SH-mining_round-AF100F   status=running  started_at=2026-09-21 00:01:36  attempts=2  timeouts=2
```

（`research-duty.timer` 上次触发是 2026-09-18 20:02、下次 2026-09-21 19:20，**不是它**；
`claim_task` 的调用者只有 MCP 工具与 `scripts/research_duty.sh` 两条路。）
装 profile **之前**这些唤醒全部死在 `profile-missing`、不会领任务；装上之后它们会——
即**这次安装把一个「白名单外写操作可达」的路径真正激活了**。

**（f）建议的修法（本轮未实施，属下一轮）**：把拒绝集下沉到**服务侧**——
在 `mcp_discovery.call_tool` 转发前用同一份事实源
（`v3_ops.WRITE_TOOLS` + `e2e_probe.WRITE_ENDPOINTS` + `mcp_tools.MCP_EXCLUDED_ENDPOINTS`）
按**内层 name** 判一次，命中就回 `mcp/denied-by-policy`（这样两种表面模式一致、profile 侧不用改）；
或退一步：在 `hooks.json` 的 matcher 里加上 `mcp__quantwb__call_tool`（**但那会把只读代理也一起关掉**，
代价明确，不推荐）。**不要**把现有白名单当成已经兜住了写工具。

> **→ 2026-09-21 已实施**（按更稳的形态）：新增只读面 **`/mcp/ro`**（第二个 MCP 端点，
> 全局 `/mcp` 保持现状、值班链不受影响），本 profile 的 `quant-platform-mcp` 已指向它。
> 判据用注册表 annotations（`readOnlyHint=true` + 直连保留件），比按写类名单枚举更稳
> （新增路由漏登记名单时它缺省被拒——fail-closed）；被拒返回 `mcp/denied-by-policy`、
> handler 零调用。原理与代价见「一之补二」；测试见
> `platform/tests/test_mcp_discovery.py` 的 `ReadonlySurfaceTests`。

### 6.7 排障（本轮真机踩到的）

| 现象 | 真实原因 | 处置 |
|---|---|---|
| `outcome=profile-missing`（`duration_ms=0`） | `$DSH_HOME/profiles/quant-headless/package.json` 不存在 | 按 §一 方式 B 拷贝；**不要**指望回退到没有白名单的 `headless` profile（有意不回退） |
| `outcome=whitelist-unverified` | profile 目录里缺 `hooks.json` / `tool-whitelist.json`，或 patch 里少了 `quant-headless-tool-deny` 行，或 matcher 覆盖不全 | 六个文件一起拷；`runner.state()["whitelist"]` 的 `missingDisabledRows` / `uncoveredTools` 会指出缺哪一项 |
| `verified=false` 且 `detail=null` | 校验在 profile 缺 `package.json` 时**提前返回**，白名单根本没查 | 先装 profile，再看 `verified` |
| 唤醒起来但 `outcome=timeout`（`exit -9`、`killed=1`） | 单次预算 300 s（`timeoutSeconds`）对「持仓/风控复盘」这类大提示词偏紧，3 并发下更容易超 | 调 `timeoutSeconds`（`<home>/v3-headless.json` 或环境变量）；先看 stderr 里有没有 `dsh: reasoning:`——有就说明会话真的在跑，不是配置问题 |
| `dsh: warning: … declares no dsh.bundle` | `quant-tool-whitelist` 之类插件**不是** bundle，不该进 `dsh.profile.bundles` | 这是正常提示（`dsh plugin add` 会打），不是错误 |
| 改了 profile 的 patch 但行为没变 | 线上**已启动**的服务进程按运行时发现 profile，目录级改动立刻生效；但**平台侧配置**（如 `QUANT_MCP_SURFACE`）要重启服务才生效 | 分开看这两件事；本轮**没有重启 8397** |

### 6.8 本节未解决项（如实登记）

1. **6.6 的 MCP 写工具缺口**：默认 `discovery` 面下 `call_tool` 可转发任意写工具，逐名拒绝钩子拦不住；
   已实测 `research_tasks_claim` 被线上唤醒真领走 2 条队列任务。**未修**（修法见 6.6(f)）。
   → **2026-09-21 已修复**：只读面 `/mcp/ro` 已实现并作为本 profile 的 MCP 入口（见「一之补二」）；
   服务重启后生效（`/mcp/ro` 是新端点，运行中的旧进程还没有它）。
2. **跑挂的两条唤醒留下的 `running` 任务**：`RT-20260918-HK-factor_patrol-EAFE8E` /
   `RT-20260918-SH-mining_round-AF100F` 会在 30 min 后被 `reclaim`（超时计数 +1，连续 3 次转 failed）。
   本轮**没有**代替它们回报（回报会改队列状态，且本轮只做通道验证）。
3. **`timeoutSeconds=300` 未标定**：两条 `timeout` 是工程默认值下的真实结果，需要按提示词体量标定。
4. **`skill-filesystem` 与 `hooks.json` 的绝对路径**指向 `$(pwd)` 所在的仓库；换机器/换 DSH_HOME
   必须改 `cordis.patch.yml` 的两处绝对路径（skill 目录 + `configPath`）。
