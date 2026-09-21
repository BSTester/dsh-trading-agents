# 第四轮交付总结与差异性说明（策略层 + 因子数据侧）

> 日期：2026-09-21 · 基线：`8821595` · 尺子：`docs/v3-spec.md`（FR-STRAT-001/002/003、FR-DATA-003）
> 本文回答两个问题：**这轮补了什么、与设计稿还差什么**。逐条对齐账目见 `docs/v3-capability-alignment.md`。

## 一、交付总览（全部真机验证）

| 任务 | 状态 | 交付物 | 真机证据 |
|---|---|---|---|
| 事件驱动策略 | ✅ 完成 | `v3_strategies.py` → `GET /api/v3/strategies/event-study` | 线上 200；事件带可核验时点，PIT 经 `data.cache` |
| 统计套利策略 | ✅ 完成 | 同文件 → `GET /api/v3/strategies/stat-arb` | 线上 200；OLS 对冲 + ADF（MacKinnon 近似 p，标注 `impl`）+ z-score 双腿 |
| NLP 事件识别 | ✅ 完成 | `v3_nlp.py` `classify_events()`，33 类事件（要求 ≥25） | 线上 `/api/v3/sentiment` 返回 `events`：腾讯 00700 → `buyback×5, +1`；否定翻转口径与情绪打分同源 |
| 成长因子补齐 | ✅ 完成 | `merge-announcements --period 20250630` 真跑 | 矩阵覆盖 **revenue_yoy / net_profit_yoy 0→4/5** |
| roe/roa 离线落库通道 | ⚠️ 通道建成，覆盖未动 | `v3_fundamentals_sync.py`（幂等 upsert、announced_at PIT、CLI + dry-run） | 模块与 17,415 字节测试在；**矩阵仍 0/5**（上游拉取未在本机成功，失败清单口径见模块） |
| 价量因子 as_of | ⚠️ 部分 | `compute._factors_args` 透传 + `factors.py pit_gate` + 矩阵双 as_of 并列披露 | CLI/PIT 闸门生效；**工具面白名单（`app.ANALYTICS_ENDPOINTS`）未登记 `as_of`**，经工具面调用仍带不进去 |
| 扫参"数千次完整回测" | ❌ 未完成 | — | 上限仍 8×8=64（`v3_analytics.py` `_int_list(...,8)`），无 `combos_executed`/时间预算 |

## 二、与设计稿的差异性说明（逐项）

| 规格原句（摘录） | 实现状态 | 差异/限制 |
|---|---|---|
| FR-STRAT-002「事件驱动策略」 | 已实现（公告后漂移/事件窗） | 事件源=富途 events + NLP 事件识别（守卫导入）；样本不足时如实返回，不硬凑 |
| FR-STRAT-002「统计套利策略」 | 已实现（协整价差回归） | ADF p 值为 **MacKinnon 近似**（无 statsmodels，`impl` 如实标注）；样本不足/无协整对如实返回 |
| FR-STRAT-002「参数优化支持数千次完整回测和热力图」 | **未达标** | 现状 64 格/次；热力图可视化已有，规模差距 ≈50× |
| FR-STRAT-003「事件识别」 | 已实现（rule-v1，33 类） | 规则法边界：否认/远距离否定语境读不出（茅台案误判已如实登记）；讽刺不识别 |
| FR-STRAT-003「情绪因子构建」 | 已实现（矩阵接入） | 矩阵情绪列目前读 `sentiment_snapshots`（按日采集）；**未接实时打分**，采集不新鲜时覆盖为 0（如实 null） |
| FR-STRAT-001「价值/成长/质量/动量/情绪/另类六类因子」 | 框架全通，覆盖受数据侧限制 | gross_margin 4/5、revenue_yoy 4/5、net_profit_yoy 4/5；**roe/roa 0/5**（等待离线通道真跑成功）；另类（capital_flow/short_interest）需显式取数 |
| FR-DATA-003「价量 as_of」 | 部分 | 数据层 PIT 闸门与矩阵双 as_of 已就绪；**工具面白名单差一行登记**（`app.ANALYTICS_ENDPOINTS` + `as_of`） |

## 三、未完成清单（下一轮）

1. **扫参放开到数千次**（策略 agent 未完成部分）：放开 `_int_list` 条目上限 + `combos_executed/planned/elapsed/truncated` 字段 + 时间预算；先实测速度再定上限。
2. **`test_v3_strategies.py` 缺失**：两个新策略端点目前无专属离线单测（未来数据不得进入 / 样本不足 / 协整不显著三条路径必须钉住）。
3. **工具面 `as_of` 白名单登记**：`app.ANALYTICS_ENDPOINTS["factors"]` 加 `as_of`（一行）+ `v3_mcp.PARAM_DOCS`。
4. **roe/roa 真跑成功一次**：模块已就绪，需排查上游拉取失败原因（网络/限速/字段）后重跑，让覆盖从 0 动起来。
5. **矩阵情绪列接实时打分**：从 `sentiment_snapshots` 扩展为「快照 + `v3_nlp` 实时打分（经 pit-cache）」，消除采集新鲜度依赖。

## 四、过程记录（诚实归档）

- NLP agent 曾在共享树 `git stash`，扫走并行 agent 在途改动后 pop 中止——已逐层核验 `v3_analytics.py` 三层改动（G 的 PIT 接线 / D 的 risk_detail+注册表 / 本轮策略改动）齐整；**`stash@{0}` 保留未删**（内含被扫走版本，确认无用后可 `git stash drop`）。
- NLP agent 顺带修复一颗既有测试时间炸弹（固定 NOW vs 墙钟窗口，2026-09-21 11:00 后必红）。
- 本轮验收：`test_v3_fundamentals_sync + test_v3_nlp + test_v3_analytics + test_v3_ml` = **280 例 OK**；线上重启后 event-study/stat-arb/sentiment-events/factors 覆盖全部实测通过。
