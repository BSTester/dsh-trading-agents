"""研报 PDF 渲染（服务端）：markdown → HTML → 封面页 + 暗色专业研报主题 → chromium 打印为 PDF。

为什么服务端渲染：中文研报要「一键下载、样式可控、带独占封面页」，浏览器端要么需要内嵌中文字体
（十余 MB），要么只能走打印对话框。服务端用既有的 headless chromium 直接产出 PDF，正文仍是**可选中的
文字**，且主题与页面版式同源。

主题一致性：本文件的 ``REPORT_CSS`` 与前端 ``platform/web-pro/index.html`` 里的 ``.report-doc`` 样式
使用同一组设计 token（bg/panel/border/text/accent）；``tests/test_v3_report.py`` 会断言两边 token 一致，
避免「页面看一套、导出另一套」。
"""
import html
import os
import re
import shutil
import subprocess
import tempfile

# ── 设计 token（与页面 .report-doc 同源）──────────────────────────────────────
TOKENS = {
    "bg": "#0b0e13",
    "panel": "#12161d",
    "panel2": "#171c26",
    "border": "#232b37",
    "text": "#e6edf3",
    "muted": "#8b97a5",
    "faint": "#626d7c",
    "accent": "#4c8dff",
    "green": "#3fb950",
    "red": "#f8514d",
    "amber": "#d9a112",
    "mono": "ui-monospace, Menlo, Consolas, monospace",
}

#: 研报正文样式（暗色、专业机构版式）：封面页独占一页；标题不落单；表格/代码块不跨页断开。
REPORT_CSS = """
@page {{ size: A4; margin: 18mm 16mm 16mm; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: {bg}; color: {text};
  font: 11.5px/1.75 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
/* ── 封面页（独占一页）──────────────────────────────────────────── */
.cover {{ height: 261mm; display: flex; flex-direction: column; page-break-after: always;
  padding: 24mm 20mm; background: linear-gradient(158deg, {bg} 0%, {panel} 55%, {panel2} 100%);
  border: 1px solid {border}; }}
.cover .brand {{ font-size: 12px; letter-spacing: .18em; color: {muted}; text-transform: uppercase; }}
.cover h1 {{ font-size: 27px; line-height: 1.35; margin: 14mm 0 0; font-weight: 600; }}
.cover .subtitle {{ font-size: 13px; color: {muted}; margin-top: 4mm; }}
.cover .rule {{ width: 58mm; height: 3px; background: {accent}; margin: 9mm 0; }}
.cover .rating {{ display: inline-block; padding: 3px 14px; border: 1px solid {accent};
  border-radius: 3px; font-size: 15px; color: {accent}; letter-spacing: .08em; }}
.cover .meta {{ margin-top: 12mm; width: 100%; border-collapse: collapse; }}
.cover .meta th, .cover .meta td {{ border-top: 1px solid {border}; padding: 3.6mm 0; font-size: 11px; text-align: left; }}
.cover .meta th {{ color: {faint}; font-weight: 400; width: 42mm; }}
.cover .meta td {{ color: {text}; }}
.cover .spacer {{ flex: 1; }}
.cover .foot {{ font-size: 10px; color: {faint}; line-height: 1.7; border-top: 1px solid {border}; padding-top: 4mm; }}
.cover .foot b {{ color: {muted}; }}
/* ── 正文 ───────────────────────────────────────────────────────── */
.page {{ padding: 0 2mm; }}
.report-doc h1 {{ font-size: 20px; margin: 0 0 5mm; padding-bottom: 3mm; border-bottom: 1px solid {border}; }}
.report-doc h2 {{ font-size: 15px; margin: 8mm 0 3mm; padding-left: 8px; border-left: 3px solid {accent};
  page-break-after: avoid; }}
.report-doc h3 {{ font-size: 13px; margin: 6mm 0 2mm; color: {text}; page-break-after: avoid; }}
.report-doc h4 {{ font-size: 12px; margin: 5mm 0 2mm; color: {muted}; page-break-after: avoid; }}
.report-doc p {{ margin: 0 0 3.2mm; }}
.report-doc strong {{ color: #fff; }}
.report-doc .pos {{ color: {green}; }}
.report-doc .neg {{ color: {red}; }}
.report-doc .rating-up {{ color: {green}; }}
.report-doc .rating-down {{ color: {red}; }}
.report-doc em {{ color: {muted}; }}
.report-doc a {{ color: {accent}; text-decoration: none; word-break: break-all; }}
.report-doc ul, .report-doc ol {{ margin: 0 0 3.2mm; padding-left: 6mm; }}
.report-doc li {{ margin-bottom: 1.2mm; }}
.report-doc blockquote {{ margin: 0 0 3.6mm; padding: 2.6mm 4mm; background: {panel2};
  border-left: 3px solid {amber}; color: {muted}; }}
.report-doc code {{ font-family: {mono}; font-size: 10.5px; background: {panel2};
  border: 1px solid {border}; border-radius: 3px; padding: 0 3px; }}
.report-doc pre {{ background: {panel2}; border: 1px solid {border}; border-radius: 4px;
  padding: 3mm 4mm; overflow-wrap: anywhere; page-break-inside: avoid; }}
.report-doc pre code {{ border: 0; background: transparent; padding: 0; }}
.report-doc table {{ width: 100%; border-collapse: collapse; margin: 0 0 4mm; font-size: 10.5px;
  page-break-inside: avoid; }}
.report-doc th, .report-doc td {{ border: 1px solid {border}; padding: 2mm 2.4mm; text-align: left;
  vertical-align: top; }}
.report-doc th {{ background: {panel2}; color: {muted}; font-weight: 500; }}
.report-doc td {{ font-variant-numeric: tabular-nums; }}
.report-doc hr {{ border: 0; border-top: 1px solid {border}; margin: 5mm 0; }}
.report-doc .sources {{ margin-top: 6mm; }}
.report-doc .sources caption {{ text-align: left; color: {muted}; font-size: 11px; padding-bottom: 2mm; }}
.report-doc .disclaimer {{ margin-top: 8mm; padding-top: 3mm; border-top: 1px solid {border};
  color: {faint}; font-size: 9.5px; line-height: 1.7; }}
""".format(**TOKENS)


def _inline(text: str) -> str:
    """行内元素：先转义，再按顺序还原 markdown 标记（避免 HTML 注入）。"""
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", out)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', out)
    out = re.sub(r"(?<![\"'=])(https?://[^\s<]+)", r'<a href="\1">\1</a>', out)
    return out


def markdown_to_html(markdown: str) -> str:
    """研报常用 markdown 子集 → HTML（标题/段落/列表/表格/引用/代码/分割线/行内样式）。

    支持的子集就是研报实际会写的那几种（见 trading-agents 技能的发布约定）；
    不支持的语法按纯文本段落输出，不抛错、不丢内容。
    """
    lines = str(markdown or "").replace("\r\n", "\n").split("\n")
    out, index = [], 0
    list_buffer, list_kind = [], None

    def flush_list():
        nonlocal list_buffer, list_kind
        if list_buffer:
            tag = "ul" if list_kind == "ul" else "ol"
            out.append(f"<{tag}>" + "".join(f"<li>{item}</li>" for item in list_buffer) + f"</{tag}>")
        list_buffer, list_kind = [], None

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        # 代码块
        if stripped.startswith("```"):
            flush_list()
            index += 1
            block = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                block.append(lines[index])
                index += 1
            index += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(block))}</code></pre>")
            continue

        # 表格（| a | b | 且下一行是分隔行）
        if stripped.startswith("|") and index + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[index + 1].strip()):
            flush_list()
            header = [cell.strip() for cell in stripped.strip("|").split("|")]
            index += 2
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append([cell.strip() for cell in lines[index].strip().strip("|").split("|")])
                index += 1
            head_html = "".join(f"<th>{_inline(cell)}</th>" for cell in header)
            body_html = "".join(
                "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>" for row in rows
            )
            out.append(f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>")
            continue

        # 标题
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        # 分割线
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush_list()
            out.append("<hr />")
            index += 1
            continue

        # 引用
        if stripped.startswith(">"):
            flush_list()
            quote = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(lines[index].strip().lstrip(">").strip())
                index += 1
            out.append(f"<blockquote>{_inline(' '.join(quote))}</blockquote>")
            continue

        # 列表
        unordered = re.match(r"^[-*+]\s+(.*)$", stripped)
        ordered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if unordered or ordered:
            kind = "ul" if unordered else "ol"
            if list_kind and list_kind != kind:
                flush_list()
            list_kind = kind
            list_buffer.append(_inline((unordered or ordered).group(1)))
            index += 1
            continue

        # 空行/段落
        flush_list()
        if not stripped:
            index += 1
            continue
        paragraph = [stripped]
        index += 1
        while index < len(lines) and lines[index].strip() and not re.match(
            r"^(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|\||```|-{3,})", lines[index].strip()
        ):
            paragraph.append(lines[index].strip())
            index += 1
        out.append(f"<p>{_inline(' '.join(paragraph))}</p>")

    flush_list()
    return "\n".join(out)


def report_html(report: dict, *, mode: str | None = None, sources: list | None = None,
                generated_at: str | None = None, notice: str | None = None) -> str:
    """封面页 + 正文 + 来源表 + 免责声明 的完整 HTML（供 chromium 打印）。"""
    ticker = str(report.get("ticker") or "—")
    rating = str(report.get("rating_label") or report.get("rating") or "—")
    published = str(report.get("published_at") or "—")
    body = markdown_to_html(report.get("report") or "")
    sources = sources if sources is not None else (report.get("sources") or [])
    source_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(item.get("name") or "—")),
            html.escape(str(item.get("as_of") or "—")),
            html.escape(str(item.get("reference") or "—")),
        )
        for item in sources
    )
    sources_table = (
        f'<table class="sources"><caption>来源与数据时间（共 {len(sources)} 项）</caption>'
        "<thead><tr><th>来源</th><th>数据时间</th><th>引用</th></tr></thead>"
        f"<tbody>{source_rows}</tbody></table>"
        if sources else '<p class="disclaimer">本报告未附来源。</p>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<title>{html.escape(ticker)} 研究报告</title>
<style>{REPORT_CSS}</style></head>
<body>
<section class="cover">
  <div class="brand">量化决策平台 V3 · 研究报告</div>
  <h1>{html.escape(ticker)} 研究报告</h1>
  <div class="subtitle">Harness 决策大脑 · trading-agents 多角色投研流水线产出</div>
  <div class="rule"></div>
  <div class="rating">{html.escape(rating)}</div>
  <table class="meta">
    <tr><th>标的</th><td>{html.escape(ticker)}</td></tr>
    <tr><th>评级</th><td>{html.escape(rating)}（{html.escape(str(report.get('rating') or '—'))}）</td></tr>
    <tr><th>账户模式</th><td>{html.escape(str(mode or report.get('mode') or '—'))}</td></tr>
    <tr><th>发布时间</th><td>{html.escape(published)}</td></tr>
    <tr><th>研究 run</th><td>{html.escape(str(report.get('id') or '—'))}</td></tr>
    <tr><th>来源项数</th><td>{len(sources)}</td></tr>
    <tr><th>导出时间</th><td>{html.escape(str(generated_at or '—'))}</td></tr>
  </table>
  <div class="spacer"></div>
  <div class="foot">
    <b>数据来源</b>：平台 /api/v3/* 与工作台工具面（富途行情 / 台账 / 公开数据源）；每条证据标注来源与数据时间。<br />
    <b>免责声明</b>：本报告由研究流程自动产出，仅供研究与内部决策参考，不构成投资建议；不保证数据完整与实时，
    历史表现不代表未来收益。执行仍须经平台受约束入口与人工确认。
    {'<br /><b>记录提示</b>：' + html.escape(str(notice)) if notice else ''}
  </div>
</section>
<section class="page"><article class="report-doc">
{body}
{sources_table}
<div class="disclaimer">
  本页由「量化决策平台 V3」导出（{html.escape(str(generated_at or '—'))}）。研报正文与来源来自工作台研究记录，
  未作任何改写；评级与结论的原始定义见发布时的 rating 字段。
</div>
</article></section>
</body></html>"""


def chromium_bin() -> str | None:
    """可用浏览器可执行文件（可用 QUANT_CHROMIUM_BIN 覆盖）。"""
    return os.environ.get("QUANT_CHROMIUM_BIN") or shutil.which("chromium") or shutil.which(
        "chromium-browser"
    ) or shutil.which("google-chrome")


def render_pdf(html_text: str, *, timeout: int = 90) -> tuple[bytes | None, dict]:
    """HTML → PDF 字节。返回 ``(pdf_bytes, meta)``；失败时 ``pdf_bytes`` 为 None 并给出原因。"""
    binary = chromium_bin()
    if not binary:
        return None, {"code": "pdf/no-browser", "message": "服务端未找到 chromium，无法生成 PDF"}
    with tempfile.TemporaryDirectory(prefix="v3report-") as workdir:
        source = os.path.join(workdir, "report.html")
        target = os.path.join(workdir, "report.pdf")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write(html_text)
        command = [
            binary, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
            "--virtual-time-budget=8000", f"--print-to-pdf={target}", f"file://{source}",
        ]
        try:
            completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return None, {"code": "pdf/timeout", "message": f"PDF 渲染超时（{timeout}s）"}
        if not os.path.isfile(target):
            detail = (completed.stderr or b"").decode("utf-8", "ignore")[-300:]
            return None, {"code": "pdf/render-failed", "message": detail or "chromium 未产出 PDF"}
        with open(target, "rb") as handle:
            data = handle.read()
        return data, {"bytes": len(data), "engine": binary}
