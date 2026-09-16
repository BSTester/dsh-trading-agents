"""WP7 表锁定（纯 Python 断言；2026-09-16 面板退役时改写）。

历史：本测试原把 ``plugins/workbench/src`` 里的 JS 字面量（rpc.js/endpoints.js/
analytics.js/series.js）提取出来与 Python 常量逐项比对，防「单一事实来源」两侧漂移。
WP7 面板退役（用户决策 2026-09-16）后 JS 面板源已整体删除，Python 表成为**唯一**实现，
因此改写为纯 Python 断言——把当前事实冻结成内嵌期望值，任何一侧被无意改动立刻红。

覆盖：
  * ``store_access.endpoints()`` ≡ 61 项内嵌清单（22 项基础清单按已删 endpoints.js
    原序冻结 + 7 项 WP7 服务自有端点 + 8 项 WP8 富途实时直通端点 + 9 项 WP8 OpenAPI
    行情端点 + 6 项 WP8 OpenAPI 交易只读端点 + 3 项 WP8 推送订阅管理端点 + 3 项 WP8
    设置页端点 + 2 项 WP10（流程页/自动流水线设置）+ 1 项 WP11（情绪快照历史），
    逐项与顺序都钉死）；
  * ``caches.CACHE_TTL_MS``：17 项 legacy TTL 逐项钉死 + WP7 增量
    ``factors-history: 5 分钟`` + WP8 任务 2 增量（基本类 5 分钟 ×4 +
    ``quote_history_kline_v2: 10 分钟``；实时类不进表，TTL 恒 0）+ WP10 增量
    ``pipeline: 30 秒`` + WP11 增量 ``sentiment-history: 5 分钟``；
    业务确认两端点与 auto_pipeline 不在表里（TTL 恒 0，不得缓存「待确认」与设置读）；
  * ``caches.ENDPOINT_SHAPE``：16 项 legacy 形状逐项钉死 + WP7 增量
    ``factors-history: ["snapshots"]`` + WP8 任务 2 增量 5 项 + WP10 ``pipeline``
    + WP11 ``sentiment-history: ["records", "summary"]``；``confirmation:
    ["pending"]`` 保留；
  * ``app.ANALYTICS_ENDPOINTS`` 逐端点字段白名单与各动作端点字段表；
  * ``compute`` 的内层缓存 TTL / 逐端点 timeout / 端点→脚本映射（脚本真实存在，
    analytics.py 子命令白名单对齐——这两条原由 tests/analytics-routing.test.mjs
    钉在 Node 侧，面板退役后由本文件接管）。
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from server import app as app_module  # noqa: E402
from server import caches, compute, store_access  # noqa: E402

# 工作台 Python 脚本目录（服务取数子进程的真实落点，面板退役后仍是唯一脚本源）
PYTHON_DIR = ROOT / "plugins" / "workbench" / "python"

# 22 项基础清单：按已删 plugins/workbench/src/endpoints.js 的数组原序冻结
# （服务与前端零行为变化的锚点——顺序变了就是破坏性变更）。
BASE_ENDPOINTS = [
    "snapshot",
    "switch-mode",
    "series",
    "equity",
    "positions",
    "correlation",
    "sensitivity",
    "risk",
    "trades",
    "events",
    "factors",
    "ic",
    "audit",
    "sources",
    "instrument",
    "quality",
    "plan",
    "plan-execute",
    "schedule",
    "reconcile",
    "confirmation",
    "confirm-decide",
]

# WP7 服务自有端点（任务 2：factors-history；任务 3：受约束交易工具 × 6）
WP7_ENDPOINTS = ["factors-history", "trade_place", "trade_modify", "trade_cancel",
                 "account_positions", "account_orders", "account_funds"]

# WP8 富途实时直通端点（8 个：取数在 server/futu_data.py，实时零缓存；
# 与 store_access.FUTU_ENDPOINTS 逐项同序）
FUTU_ENDPOINTS = ["rt_quote", "rt_order_book", "capital_flow", "capital_flow_history",
                  "capital_distribution", "option_expiration", "option_chain", "option_screen"]

# WP8 任务 2：OpenAPI 行情接入新增端点（9 个；与 store_access.WP8_MARKET_ENDPOINTS
# 逐项同序）。实时四类（market_snapshot/cur_kline/rt_data/rt_ticker）TTL 0 不进
# 缓存表；基本四类 + 历史 K 线 v2 进 TTL/形状表。
WP8_MARKET_ENDPOINTS = ["market_snapshot", "cur_kline", "rt_data", "rt_ticker",
                        "info_basicinfo", "info_trading_days", "info_search",
                        "info_market_state", "quote_history_kline_v2"]

# WP8 任务 3：OpenAPI 交易只读端点（6 个；与 store_access.WP8_TRADE_ENDPOINTS 逐项
# 同序）。全部实时直通：TTL 0，不进 TTL/形状表；受模式约束（载荷 mode 或缺省读模式文件）。
WP8_TRADE_ENDPOINTS = ["trade_max_qty", "orders_open", "orders_history",
                       "orders_detail", "deals_today", "deals_history"]

# WP8 任务 6：推送订阅管理面（3 个；与 store_access.WP8_PUSH_ENDPOINTS 逐项同序）。
# push_status 是读（TTL 0，与 /healthz 的 push 同形）；push_subscribe/push_unsubscribe
# 只改本地连接订阅意图（**非交易**：不改模式、不过风控、不产生订单），TTL 0 不进缓存表。
WP8_PUSH_ENDPOINTS = ["push_status", "push_subscribe", "push_unsubscribe"]

# WP8 任务 7：设置页端点（读写在 server/settings_api.py；有意排除在 MCP 工具面）。
# WP8 OAuth 集成增补 openapi_oauth（授权流程 start/status/cancel，server/oauth_flow.py）。
WP8_SETTINGS_ENDPOINTS = ["openapi_config", "openapi_test", "openapi_oauth"]

# WP10 端点（与 store_access.WP10_ENDPOINTS 逐项同序）：
#   任务 1 pipeline：流程页只读聚合，取数在 trading_core pipeline.py
#   （snapshot-pipeline 子命令），进 TTL/形状表；
#   任务 2 auto_pipeline：自动流水线设置读/写——进端点清单但**两张缓存表都不进**
#   （读/写端点，读侧必须立刻反映刚写入的配置；与 openapi_config 同口径）。
# 因此下面把「WP10 在缓存表里的条目」单列成 dict：表大小断言按实际条目数算，
# 而不是按 WP10 端点总数算（后者会随不该进缓存的端点一起漂移）。
WP10_ENDPOINTS = ["pipeline", "auto_pipeline"]
WP10_TTL = {"pipeline": 30_000}
WP10_SHAPE = {"pipeline": ["date", "markets", "global", "auto_pipeline"]}

# WP11 任务 3：情绪快照历史/摘要（与 store_access.WP11_ENDPOINTS 逐项同序）。
# 只读查询，按日采集：进 TTL（5 分钟，与 factors-history 同量级）与形状表
# （records/summary 两键恒在——给 symbol 时 summary 为 None，不给时 records 为空数组）。
WP11_ENDPOINTS = ["sentiment-history"]
WP11_TTL = {"sentiment-history": 5 * 60_000}
WP11_SHAPE = {"sentiment-history": ["records", "summary"]}


class EndpointListLockTests(unittest.TestCase):
    def test_endpoints_is_frozen_61_item_list(self):
        """``store_access.endpoints()`` ≡ 22 基础 + 7 WP7 + 8 WP8 直通 + 9 WP8 行情
        + 6 WP8 交易 + 3 WP8 推送 + 3 WP8 设置 + 2 WP10（流程页 + 自动流水线设置）
        + 1 WP11（情绪快照）= 61 项，同序。"""
        self.assertEqual(store_access.endpoints(),
                         BASE_ENDPOINTS + WP7_ENDPOINTS + FUTU_ENDPOINTS
                         + WP8_MARKET_ENDPOINTS + WP8_TRADE_ENDPOINTS
                         + WP8_PUSH_ENDPOINTS + WP8_SETTINGS_ENDPOINTS + WP10_ENDPOINTS
                         + WP11_ENDPOINTS)
        self.assertEqual(len(store_access.endpoints()), 61)
        self.assertEqual(len(store_access._BASE_ENDPOINTS), 22)
        self.assertEqual(list(store_access.WP7_ENDPOINTS), WP7_ENDPOINTS)
        self.assertEqual(list(store_access.FUTU_ENDPOINTS), FUTU_ENDPOINTS)
        self.assertEqual(list(store_access.WP8_MARKET_ENDPOINTS), WP8_MARKET_ENDPOINTS)
        self.assertEqual(list(store_access.WP8_TRADE_ENDPOINTS), WP8_TRADE_ENDPOINTS)
        self.assertEqual(list(store_access.WP8_PUSH_ENDPOINTS), WP8_PUSH_ENDPOINTS)
        self.assertEqual(list(store_access.WP8_SETTINGS_ENDPOINTS), WP8_SETTINGS_ENDPOINTS)
        self.assertEqual(list(store_access.WP10_ENDPOINTS), WP10_ENDPOINTS)
        self.assertEqual(list(store_access.WP11_ENDPOINTS), WP11_ENDPOINTS)
        # 尾部锚点：业务确认两端点收尾基础清单；WP7/WP8/WP10/WP11 增量按任务顺序追加
        self.assertEqual(store_access.endpoints()[-41:-39],
                         ["confirmation", "confirm-decide"])
        self.assertEqual(store_access.endpoints()[-39:-32], WP7_ENDPOINTS)
        self.assertEqual(store_access.endpoints()[-32:-24], FUTU_ENDPOINTS)
        self.assertEqual(store_access.endpoints()[-24:-15], WP8_MARKET_ENDPOINTS)
        self.assertEqual(store_access.endpoints()[-15:-9], WP8_TRADE_ENDPOINTS)
        self.assertEqual(store_access.endpoints()[-9:-6], WP8_PUSH_ENDPOINTS)
        self.assertEqual(store_access.endpoints()[-6:-3], WP8_SETTINGS_ENDPOINTS)
        self.assertEqual(store_access.endpoints()[-3:-1], WP10_ENDPOINTS)
        self.assertEqual(store_access.endpoints()[-1:], WP11_ENDPOINTS)
        self.assertEqual(store_access.endpoints()[0], "snapshot")
        # 无重复；重复调用返回等值副本（调用方改动不污染后续结果）
        self.assertEqual(len(set(store_access.endpoints())), 61)
        sample = store_access.endpoints()
        sample.append("bogus")
        self.assertEqual(len(store_access.endpoints()), 61)

    def test_analytics_endpoints_are_declared(self):
        self.assertTrue(set(app_module.ANALYTICS_ENDPOINTS) <= set(store_access.endpoints()))


# WP8 任务 2 的 TTL 增量（任务书：实时类 TTL 0，基本类 5m，history-kline-v2 10m）
WP8_MARKET_TTL_MS = {
    "info_basicinfo": 5 * 60_000,
    "info_trading_days": 5 * 60_000,
    "info_search": 5 * 60_000,
    "info_market_state": 5 * 60_000,
    "quote_history_kline_v2": 10 * 60_000,
}


class CacheTtlLockTests(unittest.TestCase):
    #: legacy 17 项 TTL（毫秒）——面板退役前 rpc.js/CACHE_TTL_MS 的最终事实
    LEGACY_TTL_MS = {
        "instrument": 10 * 60_000,
        "series": 10 * 60_000,
        "equity": 5 * 60_000,
        "positions": 5 * 60_000,
        "correlation": 30 * 60_000,
        "sensitivity": 60 * 60_000,
        "risk": 15 * 60_000,
        "trades": 5 * 60_000,
        "events": 60 * 60_000,
        "factors": 30 * 60_000,
        "ic": 30 * 60_000,
        "audit": 2 * 60_000,
        "sources": 5 * 60_000,
        "quality": 60 * 60_000,
        "plan": 60_000,
        "schedule": 30_000,
        "reconcile": 5 * 60_000,
    }

    def test_legacy_ttl_table_is_frozen(self):
        self.assertEqual({name: ttl for name, ttl in caches.CACHE_TTL_MS.items()
                          if name not in WP7_ENDPOINTS
                          and name not in WP8_MARKET_ENDPOINTS
                          and name not in WP10_ENDPOINTS
                          and name not in WP11_ENDPOINTS}, self.LEGACY_TTL_MS)
        self.assertEqual(self.LEGACY_TTL_MS["instrument"], 10 * 60_000)

    def test_wp7_ttl_delta_is_pinned(self):
        """WP7 增量钉死：factors-history 5 分钟（面板退役前的服务自有值）。"""
        self.assertEqual(caches.CACHE_TTL_MS.get("factors-history"), 5 * 60_000)
        self.assertEqual(len(caches.CACHE_TTL_MS),
                         len(self.LEGACY_TTL_MS) + 1 + len(WP8_MARKET_TTL_MS)
                         + len(WP10_TTL) + len(WP11_TTL))

    def test_wp11_ttl_delta_is_pinned(self):
        """WP11 增量：情绪快照历史/摘要 5 分钟（按日采集，与 factors-history 同量级）。"""
        self.assertEqual({name: caches.CACHE_TTL_MS[name]
                          for name in WP11_ENDPOINTS
                          if name in caches.CACHE_TTL_MS}, WP11_TTL)

    def test_wp10_ttl_delta_is_pinned(self):
        """WP10 增量：流程快照 30 秒（与 schedule 同量级，页面要看到刚跑完的作业）；
        auto_pipeline 有意不进表（读/写端点，读不吃缓存）。"""
        self.assertEqual({name: caches.CACHE_TTL_MS[name]
                          for name in WP10_ENDPOINTS
                          if name in caches.CACHE_TTL_MS}, WP10_TTL)
        self.assertNotIn("auto_pipeline", caches.CACHE_TTL_MS)

    def test_wp8_market_ttl_delta_is_pinned(self):
        """WP8 任务 2 增量：基本类 5 分钟 ×4 + 历史 K 线 v2 10 分钟；实时四类不进表。"""
        self.assertEqual({name: caches.CACHE_TTL_MS[name]
                          for name in WP8_MARKET_ENDPOINTS
                          if name in caches.CACHE_TTL_MS}, WP8_MARKET_TTL_MS)
        for endpoint in ("market_snapshot", "cur_kline", "rt_data", "rt_ticker"):
            self.assertNotIn(endpoint, caches.CACHE_TTL_MS, endpoint)
            self.assertEqual(caches.CACHE_TTL_MS.get(endpoint, 0), 0, endpoint)

    def test_business_confirmation_endpoints_are_not_cached(self):
        """业务确认两端点 TTL 恒为 0：缓存住「待确认」会让界面拿到已处理掉的请求。"""
        for endpoint in ("confirmation", "confirm-decide"):
            self.assertNotIn(endpoint, caches.CACHE_TTL_MS, endpoint)
            self.assertEqual(caches.CACHE_TTL_MS.get(endpoint, 0), 0, endpoint)


class EndpointShapeLockTests(unittest.TestCase):
    #: legacy 16 项形状——面板退役前 endpoints.js/ENDPOINT_SHAPE 的最终事实
    LEGACY_SHAPE = {
        "series": ["ticker", "bars"],
        "equity": ["mode", "points"],
        "positions": ["mode", "groups"],
        "correlation": ["matrix"],
        "sensitivity": ["ticker", "matrix"],
        "risk": ["config"],
        "trades": ["trades"],
        "events": ["ticker", "events"],
        "factors": ["tickers"],
        "ic": ["points"],
        "sources": ["sources"],
        "instrument": ["ticker"],
        "quality": ["ticker"],
        "plan": ["plans", "alerts"],
        "confirmation": ["pending"],
        "schedule": ["heartbeat", "jobs"],
        "reconcile": ["diffs", "tca"],
    }

    def test_legacy_shape_table_is_frozen(self):
        self.assertEqual({name: fields for name, fields in caches.ENDPOINT_SHAPE.items()
                          if name not in WP7_ENDPOINTS
                          and name not in WP8_MARKET_ENDPOINTS
                          and name not in WP10_ENDPOINTS
                          and name not in WP11_ENDPOINTS}, self.LEGACY_SHAPE)
        self.assertEqual(caches.ENDPOINT_SHAPE["confirmation"], ["pending"])

    def test_wp7_shape_delta_is_pinned(self):
        """WP7 增量钉死：factors-history 最小字段只有一个快照数组。"""
        self.assertEqual(caches.ENDPOINT_SHAPE["factors-history"], ["snapshots"])
        self.assertEqual(len(caches.ENDPOINT_SHAPE),
                         len(self.LEGACY_SHAPE) + 1 + len(WP8_MARKET_SHAPE)
                         + len(WP10_SHAPE) + len(WP11_SHAPE))

    def test_wp11_shape_delta_is_pinned(self):
        """WP11 增量：情绪快照两形态共用同一出口，records/summary 两键恒在。"""
        self.assertEqual({name: caches.ENDPOINT_SHAPE[name]
                          for name in WP11_ENDPOINTS
                          if name in caches.ENDPOINT_SHAPE}, WP11_SHAPE)

    def test_wp10_shape_delta_is_pinned(self):
        """WP10 增量：流程快照最小字段（每市场阶段表 + 全局阶段 + 配置摘要）；
        auto_pipeline 有意不进形状表（与 openapi_config 同口径）。"""
        self.assertEqual({name: caches.ENDPOINT_SHAPE[name]
                          for name in WP10_ENDPOINTS
                          if name in caches.ENDPOINT_SHAPE},
                         WP10_SHAPE)
        self.assertNotIn("auto_pipeline", caches.ENDPOINT_SHAPE)

    def test_wp8_market_shape_delta_is_pinned(self):
        """WP8 任务 2 增量：5 个进缓存端点的最小字段（与上游 data 键逐项对应）。"""
        self.assertEqual({name: caches.ENDPOINT_SHAPE[name]
                          for name in WP8_MARKET_ENDPOINTS
                          if name in caches.ENDPOINT_SHAPE}, WP8_MARKET_SHAPE)
        for endpoint in ("market_snapshot", "cur_kline", "rt_data", "rt_ticker"):
            self.assertNotIn(endpoint, caches.ENDPOINT_SHAPE, endpoint)


# WP8 任务 2 的形状增量：最小字段取自官方文档 data 顶层键（与 MCP 通道 data 同形）
WP8_MARKET_SHAPE = {
    "info_basicinfo": ["basic_list"],
    "info_trading_days": ["trading_days"],
    "info_search": ["news_list"],
    "info_market_state": ["market_state_list"],
    "quote_history_kline_v2": ["kline_list"],
}


class WhitelistLockTests(unittest.TestCase):
    #: 12 个分析端点的字段白名单——面板退役前 app.ANALYTICS_ENDPOINTS 的最终事实
    ANALYTICS_FIELDS = {
        "equity": ("mode", "window"),
        "positions": ("mode", "window"),
        "correlation": ("tickers", "window"),
        "sensitivity": ("ticker", "strategy", "metric", "fast_grid", "slow_grid",
                        "buy_grid", "sell_grid", "start"),
        "risk": (),
        "trades": ("mode", "limit"),
        "events": ("ticker", "days"),
        "factors": ("tickers", "window"),
        "ic": ("tickers", "factor", "forward", "window"),
        "sources": ("no_probe",),
        "instrument": ("ticker",),
        "quality": ("ticker",),
    }

    def test_analytics_allowed_fields_are_frozen(self):
        self.assertEqual(app_module.ANALYTICS_ENDPOINTS, self.ANALYTICS_FIELDS)

    def test_action_endpoint_field_tables_are_frozen(self):
        self.assertEqual(list(app_module.SWITCH_MODE_FIELDS), ["mode", "expected_mode", "confirmation"])
        # 确认通道的载荷只有 id/decision：白名单外字段在 handle 层就被拒（不能变成下单通道）
        self.assertEqual(list(app_module.CONFIRM_DECIDE_FIELDS), ["id", "decision"])
        self.assertEqual(list(app_module.PLAN_EXECUTE_FIELDS),
                         ["plan_hash", "expected_mode", "confirmation", "action"])
        self.assertEqual(list(app_module.SERIES_FIELDS), ["ticker", "period", "limit"])
        self.assertEqual(set(app_module.EMPTY_PAYLOAD_ENDPOINTS),
                         {"snapshot", "audit", "confirmation", "plan", "schedule",
                          "reconcile", "pipeline"})


class ComputeTableLockTests(unittest.TestCase):
    def test_inner_cache_ttl_and_timeouts_are_frozen(self):
        self.assertEqual(compute.INNER_CACHE_TTL_MS, 30_000)
        self.assertEqual(compute.TIMEOUT, 180_000)
        self.assertEqual(compute.ENDPOINT_TIMEOUT_MS, {"instrument": 120_000})
        self.assertEqual(compute.timeout_for("instrument"), 120_000)
        self.assertEqual(compute.timeout_for("equity"), 180_000)

    def test_endpoint_script_map_is_frozen_and_scripts_exist(self):
        """12 个分析端点都有 (脚本, 参数构造, 源行号)，且脚本真实存在。

        「脚本都真实存在」原由 tests/analytics-routing.test.mjs 在 Node 侧钉死
        （该文件随 analytics.js 退役删除），现由本断言接管。
        """
        self.assertEqual(set(compute.ENDPOINTS), set(app_module.ANALYTICS_ENDPOINTS))
        for name, (script, build, _source) in compute.ENDPOINTS.items():
            self.assertTrue(script.endswith(".py"), name)
            self.assertTrue(callable(build), name)
            self.assertTrue((PYTHON_DIR / script).is_file(),
                            f"{name} 指向了不存在的脚本 {script}")

    def test_analytics_py_subcommand_whitelist(self):
        """analytics.py 的子命令只能来自它自己声明的集合。

        规则来源：曾有五个接口把不存在的子命令交给 analytics.py，运行时表现为
        argparse `invalid choice`（原 analytics-routing 测试的立项理由）。
        """
        source = (PYTHON_DIR / "analytics.py").read_text(encoding="utf-8")
        subcommands = {m.group(1) for m in re.finditer(r'add_parser\(\s*"([^"]+)"', source)}
        self.assertEqual(sorted(subcommands),
                         ["correlation", "equity", "positions", "risk", "trades"],
                         "analytics.py 的子命令变了，请同步检查 compute.ENDPOINTS 的路由")
        # 子命令由参数构造函数决定，这里抽查三个直接可见的路由（其余由
        # test_wp6_service.test_arg_construction_matches_analytics_js 逐参数钉死）
        self.assertEqual(compute.ENDPOINTS["equity"][0], "analytics.py")
        self.assertEqual(compute.ENDPOINTS["risk"][0], "analytics.py")
        self.assertEqual(compute.ENDPOINTS["trades"][0], "analytics.py")

    def test_series_routes_to_bars_script(self):
        """series 不在 ENDPOINTS 映射里，走独立的 compute.series → bars.py。"""
        self.assertNotIn("series", compute.ENDPOINTS)
        self.assertTrue((PYTHON_DIR / "bars.py").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
