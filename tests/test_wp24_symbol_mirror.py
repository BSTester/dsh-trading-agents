"""WP24 锁定测试：标的联想候选的跨语言镜像 + snapshot 关注池载荷。

本次改动把「代码 → 富途 MARKET.CODE」的归一规则**抄了一份到前端**
（``platform/web/lib/services/symbols.js``）：输入框要能在用户敲代码时立刻给出候选，
而候选值必须是带前缀的写法（``SH.600519``），所以前端必须本地归一。规则在后端已有唯一
实现 ``trading_datasource.market.to_futu_symbol``——抄一份就有漂移风险：后端改了 6 位数字
的首位分档（6/9→SH、4/8→BJ）、或改了港股补零位数，前端会继续按旧规则产出**看起来正常
但查不到**的候选（最坏情况：候选与用户手打的值指向不同标的）。

手法沿用 ``tests/test_wp10_locks.py``：**正则解析 JS 源码**，不相信「记得同步」。
本测试从 JS 里抠出四个常量（前缀集合、SH/BJ 首位数字、港股位数）与三条分支判据的存在性，
按这些**解析值**重建一份 JS 规则，再与后端 ``to_futu_symbol`` 在**同一张输入表**上逐个比对
结果——任何一侧改了规则，这张表先红。

第二半：``snapshot`` 载荷必须带 ``watchlist``（前端全局轮询它取关注池候选，不新增端点：
端点数 82 与 MCP 工具数 77 是锁定不变式，见 tests/test_wp6_store_access.py 与
tests/test_wp8_tool_locks.py）。
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 仓库副本优先于 venv 里的安装副本（与 tests/test_core_wp2_locks.py 同口径）：
# 后端规则的「唯一实现」是仓库里的 plugins/，不是安装器解出的那份。
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "platform"))

from trading_datasource.market import to_futu_symbol  # noqa: E402
from trading_core.watchlist import watchlist_symbols  # noqa: E402

SYMBOLS_JS = ROOT / "platform" / "web" / "lib" / "services" / "symbols.js"  # 共享契约源（随 UI 一起搬到 lib/）


def _js_source():
    return SYMBOLS_JS.read_text(encoding="utf-8")


def _function_body(source, name):
    """抠出 ``export function NAME(...) { ... }`` 的函数体（花括号配平）。"""
    match = re.search(rf"export\s+function\s+{name}\s*\([^)]*\)\s*\{{", source)
    if not match:
        raise AssertionError(f"未找到 {name} 的函数定义")
    start = match.end() - 1
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1:index]
    raise AssertionError(f"{name} 括号不配平")


def _string_array(source, name):
    match = re.search(rf"export\s+const\s+{name}\s*=\s*\[([^\]]*)\]", source)
    if not match:
        raise AssertionError(f"未找到常量 {name}")
    return re.findall(r"\"([^\"]*)\"", match.group(1))


def _string_const(source, name):
    match = re.search(rf"export\s+const\s+{name}\s*=\s*\"([^\"]*)\"", source)
    if not match:
        raise AssertionError(f"未找到常量 {name}")
    return match.group(1)


def _int_const(source, name):
    match = re.search(rf"export\s+const\s+{name}\s*=\s*(\d+)", source)
    if not match:
        raise AssertionError(f"未找到常量 {name}")
    return int(match.group(1))


#: 同一张输入表：五条分支逐条覆盖（含大小写/空白归一）。
#: 表值只给输入，期望值由两侧规则各自算出后比对——不在此处写死「正确答案」，
#: 否则改表就等于改答案，锁会退化成自证。
MIRROR_INPUTS = (
    "600519", "900901", "000001", "002475", "300750", "830799", "430047",
    "700", "0700", "00700", "1", "99999",
    "AAPL", "BRK.B", "SPY",
    "SH.600519", "hk.00700", "bj.830799", "us.aapl", "SH.000300",
    "600519.SH", "000001.sz", "aapl.us",
    " 600519 ",
)

#: 每条分支至少要有一个代表输入（防止有人把表删空后锁静默通过）。
BRANCH_SAMPLES = {
    "已带前缀": "SH.600519",
    "代码.市场换序": "600519.SH",
    "6 位纯数字": "600519",
    "1~5 位纯数字": "700",
    "兜底（其余按美股）": "AAPL",
}


class ToFutuSymbolMirrorTests(unittest.TestCase):
    """前端 ``toFutuSymbol`` 必须是后端 ``to_futu_symbol`` 的忠实镜像。"""

    def setUp(self):
        self.source = _js_source()
        self.body = _function_body(self.source, "toFutuSymbol")
        self.prefixes = _string_array(self.source, "FUTU_PREFIXES")
        self.sh_first = _string_const(self.source, "SH_FIRST_DIGITS")
        self.bj_first = _string_const(self.source, "BJ_FIRST_DIGITS")
        self.hk_length = _int_const(self.source, "HK_CODE_LENGTH")

    # ------------------------------------------------------------ 结构判据
    def test_every_branch_is_present_in_the_js(self):
        self.assertRegex(self.body, r"/\^\\d\{6\}\$/", "缺 6 位纯数字分支")
        self.assertRegex(self.body, r"/\^\\d\{1,5\}\$/", "缺 1~5 位纯数字（港股）分支")
        self.assertRegex(self.body, r"padStart\(\s*HK_CODE_LENGTH\s*,\s*\"0\"\s*\)",
                         "港股补零必须用 HK_CODE_LENGTH 常量")
        self.assertRegex(self.body, r"SH_FIRST_DIGITS\.includes\(")
        self.assertRegex(self.body, r"BJ_FIRST_DIGITS\.includes\(")
        self.assertRegex(self.body, r"return\s+`US\.\$\{", "缺兜底分支（其余按美股）")
        # 两个前缀分支都从 FUTU_PREFIXES 派生（前缀集合只有一份）
        self.assertIn("FUTU_PREFIXES", self.source)
        self.assertRegex(self.source, r"FUTU_PREFIXES\.join\(")

    def test_prefix_set_matches_the_python_regex(self):
        """前缀集合用**行为**反查后端：列出的原样通过，未列出的落进美股兜底。"""
        for prefix in self.prefixes:
            self.assertEqual(to_futu_symbol(f"{prefix}.TEST"), f"{prefix}.TEST",
                             f"{prefix} 不在后端 to_futu_symbol 的已带前缀分支里")
        for prefix in ("XX", "SZ2", "NASDAQ"):
            self.assertEqual(to_futu_symbol(f"{prefix}.TEST"), f"US.{prefix}.TEST",
                             f"{prefix} 不该被后端当成交易所前缀")

    def test_digit_split_matches_python_for_every_leading_digit(self):
        """6 位数字的首位分档逐个数字比对（前端常量 vs 后端行为）。"""
        for digit in "0123456789":
            code = digit + "00000"
            expected = (
                "SH" if digit in self.sh_first
                else "BJ" if digit in self.bj_first
                else "SZ")
            self.assertEqual(to_futu_symbol(code), f"{expected}.{code}",
                             f"首位 {digit} 的分档两侧不一致")

    def test_hk_padding_length_matches_python(self):
        for length in range(1, 6):
            code = "7" * length
            self.assertEqual(to_futu_symbol(code), "HK." + code.zfill(self.hk_length))

    # ------------------------------------------------------------ 同表比对
    def _js_rule(self, text):
        """按**从 JS 解析出的常量**重建前端规则（不是调用 Python 实现）。"""
        prefixes = "|".join(self.prefixes)
        value = str(text).strip().upper()
        if re.fullmatch(rf"^({prefixes})\.[A-Z0-9.]+$", value):
            return value
        swapped = re.fullmatch(rf"^([A-Z0-9.]+)\.({prefixes})$", value)
        if swapped:
            return f"{swapped.group(2)}.{swapped.group(1)}"
        if re.fullmatch(r"\d{6}", value):
            head = ("SH" if value[0] in self.sh_first
                    else "BJ" if value[0] in self.bj_first else "SZ")
            return f"{head}.{value}"
        if re.fullmatch(r"\d{1,5}", value):
            return f"HK.{value.zfill(self.hk_length)}"
        return f"US.{value}"

    def test_same_table_agrees_on_both_sides(self):
        for text in MIRROR_INPUTS:
            self.assertEqual(to_futu_symbol(text), self._js_rule(text),
                             f"{text!r} 两侧归一结果不一致")

    def test_frozen_expectations_for_the_known_cases(self):
        """表里的关键取值冻结一遍：两侧同时漂移（都改成错的）也要红。"""
        expected = {
            "600519": "SH.600519", "000001": "SZ.000001", "830799": "BJ.830799",
            "700": "HK.00700", "AAPL": "US.AAPL",
            "SH.600519": "SH.600519", "600519.SH": "SH.600519",
        }
        for text, value in expected.items():
            self.assertIn(text, MIRROR_INPUTS, f"输入表缺 {text!r}")
            self.assertEqual(to_futu_symbol(text), value)
            self.assertEqual(self._js_rule(text), value)

    def test_table_covers_every_branch(self):
        for label, sample in BRANCH_SAMPLES.items():
            self.assertIn(sample, MIRROR_INPUTS, f"输入表缺「{label}」的代表输入")


class SnapshotWatchlistTests(unittest.TestCase):
    """snapshot 载荷（前端全局轮询的那一份）必须带 watchlist，且来源是唯一实现。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def _snapshot_value(self, home=None):
        from server import app as app_module
        body = app_module.create_handler(str(home or self.home))("snapshot", {})
        self.assertTrue(body["ok"], body)
        return body["value"]

    def _write_config(self, payload):
        (self.home / "trading-platform.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_key_is_always_present_even_without_config(self):
        value = self._snapshot_value()
        self.assertIn("watchlist", value)
        self.assertEqual(value["watchlist"], [])

    def test_payload_equals_the_single_implementation(self):
        self._write_config({"watchlist": ["SH.600519", "hk.00700", "", "  ", "SZ.300750"]})
        value = self._snapshot_value()
        self.assertEqual(value["watchlist"], ["SH.600519", "HK.00700", "SZ.300750"])
        self.assertEqual(value["watchlist"], watchlist_symbols(self.home))

    def test_broken_config_degrades_to_empty_pool_not_an_error(self):
        """配置读不懂时按空池（daemon.platform_config 既有口径），关注池读取本身不抛。

        注意：这里直接调 ``compute.watchlist_symbols`` 而不是经 ``create_handler``——
        配置损坏时**服务装配本身**就会失败（futu_data/channel.py 的 channel_of 解析
        同一份文件并抛 ValueError），那是既有的 fail-closed 语义，与关注池无关。
        """
        from server import compute
        (self.home / "trading-platform.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(compute.watchlist_symbols(self.home), [])

    def test_watchlist_key_does_not_change_the_endpoint_manifest(self):
        """不新增端点：端点数不变式（82）由 tests/test_wp6_store_access.py 整表钉死。"""
        from server import store_access
        value = self._snapshot_value()
        self.assertEqual(value["endpoints"], store_access.endpoints())
        self.assertEqual(len(value["endpoints"]), 82)


if __name__ == "__main__":
    unittest.main()
