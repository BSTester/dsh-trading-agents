"""中文标签表的两份拷贝必须一致。

唯一事实来源是 `trading_datasource/labels.py`：

  * 产出侧（engine/backtest/analytics）用它给结论字段附中文标签；
  * `plugins/workbench/src/labels.js` 是 Host 侧镜像，用于翻译旧记录。

（历史上第三份拷贝是 `plugins/workbench/src/client.js` 里的浏览器侧回退表 `ZH`；
该文件已随 legacy 面板于 WP7 退役——独立 Web 前端直接复用 labels.py 的产出，
不再内置回退表——相应比对段随之删除。）

两份表一旦漂移，界面就会在旧记录上翻出英文，或者同一结论在不同页面
显示成不同中文。因此这里直接解析两份源码比对，而不是相信"记得同步"。
"""
import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from trading_datasource import labels as canonical  # noqa: E402

LABELS_JS = ROOT / "plugins" / "workbench" / "src" / "labels.js"

# labels.py 里所有"表"，以及它们对应的导出名
TABLES = ["SIGNAL", "ACTION", "SIDE", "TRADE_TYPE", "TRADE_STATUS", "RATING",
          "SOURCE_STATUS", "EXECUTION_TIMING", "END_POSITION_POLICY",
          "EXECUTION_SOURCE", "RISK_CONFIG", "METRIC", "GRID_AXIS", "STRATEGY"]


def parse_js_object(source, name):
    """从 JS 源码里抠出 `export const NAME = {...};` 或 `NAME: {...}` 的键值对。"""
    # 允许两种写法：export const X = { ... };
    match = re.search(rf"(?:export\s+const\s+{name}\s*=|{name}\s*:)\s*\{{", source)
    if not match:
        return None
    start = match.end() - 1
    depth, end = 0, -1
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return None
    body = source[start + 1:end]
    pairs = {}
    # 键可能是标识符（BUY）也可能是纯数字（1=Buy 2=Sell）
    for key, value in re.findall(r'(\d+|[A-Za-z_][A-Za-z0-9_]*)\s*:\s*"([^"]*)"', body):
        pairs[key] = value
    return pairs


def python_table(name):
    table = getattr(canonical, name)
    return {str(k): str(v) for k, v in table.items()}


class LabelParityTests(unittest.TestCase):
    def test_host_js_mirror_matches_python(self):
        source = LABELS_JS.read_text(encoding="utf-8")
        for name in TABLES:
            with self.subTest(table=name):
                parsed = parse_js_object(source, name)
                self.assertIsNotNone(parsed, f"labels.js 缺少 {name}")
                self.assertEqual(parsed, python_table(name),
                                 f"{name} 在 labels.py 与 labels.js 之间不一致")

    def test_every_conclusion_code_has_a_chinese_label(self):
        """结论枚举不能有漏网的英文值——漏了就说明界面还可能显示英文。"""
        for name in ("SIGNAL", "ACTION", "TRADE_TYPE", "TRADE_STATUS", "RATING", "SOURCE_STATUS"):
            for code, label in python_table(name).items():
                with self.subTest(table=name, code=code):
                    self.assertTrue(re.search(r"[\u4e00-\u9fff]", label),
                                    f"{name}.{code} 的标签不是中文：{label}")

    def test_unknown_value_is_returned_unchanged(self):
        """未知枚举原样返回，不静默吞掉——否则新枚举出现时没人发现。"""
        self.assertEqual(canonical.zh(canonical.ACTION, "WHAT"), "WHAT")
        self.assertIsNone(canonical.zh(canonical.ACTION, None))

    def test_lookup_is_case_insensitive(self):
        """历史记录里 BUY / buy / Buy 都出现过，必须都能翻。"""
        self.assertEqual(canonical.zh(canonical.ACTION, "buy"), "买入")
        self.assertEqual(canonical.zh(canonical.ACTION, "Buy"), "买入")
        self.assertEqual(canonical.zh(canonical.RATING, "overweight"), "增持")


if __name__ == "__main__":
    unittest.main()
