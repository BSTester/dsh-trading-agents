"""WP10 锁定测试（2026-09-16 代码质量审查 N1/N2/N3）：三处「两侧各写一份」的镜像必须一致。

审查实证的三类静默退化：

  * **N1** —— 前端 ``services/pipeline.js`` 镜像 auto_pipeline 缺省值（exec_at 时刻表、
    窗口 30 分钟、对账 19:00）。core 改了默认而前端没跟，首屏占位与服务端有效配置不一致；
  * **N2** —— 设置页 InputNumber 的 max 与 core 上界：页面上限更宽会让「页面允许填的值
    被服务端拒绝」，更窄则用户填不到合法值；
  * **N3** —— ``pipeline._ALERT_STATUS`` 把告警**标题字面量**映射到阶段状态。emit 点改
    标题而表没跟，阶段就静默退回 ``pending``（页面显示「待运行」，真因丢失）。

手法沿用 ``tests/test_labels.py``：**解析源码比对**，不相信「记得同步」。N1/N2 比 Python
常量与 JS 字面量；N3 反查每个标题字面量的 emit 点是否仍然存在。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import autopipeline, pipeline  # noqa: E402

PIPELINE_JS = ROOT / "platform" / "js" / "services" / "pipeline.js"   # 纯逻辑契约源（设计稿版控制台不执行它，测试只解析文本）
CORE_DIR = ROOT / "plugins" / "core" / "python" / "trading_core"
V3_SETTINGS_JS = ROOT / "platform" / "web-pro" / "src" / "pages" / "settings.jsx"  # V3 工作台（Ant Design Pro）设置页


def _js_source():
    return PIPELINE_JS.read_text(encoding="utf-8")


def _object_body(source, name):
    """抠出 `export const NAME = { ... }` 或嵌套键 `NAME: { ... }` 的括号体（花括号配平）。"""
    match = re.search(rf"(?:export\s+const\s+{name}\s*=|{name}\s*:)\s*\{{", source)
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
    raise AssertionError(f"{name} 括号不配平")


class FrontendMirrorTests(unittest.TestCase):
    """N1：JS 缺省值镜像必须等于 core 的 AUTO_PIPELINE_DEFAULTS。"""

    def setUp(self):
        self.source = _js_source()
        self.body = _object_body(self.source, "AUTO_PIPELINE_DEFAULTS")

    def test_scalar_defaults_match(self):
        self.assertRegex(self.body, r"enabled:\s*false")
        self.assertRegex(self.body, r"strategies:\s*\[\s*\]")
        self.assertEqual(
            int(re.search(r"exec_window_minutes:\s*(\d+)", self.body).group(1)),
            autopipeline.AUTO_PIPELINE_DEFAULTS["exec_window_minutes"])
        self.assertEqual(
            re.search(r'reconcile_at:\s*"([^"]+)"', self.body).group(1),
            autopipeline.AUTO_PIPELINE_DEFAULTS["reconcile_at"])

    def test_exec_at_table_matches(self):
        exec_at = _object_body(self.body, "exec_at")
        parsed = dict(re.findall(r"([A-Z]{2}):\s*\"([^\"]+)\"", exec_at))
        self.assertEqual(parsed, autopipeline.AUTO_PIPELINE_DEFAULTS["exec_at"])

    def test_exec_window_max_matches_core(self):
        """N2：设置页上限与 core 上界同源（页面允许 = 服务端接受）。"""
        match = re.search(r"export\s+const\s+EXEC_WINDOW_MAX_MINUTES\s*=\s*(\d+)",
                          self.source)
        self.assertIsNotNone(match, "services/pipeline.js 缺 EXEC_WINDOW_MAX_MINUTES 镜像")
        self.assertEqual(int(match.group(1)), autopipeline.EXEC_WINDOW_MAX_MINUTES)

    def test_settings_page_window_bound_matches_core(self):
        """设置页（V3 工作台）里的执行窗口上限常量必须与 core 一致。

        现行工作台是独立 ESM，不 import core 常量，而是在页内声明镜像常量
        （当前实现：`const EXEC_WINDOW_MAX = 240;`）。本断言只钉住这个会漂移的常量：
        一旦页面改了上限而 core 没改（或反之），这里就红。
        页面若改为完全交由服务端校验（不声明常量），断言自动跳过（等价可接受）。
        """
        source = V3_SETTINGS_JS.read_text(encoding="utf-8")
        bounds = {int(m) for m in re.findall(r"EXEC_WINDOW_MAX\s*=\s*(\d+)", source)}
        if not bounds:
            self.skipTest("设置页未声明本地窗口上限常量（交由服务端校验，等价可接受）")
        for bound in bounds:
            self.assertEqual(bound, autopipeline.EXEC_WINDOW_MAX_MINUTES,
                             f"设置页窗口上限 {bound} 与 core 常量不一致")


class AlertTitleLockTests(unittest.TestCase):
    """N3：表里的每个标题字面量都必须仍有 emit 点（标题改了要同步改表）。

    判定：该字面量出现在**除 pipeline.py 之外**的 core 模块里，且该文件有 ``emit(``
    调用（planner/autopilot 的局部 ``skip()`` 助手最终落到 ``alerts.emit``，故按
    文件级判定而不是按 `title=` 参数形状——两种写法都是真实 emit 点）。
    """

    def emit_sources(self):
        sources = {}
        for path in sorted(CORE_DIR.glob("*.py")):
            if path.name == "pipeline.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "emit(" in text:
                sources[path.name] = text
        return sources

    def test_every_stage_alert_title_has_an_emit_site(self):
        sources = self.emit_sources()
        titles = list(pipeline._ALERT_STATUS) + list(pipeline._CHAIN_ALERT_STATUS) \
            + list(pipeline._CONFIG_ALERT_STATUS) + list(pipeline._CHAIN_NOTICE_ALERT_TITLES)
        missing = [title for title in titles
                   if not any(f'"{title}"' in text for text in sources.values())]
        self.assertEqual(missing, [], f"这些标题在 core 已无 emit 点：{missing}")

    def test_emit_scan_actually_finds_the_known_emitters(self):
        """反证扫描器本身有效：已知 emit 文件必须在集合里（否则上面的用例是空跑）。"""
        sources = self.emit_sources()
        self.assertIn("planner.py", sources)
        self.assertIn("autopilot.py", sources)
        self.assertIn("reconcile.py", sources)
        self.assertIn("daemon.py", sources)


if __name__ == "__main__":
    unittest.main()
