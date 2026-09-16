# 量化交易平台 · 实现计划总索引（WP1–WP5）

> 本文件是五个工作包计划的**唯一入口与全局约定**。各计划可独立执行，但必须遵守本文的全局约定。

## 一、工作包与依赖图

```
WP1 数据基座 ──► WP2 研究层 ──► WP4 调度与工作台 ──► WP5 运维与验收
      └─────────► WP3 执行闭环 ────────┘（WP3 依赖 WP1，可与 WP2 并行）
```

| 计划文件 | 范围 | 状态 |
|---|---|---|
| `2026-09-14-wp1-data-foundation.md` | core 包/PIT 存储/日历/同步/质量/公告日合并 | 计划就绪 |
| `2026-09-14-wp2-research.md` | 因子注册表/横截面打分/IC 检验/策略注册/组合回测/walk-forward | 计划就绪 |
| `2026-09-14-wp3-execution.md` | 风控硬拦截/计划冻结/OMS 状态机/券商适配/对账/TCA | 计划就绪 |
| `2026-09-14-wp4-scheduler-workbench.md` | daemon/指令队列/告警/工作台 4 RPC + 计划调度两页签/文案清理 | 计划就绪 |
| `2026-09-14-wp5-ops-acceptance.md` | 演练/恢复/文档修订/全量验收 | 计划就绪 |

每个 WP 的验收标准以规格 `docs/superpowers/specs/2026-09-14-quant-platform-design.md` §十 为准；live 准入永远按 P4 清单人工评估，任何 WP 都不自动升级实盘。

## 二、全局约定（所有计划强制）

### 2.1 UI 文案规范（设计稿 ≠ 系统页面）

**规则：系统页面文案只保留与实际功能相关的提示。设计要求、规范依据、实现原则一律写进代码注释或文档，不出现在页面上。**

页面文案白名单（三类，仅此三类）：
1. **数据事实**：来源、数据时间（as_of）、缓存/新鲜度、缺口与降级原因（如「A股实时行情不可用，显示最近收盘」）；
2. **操作反馈**：动作结果、失败原因、下一步动作（如「该单被规则5拦截：成交后单票市值 27.4% > 25%」）；
3. **安全与合规必需**：SIM/LIVE 模式徽章、live 口令输入与复核提示、一行免责声明。

禁止上页面（设计稿里出现、实现时必须剔除）：

| 设计稿元素 | 实现处理 |
|---|---|
| 「设计稿 · 示例数据」页脚/标注 | 删除——页面渲染真实数据 |
| 「trading-core 规格见 §6.2」等规格引用 | 删除——写入组件文件头注释 |
| 「宁可空也不显示假数据」等原则陈述 | 删除——写入模块 docstring |
| 「窄门的技术保证」等边界解说 | 删除——写入 risk.py / plan-execute handler 注释 |
| 「色板可翻转」「token 说明」 | 删除——写入主题常量注释 |
| 演示性提示（「模拟断点」「切换展示」） | 删除 |

审查方法：每个 UI 任务完成后 grep 页面渲染字符串，命中「规格|设计稿|示例|宁可|窄门|token」即违规。

### 2.2 数据可行性验证协议（每 WP 第一个任务）

1. 该 WP 依赖的每一个外部数据/工具，先经 **tools/list schema 读取或最小实调**锁定：工具名、必填参数、返回字段、失败语义；
2. 结论写入该计划的「依赖锁定表」（含探测日期），后续任务只允许引用表内字段；
3. 核验失败的依赖 → 回规格修订降级方案，**禁止在代码里猜字段名或硬编码猜测参数**；
4. 锁定表配一个离线常量锁定测试（`tests/test_core_wpN_locks.py`），上游 schema 变化时人工重跑核验脚本更新。

已完成的锁定（2026-09-14，证据见规格 §13 与各计划附录）：
WP1（§13.2 六项）、WP2（估值字段路径复用 `workbench/python/factors.py::valuation_values` 实测实现；中证800=`SH.000906` K线实测「CSI 800 Index」）、WP3（券商工具清单与下单参数 schema，见 WP3 任务 0）。

### 2.3 工程约定

- 测试：标准库 unittest，全部离线（网络类行为用注入假取数器/假 fetcher）；运行 `~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v`；
- 新表迁移：改 `store.py` 的 `_SCHEMA` + `SCHEMA_VERSION` 递增（WP2→2，WP3→3）；
- 取数唯一实现：一切富途调用经 `trading_datasource.futu_mcp.call_tool`，行情经 `market.load_bars`，禁止旁路；
- 提交：每任务至少一 commit，格式 `feat(core|workbench|...): 摘要`；
- 文案：界面简体中文；CLI 输出 JSON（ensure_ascii=False）。

---

## 三、执行顺序与验收门

1. WP1 → 验收（规格 §十 WP1 行）→ WP2、WP3 可并行；
2. WP3 的 sim 真实下单冒烟需要用户在场（P4 口径：对话逐笔确认）；
3. WP4 依赖 WP1+WP3；WP5 最后；
4. 每个计划文件末尾维护「验收记录」小节，验收证据（JSON 原文/测试输出）就地留档。

## WP6 独立服务化（已立项，2026-09-15 用户决策）
1. 工作台**一切能力**经 MCP 暴露给 Harness 操作（与面板完全对等，非子集）；
2. 工作台 UI 用 **Ant Design Pro** 重实现（脱离 Harness 面板宿主，独立 Web）；
3. Harness 定位不变：入口 = 决策与操作确认；固定信息收集由服务定时跑。
落地：6a mcp_server（工具面=工作台全功能清单）；6b Ant Design Pro 前端 + 服务 HTTP；
6c preset 行替换与审批回归。
规格：`docs/superpowers/specs/2026-09-15-wp6-standalone-service.md`（2026-09-15 产出，前端形态经用户确认为 Vite + antd5 + ProComponents；同日第二次决策把服务后端改为 **FastAPI（Python）单进程**，规格与计划已同步修订）。
实现计划：`docs/superpowers/plans/2026-09-15-wp6-standalone-service.md`（含补遗 A–E：FastAPI 架构变更与 Node 服务层退役）。
验收记录：**`docs/superpowers/plans/2026-09-15-wp6-standalone-service.md` 末节「WP6 验收记录」**（分支与提交清单、全量测试证据、规格 §六 关键验收对照、真实进程冒烟、人工会话回归留位、执行偏差与 live 准入）。

## WP7 独立量化平台（已立项，2026-09-16 用户决策）

合并 WP6 后重构为**完整独立版本**：工作台（FastAPI 单进程）自带调度与因子快照定时收集（吸收 daemon）、自带富途通道（账户/交易/行情的权威通道，写路径唯一：mode→风控→kill→业务确认）；Harness 只做大脑（研究/决策/分析），富途直连降级为只读研究通道（写类被 policy 拒绝并指引工作台）；提供一键安装提示词（`install/HARNESS_SETUP.md`）。
规格：`docs/superpowers/specs/2026-09-16-wp7-standalone-platform.md`；计划：`docs/superpowers/plans/2026-09-16-wp7-standalone-platform.md`。
验收记录：**`docs/superpowers/plans/2026-09-16-wp7-standalone-platform.md` 末节「WP7 验收记录」**（任务 1-6 提交清单、全量测试证据、规格 §四 验收对照表、已知限制、人工验收留位）。

## WP8 富途 OpenAPI 统一接入（已立项，2026-09-16 用户决策）

富途三种开放能力（OpenAPI REST+WS / 托管 MCP / SkillHub）中，**OpenAPI 是唯一全能力层**（行情与交易 WebSocket 推送、二次确认、8 种订单类型、多腿、加密货币、深度数据）。WP8 把 OpenAPI 统一接入工作台服务端（OAuth 2.1+PKCE / AppKey 双认证），quantwb 工具面完整覆盖官方交易链路；托管 MCP 降级为可选只读研究通道；SkillHub 仅作能力对照。规格：`docs/superpowers/specs/2026-09-16-wp8-futu-openapi-unification.md`；计划：`docs/superpowers/plans/2026-09-16-wp8-futu-openapi-unification.md`。
