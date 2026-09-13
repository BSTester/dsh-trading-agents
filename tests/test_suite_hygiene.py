"""测试套件自身的卫生检查。

起因：`test_quant.py` 的 `if __name__ == "__main__"` 块写在文件中间，其后又追加了
9 个测试类/方法。模块自上而下执行，`unittest.main()` 先跑完，后面的定义**根本
不存在**——那 9 个测试从未执行过，而 `Ran 32 tests ... OK` 看起来一切正常。

这类"静默不执行"比测试失败更危险：它给出的是虚假的安全感。所以这里直接扫源码，
不允许任何测试定义出现在 `__main__` 块之后。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
DEFINITION = re.compile(r"^(class\s+\w+|def\s+test_\w+|    def\s+test_\w+)")


class SuiteHygieneTests(unittest.TestCase):
    def test_no_test_definition_after_main_block(self):
        offenders = []
        for path in sorted(TESTS.glob("test_*.py")):
            lines = path.read_text(encoding="utf-8").split("\n")
            starts = [i for i, line in enumerate(lines) if line.startswith("if __name__")]
            if not starts:
                continue
            for index in range(starts[0] + 1, len(lines)):
                if DEFINITION.match(lines[index]):
                    offenders.append(f"{path.name}:L{index + 1} {lines[index].strip()[:50]}")
        self.assertEqual(offenders, [],
                         "这些测试定义在 __main__ 块之后，永远不会被执行：\n  "
                         + "\n  ".join(offenders))

    def test_every_test_file_has_a_main_block(self):
        """没有 __main__ 块的文件只能靠 discover 跑到；本仓库统一用直接执行。"""
        missing = [path.name for path in sorted(TESTS.glob("test_*.py"))
                   if "if __name__" not in path.read_text(encoding="utf-8")]
        self.assertEqual(missing, [], f"这些测试文件缺少 __main__ 块：{missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
