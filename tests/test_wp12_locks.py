"""WP12 任务 1：富途 OpenAPI 数据面端点依赖锁定表——结构与完整性锁定测试。

约束五件事（**不绑定传输层代码**——方法组在 WP12 任务 2/3 才实现，本测试只守锁定表本身）：

* 锁定表存在、带核对日期与四种状态图例；
* 每条端点行四要素齐备：方法（GET/POST/PUT/DELETE）+ `/api/v1.0/...` 路径 + 非空
  参数/响应列 + 状态图例 + 官方文档链接（域名为官方文档站相对路径）；
* 路径占位符只允许 {symbol}/{market}/{acc_id}/{order_id}——**禁止出现猜测性路径**；
* (方法, 路径) 全局唯一，且**分组端点数量锚定**（防"删一行"式静默退化）：
  基础数据 4 / 板块 2 / 筛选 2 / IPO 1 / 个股深度 26 / 卖空 2 / 衍生品 4 / 自选 3 /
  模拟交易 9 = 合计 53 条新接入目标；
* ⛔ BLOCKED 行必须带原因（当前为 0 条）；范围外族（§C.10）行必须落在带 🚫 的章节内。

背景（血泪教训，见锁定表 §D.2）：`llms.txt` 的 `/api/quote/f10/*.md` **全部 404**，官方已把
F10 族重组为 financials/research/valuation/corporate-actions/shareholders/company/top-brokers
七个命名空间，并**漏列 2 个估值端点**——本测试是"禁止猜路径"的机械保证。
"""
import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
LOCK = _REPO / "docs" / "superpowers" / "plans" / "wp12-endpoint-lock.md"

METHODS = ("GET", "POST", "PUT", "DELETE")
STATUS_GLYPHS = ("✅", "⚠️", "🚫", "⛔")
PATH_RE = re.compile(r"/api/v1\.0/[A-Za-z0-9/{}_.*-]+")
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
ALLOWED_PLACEHOLDERS = {"symbol", "market", "acc_id", "order_id"}
DOC_LINK_RE = re.compile(r"`(/zh-cn/api/[A-Za-z0-9/_.-]+)`")

# 分组端点数量锚（key = 章节号，与文档标题一一对应）
GROUP_COUNTS = {
    "C.1": 4,   # 基础数据
    "C.2": 2,   # 板块
    "C.3": 2,   # 筛选
    "C.4": 1,   # IPO
    "C.5": 26,  # 个股深度（financials 4 / research 3 / valuation 4 / corporate-actions 3
                #            / shareholders 6 / company 4 / top-brokers 2）
    "C.6": 2,   # 卖空
    "C.7": 4,   # 衍生品
    "C.8": 3,   # 自选
    "C.9": 9,   # 模拟交易
}
TOTAL_NEW = 53


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _iter_rows():
    """产出 (section, line, cells)。section 取最近的 ``### `` 标题（如 ``### C.5 …``）。"""
    section = ""
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            section = line[4:].strip()
        if line.startswith("|"):
            yield section, line, _cells(line)


_ENDPOINT_SECTION_RE = re.compile(r"^C\.\d+")


def _inventory_sections():
    """§C.x 端点清单章节（D/E 章节的说明行与统计行不参与端点断言）。"""
    return [(s, line, c) for s, line, c in _iter_rows()
            if _ENDPOINT_SECTION_RE.match(s)]


def _endpoint_rows():
    """含 `/api/v1.0/` 的清单行；排除 §C.10 范围外（其路径为通配族）。"""
    return [(s, line, c) for s, line, c in _inventory_sections()
            if PATH_RE.search(line) and not s.startswith("C.10")]


class LockTableStructureTests(unittest.TestCase):

    def test_file_has_date_and_legend(self):
        text = LOCK.read_text(encoding="utf-8")
        self.assertIn("2026-09-16", text)
        for glyph in STATUS_GLYPHS:
            self.assertIn(glyph, text, f"状态图例缺 {glyph}")

    def test_every_endpoint_row_is_complete(self):
        rows = _endpoint_rows()
        self.assertGreater(len(rows), 0)
        for section, line, cells in rows:
            with self.subTest(section=section, row=line[:70]):
                self.assertTrue(any(f" {m} " in f" {line} " for m in METHODS),
                                f"缺方法: {line[:90]}")
                self.assertTrue(any(g in line for g in ("✅", "⚠️")),
                                f"缺状态图例: {line[:90]}")
                self.assertTrue(DOC_LINK_RE.search(line),
                                f"缺官方文档链接: {line[:90]}")
                body = [c for c in cells if c and "---" not in c]
                self.assertGreaterEqual(len(body), 4, f"列数不足四要素: {line[:90]}")
                for col in body[2:]:
                    self.assertTrue(col.strip(), f"存在空列: {line[:90]}")

    def test_paths_use_only_allowed_placeholders(self):
        for section, line, _ in _endpoint_rows():
            for m in PATH_RE.finditer(line):
                for ph in PLACEHOLDER_RE.findall(m.group(0)):
                    with self.subTest(path=m.group(0), ph=ph):
                        self.assertIn(ph, ALLOWED_PLACEHOLDERS,
                                      f"未授权占位符 {{{ph}}}（禁止猜路径）")

    def test_no_duplicate_method_path(self):
        seen = {}
        for section, line, _ in _endpoint_rows():
            method = next((m for m in METHODS if f" {m} " in f" {line} "), None)
            path = PATH_RE.search(line).group(0)
            key = (method, path)
            with self.subTest(key=key):
                self.assertNotIn(key, seen, f"重复端点：{key}（另见 {seen.get(key)}）")
            seen[key] = section
        self.assertEqual(len(seen), TOTAL_NEW,
                         f"端点总数应为 {TOTAL_NEW}，实为 {len(seen)}")

    def test_group_inventory_counts(self):
        counts = {}
        for section, line, _ in _endpoint_rows():
            key = section.split(" ")[0].rstrip("：")
            counts[key] = counts.get(key, 0) + 1
        for group, want in GROUP_COUNTS.items():
            with self.subTest(group=group):
                self.assertEqual(counts.get(group, 0), want,
                                 f"{group} 端点数量漂移：期望 {want}，实为 {counts.get(group, 0)}")

    def test_blocked_rows_carry_reason(self):
        blocked = [(s, l) for s, l, _ in _inventory_sections()
                   if "⛔" in l and l.startswith("|")]
        for section, line in blocked:
            with self.subTest(row=line[:70]):
                tail = line.split("⛔", 1)[1].strip(" |")
                self.assertTrue(tail, f"BLOCKED 行缺原因: {line[:90]}")
        # 当前无 BLOCKED：48 份官方文档全部取到
        self.assertEqual(len(blocked), 0, f"出现 BLOCKED 行，需人工处置：{blocked}")

    def test_out_of_scope_rows_live_in_marked_section(self):
        crypto = [(s, l) for s, l, _ in _inventory_sections()
                  if "crypto-trading" in l and l.startswith("|")]
        self.assertTrue(crypto, "范围外族（加密货币）应登记在案")
        for section, line in crypto:
            with self.subTest(section=section):
                self.assertTrue(section.startswith("C.10"), "加密货币端点必须在 §C.10")
                self.assertIn("🚫", section + line)

    def test_doc_links_are_official_relative_paths(self):
        text = LOCK.read_text(encoding="utf-8")
        links = DOC_LINK_RE.findall(text)
        self.assertGreaterEqual(len(links), 40)
        for link in links:
            with self.subTest(link=link):
                self.assertTrue(link.startswith("/zh-cn/api/"), f"非官方相对路径: {link}")
                self.assertNotIn("http", link)

    def test_corrected_conflicts_are_recorded(self):
        """§D 必须记录三处实测漂移，供后续任务引用（防止旧 plan 文本回流）。"""
        text = LOCK.read_text(encoding="utf-8")
        self.assertIn("info_search_stock", text, "§D.1 必须纠正不存在的证券搜索工具")
        self.assertIn("index-stocks", text)
        self.assertIn("index-stock-plates", text)
        self.assertIn("top-brokers-history", text)


if __name__ == "__main__":
    unittest.main()
