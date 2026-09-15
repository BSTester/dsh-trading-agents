"""WP6 表锁定（补遗 C）：Python 侧常量与 JS 移植源逐项比对。

为什么解析 JS 源而不是再抄一份期望值：这些表是「单一事实来源」，两边各写一份就必然漂移。
本测试把 ``plugins/workbench/src`` 里的 JS 字面量提取出来，与 Python 常量逐项比对——
JS 源被改动而 Python 没跟上时立刻红。Python 侧一律直接 import 后比对常量（不重复解析）。

**迁移说明（任务 E）**：JS 服务层已退役（platform/server 下的 Node 服务源已删除）；本测试
锁定的是 workbench legacy 面板源（``plugins/workbench/src/*.js``，仍保留），其删除另行决策。
届时本文件应当**二选一**：
  1. 改成纯 Python 断言——把下面 ``js_*`` 提取出来的值内联成期望常量（等于把 JS 的
     当前事实冻结在测试里），或
  2. 整体删除——若那时已有更权威的单一事实来源（例如 Python 表本身就是唯一来源）。
两条路都不需要改 ``caches.py``/``app.py``/``compute.py`` 的表；只要别让 JS 文件消失后
本文件因 ``FileNotFoundError`` 变成整套测试的红。

覆盖：
  * rpc.js ``CACHE_TTL_MS`` ↔ ``caches.CACHE_TTL_MS``（业务确认两端点**都不在**表里 = TTL 0；
    WP7 起服务自有端点不回写 legacy JS，按「JS 表 + WP7 增量 ≡ Python 表」比对）；
  * endpoints.js ``ENDPOINT_SHAPE`` ↔ ``caches.ENDPOINT_SHAPE``（含 ``confirmation: ["pending"]``
    与 WP7 增量 ``factors-history: ["snapshots"]``）；
  * endpoints.js ``ENDPOINTS`` + ``store_access.WP7_ENDPOINTS`` ↔ ``store_access.endpoints()``
    （22 legacy + WP7 增量，含 confirmation/confirm-decide）；
  * rpc.js 各端点 allowed 字段表 ↔ ``app.ANALYTICS_ENDPOINTS`` / ``SWITCH_MODE_FIELDS``
    / ``CONFIRM_DECIDE_FIELDS`` / ``PLAN_EXECUTE_FIELDS`` / ``SERIES_FIELDS``
    / ``EMPTY_PAYLOAD_ENDPOINTS``；
  * analytics.js + series.js 的内层缓存 TTL ↔ ``compute.INNER_CACHE_TTL_MS``；
  * analytics.js 的逐端点 timeout ↔ ``compute.ENDPOINT_TIMEOUT_MS`` / ``compute.TIMEOUT``。
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

SRC = ROOT / "plugins" / "workbench" / "src"
# WP7 起服务自有端点不回写 legacy 面板源（三张表的比对基准 = JS 字面量 + 本增量）。
WP7_ENDPOINTS = store_access.WP7_ENDPOINTS


def without_wp7(table):
    """Python 表去掉 WP7 增量后的 legacy 部分（与 JS 字面量逐项比对用）。"""
    return {name: value for name, value in table.items() if name not in WP7_ENDPOINTS}


def read(name):
    return (SRC / name).read_text(encoding="utf-8")


RPC_JS = read("rpc.js")
ENDPOINTS_JS = read("endpoints.js")
ANALYTICS_JS = read("analytics.js")
SERIES_JS = read("series.js")


def js_strings(block):
    """JS 字符串数组字面量的元素：``["a", "b"]`` → ``["a", "b"]``。"""
    return re.findall(r'"([^"]*)"', block)


def js_number(expr):
    """把 JS 数字字面量（可含 ``_`` 与 ``*``）算成 int：``10 * 60_000`` → 600000。"""
    cleaned = expr.replace("_", "").strip()
    if not re.fullmatch(r"[0-9]+(?:\s*\*\s*[0-9]+)*", cleaned):
        raise AssertionError(f"不是可解析的 JS 数字字面量：{expr!r}")
    result = 1
    for part in cleaned.split("*"):
        result *= int(part.strip())
    return result


def js_cache_ttls(text):
    """rpc.js:11-32 的 ``export const CACHE_TTL_MS = {...}`` → ``{endpoint: ms}``。"""
    block = re.search(r"export const CACHE_TTL_MS = \{(.*?)\n\};", text, re.S)
    assert block, "rpc.js 里找不到 CACHE_TTL_MS"
    return {name: js_number(expr)
            for name, expr in re.findall(r"^\s*([A-Za-z][\w-]*):\s*([0-9][0-9_ *]*),",
                                         block.group(1), re.M)}


def js_shape_table(text):
    """endpoints.js:42-60 的 ``ENDPOINT_SHAPE`` → ``{endpoint: [fields]}``（先剥注释）。"""
    block = re.search(r"export const ENDPOINT_SHAPE = \{(.*?)\n\};", text, re.S)
    assert block, "endpoints.js 里找不到 ENDPOINT_SHAPE"
    body = re.sub(r"//[^\n]*", "", block.group(1))
    return {name: js_strings(arr)
            for name, arr in re.findall(r"(\w+):\s*(\[[^\]]*\])", body)}


def js_endpoint_list(text):
    """endpoints.js:12-37 的 ``ENDPOINTS`` 数组（先剥 ``//`` 行注释）。

    必须剥注释：数组内的业务确认注释里带 ASCII 双引号（``"待确认"``），不剥会被字符串正则
    误当成第 23 个端点（``store_access.endpoints()`` 侧同样剥注释，两边口径一致）。
    """
    block = re.search(r"export const ENDPOINTS = \[(.*?)\];", text, re.S)
    assert block, "endpoints.js 里找不到 ENDPOINTS"
    return js_strings(re.sub(r"//[^\n]*", "", block.group(1)))


def js_allowed_fields(text):
    """rpc.js 的逐端点 allowed 字段表 → ``{endpoint: [fields]}``。

    * equity/positions 与 correlation 走同一行的三元（rpc.js:115）；
    * sensitivity/trades/events/factors/ic/sources/instrument/quality 是 147-155 的链；
    * risk 落在链尾的 ``: []``。
    """
    allowed = {}
    branch = re.search(r'endpoint === "correlation"\s*\?\s*(\[[^\]]*\])\s*:\s*(\[[^\]]*\])', text)
    assert branch, "rpc.js 里找不到 equity/positions/correlation 的 allowed 表"
    allowed["correlation"] = js_strings(branch.group(1))
    allowed["equity"] = allowed["positions"] = js_strings(branch.group(2))
    for name, arr in re.findall(r'endpoint === "([a-z-]+)"\s*\?\s*(\[[^\]]*\])', text):
        allowed[name] = js_strings(arr)
    allowed.setdefault("risk", [])
    return allowed


def js_guard_lists(text):
    """rpc.js 里所有 ``![...].includes(key)`` 的白名单数组（按出现顺序）。"""
    return [js_strings(block)
            for block in re.findall(r"!\[([^\]]*)\]\.includes\(key\)", text)]


class CacheTtlLockTests(unittest.TestCase):
    def test_cache_ttl_table_matches_rpc_js(self):
        self.assertEqual(js_cache_ttls(RPC_JS), without_wp7(dict(caches.CACHE_TTL_MS)))
        self.assertEqual(js_cache_ttls(RPC_JS)["instrument"], 10 * 60_000)
        # WP7 增量逐项钉死：JS 表不含、Python 表含且值锁定
        self.assertEqual(caches.CACHE_TTL_MS.get("factors-history"), 5 * 60_000)

    def test_business_confirmation_endpoints_are_not_cached(self):
        """业务确认两端点 TTL 恒为 0：rpc.js 的 CACHE_TTL_MS 本就不含它们，Python 侧同样不加。

        缓存住「待确认」会让界面拿到已经处理掉的请求（rpc.js:116 的注释），因此这里既比对 JS
        源（``test_cache_ttl_table_matches_rpc_js`` 已逐项相等），也显式钉死「不在表里」。
        """
        for endpoint in ("confirmation", "confirm-decide"):
            self.assertNotIn(endpoint, js_cache_ttls(RPC_JS), endpoint)
            self.assertNotIn(endpoint, caches.CACHE_TTL_MS, endpoint)
            self.assertEqual(caches.CACHE_TTL_MS.get(endpoint, 0), 0, endpoint)


class EndpointTableLockTests(unittest.TestCase):
    def test_shape_table_matches_endpoints_js(self):
        self.assertEqual(js_shape_table(ENDPOINTS_JS), without_wp7(dict(caches.ENDPOINT_SHAPE)))
        self.assertEqual(caches.ENDPOINT_SHAPE["confirmation"], ["pending"])
        self.assertEqual(caches.ENDPOINT_SHAPE["factors-history"], ["snapshots"])

    def test_endpoint_list_matches_endpoints_js_plus_wp7_delta(self):
        """legacy JS 清单（22）+ WP7 服务自有端点 ≡ store_access.endpoints()（29）。"""
        endpoints = js_endpoint_list(ENDPOINTS_JS)
        self.assertEqual(len(endpoints), 22)
        self.assertEqual(endpoints[-2:], ["confirmation", "confirm-decide"])
        self.assertEqual(store_access.endpoints(), endpoints + list(WP7_ENDPOINTS))
        self.assertEqual(len(store_access.endpoints()), 22 + len(store_access.WP7_ENDPOINTS))
        self.assertEqual(len(store_access.endpoints()), 29)
        # WP7 任务 3 增量逐项钉死（写三个 + 读三个，与 mcp_tools.ENDPOINT_TOOL_ENDPOINTS 对齐）
        self.assertEqual(list(store_access.WP7_ENDPOINTS),
                         ["factors-history", "trade_place", "trade_modify", "trade_cancel",
                          "account_positions", "account_orders", "account_funds"])

    def test_analytics_endpoints_are_declared_by_endpoints_js(self):
        self.assertTrue(set(app_module.ANALYTICS_ENDPOINTS) <= set(store_access.endpoints()))


class WhitelistLockTests(unittest.TestCase):
    def test_analytics_allowed_fields_match_rpc_js(self):
        expected = {name: list(fields)
                    for name, fields in app_module.ANALYTICS_ENDPOINTS.items()}
        self.assertEqual(js_allowed_fields(RPC_JS), expected)

    def test_guard_lists_match_python_field_tables(self):
        guards = js_guard_lists(RPC_JS)
        # guards[0] 是 platform/server/rpc fetch 的 RPC 信封白名单（与端点无关）
        self.assertEqual(guards[0], ["type", "rpcId", "method", "payload"])
        self.assertIn(list(app_module.SWITCH_MODE_FIELDS), guards)
        self.assertIn(list(app_module.CONFIRM_DECIDE_FIELDS), guards)
        self.assertIn(list(app_module.PLAN_EXECUTE_FIELDS), guards)
        self.assertIn(list(app_module.SERIES_FIELDS), guards)
        # 确认通道的载荷只有 id/decision：白名单外字段在 handle 层就被拒（不能变成下单通道）
        self.assertEqual(list(app_module.CONFIRM_DECIDE_FIELDS), ["id", "decision"])

    def test_empty_payload_endpoints_match_rpc_js(self):
        named = set(re.findall(r'WorkbenchError\("([a-z-]+) takes no payload"\)', RPC_JS))
        literal = ('if (endpoint === "plan" || endpoint === "schedule" '
                   '|| endpoint === "reconcile") {')
        start = RPC_JS.find(literal)
        self.assertGreaterEqual(start, 0, "rpc.js 的 plan/schedule/reconcile 空载荷分支变了形")
        window = RPC_JS[start:RPC_JS.find("takes no payload`", start)]
        templated = set(re.findall(r'endpoint === "([a-z-]+)"', window))
        self.assertEqual(named | templated | {"snapshot"},
                         set(app_module.EMPTY_PAYLOAD_ENDPOINTS))
        self.assertIn("confirmation", app_module.EMPTY_PAYLOAD_ENDPOINTS)
        self.assertIn('endpoint === "snapshot" && Object.keys(payload).length === 0', RPC_JS)


class ComputeTableLockTests(unittest.TestCase):
    def test_inner_cache_ttl_matches_analytics_and_series_js(self):
        for text in (ANALYTICS_JS, SERIES_JS):
            found = re.search(r"const CACHE_TTL_MS = ([0-9_]+);", text)
            self.assertIsNotNone(found, "找不到 JS 侧内层缓存 TTL")
            self.assertEqual(compute.INNER_CACHE_TTL_MS, js_number(found.group(1)))

    def test_instrument_timeout_matches_analytics_js(self):
        # analytics.js:169-170 的 instruments.py 调用写死 120_000
        found = re.search(r'instruments\.py"\), "--ticker", ticker\],\s*'
                          r"\{ timeout: ([0-9_]+),", ANALYTICS_JS)
        self.assertIsNotNone(found, "analytics.js:169-170 的 instrument timeout 变了形")
        self.assertEqual(compute.ENDPOINT_TIMEOUT_MS["instrument"], js_number(found.group(1)))
        self.assertEqual(compute.timeout_for("instrument"), 120_000)
        self.assertEqual(compute.timeout_for("equity"), 180_000)
        # 其余端点共用 analytics.js:67 的默认 180s；series.js:37 也是 120s（compute.series 内联）
        self.assertIn("{ timeout: 180_000,", ANALYTICS_JS)
        self.assertEqual(compute.TIMEOUT, 180_000)
        self.assertIn("timeout: 120_000", SERIES_JS)

    def test_endpoint_script_map_is_consistent(self):
        """12 个分析端点都有 (脚本, 参数构造, 源行号)，且脚本名出现在 analytics.js 里。"""
        self.assertEqual(set(compute.ENDPOINTS), set(app_module.ANALYTICS_ENDPOINTS))
        for name, (script, build, source) in compute.ENDPOINTS.items():
            self.assertTrue(script.endswith(".py"), name)
            self.assertTrue(callable(build), name)
            self.assertTrue(source.startswith("analytics.js:"), name)
            self.assertIn(script, ANALYTICS_JS, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
