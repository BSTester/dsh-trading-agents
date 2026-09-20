"""WP25 期权筛选表单化（2026-09-18）：市场类别 7 项**三处同源**的锁。

用户需求原文：「期权的筛选，能改成选项或输入框吗？而不是这种 json 格式的，
{"market_category_list": [1]}」——表单化的市场类别下拉只能放**真机验证过**的 7 个值：
非支持值会被上游**静默忽略**（回空列表 + total=0，见 `docs/TOOL-LIMITS.md`），
放自由输入等于给用户一个「看起来成功、其实什么都没筛」的陷阱。

三处来源，任何一处漂移都要在这里炸出来（手法沿用 ``tests/test_wp10_locks.py``：
解析源码比对，不相信「记得同步」）：

  1. ``platform/js/services/optionScreen.js`` 的 ``OPTION_MARKET_CATEGORIES``
     ——表单单选/多选的选项面；
  2. ``platform/server/futu_data.py`` 的 ``OPTION_MARKET_CATEGORIES``
     ——服务端错误消息与 MCP 工具描述引用的事实源；
  3. ``docs/TOOL-LIMITS.md`` 的真机清单——人读的口径。

本锁**只**管市场类别码；``indicator_type``（仅验证过 1003）与 ``field_filter``
字段名（仅验证过 3 个）有意不编造完整枚举，前端按「已验证值预填 + 允许自由输入」
处理（见 services/optionScreen.js 的注释），因此不在此处锁成封闭集合。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))

from server import futu_data, mcp_tools  # noqa: E402

OPTION_SCREEN_JS = ROOT / "platform" / "js" / "services" / "optionScreen.js"  # 共享契约源
TOOL_LIMITS = ROOT / "docs" / "TOOL-LIMITS.md"


def _array_body(source, name):
    """抠出 ``export const NAME = [ ... ]`` 的方括号体（配平；只看数组字面量）。"""
    match = re.search(rf"export\s+const\s+{name}\s*=\s*\[", source)
    if not match:
        raise AssertionError(f"未找到 {name} 的数组字面量")
    start = match.end() - 1
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "[":
            depth += 1
        elif source[index] == "]":
            depth -= 1
            if depth == 0:
                return source[start + 1:index]
    raise AssertionError(f"{name} 方括号不配平")


def js_market_categories():
    """JS 侧 7 项 ``{value, label}`` → ``{码: label}``（value 必须是数字字面量）。"""
    body = _array_body(OPTION_SCREEN_JS.read_text(encoding="utf-8"),
                       "OPTION_MARKET_CATEGORIES")
    rows = re.findall(r"\{\s*value:\s*(\d+)\s*,\s*label:\s*\"([^\"]+)\"\s*\}", body)
    if not rows:
        raise AssertionError("OPTION_MARKET_CATEGORIES 里没有解析到 {value, label} 项")
    return {int(value): label for value, label in rows}


def docs_market_categories():
    """``docs/TOOL-LIMITS.md`` 里 `` `0`=US_STOCK / `1`=US_INDEX ... `` 的清单。

    只扫 ``market_category_list`` 那一条**带类别码**的项目符号（清单可以换行、同一文件别处
    也提过这个键名但没有码），不全文捞 ``N=CODE``——文档别处再出现同类写法时不该算成漂移。
    """
    lines = TOOL_LIMITS.read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if "`market_category_list`" in line]
    for start in starts:
        found = {}
        for line in lines[start:start + 8]:
            pairs = re.findall(r"`(\d+)`=([A-Z][A-Z_]*)", line)
            if not pairs and found:
                break
            found.update((int(value), code) for value, code in pairs)
        if found:
            return found
    raise AssertionError("docs/TOOL-LIMITS.md 找不到 market_category_list 的真机清单条目")


class MarketCategoryLockTests(unittest.TestCase):
    """7 个类别码：JS 表单 ↔ Python 常量 ↔ 文档清单，双向一致。"""

    def setUp(self):
        self.py = dict(futu_data.OPTION_MARKET_CATEGORIES)
        self.js = js_market_categories()
        self.docs = docs_market_categories()

    def test_python_constant_is_the_seven_verified_codes(self):
        self.assertEqual(sorted(self.py), [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(len(set(self.py.values())), 7, "英文码不得重复")
        for code in self.py.values():
            self.assertRegex(code, r"^[A-Z][A-Z_]*$")

    def test_js_options_match_python_codes_bidirectionally(self):
        self.assertEqual(sorted(self.js), sorted(self.py), "JS 表单选项面与 Python 常量漂移")
        for value, code in self.py.items():
            self.assertIn(code, self.js[value], f"JS label 应带上游英文码 {code}")

    def test_docs_list_matches_python_constant(self):
        self.assertEqual(self.docs, self.py, "docs/TOOL-LIMITS.md 的真机清单与常量漂移")

    def test_scanners_actually_find_something(self):
        """反证扫描器有效：如果两处正则都没命中，上面的相等断言就是空跑。"""
        self.assertTrue(self.js, "JS 扫描器没命中任何市场类别")
        self.assertTrue(self.docs, "文档扫描器没命中任何市场类别")

    def test_array_body_scanner_rejects_unknown_name(self):
        with self.assertRaises(AssertionError):
            _array_body("export const A = [];", "NOT_THERE")


class ToolDescriptionLockTests(unittest.TestCase):
    """工具描述必须**引用**常量（不再写死 0/1/3 三个），否则模型看到的就是错的枚举。"""

    def setUp(self):
        self.description = next(t.description for t in mcp_tools.TOOLS
                                if t.name == "option_screen")

    def test_description_lists_all_seven_codes(self):
        expected = "/".join(f"{value}={code}"
                            for value, code in futu_data.OPTION_MARKET_CATEGORIES)
        self.assertIn(expected, self.description,
                      "工具描述应直接引用 futu_data.OPTION_MARKET_CATEGORIES 的清单文本")

    def test_description_keeps_the_other_verified_anchors(self):
        # 既有回归钉（tests/test_e2e_defect_fixes.py::test_tool_description_contains_the_example）
        for anchor in ("market_category_list", "indicator_type", "field_filter"):
            self.assertIn(anchor, self.description)

    def test_old_three_code_claim_is_gone(self):
        self.assertNotIn("0=US_STOCK/1=US_INDEX/3=HK_STOCK", self.description,
                         "旧描述只列了 3 个类别码，与 docs/TOOL-LIMITS.md 的 7 个不一致")


if __name__ == "__main__":
    unittest.main()
