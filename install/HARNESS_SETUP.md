# 一键安装提示词（Harness 安装协调员）

> **用法**：把下面 ① 的提示词**整段**复制进一个新的 Harness 会话发送即可。提示词自包含：
> 安装手册、失败排查、安全边界都在这一段里，Harness 无需阅读本文件其余部分也能执行。
> ②③④ 是同一份手册的**人类对照版**（含预期输出与背景解释），供人查阅与维护，不是第二执行依据。

> **能力清单**：一键安装覆盖「拉代码 → 对话模式（preset 行 + `skills/`）→ 平台服务
> （venv/依赖/Web 构建/服务启动/验证）→ MCP 集成（quantwb 工作台 + 可选富途只读）」全栈，
> 并包含**富途官方 skills 参考集成（7 个，数据通道对接工作台，工具清单见
> `skills/futu-skills/README.md`）**；另有一条**可选：last30days 社媒研究技能**
> （上游近 30 天社媒/全网研究引擎，不随仓库分发，需单独运行
> `python3 scripts/install_last30days.py`，见 ② 手册第 3c 步）。

---


> **首启必做：三步自举数据（否则平台在跑但什么都没发生）**——2026-09-18 实测补正：
> 只跑 `watchlist-init` 在全新机器上**必然失败**（它依赖 `universe` 成分快照），且缺交易日历时
> 市场链会被判定「日历未同步 / 非交易日」整天跳过：
> ```bash
> ~/.dsh/trading-venv/bin/python -m trading_core universe --index SH.000300          # ① 成分快照
> ~/.dsh/trading-venv/bin/python -m trading_core watchlist-init --from-index SH.000300 --limit 20   # ② 关注池
> for m in SH HK US; do ~/.dsh/trading-venv/bin/python -m trading_core calendar --market "$m" \
>   --start 2026-01-01 --end 2027-12-31; done                                       # ③ 交易日历
> ```
> ③ 之后由服务内 `sync_calendar` 作业（18:50，自节流）自动维护，但它当天 18:50 才生效，
> 首启当天仍需手工跑一次。缺关注池时流程页显示「关注池未配置」提示。

## ① 提示词（整段粘贴给 Harness）

```text
你是安装协调员。请严格按本提示词内的《安装手册》与《失败排查》，把本仓库安装为
「独立量化工作台 + Harness 对话模式」。执行规则：
- 先执行第 0 步确认仓库根，之后所有相对路径都相对仓库根展开为绝对路径再执行；
- 逐步执行：每步执行命令 → 校验输出 → 成功才进入下一步；失败先按《失败排查》处置，
  处置后仍失败就停下，把失败步骤、完整输出与你的判断报告给用户；不许静默跳过任何步骤；
- 安装器每步会输出一行 JSON 摘要 {"step","ok","detail"}，原样转述给用户，不要改写；
- 全部完成后输出汇总表（列：步骤 | 结果 | 证据——命令关键输出或 JSON 摘要行），
  并重申《安全边界》四条，最后执行第 9 步的用户验证提示。

《安装手册》（环境要求：git；Node ≥22 + pnpm（`corepack enable pnpm`，安装器依赖它打包）；Python ≥3.13；可访问外网。第 1 步先核验，
任一缺失即报告用户并停止，不要代装系统级依赖。）

0. 确认仓库根：
   pwd
   REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
   [ -z "$REPO_ROOT" ] && REPO_ROOT="$HOME/.dsh/.agent-presets/dsh-trading-agents"
   生效的 Harness preset 是 "$HOME/.dsh/.agent-presets/dsh-trading-agents"；
   若它与 $REPO_ROOT 不同，后续编辑 agent.cordis.yml 一律以 preset 副本为准。
1. 核验环境：node -v（≥22）、pnpm -v（缺失则 corepack enable pnpm 或 npm i -g pnpm）、python3 -V（≥3.13）。
2. 获取/更新仓库：若 "$REPO_ROOT/.git" 已存在：
     git -C "$REPO_ROOT" pull --ff-only
   （若因「本地 preset 行被安装器翻转 + 远端同改 agent.cordis.yml」而失败：改用
    python3 scripts/install_plugins.py update --repo "$REPO_ROOT" --dsh-home "$HOME/.dsh"——
    它专为已挂载副本的更新设计；其余本地修改场景提示用户先提交或 stash，不得丢弃、不得 reset）；
   否则：
     git clone --depth 1 https://github.com/BSTester/dsh-trading-agents.git "$REPO_ROOT"
3. 安装插件并激活 preset 工具行：
   python3 scripts/install_plugins.py install --repo "$REPO_ROOT" --dsh-home "$HOME/.dsh"
3b. skills 技能就位：skills/ 随仓库分发（preset 的 customSkillDirs 直读仓库目录，
    含 skills/trading-agents 与 skills/futu-skills 七个富途技能），第 2 步的 clone/pull
    已把它们带到本地，无需单独安装；**已安装过对话模式**的旧副本在本步用
    python3 scripts/install_plugins.py update --repo "$REPO_ROOT" --dsh-home "$HOME/.dsh"
    更新（skills/ 与 preset 行一并刷新，幂等，可重复运行）。
    校验：ls "$REPO_ROOT/skills/futu-skills" 应列出 7 个技能目录与 README.md。
4. 平台安装器（幂等，重复运行安全）：
   python3 scripts/install_platform.py --home "$HOME/.dsh"
   依次输出 venv/deps/web/service/verify 五步 JSON 摘要。「venv 已存在报
   already-exists」「dist 比 src 新跳过重建」「8397 已有服务报 already-running」
   都是正常幂等行为，不是错误；某步 ok=false 时进程以非零码退出，按《失败排查》处理。
   --dry-run 只打印计划不执行，可用于先向用户展示将做什么。
5. 数据层链接（依赖第 4 步建好的 venv）：
   python3 scripts/install_plugins.py link --repo "$REPO_ROOT" --dsh-home "$HOME/.dsh"
6. 启用平台行：编辑 agent.cordis.yml：
   - 把「- id: quant-platform-mcp」一行的 disabled: true 改为 disabled: false；
   - 保持该行 toolCallTimeoutMs: 180000（trade_* 写工具的确认 TTL 为 120s，超时自动拒绝
     即 fail-closed；多出的 60s 给用户在 Web 确认卡片作答与子进程取数留余量）；
   - 确认「- id: fin-data」与「- id: trading-engine」两行为 disabled: false
     （仓库默认已是 false，确认即可）；
   - 不要动其他行：futu-keepalive 等保持原状。
6b. 技能可用提示：preset 的 customSkillDirs 直读仓库 skills/ 目录，此时
    futu-skills 的 7 个技能已可用（新建会话生效）；每个技能用哪些工具、缺口能力
    如何降级，工具清单见 skills/futu-skills/README.md。
6c. 值班队列的兜底纪律：仓库根 `AGENTS.md` 由 `@deepseek-ai/dsh-agent-instructions`
    在会话开始时自动加载（工程根 = 本仓库，以 `.git` 标记；**会话工作目录必须在本仓库内**
    才会加载）。它要求会话首次交互先消费研究任务队列积压（L3 兜底）。定时器
    （`install/research-duty.{service,timer}`，19:20 唤醒）是**主**路径，本纪律是**兜底**：
    没有任何 turn 的会话不会消费队列（任务不丢、只延迟）。
7. （备用）手动启动服务（若第 4 步已报 already-running 则跳过本步）：
   会话自动拉起已默认启用（preset 行 platform-autostart）：之后新建的会话会自动
   /healthz 检测并以分离进程拉起服务，通常无需手动；本步仅用于在当前安装会话里
   立即验证（供第 8 步 curl）：
   cd "$REPO_ROOT/platform" && "$HOME/.dsh/trading-venv/bin/python" -m server.run
   放在 Harness 的后台终端/pty 里保持进程存活。启动成功会打印单行 JSON
   （形如 {"ok":true,"service":"quant-platform","url":...,"tools":...}）。
   禁止用 python -m platform.server.run：标准库 platform 模块会遮蔽同名包导致失败。
8. 验证：curl -fsS http://127.0.0.1:8397/healthz
   期望 {"ok":true,"mode":"sim"或"live",...}。若 ~/.dsh/trading-platform.json 覆盖了
   端口，按配置端口访问。curl 不可用时可用
   "$HOME/.dsh/trading-venv/bin/python" -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8397/healthz').read().decode())"
9. 收尾：提示用户新建一个 Harness 会话（当前会话不会热加载第 6 步改的行），在新会话里
   确认工具面出现 mcp__quantwb__*（如 trading_status）。富途 token 可选：未配置时
   mcp__futu__* 不出现，不影响 quantwb；需要时运行
   "$HOME/.dsh/trading-venv/bin/python" scripts/futu_auth.py 完成授权。

《失败排查》（细则见仓库 docs/RUNBOOK.md「平台服务」节与场景 1-4）
- node -v 无输出或 <22：报告用户安装 Node ≥22 后重跑第 1 步（前端构建需要）。
- install_plugins.py 报 FileNotFoundError: pnpm：`corepack enable pnpm` 或 `npm i -g pnpm` 后重跑该步（安装器用 pnpm 打包内容寻址 tarball）。
- python3 -V <3.13：报告用户安装 Python ≥3.13 后重跑；安装器用当前解释器建 venv。
- venv 步失败（exit 非 0）：磁盘空间/权限问题居多；删除 "$HOME/.dsh/trading-venv" 后重跑第 4 步。
- deps 步失败：多为网络不通或 pip 源不可达；修复网络或配置镜像后重跑第 4 步
  （pip 幂等，已装的包会秒过）。
- web 步失败：npm install 多为网络/registry 问题；npm run build 失败把完整报错给用户。
- 8397 端口已被占用但 /healthz 不通：非本服务在占端口；用
  "$HOME/.dsh/trading-platform.json" 的 service.port 换端口后重启。
- /healthz 不通但服务进程在：看服务日志；解释器与 venv 不一致会先打一行告警 JSON
  （服务必须用 "$HOME/.dsh/trading-venv/bin/python" 启动）。
- 第 5 步 link 失败：行情与回测工具可能不可用，重跑一次；仍失败则报告用户
  （不阻塞其余步骤，但要在汇总表标注）。

《安全边界》（必须原样重申给用户）
1. 平台服务默认只绑定 127.0.0.1（loopback），不要改成对外地址；可选在
   ~/.dsh/trading-platform.json 配置 service.token（/api 与 /mcp 需 Bearer，/healthz 豁免）。
2. 模式切换：Harness 侧只能 live→sim；sim→live 只能在独立 Web 工作台由用户输入口令完成。
3. 富途写通道已收窄至工作台：Harness 进程内 sim_trade_*/trading_* 写类工具被策略一律拒绝
   （未知动词同样按写拒绝），实盘下单唯一路径是 Web 确认卡片批准后的 quantwb trade_* 或计划执行；
   futu-skills 的 7 个技能同样遵守本条——它们的交易动作也只走工作台确认卡片。
4. 不要为绕过工具策略而用 shell/HTTP 直接调券商接口；分析结论都应标注数据时间与不确定性。
```

---

## ② 安装手册正文（人类对照版）

### 环境要求

| 依赖 | 版本 | 用途 | 核验命令 |
|---|---|---|---|
| git | 任意近期版本 | 拉取/更新仓库 | `git --version` |
| Node.js | ≥ 22 | `platform/web` 前端 npm install + vite build | `node -v` |
| Python | ≥ 3.13 | 创建 `~/.dsh/trading-venv` 并运行平台服务 | `python3 -V` |
| 网络 | 可访问外网 | pip/npm 依赖与仓库克隆 | —— |

### 逐步命令与预期输出

> 路径均相对仓库根；`REPO_ROOT` 的确认与 preset 副本规则见提示词第 0 步。

| 步骤 | 命令 | 预期输出 | 幂等语义 |
|---|---|---|---|
| 2 获取/更新 | `git -C "$REPO_ROOT" pull --ff-only` 或 `git clone --depth 1 …` | 更新/克隆成功 | 已是最新则 `Already up to date.` |
| 3 插件安装 | `python3 scripts/install_plugins.py install --repo "$REPO_ROOT" --dsh-home "$HOME/.dsh"` | 安装 web profile 工作台 Host 与投研/数据插件，激活 preset 工具行 | 重复安装覆盖同版本包 |
| 3b skills 就位 | skills/ 随仓库分发（customSkillDirs 直读仓库，无需单独安装）；已安装过对话模式的旧副本跑 `python3 scripts/install_plugins.py update --repo "$REPO_ROOT" --dsh-home "$HOME/.dsh"` | `ls skills/futu-skills` 列出 7 个技能目录与 README.md | update = git pull + preset 行翻转回填，幂等，可重复运行 |
| 3c last30days（可选） | `python3 scripts/install_last30days.py` | 两行 JSON 摘要：`install`（或 `update`，detail 含 commit SHA）+ `verify`（`skills/last30days/SKILL.md` 存在） | 已存在 → `git pull --ff-only` 更新；`--remove` 删除目录；幂等可重复 |
| 4 平台安装器 | `python3 scripts/install_platform.py --home "$HOME/.dsh"` | 五行 JSON 摘要：`venv`/`deps`/`web`/`service`/`verify` | venv 已存在 → `already-exists`；dist 新于 src → 跳过重建；端口在跑 → `already-running` |
| 5 数据层链接 | `python3 scripts/install_plugins.py link --repo "$REPO_ROOT" --dsh-home "$HOME/.dsh"` | 把 `trading_core`/`trading_datasource` 写入 venv（.pth） | 每次合并 WP 分支后重新 link 一次 |
| 6 启用行 | 编辑 `agent.cordis.yml` | `quant-platform-mcp` 行 `disabled: false`；`fin-data`/`trading-engine` 确认 `disabled: false` | 只改 quant-platform-mcp 行的 disabled，`toolCallTimeoutMs: 180000` 保持不动（确认 TTL 120s + 作答余量）；`futu-keepalive` 不动 |
| 6b 技能可用 | 无命令（提示项） | futu-skills 的 7 个技能已可用（新建会话生效）；工具清单见 `skills/futu-skills/README.md` | 技能由 customSkillDirs 直读仓库目录，随 3b 的仓库副本更新 |
| 7 （备用）手动启动 | `cd platform && "$HOME/.dsh/trading-venv/bin/python" -m server.run`（后台） | 单行 JSON：`{"ok":true,"service":"quant-platform","url":…,"tools":…}` | 已在跑则跳过（第 4 步会报 `already-running`）；新建会话由 platform-autostart 自动拉起，通常无需手动 |
| 8 验证 | `curl -fsS http://127.0.0.1:8397/healthz` | `{"ok":true,"mode":"sim","scheduler":{…}}` | 只读探测 |
| 9 新会话验证 | 用户新建 Harness 会话 | 工具面出现 `mcp__quantwb__*`（如 `trading_status`） | 行加载发生在会话创建时 |

**last30days（第 3c 步，可选组件）**：安装器把上游
[last30days-skill](https://github.com/mvanhorn/last30days-skill)（MIT）clone 到
`~/.dsh/last30days-skill`，经 preset 的 customSkillDirs 挂载其 `skills/` 子目录（目录缺失时
该条目静默零技能，不影响其余能力；装完新建会话生效）。**信任边界要如实告知用户**：这是
第三方代码，会在本机运行（Python 脚本发起网络请求、读取其自身配置），安装前应征得用户
同意；仓库树不 vendor 上游代码，更新只走 `git pull --ff-only`，不要把本机凭据（浏览器
Cookie、富途 token 等）传给引擎。免密钥来源（Reddit、Hacker News、Polymarket、GitHub、
StockTwits）开箱即用；X/YouTube/TikTok/Instagram/Threads/Pinterest/LinkedIn/小红书/
Perplexity/Brave 等需按上游 README 配置密钥或会话，未配置时对应来源缺席而非报错。
组合方法论见 `skills/last30days-bridge/SKILL.md`（社媒信号只生成假设，验证走 quantwb
工作台数据，交易只经工作台确认卡片）。

**会话自动拉起**：preset 行 `platform-autostart` 已默认启用——会话启动自动 `/healthz`
检测，未启动则以分离进程拉起（日志 `~/.dsh/trading-platform-service.log`）；未安装时仅
日志提示，按本手册安装。安装器（`install_plugins.py` install/update 与
`install_platform.py` 的 service 步）会写仓库标记文件 `~/.dsh/trading-platform-repo`
供插件定位仓库；行为、日志与并发会话竞态说明见 `docs/RUNBOOK.md`「会话自动拉起」节。

平台安装器的分层跳过（按需组合）：

```bash
python3 scripts/install_platform.py --home ~/.dsh --dry-run              # 只打印计划，零副作用
python3 scripts/install_platform.py --home ~/.dsh --skip-venv            # venv 已建好，只补依赖
python3 scripts/install_platform.py --home ~/.dsh --skip-web             # 前端没改，跳过 npm
python3 scripts/install_platform.py --home ~/.dsh --skip-deps --skip-web # 已装完，只做服务提示+验证
python3 scripts/install_platform.py --home ~/.dsh --skip-all             # 干跑：只输出摘要，零副作用
```

每步一行 JSON 摘要 `{"step","ok","detail"}`，可被 Harness 逐行解析；任一步 `ok=false`
进程以退出码 1 结束；`verify` 的健康检查失败只出 `warning`，不阻塞安装。

服务配置样例（`~/.dsh/trading-platform.json`，可选；文件缺失即默认 loopback:8397）：

```json
{ "service": { "port": 8397, "host": "127.0.0.1", "token": null } }
```

---

## ③ 失败排查指引

先查 `docs/RUNBOOK.md`：**「平台服务（FastAPI 单进程，WP6）」节**（依赖安装/启动/配置/健康检查与冒烟/systemd unit 样例）与**场景 1-4**（daemon 崩溃、订单 unknown、对账 halt、token 过期）。速查：

| 症状 | 首要处置 |
|---|---|
| `node -v` 缺失或 < 22 | 安装 Node ≥ 22（并启用 pnpm：`corepack enable pnpm`，安装器依赖它） 后重跑（前端构建必需） |
| `python3 -V` < 3.13 | 安装 Python ≥ 3.13 后重跑安装器（用当前解释器建 venv） |
| `venv` 步失败 | 查磁盘/权限；必要时删除 `~/.dsh/trading-venv` 重建 |
| `deps` 步失败 | 网络/pip 源问题；修复后重跑（pip 幂等） |
| `web` 步失败 | `npm install` 查 registry；`build` 失败收集完整报错 |
| 8397 被占且 `/healthz` 不通 | 非本服务占用；`trading-platform.json` 换端口 |
| 服务在但 `/healthz` 不通 | 服务必须用 `~/.dsh/trading-venv/bin/python` 启动（解释器不一致会先打告警 JSON） |
| 富途工具批量 internal error | token 过期特征，见 RUNBOOK 场景 4；`scripts/futu_auth.py --refresh` 续期 |
| 数据依赖可疑 | `python3 scripts/verify-data-deps.py`（离线常量 + 真实通道逐项归因） |

---

## ④ 安全提示

- **loopback**：平台服务默认 `127.0.0.1:8397`，仅本机可访问；不要改为对外绑定。
- **token 可选**：`~/.dsh/trading-platform.json` 的 `service.token` 非空时，`/api/*` 与
  `/mcp` 要求 `Authorization: Bearer <token>`（`/healthz` 与静态前端豁免）。
- **live 切换在 Web**：Harness 侧（`quantwb switch_mode`）只接受 live→sim；
  sim→live 必须在独立 Web 工作台由用户输入口令完成。切模式不等于授权下单。
- **写通道收窄**：富途写类（`sim_trade_*`/`trading_*` 的下单/改单/撤单动词）在 Harness
  进程内被工具策略一律拒绝，且未知动词同样按写拒绝（fail-closed）；实盘下单唯一路径是
  **Web 确认卡片批准后的 `quantwb trade_*` 工具或计划执行（需用户打字确认）**。
  不要尝试用 shell/HTTP 绕过工具策略直连券商。
- **futu-skills 同规**：`skills/futu-skills/` 的 7 个富途技能只提供只读分析与工作台取数，
  它们的交易动作同样只走工作台确认卡片；任何技能里出现"直连富途下单"的指引都属异常。
