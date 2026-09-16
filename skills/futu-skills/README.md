# futu-skills：富途官方 skills 参考集成（数据通道对接工作台）

本目录把富途官方 SkillHub 的 7 个 skills 参考集成进本仓库。**不是**安装 SkillHub 官方形态
（那套依赖 OpenD/本地网关，与单进程工作台冲突）：全部 7 个技能的**数据通道都对接本仓库
工作台（quantwb）**，工作台没有的数据用 `mcp__futu__*` 只读补充，写操作只走工作台
确认卡片。技能正文即提示词：Harness 加载后按各 SKILL.md 的工作流程执行。

## 技能总览 × 数据通道映射

| 技能 | 目录 | 工作台通道（现状） | 缺口与降级 |
|---|---|---|---|
| Futu API Skill（交易/持仓/行情） | `trading/SKILL.md`（`futu-trading`） | quantwb：`snapshot`/`series`/`instrument` + `trade_place`/`trade_modify`/`trade_cancel` + `account_positions`/`account_orders`/`account_funds` + `confirmation`/`plan`/`plan_execute` + Web 确认卡片（已交付） | live 写协议未接入（提交即被闸门拒绝），SKILL.md 如实标注 |
| 技术面异动 | `technical-alerts/SKILL.md`（`futu-technical-alerts`） | quantwb：`series`（K 线）+ `instrument`；指标由 skill 指引 Harness 自算（MACD/KDJ/RSI 公式写进 SKILL.md） | 无 |
| 资金面异动 | `capital-alerts/SKILL.md`（`futu-capital-alerts`） | WP8 规划 `flow_*`（capital-flow）**尚未交付** | 标注待 WP8 任务 5；当前降级 `mcp__futu__capital_flow` 只读 |
| 衍生品异动 | `derivatives-alerts/SKILL.md`（`futu-derivatives-alerts`） | WP8 规划 `deriv_*`（option-chain 等）**尚未交付** | 标注待 WP8 任务 5；当前降级 `mcp__futu__` 期权类只读 |
| 资讯搜索 | `news-search/SKILL.md`（`futu-news-search`） | 工作台无新闻端点 | 走 `fin_news`（首选）与 `mcp__futu__*` 只读；标的解析用 quantwb `instrument` |
| 情绪温度计 | `sentiment/SKILL.md`（`futu-sentiment`） | 工作台无情绪端点 | 走 `fin_sentiment`（首选）与 `mcp__futu__*` 只读；标的解析用 quantwb `instrument` |
| 个股解读 | `stock-digest/SKILL.md`（`futu-stock-digest`） | quantwb：`snapshot`/`series`/`factors`/`quality`/`instrument`/`events`（混合） | 新闻/情绪面同资讯与情绪技能的只读通道 |

## 数据通道纪律（每个 SKILL.md 都遵守）

1. **优先 quantwb 工具**：工作台有的数据一律走工作台（同一 handler 与 HTTP 面同源）。
2. **工作台没有的数据用 `mcp__futu__*` 只读并标注**：输出必须写明来源与数据时间；
   已知工具陷阱（空结果、必传参数、上下文炸弹）见 `docs/TOOL-LIMITS.md`。
3. **写操作一律 quantwb `trade_*` + Web 确认卡片**：模型发起、用户在独立 Web 批准
   （TTL 120 秒，超时按拒绝）；禁止经 `mcp__futu__` 或任何绕过工作台的方式下单——
   富途写类工具在本 Harness 内被工具策略一律拒绝。live 写通道未接入（当前仅 sim 可交易），
   各技能如实说明。

## 依赖说明

- **无需 OpenD、无需安装 SkillHub 官方包**：技能只是随仓库分发的 Markdown 提示词，
  由 preset 的 `customSkillDirs` 直读本目录加载（新建会话生效）。
- **对话模式（必需）**：preset 已含本目录；`mcp__quantwb__*` 需要 quant-platform-mcp 行
  启用且平台服务已启动（见 `install/HARNESS_SETUP.md` 第 6/7 步）。
- **fin-data 行（推荐）**：`fin_news`/`fin_sentiment` 原生工具来自 fin-data 插件，
  安装器默认启用；未安装时按各技能的降级路径走。
- **富途 token（可选）**：未配置时 `mcp__futu__*` 不出现，只读补充通道不可用，
  其余通道照常；需要时运行 `scripts/futu_auth.py` 完成授权。
- **更新**：skills 随仓库分发。已安装过对话模式的副本用
  `python3 scripts/install_plugins.py update --repo <仓库> --dsh-home ~/.dsh` 更新
  （git pull 会把 `skills/` 与 preset 行一并刷新，幂等）。
- 工具清单以各 SKILL.md 的「数据通道」段为准；本 README 的映射表是速查视图，
  两者由 `tests/test_wp8_skills.py` 锁定（引用的 quantwb 工具必须真实存在于平台工具面）。

## 免责声明

以上内容基于公开信息整理，不构成投资建议。所有技能输出均须附带该声明并标注数据时间；
交易动作一律由用户在工作台确认卡片亲自批准。
