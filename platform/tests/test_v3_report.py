"""``server/v3_report.py`` 契约测试：研报 markdown→HTML、暗色专业主题、封面页、PDF 渲染。

覆盖：
  * markdown 子集转换（标题/列表/表格/引用/代码/行内样式）与 **HTML 转义**（不注入）；
  * 封面页**独占一页**（``page-break-after: always``）与关键元信息（标的/评级/来源数/免责声明）；
  * 页面与 PDF **主题 token 一致**（``platform/web-pro/index.html`` 的 ``.report-doc`` ↔ 本模块 ``REPORT_CSS``）；
  * PDF 渲染冒烟：有 chromium 时产出的字节以 ``%PDF`` 开头且体积合理；无浏览器时返回明确错误码。
"""
import pathlib
import sys
import unittest

PLATFORM = str(pathlib.Path(__file__).resolve().parents[1])
REPO = pathlib.Path(PLATFORM).parent
if PLATFORM not in sys.path:
    sys.path.insert(0, PLATFORM)

from server import v3_report  # noqa: E402

SAMPLE = {
    "id": "run-1",
    "ticker": "SH.600000",
    "mode": "sim",
    "rating": "Overweight",
    "rating_label": "增持",
    "published_at": "2026-09-19T02:33:26.105Z",
    "report": (
        "# 结论\n\n**增持**，目标区间 9.5–10.2。\n\n"
        "## 论据\n\n- 资产质量改善\n- 息差企稳\n\n"
        "| 指标 | 数值 |\n| --- | --- |\n| 营收 | 1,234 亿 |\n\n"
        "> 风险：政策与利率波动。\n\n```\nraw <script>alert(1)</script>\n```\n"
    ),
    "sources": [
        {"name": "futu/quote_history_kline", "as_of": "2026-09-18", "reference": "series"},
        {"name": "akshare/stock_news_em", "as_of": "2026-09-19", "reference": "https://example.com/n"},
    ],
}


class MarkdownTests(unittest.TestCase):
    def test_headings_lists_tables_quotes_code(self):
        html = v3_report.markdown_to_html(SAMPLE["report"])
        self.assertIn("<h1>结论</h1>", html)
        self.assertIn("<h2>论据</h2>", html)
        self.assertIn("<strong>增持</strong>", html)
        self.assertIn("<ul><li>资产质量改善</li><li>息差企稳</li></ul>", html)
        self.assertIn("<table><thead><tr><th>指标</th><th>数值</th></tr></thead>"
                      "<tbody><tr><td>营收</td><td>1,234 亿</td></tr></tbody></table>", html)
        self.assertIn("<blockquote>", html)
        self.assertIn("<pre><code>", html)

    def test_html_is_escaped(self):
        html = v3_report.markdown_to_html("段落 <img src=x onerror=alert(1)>\n\n```\n<b>x</b>\n```")
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)
        self.assertIn("&lt;b&gt;", html)

    def test_ordered_list_and_inline_code(self):
        html = v3_report.markdown_to_html("1. 第一\n2. 第二\n\n行内 `code` 与链接 [x](https://a.example)")
        self.assertIn("<ol><li>第一</li><li>第二</li></ol>", html)
        self.assertIn("<code>code</code>", html)
        self.assertIn('<a href="https://a.example">x</a>', html)

    def test_unknown_syntax_kept_as_text(self):
        html = v3_report.markdown_to_html("::: 未支持的块 :::\n\n普通段落")
        self.assertIn("<p>", html)
        self.assertIn("未支持的块", html)


class ReportHtmlTests(unittest.TestCase):
    def setUp(self):
        self.html = v3_report.report_html(SAMPLE, mode="sim", generated_at="2026-09-20T10:00:00+08:00")

    def test_cover_page_is_own_page(self):
        self.assertIn('class="cover"', self.html)
        self.assertIn("page-break-after: always", v3_report.REPORT_CSS)

    def test_cover_metadata_and_disclaimer(self):
        for token in ["SH.600000 研究报告", "增持", "研究 run", "run-1", "来源项数", "免责声明", "量化决策平台 V3"]:
            self.assertIn(token, self.html)
        self.assertIn("来源与数据时间（共 2 项）", self.html)
        self.assertIn("futu/quote_history_kline", self.html)

    def test_body_is_dark_renderable_markdown(self):
        self.assertIn('class="report-doc"', self.html)
        self.assertIn("<h1>结论</h1>", self.html)
        self.assertIn("print-color-adjust: exact", v3_report.REPORT_CSS)

    def test_missing_sources_are_stated_not_faked(self):
        html = v3_report.report_html({**SAMPLE, "sources": []})
        self.assertIn("本报告未附来源", html)


class ThemeParityTests(unittest.TestCase):
    """页面主题与 PDF 主题必须同源：token 一致才谈得上「页面与 PDF 观感一致」。"""

    def setUp(self):
        self.page = (REPO / "platform" / "web-pro" / "index.html").read_text(encoding="utf-8")

    def test_same_design_tokens(self):
        for name, value in v3_report.TOKENS.items():
            if name == "mono":
                continue
            with self.subTest(token=f"--r-{name}"):
                self.assertIn(value, v3_report.REPORT_CSS)
                self.assertIn(value, self.page, f"页面主题缺少 token {value}")

    def test_page_theme_has_same_structural_rules(self):
        for rule in [".report-doc h2", ".report-doc blockquote", ".report-doc table", "@media print"]:
            self.assertIn(rule, self.page)


class PdfRenderTests(unittest.TestCase):
    def test_render_pdf_smoke(self):
        binary = v3_report.chromium_bin()
        if not binary:
            data, meta = v3_report.render_pdf("<html><body>x</body></html>")
            self.assertIsNone(data)
            self.assertEqual(meta["code"], "pdf/no-browser")
            self.skipTest("环境无 chromium：已覆盖 no-browser 分支")
        html = v3_report.report_html(SAMPLE, mode="sim", generated_at="2026-09-20T10:00:00+08:00")
        data, meta = v3_report.render_pdf(html, timeout=120)
        self.assertIsNotNone(data, meta)
        self.assertTrue(data.startswith(b"%PDF"), "产出必须以 %PDF 开头")
        self.assertGreater(len(data), 15000, "PDF 体积过小，可能未渲染出内容")
        self.assertIn("bytes", meta)


if __name__ == "__main__":
    unittest.main()
