"""WP20 辅助用例：yfinance 必须在**每一条**安装路径上被声明。

缺陷 3 的背景（2026-09-18 全新安装演练）：`yfinance` 是数据层生产依赖
（`fundamentals` 的 Yahoo 财报备用源 + `market.fetch_yahoo` 港美股长历史通道），
而 `install.sh`/`install.ps1` 只装 akshare+playwright、`platform/requirements.txt`
里也没有——全新 venv 上 Python 套件报 `ModuleNotFoundError: No module named
'yfinance'`。缺它**不会报错**（静默降级），所以只能靠「声明是否写全」的测试兜住。

全部离线：只读安装脚本 / requirements / README 文本。
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DependencyDeclarationTest(unittest.TestCase):
    """yfinance 必须在**每一条**安装路径上被声明（漏了不会报错，只会静默降级）。"""

    def test_install_entrypoints_declare_yfinance(self):
        for name in ("install.sh", "install.ps1"):
            with self.subTest(script=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn("yfinance", text)
                self.assertIn("akshare playwright yfinance", text)

    def test_platform_requirements_pins_yfinance(self):
        raw = (ROOT / "platform" / "requirements.txt").read_text(encoding="utf-8").splitlines()
        lines = [line.strip() for line in raw]
        self.assertIn("yfinance==1.7.0", lines)
        index = lines.index("yfinance==1.7.0")
        comment = []
        for line in reversed(raw[:index]):      # 紧贴其上的连续注释块
            if not line.strip().startswith("#"):
                break
            comment.append(line)
        block = "\n".join(reversed(comment))
        self.assertIn("数据层", block, "必须说明它属于数据层、为何放在平台依赖里")
        self.assertIn("Yahoo", block, "必须写清用途（Yahoo 财报备用源 / 港美股长历史）")

    def test_readme_install_instructions_mention_yfinance(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        section = text[text.index("方式 C · 让 AI 帮你装"):text.index("## 一键启动")]
        self.assertIn("yfinance", section)
        self.assertIn("akshare", section)


if __name__ == "__main__":
    unittest.main(verbosity=2)
