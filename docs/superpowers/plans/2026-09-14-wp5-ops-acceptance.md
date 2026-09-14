# WP5 运维与验收 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。
> **全局约定**：见 `2026-09-14-platform-plan-index.md`。前置：WP1–WP4 全部完成并通过各自验收。

**目标：** kill switch/熔断/恢复演练、运维 runbook、四份既有文档修订（规格 §十二清单）、全量验收并留档。

**验收（规格 §十 WP5）：** kill switch/熔断演练通过、文档与实现一致。

---

### 任务 1：演练脚本

**文件：** 创建 `scripts/drills.sh`；创建 `tests/test_core_wp5_drills.py`（离线部分）

- [ ] 步骤 1：失败测试（演练脚本的可测内核：`scripts/drills.py` 的纯函数——kill 文件创建/清除与风控联动的状态断言）：

```python
"""WP5 演练内核：kill 状态机与 halt 生命周期（离线）。"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import drills, risk, store  # noqa: E402


class DrillsTest(unittest.TestCase):
    def test_kill_lifecycle(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        kill = Path(tmp.name) / "trading-kill"
        drills.kill_on(kill)
        v = risk.pre_trade_checks(
            {"symbol": "SH.600519", "side": "BUY", "qty": 100, "price": 1.0, "mode": "SIM",
             "plan_id": "P", "plan_hash": "h", "stop_dist": 0.1},
            {"mode": "SIM", "kill_path": str(kill), "equity": 1e6, "positions_value": {},
             "positions_count": 0, "day_pnl_pct": 0.0, "is_trading_day": True,
             "config": {"risk_per_trade": 0.01, "max_positions": 5,
                        "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03},
             "plan_hash": "h", "plan_status": "frozen"})
        self.assertFalse(v.allowed)
        drills.kill_off(kill)
        self.assertFalse(kill.exists())
```

- [ ] 步骤 2：FAIL；步骤 3：实现 `plugins/core/python/trading_core/drills.py`：

```python
"""演练内核：kill 开关生命周期（W P5 演练的纯函数部分）。"""


def kill_on(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("")


def kill_off(path):
    Path(path).unlink(missing_ok=True)
```

（顶部 `from pathlib import Path`；`drills.sh` 为薄封装：依次调用 `kill_on → 断言拒单 → kill_off` 并打印结果 JSON。）
- [ ] 步骤 4：PASS；`git commit -m "feat(core): WP5 演练内核与脚本"`

### 任务 2：恢复演练（人工 runbook 执行）

**文件：** 创建 `docs/RUNBOOK.md`

- [ ] 步骤 1：编写 runbook（含四场景的逐步操作与预期）：
  1. daemon `kill -9` → systemd 重启 → 心跳恢复 → 指令按 processed/ 去重（重放不重复执行）；
  2. 执行中进程中断 → 订单留 unknown → 按对账作业输出人工核对券商 → 状态机迁移；
  3. 对账差异 → critical 告警 + halt → 人工核对后 `unkill`/清 halt → 次日计划恢复；
  4. token 过期（internal error 特征）→ futu_mcp 自动续期 → 仍失败时的 `futu_auth.py --refresh` 流程。
- [ ] 步骤 2：逐场景实际执行并粘贴输出到 runbook 的「演练记录」小节；`git commit -m "docs: 运维 runbook 与演练记录"`

### 任务 3：既有文档修订（规格 §十二 清单）

**文件：** 修改 `docs/architecture.md`、`docs/P4-live-trading.md`、`README.md`、`docs/HANDOVER.md`

- [ ] 步骤 1：`architecture.md`：组件职责表加 trading-core/daemon；「明确不做」更新为「工作台恰好一个受约束执行入口」表述；Host/Client 协议表加 plan/plan-execute/schedule/reconcile；
- [ ] 步骤 2：`P4-live-trading.md`：实盘准入清单追加「daemon 执行链路演练、kill switch 演练、对账零差异连续 3 交易日」；
- [ ] 步骤 3：`README.md`：目录结构加 `plugins/core`；安装说明覆盖 `LIBRARIES=("datasource","core")` 与自检新口径；
- [ ] 步骤 4：`HANDOVER.md`：交接注意加 daemon 指令目录/kill 文件/心跳文件的运维操作与「不自动清除未知在途租约」；
- [ ] 步骤 5：核对文档与实现一致（抽查：文档提到的每个 RPC/文件路径都存在）；`git commit -m "docs: 按规格 §十二 修订四份文档"`

### 任务 4：数据依赖核验的定期回归（防上游漂移）

**文件：** 创建 `scripts/verify-data-deps.py`

- [ ] 步骤 1：脚本聚合各 WP 的核验调用（§13.2 六项 + WP2 估值/中证800 + WP3 工具 schema）逐项输出 `OK/FAIL` JSON；失败项附「影响工作包」与「规格缺口编号」。
- [ ] 步骤 2：手动真实运行一次，输出贴入本文件验收记录；约定：每次富途侧发版异常（internal error 面扩大）时先跑本脚本。`git commit -m "feat(scripts): 数据依赖核验聚合脚本"`

### 任务 5：全量验收

- [ ] 步骤 1：`~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v` 全绿；
- [ ] 步骤 2：规格 §十 五个工作包验收行逐条打勾，证据链接到各计划文件的验收记录小节；
- [ ] 步骤 3：确认 live 准入仍按 P4 清单（人工评估项全部未勾选状态如实保留）；
- [ ] 步骤 4：`git commit -m "docs(plans): WP5 全量验收记录"`

### WP5 验收记录（执行时填写）

- 演练输出：四场景 runbook 记录链接
- 全量测试：N tests, 0 failures
- 遗留与升级项（如 parquet 迁移、港美披露预约）：
