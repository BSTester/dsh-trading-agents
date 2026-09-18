"""WP21 锁定测试（2026-09-18 实机缺陷）：流程页阶段节点的下钻目标必须覆盖全部阶段。

缺陷实证：``services/pipeline.js`` 的 ``DRILL`` 只登记了 4 个阶段
（plan/execute/digest/reconcile），其余一律回落 ``#/schedule``。实机上点「行情同步」
「因子快照」「情绪快照」「研究快照」「日历同步」「研究任务入队」等节点**全部跳到调度页**，
用户看到的是「点击节点没有正确跳转到对应页面」；而路由本身（hashchange 监听）是好的，
所以这类缺陷不会报错、也没有任何提示，只能靠「两侧事实比对」把它钉住。

两侧事实：
  * 服务端 —— ``pipeline._JOB_LABELS``（阶段 key → 中文标签表；``pipeline._stage`` 对未登记
    作业回退英文作业名，因此阶段 key 集合 ⊇ 该表键集合）、``daemon.JOBS_DEFAULT`` 的作业名、
    以及 auto_pipeline 派生的作业名（build_plan/auto_execute/reconcile）；
  * 前端 —— ``services/pipeline.js`` 的 ``DRILL``（阶段 key → 页面 key）与 ``app.jsx`` 的
    ``PAGES``（页面 key 集合，哈希路由 ``#/<key>``）。

手法与 ``tests/test_wp10_locks.py`` 一致：**解析源码比对**，不相信「记得同步」。
本测试是双向的：新增阶段却没登记下钻目标 → 失败；登记了服务端发不出的阶段（腐化条目）→ 失败。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import autopipeline, daemon, pipeline, planner, store  # noqa: E402

PIPELINE_JS = ROOT / "platform" / "web" / "src" / "services" / "pipeline.js"
APP_JSX = ROOT / "platform" / "web" / "src" / "app.jsx"


def _object_body(source, name):
    """抠出 `const NAME = { ... }` 的括号体（花括号配平；注释里出现同名不算）。"""
    match = re.search(rf"(?:(?:export\s+)?const\s+{name}\s*=|^\s*{name}\s*:)\s*\{{",
                      source, re.MULTILINE)
    if not match:
        raise AssertionError(f"未找到 {name} 的对象字面量")
    start = match.end() - 1
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1:index]
    raise AssertionError(f"{name} 的对象字面量未闭合")


def _drill_map():
    """前端 DRILL：{阶段 key: 页面 key}。键名可带引号（英文键一般不带，两种都容忍）。"""
    body = _object_body(PIPELINE_JS.read_text(encoding="utf-8"), "DRILL")
    return dict(re.findall(r'"?([A-Za-z_][A-Za-z0-9_]*)"?\s*:\s*"([a-z_]+)"', body))


def _page_keys():
    """app.jsx 的 PAGES 里登记的页面 key（哈希路由目标集合）。"""
    source = APP_JSX.read_text(encoding="utf-8")
    match = re.search(r"const PAGES = \[(.*?)\n\];", source, re.S)
    if not match:
        raise AssertionError("未找到 app.jsx 的 PAGES 数组")
    return set(re.findall(r'key:\s*"([a-z_]+)"', match.group(1)))


def _server_stage_keys():
    """服务端可能出现在流程页上的阶段 key 全集（三条来源取并）。"""
    names = set(pipeline._JOB_LABELS)
    for chain in daemon.JOBS_DEFAULT.values():
        names.update(job["name"] for job in chain)
    # auto_pipeline 开启时由 build_jobs 派生；关闭态不会出现，但下钻表必须覆盖
    names.update({"build_plan", "auto_execute", "reconcile"})
    return names


class DrillCoverageTest(unittest.TestCase):
    def test_every_server_stage_has_a_drill_target(self):
        """服务端发得出的每个阶段都必须有下钻目标——缺一个就退回调度页（本次实机缺陷）。"""
        missing = sorted(_server_stage_keys() - set(_drill_map()))
        self.assertEqual(missing, [], f"这些阶段没登记下钻目标，点节点会错跳到调度页：{missing}")

    def test_no_rotted_drill_entries(self):
        """反向核对：登记了服务端发不出的阶段 = 腐化条目（改作业名后留下的死映射）。"""
        rotted = sorted(set(_drill_map()) - _server_stage_keys())
        self.assertEqual(rotted, [], f"这些下钻条目对应的阶段服务端已不再产生：{rotted}")

    def test_every_drill_target_is_a_real_page(self):
        """目标必须是 app.jsx PAGES 里真实存在的页面 key，否则 hash 路由会落到首屏。"""
        pages = _page_keys()
        unknown = sorted({target for target in _drill_map().values() if target not in pages})
        self.assertEqual(unknown, [], f"这些下钻目标不是已登记页面：{unknown}")

    def test_drill_targets_are_not_the_lazy_fallback(self):
        """除 sync_calendar（作业表/日历告警确实在调度页）外，不得把阶段一律丢给调度页。

        这条正是本次缺陷的形状：11 个阶段全部回落 ``schedule``，页面看起来「有链接」，
        点下去全是同一页。
        """
        drill = _drill_map()
        to_schedule = sorted(key for key, target in drill.items() if target == "schedule")
        self.assertEqual(to_schedule, ["sync_calendar"],
                         "只有 sync_calendar 该落到调度页；其余阶段必须指向其事实来源页")

    def test_frontend_exposes_the_key_list_for_this_lock(self):
        """``DRILL_KEYS`` 是给本锁测试读的稳定出口（前端逻辑不得反向依赖它当契约）。"""
        source = PIPELINE_JS.read_text(encoding="utf-8")
        self.assertIn("export const DRILL_KEYS = Object.keys(DRILL)", source)


if __name__ == "__main__":
    unittest.main()


class SingleTickerStrategyInAutoPipelineTest(unittest.TestCase):
    """配置里写单标的策略（rsi/ma_cross）时，``plan_auto`` 必须软跳过而非抛异常。

    2026-09-18 实测缺陷：``rsi``/``ma_cross`` 继承 ``SingleTicker``，**没有**
    ``target_weights``；配置里写成它们时 ``strategy.target_weights`` 直接
    ``AttributeError``，违反 ``plan_auto`` 的「永不抛」作业契约，且告警里看不出该怎么改。
    """

    def setUp(self):
        import json
        import tempfile

        self.json = json
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "trading-account-mode").write_text("sim", encoding="utf-8")
        (self.home / "trading-platform.json").write_text(self.json.dumps({
            "watchlist": ["SH.600519"],
            "auto_pipeline": {"enabled": True,
                              "strategies": [{"market": "SH", "strategy": "rsi"}]},
        }, ensure_ascii=False), encoding="utf-8")
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        store.upsert_calendar(self.conn, "SH", [
            {"day": day, "trade_date_type": "WHOLE", "trade_second": 14400}
            for day in ("2026-09-15", "2026-09-16")])
        # 数据就绪门要求「最后交易日 = 2026-09-16」；20 根只是为了满足 ATR 窗口
        import datetime as dt
        end = dt.date(2026, 9, 16)
        days = [(end - dt.timedelta(days=19 - i)).isoformat() for i in range(20)]
        store.upsert_bars(self.conn, "SH.600519", "1d", [
            {"t": day, "o": 5.0, "h": 5.5, "l": 4.5, "c": 5.0, "v": 1000.0}
            for day in days], source="test")

    def test_single_ticker_strategy_soft_skips_instead_of_raising(self):
        result = planner.plan_auto(self.conn, str(self.home), "SH", today="2026-09-16",
                                   broker_call=lambda *a, **k: None)
        self.assertTrue(result["ok"], "必须软跳过（ok=True + skipped），不得抛异常")
        self.assertIn("target_weights", result["skipped"])
        self.assertIn("rsi", result["skipped"])
        titles = [r["title"] for r in self.conn.execute("SELECT title FROM alerts")]
        self.assertIn("策略不支持自动计划", titles, "告警标题是 pipeline 归因的契约字面量")

    def test_pipeline_attributes_the_new_title_to_build_plan_skipped(self):
        self.assertEqual(pipeline._ALERT_STATUS.get("策略不支持自动计划"), ("build_plan", "skipped"))
