# WP11 资讯 PIT 地基 实现计划

> **面向 AI 代理的工作者：** 必需子技能：superpowers:subagent-driven-development（推荐）逐任务实现，两阶段审查。步骤 `- [ ]` 跟踪。
> **规格**：`docs/superpowers/specs/2026-09-16-wp9-15-autopipeline-research-institute-design.md` §六。全局约定见索引。
> 三套测试保持全绿；提交 `feat(core|platform): 摘要`。

**目标**：sentiment_snapshots 每日按关注池落三源原始事实（fin_sentiment/富途资讯/last30days），缺席降级不报错；查询端点与流程页/因子页展示；250 交易日演进条款落文档。

**架构**：新表只存原始 payload（不打分）；采集作业 = trading-venv 子进程调 fin-data 同一 Python 脚本（零第二实现）+ last30days 可选引擎；查询三路同源（HTTP/CLI/MCP）。

---

### 任务 1：sentiment_snapshots 表 + store 读写

**文件**：修改 `plugins/core/python/trading_core/store.py`；测试 `tests/test_core_store.py` 追加。

- [ ] **步骤 1：失败测试**

```python
def test_sentiment_snapshots_roundtrip(self):
    # insert_sentiment(conn, date, symbol, source, payload, fetched_at)  # payload=原始 JSON 字符串
    # read_sentiments(conn, symbol, limit=30, before=None)  # 按日期倒序；PIT：before 给定时只返 date<=before
    # sentiment_streak(conn, today)  # 连续积累天数（从今日往前数连续有任意记录的交易日数，断档即停）
    # 重复 (date,symbol,source) → INSERT OR REPLACE（当日重跑幂等）
    ...
```

- [ ] **步骤 2：验证失败** → **步骤 3：实现**：`_SCHEMA` 追加

```sql
CREATE TABLE IF NOT EXISTS sentiment_snapshots(
  date TEXT NOT NULL, symbol TEXT NOT NULL, source TEXT NOT NULL,
  payload TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY(date, symbol, source));
```

- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): sentiment_snapshots PIT 表与读写/连续天数"`

### 任务 2：三源采集作业（CLI `sentiment-snapshot`）

**文件**：新建 `plugins/core/python/trading_core/sentiment.py`；`cli.py` 加子命令；`daemon.py` 的 `build_jobs` 里 WP9 预留的 `sentiment_snapshot` 作业 cmd 指向本命令；测试 `tests/test_wp11_sentiment_job.py`。

- [ ] **步骤 1：失败测试**（子进程全部注入假 runner）：
  ① fin_sentiment 源：runner 返回 `{"ticker":"600519","x":[...],"a_share_comment":{...},"sources_status":{...}}` → 落库 source="fin_sentiment"，payload=原文 JSON；
  ② 资讯源（**实现期修正**：`find-news`/`quote_news_search` 实测恒空——docs/TOOL-LIMITS.md「取新闻」行三种参数均 `data: []`，接恒空通道等于造一个永远空的源；改用 fin-data 统一快讯脚本 `fin_news.py`，真实上游 futu/akshare/yahoo 留在 payload.sources_status 自证）→ source="fin_news"；
  ③ last30days 源：引擎路径存在 → `--emit=json` 输出落 source="last30days"；**路径不存在 → 该源缺席，结果标 `absent:["last30days"]`，退出码 0**；
  ④ 单源 runner 抛异常 → 该源 absent + warn 告警，其余源照常；全部失败 → warn 告警 + 退出码 0（**不阻塞链**）；
  ⑤ 关注池为空 → 跳过 + 告警。
- [ ] **步骤 2：验证失败** → **步骤 3：实现**：`sentiment.py` 提供 `run(home, market, conn=None, runner=None, news_call=None)`；runner 缺省真实子进程：`[venv_python, str(Path(home)/"trading-python/fin-data/fin_sentiment.py"), "--ticker", code]`；last30days 路径 `Path(home)/"last30days-skill/skills/last30days/scripts/last30days.py"`。超时 90s/标的，串行限速（关注池 ≤200 时可接受）。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): sentiment-snapshot 三源采集作业（缺席降级不阻塞）"`

### 任务 3：sentiment-history 端点 + 展示

**文件**：修改 `platform/server/compute.py`/`app.py`/`mcp_tools.py`/`caches.py`（TTL 5m）/`store_access.py` 声明；`platform/web/src/pages/factors.jsx`（情绪卡片）；测试 `tests/test_wp11_history.py` + `platform/web/tests/endpoints.test.mjs`。

- [ ] **步骤 1：失败测试**：
  ① 端点 `{symbol? , limit?}`（1..120）→ 按日期倒序 `[{date, symbol, source, payload}]`，symbol 缺省返回当日全量摘要 `{date, symbols, sources}`；
  ② MCP 工具 `sentiment_history` 注册且进 MCP 面（**读类，允许进**；工具计数 +1，更新锁定测试与预算）；
  ③ 流程页情绪阶段 summary 含「已连续积累 N 天」（读 `sentiment_streak`——pipeline 端点测试补一条）。
- [ ] **步骤 2：验证失败** → **步骤 3：实现**：三路同源模式照抄 `factors-history`（WP7 先例：HTTP/CLI/MCP 同一 provider 函数）；factors.jsx 加「情绪快照」折叠卡片：最近日期、三源在场/缺席标记、「连续 N 天」。
- [ ] **步骤 4：通过**；三套绿；UI 文案 grep 无违规。
- [ ] **步骤 5：Commit**：`git commit -m "feat(platform): sentiment-history 三路查询+因子页情绪卡片+流程页天数"`

### 任务 4：演进条款与文档

- [ ] `docs/architecture.md`：新表/端点/作业登记 + 演进条款一句话（≥250 交易日可提检验申请，走 WP14 验证门，转正前不进规则 factors）。
- [ ] 三套绿；验收对照规格 §6.4：落库/降级/查询/流程页联动 + 假时钟（并入 `tests/test_wp9_e2e.py` 的时间线：D1 16:25 sentiment_snapshot ran → D2 流程页情绪阶段 ok）。
- [ ] Commit：`git commit -m "docs(platform): 情绪 PIT 地基登记与演进条款"`
