# futu-skills：富途官方 skills 参考集成（数据通道对接工作台）

本目录把富途官方 SkillHub 的 7 个 skills 参考集成进本仓库。**不是**安装 SkillHub 官方形态
（那套依赖 OpenD/本地网关，与单进程工作台冲突）：资金面、衍生品与实时报价由工作台
**实时直通富途**（quantwb `rt_quote`/`rt_order_book`/`capital_flow` 家族/`option_*` 家族，
2026-09 交付），技能不再直连富途取这些数据；新闻/情绪走 Harness 只读例外（原因见下表，
有据可查）；写操作只走工作台确认卡片。技能正文即提示词：Harness 加载后按各 SKILL.md
的工作流程执行。

## 技能总览 × 数据通道映射

| 技能 | 目录 | 工作台通道（现状） | 缺口与说明 |
|---|---|---|---|
| Futu API Skill（交易/持仓/行情） | `trading/SKILL.md`（`futu-trading`） | quantwb：`snapshot`/`series`/`instrument` + `trade_place`/`trade_modify`/`trade_cancel` + `account_positions`/`account_orders`/`account_funds` + `confirmation`/`plan`/`plan_execute` + Web 确认卡片（已交付） | live 写协议未接入（提交即被闸门拒绝），SKILL.md 如实标注 |
| 技术面异动 | `technical-alerts/SKILL.md`（`futu-technical-alerts`） | quantwb：`series`（K 线）+ `instrument`；指标由 skill 指引 Harness 自算（MACD/KDJ/RSI 公式写进 SKILL.md） | 无 |
| 资金面异动 | `capital-alerts/SKILL.md`（`futu-capital-alerts`） | quantwb：`capital_flow`/`capital_flow_history`/`capital_distribution`（**工作台实时直通富途**，已交付；A 股分钟级资金流实测可用） | 无（通道失败如实报错 `trading/futu-*`，不降级直连富途） |
| 衍生品异动 | `derivatives-alerts/SKILL.md`（`futu-derivatives-alerts`） | quantwb：`option_expiration`/`option_chain`/`option_screen`（**工作台实时直通富途**，已交付；option_screen 的 field_filter 必填非空） | 无（通道失败如实报错 `trading/futu-*`，不降级直连富途） |
| 资讯搜索 | `news-search/SKILL.md`（`futu-news-search`） | `fin_news` 主通道 + 公开网络兜底 | **保留只读例外的原因**：富途上游 `quote_news_search` 实测恒空（官方通道已知问题，见 docs/TOOL-LIMITS.md）；标的解析用 quantwb `instrument` |
| 情绪温度计 | `sentiment/SKILL.md`（`futu-sentiment`） | `fin_sentiment`（X + 千股千评 + Reddit） | **保留只读例外的原因**：社区情绪无托管工具（富途侧无对应托管能力）；标的解析用 quantwb `instrument` |
| 个股解读 | `stock-digest/SKILL.md`（`futu-stock-digest`） | quantwb：`snapshot`/`series`/`factors`/`quality`/`instrument`/`events`（混合） | 新闻/情绪面同资讯与情绪技能的 fin_news/fin_sentiment 通道 |

## 数据通道纪律（每个 SKILL.md 都遵守）

1. **优先 quantwb 工具**：工作台有的数据一律走工作台（同一 handler 与 HTTP 面同源；
   资金面/衍生品/实时报价由服务端实时经富途通道获取，skills 不回退直连富途）。
2. **工作台没有的数据按只读例外处理并标注**：仅新闻（富途上游恒空）与情绪（无托管工具）
   两类走 `fin_news`/`fin_sentiment`；输出必须写明来源与数据时间；
   已知工具陷阱（空结果、必传参数、上下文炸弹）见 `docs/TOOL-LIMITS.md`。
3. **写操作一律 quantwb `trade_*` + Web 确认卡片**：模型发起、用户在独立 Web 批准
   （TTL 120 秒，超时按拒绝）；禁止经富途侧任何工具或绕过工作台的方式下单——
   富途写类工具在本 Harness 内被工具策略一律拒绝。live 写通道未接入（当前仅 sim 可交易），
   各技能如实说明。

## 依赖说明

- **无需 OpenD、无需安装 SkillHub 官方包**：技能只是随仓库分发的 Markdown 提示词，
  由 preset 的 `customSkillDirs` 直读本目录加载（新建会话生效）。
- **对话模式（必需）**：preset 已含本目录；`mcp__quantwb__*` 需要 quant-platform-mcp 行
  启用且平台服务已启动（见 `install/HARNESS_SETUP.md` 第 6/7 步）。
- **fin-data 行（推荐）**：`fin_news`/`fin_sentiment` 原生工具来自 fin-data 插件，
  安装器默认启用；未安装时新闻/情绪技能的兜底路径见各自 SKILL.md。
- **富途 token（资金面/衍生品/实时报价必需）**：实时直通在服务端取数，token 缺失/过期时
  相关调用返回 `trading/futu-unavailable` 并指向授权入口；需要时运行
  `scripts/futu_auth.py` 完成安装/授权。未配置时其余通道（K 线/因子/交易闸门等）照常。
- **更新**：skills 随仓库分发。已安装过对话模式的副本用
  `python3 scripts/install_plugins.py update --repo <仓库> --dsh-home ~/.dsh` 更新
  （git pull 会把 `skills/` 与 preset 行一并刷新，幂等）。
- 工具清单以各 SKILL.md 的「数据通道」段为准；本 README 的映射表是速查视图，
  两者由 `tests/test_wp8_skills.py` 锁定（引用的 quantwb 工具必须真实存在于平台工具面）。

## 免责声明

以上内容基于公开信息整理，不构成投资建议。所有技能输出均须附带该声明并标注数据时间；
交易动作一律由用户在工作台确认卡片亲自批准。
