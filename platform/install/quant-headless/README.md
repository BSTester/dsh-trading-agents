# `quant-headless` profile 素材（规格 §5.2 / §6）

量化专属一次性（headless）profile：**量化分析师 system prompt + 风控规则 + 工具白名单**。
本目录是**素材**，不是已安装的 profile——按下面「启用」一节拷进 `$DSH_HOME/profiles/` 才生效。
**本次没有写入线上 `~/.dsh/profiles/`**（理由见文末）。

| 文件 | 作用 |
|---|---|
| `package.json` | profile 元数据；`dsh.profile.bundles` 指向**真实存在**的包 |
| `cordis.patch.yml` | 用户 patch 层：角色 + 风控规则 + 工具白名单 + MCP 工具面 |
| `cordis.yml` | profile 根（空 `[]`，与官方 profile 一致；**不要**编辑它） |
| `pnpm-workspace.yaml` | `autoInstallPeers: false` —— **实测必需的**，缺了 `dsh plugin add` 必失败（见下） |
| `README.md` | 本文件 |

---

## 一、启用

### 方式 A：临时 DSH_HOME（不改线上，推荐先这样试）

```bash
# 1) 建临时 home，并让 profile 能解析到既有共享包（共享目录是扁平布局）
TMP=$(mktemp -d /tmp/dsh-quant-XXXX)
mkdir -p "$TMP/profiles/quant-headless" "$TMP/profiles/node_modules"
for e in ~/.dsh/profiles/node_modules/*; do ln -s "$e" "$TMP/profiles/node_modules/$(basename "$e")"; done

# 2) 拷入素材（只拷 profile 真正需要的 4 个文件；README.md 不用进 profile 目录）
cp platform/install/quant-headless/{package.json,cordis.yml,cordis.patch.yml,pnpm-workspace.yaml} \
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
cp platform/install/quant-headless/{package.json,cordis.yml,cordis.patch.yml,pnpm-workspace.yaml} \
   ~/.dsh/profiles/quant-headless/
dsh --profile quant-headless "…"
```

### 与 `platform/` 的衔接

* **取数通道**：`cordis.patch.yml` 末尾 insert 了 `@deepseek-ai/dsh-mcp-client` 行，指向
  `http://127.0.0.1:8397/mcp` —— 这就是平台服务（`platform/`）的工具面，与 HTTP
  `/api/wb/*` **同一个 handle、同一份实现**（77 个 `mcp__quantwb__*` 工具）。
  平台服务没起时 `failOnStartupError: false` 让工具面缺席，由模型**如实报告数据源不可达**，
  而不是让会话崩在启动阶段。
* **技能目录**：`skill-filesystem` 覆盖为仓库内
  `skills/` 与 `skills/futu-skills/`（绝对路径，换机器要改）。
* **交易边界**：本 profile 只挂 **MCP 客户端**行。写端点仍要求人工在 Web 确认
  （`store_access.request_confirmation`，TTL 120s 超时=拒绝，fail-closed）；
  `confirm-decide` 与设置页两端点**有意不进 MCP 工具面**，模型无法自批实盘单、无法自改凭据；
  sim→live 必须由人在 Web 输口令。**白名单是纵深的一层，不是唯一一层，也不该被当成「所以可以放心」。**
* **监控**：`platform/deploy/monitoring/` 的告警覆盖工作台与富途限流；
  本 profile 的 `quantwb` 工具面调用会计入 `/metrics` 的 `quantwb_mcp_calls_total`。

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
* **quantwb MCP 工具（77）**：全部 `mcp__quantwb__*`（账户/行情/因子/研究/风控/推送等）

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

## 四、`@helibeiqi/dsh-cordis-universal-adapter` 可用性：**存在，但装不上/用不了**（三段真实错误）

`FR-GATEWAY-001` 点名了这个包。它在 npm 上**确实存在**：

```console
$ npm view @helibeiqi/dsh-cordis-universal-adapter
@helibeiqi/dsh-cordis-universal-adapter@0.1.0 | MIT | deps: 3 | versions: 1
Universal bridge adapter for DeepSeek Harness: consume external MCP servers & Agent Plugins 1.0,
and expose DSH native tools back as an MCP server.
published 4 weeks ago by helibeiqi <18710073857@163.com>

$ npm view @helibeiqi/dsh-cordis-universal-adapter peerDependencies
{ '@deepseek-ai/cordis': '>=0.1.0', '@deepseek-ai/dsh-tools': '>=0.1.0' }
```

它的定位是 **Host 组合层双向桥**（吃外部 MCP 服务器 / Agent Plugins 1.0；把 `ctx.tools`
反向导出为 MCP 服务器），`dsh.bundle.patch` 指向自带的 `cordis.patch.yml`（默认安全空载：
`servers: []`、`export.enabled: false`、`router.enabled: false`）。

**在隔离的临时 DSH_HOME 里实测（未碰线上 `~/.dsh`），连续踩到三个真实故障：**

### 故障 1：缺 `pnpm-workspace.yaml` 时安装直接失败

```console
$ DSH_HOME=$TMP dsh plugin --profile quant-headless add @helibeiqi/dsh-cordis-universal-adapter
ERR_PNPM_NO_MATCHING_VERSION  No matching version found for
  @deepseek-ai/dsh-tools@>=0.1.0 while fetching it from https://registry.npmjs.org/

The latest release of @deepseek-ai/dsh-tools is "0.0.1-rc.1".
Other releases are:
  * next: 0.1.5-rc.2
  * alpha: 0.1.6-alpha.2
```

原因：`dsh plugin add` 是把参数转发给 pnpm 在 profile 目录里跑；pnpm 默认
`autoInstallPeers: true`，会把 adapter 的 peerDependency 去 registry 装一遍。而
`@deepseek-ai/dsh-tools` 的 npm **latest 标签停在 0.0.x-rc**，不满足 `>=0.1.0`
（满足的是 **next** 标签 0.1.5-rc.2）。官方三个 profile 都带
`pnpm-workspace.yaml` 的 `autoInstallPeers: false` 正是为了这个 ——
**本目录已补上该文件**（这也是本轮从这次失败里学到的）。

### 故障 2：补上后能装，但 profile **直接起不来**

```console
$ … add @helibeiqi/dsh-cordis-universal-adapter      # 安装成功
dependencies:
+ @helibeiqi/dsh-cordis-universal-adapter ^0.1.0
Packages: +15 … Done in 14.3s

$ DSH_HOME=$TMP dsh --profile quant-headless "hi"
Error: dsh: plugin tree failed to load: failed to apply loader entry include (cordis:include):
  failed to import loader entry dsh-cordis-universal-adapter (dsh-cordis-universal-adapter):
  Cannot find package 'dsh-cordis-universal-adapter' imported from …/profiles/quant-headless/
Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'dsh-cordis-universal-adapter'
```

原因：`dsh plugin add` 会把包加进 `dsh.profile.bundles`，于是它自带的 `cordis.patch.yml`
被当作 bundle patch 应用；而那份 patch 里的行写的是**未加 scope** 的包名：

```yaml
- insert:
    - id: dsh-cordis-universal-adapter
      name: 'dsh-cordis-universal-adapter'      # ← 实际安装的是 @helibeiqi/dsh-cordis-universal-adapter
```

**装上即把 profile 搞成不可启动**（这不是配置问题，是包自身 patch 与 npm 包名不一致）。
另外实测：**用户层 patch 改不掉已存在行的 `name`**（只有 `config`/`disabled` 会被覆盖）——
所以「按 id 覆盖 name」这条修法**无效**，我试过，组合树里 `name` 仍是未加 scope 的那个。

### 故障 3：按它 README 的替代路子手动 insert，仍然导入失败

它 README 写「也可将同样的条目手动合并进 profile 的 `cordis.patch.yml`（用户层）使用」。
照做（把包从 `bundles` 摘掉、在用户层 `insert` 显式写 scoped 包名）后，**行名解析通过了**，
但模块导入时炸在 API 不兼容上：

```console
$ DSH_HOME=$TMP dsh --profile quant-headless --patch /tmp/adapter-manual.yml "…"
Error: dsh: plugin tree failed to load: failed to apply loader entry include (cordis:include):
  failed to import loader entry dsh-cordis-universal-adapter (@helibeiqi/dsh-cordis-universal-adapter):
  The requested module '@deepseek-ai/dsh-llm' does not provide an export named 'CallId'
```

逐字核实：

```console
$ node -e "console.log(require('…/@deepseek-ai/dsh-llm/package.json').version)"
0.1.5-rc.2

$ node --input-type=module -e "import * as llm from '…/@deepseek-ai/dsh-llm/lib/index.js';
    console.log(Object.keys(llm).filter(k => /CallId/.test(k)))"
[ 'ToolCallId' ]            # 只有 ToolCallId，没有 CallId

$ head -1 /tmp/adapter-probe/package/lib/mcp-server.js
import { CallId } from '@deepseek-ai/dsh-llm';         # ← adapter 用的是旧名
```

作者自述「**已在 dsh 0.1.0-rc.6 实测通过**」，而本部署是 **0.1.5-rc.2**：
`@deepseek-ai/dsh-llm` 已把品牌构造子 `CallId` 改名为 `ToolCallId`。
adapter 的 `engines` 只约束 `node >=22.19`，**没有约束 dsh 版本区间**，所以这个破坏性变更
没有任何声明层面的拦截。

### 结论与替代方案（**平台侧做法是本轮真正验证过的那条**）

**结论：`@helibeiqi/dsh-cordis-universal-adapter@0.1.0` 在本部署（dsh 0.1.5-rc.2）不可用。**
三个故障都是可复现的真实错误，不是配置疏忽：装不上（peer 版本）→ 装上起不来（行名未加 scope）
→ 手动修好行名后仍导入失败（`CallId` → `ToolCallId` 破坏性改名）。
要用它需要包作者发一版对齐 dsh 0.1.5-rc.2 的适配（至少：patch 行名带 scope、改用 `ToolCallId`、
声明 dsh 版本区间）。

**替代方案 = 平台已经用的那条路，而且本轮实测通过**：平台侧把工具面用
**MCP 服务器 + `/mcp`（`/api/v3/mcp` 是同一份）** 暴露出来，Harness 侧用
**`@deepseek-ai/dsh-mcp-client` 行**挂载。本 profile 就是这么接的，实测拿到 **77 个
`mcp__quantwb__*` 工具**、会话正常跑完（退出码 0）。

也就是说：adapter 只是「Harness 侧挂载方式的另一种选择」，**不是**接入平台工具面的必要条件；
`dsh-mcp-client` 这条路已经在用、已经在跑，不依赖第三方包的版本对齐。

---

## 五、临时 `DSH_HOME` 验证过程与结论（可复现）

| 步骤 | 命令 | 结果 |
|---|---|---|
| 1. 组合树 | `DSH_HOME=$TMP dsh --profile quant-headless --dump-config` | **退出码 0**，88 行；`system-prompt` 的 `personaPrefix` 已是量化分析师文本；16 行 `disabled`；`quant-platform-mcp` 已 insert |
| 2. 真实一次性运行 | `DSH_HOME=$TMP dsh --profile quant-headless "…"` | **退出码 0**；模型列出 9 个本地工具 + 77 个 MCP 工具；**无 bash / subagent / web / ralph / goal / jobs** → 白名单生效 |
| 3. adapter 安装（无 pnpm-workspace.yaml） | `dsh plugin add @helibeiqi/…` | 失败：`ERR_PNPM_NO_MATCHING_VERSION`（故障 1） |
| 4. adapter 安装（有 pnpm-workspace.yaml） | 同上 | 安装成功（+15 包），但 boot 失败（故障 2） |
| 5. adapter 手动 insert 修正行名 | `--patch adapter-manual.yml` | 行名解析通过，导入失败：`CallId` 不存在（故障 3） |
| 6. 线上未被污染 | `ls ~/.dsh/profiles/node_modules \| wc -l` / `@helibeiqi` / `mtime` | **279 条、无 `@helibeiqi`、mtime 不变**；`dsh --profile headless --dump-config` 仍 **OK** |

步骤 1–5 全部在 `/tmp` 下的临时 `DSH_HOME` 里做；共享包目录用**顶层软链**而非整体软链，
必要时落包只落在临时目录。步骤 6 是事后核对，确认线上 `~/.dsh` **未被改动**。

---

## 六、为什么默认**不**写进线上 `~/.dsh/profiles/`

1. 本目录是**素材**，装到哪里是部署方的决定；直接写线上会让「仓库里有什么」与
   「机器上装了什么」不再一一对应，手改的 profile 也不会随仓库更新。
2. 线上 `~/.dsh/profiles/` 被官方 `headless`/`sdk`/`web` 共用，其中 `web` 挂着交易插件栈
   （`@bstester/dsh-trading-*`，`file:` 指向 `~/.dsh/trading-plugin-packages/*.tgz`）。
   在一个共享目录里加 profile 属于**改动生产运行面**，应由运维按变更流程执行。
3. 本轮还实测到：**adapter 一旦加进 `dsh.profile.bundles` 会让 profile 不可启动**（§四故障 2）。
   这类「装上就炸」的风险不该先落在线上。
4. 需要常驻就按 §一 方式 B 拷进去；本 README 与 `deploy/monitoring/` 的记录足以复现。
