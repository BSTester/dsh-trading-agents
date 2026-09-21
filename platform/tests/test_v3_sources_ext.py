"""``server/v3_sources_ext`` 的离线单测（该模块此前零覆盖）：封网络 + 注入假 deps。

运行::

    cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_sources_ext -v

覆盖清单（对应 2026-09-21 数据源扩展任务的验收点，逐条可核对）：

  1. **三源降级顺序**：东财失败（三种真实失败形态：网络错误信封 / 上游 ``data:null`` /
     响应字段不足）→ 逐级降级，断言 ``source`` 与 ``chain`` 每级的 ``ok``/``ms``/``error``；
     三家全失败 → 如实错误信封，响应里**没有**任何报价/盘口字段（不编造数据）；
  2. **冷却 / 预算**：冷却中的源记 ``skipped``（``quote/cooldown``）且 ``ms=0``、**一个请求
     都不发**；链墙钟预算耗尽 → 剩余源一律 ``skipped`` + ``chain/timeout``，同样不发请求；
  3. **盘口字段**：``fetch_a_share_order_book`` 给 ``book_depth``/``depth_note``，上游 ``"-"``
     档位**整档省略**（绝不填 0）；``fetch_a_share_quote`` **不塞**这两个字段；
  4. **北向披露事实**：``net_buy_disclosed=false`` 时上游 0 只是占位（不冒充真实净买额），
     南向不受影响；历史序列缺的净买额是 ``None``（不是 0）；
  5. **宏观口径并列**：主源 NBS（AKShare）与第二源 OECD（OpenBB）各带 ``caliber``、并列且不
     互相冒充；第二源拿不到 → ``openbb/*`` 错误信封（**不是空数组**）；
  6. **富途 -9 钩子边界**：HK/US/北交所标的不触发公开链（``quote/unsupported-market``）；
     混合列表只降级沪深标的、其余如实进 ``errors``；全非沪深 / 非行情端点 → 返回 ``None``
     （调用方原样上抛 -9）；盘口端点必须带 ``book_depth``/``depth_note``；
  7. **``_head`` 未导入回归**：``/api/v3/northbound`` 在 in-process app 上返回 200 且
     ``ok=true``（2026-09-21 修：``_head`` 此前漏在 import 清单外 → 该路由整条挂掉）。

末尾 ``FundamentalsIndicatorFallbackTests`` 是 ``server/v3_fundamentals_sync`` 本轮新增的
**免密替代源**（AKShare 财务指标兜底）的离线回归：本轮任务的文件归属约束只允许本文件承载
新单测（``tests/test_v3_fundamentals_sync.py`` 不在可写清单里），故放在同一文件末尾并独立成类。
"""
import contextlib
import io
import math
import re
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import futu_data, v3_fallback, v3_sources, v3_sources_ext
from server.v3_sources_ext import ExtDeps


# ---------------------------------------------------------------------------
# 测试替身：假 JSON 口 / 假文本口 / 假 akshare / 假 openbb
# ---------------------------------------------------------------------------
def make_deps(*, fetch_json=None, akshare=None, openbb=None, text_get=None, env=None,
              akshare_retry=None):
    """``ExtDeps`` + 可注入的 ``Deps``（akshare 重试压到 1 次，测试不等退避）。"""
    base = v3_sources.Deps(fetch_json=fetch_json, akshare=akshare, openbb=openbb,
                           env={} if env is None else env, timeout=1.0,
                           akshare_retry={"attempts": 1} if akshare_retry is None
                           else akshare_retry)
    return ExtDeps(base, text_get=text_get)


class CallRecorder:
    """记录每次调用的 URL（断言「发没发请求」靠它，不靠猜）。"""

    def __init__(self, handler=None):
        self.calls = []
        self._handler = handler

    def __call__(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if self._handler is None:
            return v3_sources.envelope_error("test/no-fake", f"未给假响应：{url}")
        return self._handler(url, headers, timeout)


class TextRecorder:
    """假文本口：转调签名与 ``ExtDeps.text_get`` 逐字一致（url, headers, encoding, timeout）。"""

    def __init__(self, handler=None):
        self.calls = []
        self._handler = handler

    def __call__(self, url, headers=None, encoding="gbk", timeout=None):
        self.calls.append(url)
        if self._handler is None:
            return v3_sources.envelope_error("test/no-fake", f"未给假响应：{url}")
        return self._handler(url, headers, encoding, timeout)

    def urls_with(self, needle):
        return [url for url in self.calls if needle in url]


class FakeAkshare:
    """模块样对象（不可调用 → ``Deps.module`` 原样返回）：只暴露测试给的那几个函数。"""

    def __init__(self, **funcs):
        self._funcs = funcs
        self.calls = []

    def __getattr__(self, name):
        funcs = object.__getattribute__(self, "_funcs")
        if name not in funcs:
            raise AttributeError(name)  # 缺函数 → 调用方回 akshare/missing-func（不猜）
        calls = object.__getattribute__(self, "calls")
        value = funcs[name]

        def call(**kwargs):
            calls.append((name, dict(kwargs)))
            if isinstance(value, BaseException):
                raise value
            if callable(value):
                return value(**kwargs)
            return value

        return call

    def called(self, name):
        return [kwargs for func, kwargs in self.calls if func == name]


class FakeFrame:
    """最小 DataFrame 面：``to_dict('records')``（``_rows_from_frame`` 的首选路径）。"""

    def __init__(self, rows):
        self.rows = list(rows)

    def to_dict(self, orient="dict"):
        if orient != "records":
            raise TypeError("只实现 records")
        return [dict(row) for row in self.rows]


class FakeOBBject:
    """最小 OpenBB 返回面：``.to_dataframe()``（OECD 腿靠它拿带 index 的帧）。"""

    def __init__(self, frame):
        self._frame = frame

    def to_dataframe(self):
        return self._frame


def raising(message):
    """返回一个「一调用就抛」的函数（替身上游失败）。"""

    def boom(**kwargs):
        raise RuntimeError(message)

    return boom


def tencent_text(code="600000", price=10.50, bid=None, ask=None, fields=52):
    """腾讯 ``v_sh600000="…"`` 假响应（字段位与 ``_quote_tencent`` 的映射逐位对应）。"""
    parts = ["0"] * fields
    parts[0] = "1"
    parts[1] = "浦发银行"
    parts[2] = code
    parts[3] = f"{price}"
    parts[4] = "10.40"      # 昨收
    parts[5] = "10.45"      # 今开
    parts[6] = "123456"     # 成交量（手）
    parts[30] = "20260921150000"
    parts[31] = "0.10"      # 涨跌额
    parts[32] = "0.96"      # 涨跌幅
    parts[33] = "10.60"     # 最高
    parts[34] = "10.30"     # 最低
    parts[37] = "12.34"     # 成交额（万元）
    parts[38] = "0.53"      # 换手率
    parts[39] = "6.71"      # PE
    parts[43] = "2.88"      # 振幅
    parts[44] = "1234.5"    # 流通市值（亿）
    parts[45] = "2345.6"    # 总市值（亿）
    parts[46] = "0.85"      # PB
    parts[49] = "1.02"      # 量比
    bid = bid if bid is not None else [("10.49", "100"), ("10.48", "200"),
                                       ("10.47", "300"), ("-", "400"), ("10.45", "500")]
    ask = ask if ask is not None else [("10.51", "110"), ("10.52", "210"),
                                       ("10.53", "310"), ("10.54", "410"), ("10.55", "510")]
    for index, (level_price, volume) in enumerate(bid):
        parts[9 + 2 * index] = level_price
        parts[10 + 2 * index] = volume
    for index, (level_price, volume) in enumerate(ask):
        parts[19 + 2 * index] = level_price
        parts[20 + 2 * index] = volume
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    return f'v_{prefix}{code}="' + "~".join(parts) + '";\n'


def sina_text(code="600000", price=10.50, volume_shares=12345678, bid=None, ask=None):
    """新浪 ``var hq_str_sh600000="…"`` 假响应。

    字段序与 ``_quote_sina`` 的映射逐位对应（真实接口形态）：
    ``名称,今开,昨收,现价,最高,最低,买一价,卖一价,成交量(股),成交额, 买①量,买①价,…,卖⑤量,卖⑤价, 日期,时间,状态``。
    """
    bid = bid if bid is not None else [(100, "10.49"), (200, "10.48"), (300, "10.47"),
                                       (400, "10.46"), (500, "10.45")]
    ask = ask if ask is not None else [(110, "10.51"), (210, "10.52"), (310, "10.53"),
                                       (410, "10.54"), (510, "10.55")]
    parts = ["浦发银行", "10.45", "10.40", f"{price}", "10.60", "10.30", "10.49", "10.51",
             f"{volume_shares}", "12345678.0"]
    for volume, level_price in bid:
        parts += [f"{volume}", level_price]
    for volume, level_price in ask:
        parts += [f"{volume}", level_price]
    parts += ["2026-09-21", "15:00:00", "00"]
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    return f'var hq_str_{prefix}{code}="' + ",".join(parts) + '";'


class SourcesExtTestBase(unittest.TestCase):
    def setUp(self):
        # 进程内冷却表是模块级状态：逐测试清空，避免测试之间互相污染
        self.addCleanup(v3_sources_ext._COOLDOWN.clear)
        v3_sources_ext._COOLDOWN.clear()
        self.addCleanup(futu_data.uninstall_public_fallback)


# ---------------------------------------------------------------------------
# ① 三源降级顺序：东财 → 腾讯 → 新浪
# ---------------------------------------------------------------------------
class QuoteChainTests(SourcesExtTestBase):
    def test_eastmoney_network_error_degrades_to_tencent(self):
        json_get = CallRecorder(lambda url, headers, timeout: v3_sources.envelope_error(
            "quote/network", "push2.eastmoney.com → ConnectTimeout: 测试替身断连"))
        text_get = TextRecorder(lambda url, headers, encoding, timeout: (
            {"ok": True, "value": tencent_text()}))
        deps = make_deps(fetch_json=json_get, text_get=text_get)

        result = v3_sources_ext.fetch_a_share_quote(deps, "SH.600000")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "tencent/qt.gtimg.cn")
        self.assertEqual(result["used_source"], "tencent/qt.gtimg.cn")
        chain = result["chain"]
        # 命中即停：只有两级（新浪没被试过）
        self.assertEqual([item["source"] for item in chain],
                         ["eastmoney/push2", "tencent/qt.gtimg.cn"])
        self.assertFalse(chain[0]["ok"])
        self.assertIsInstance(chain[0]["ms"], int)
        self.assertGreaterEqual(chain[0]["ms"], 0)
        self.assertEqual(chain[0]["error"]["code"], "quote/network")
        self.assertIn("ConnectTimeout", chain[0]["error"]["message"])  # 真实原文，不改写
        self.assertTrue(chain[1]["ok"])
        self.assertNotIn("error", chain[1])
        self.assertNotIn("skipped", chain[1])
        # 请求真的只打了两家：东财一次、腾讯一次、新浪零次
        self.assertEqual(len(json_get.calls), 1)
        self.assertEqual(len(text_get.urls_with("qt.gtimg.cn")), 1)
        self.assertEqual(text_get.urls_with("sinajs"), [])
        # 数值原样：报价 + 五档（买四 "-" 整档省略 → bid 4 档、depth=min(4,5)=4）
        self.assertEqual(result["quote"]["price"], 10.50)
        self.assertEqual(result["quote"]["volume"], 123456.0)
        self.assertEqual(result["book"]["depth"], 4)
        self.assertEqual([level[0] for level in result["book"]["bid"]],
                         [10.49, 10.48, 10.47, 10.45])
        # 东财失败即进冷却（避免平台流量把上游打死）
        self.assertIn("eastmoney/push2", v3_sources_ext._COOLDOWN)

    def test_eastmoney_null_data_degrades_and_keeps_real_reason(self):
        json_get = CallRecorder(lambda url, headers, timeout: (
            {"ok": True, "value": {"data": None}}))
        text_get = TextRecorder(lambda url, headers, encoding, timeout: (
            {"ok": True, "value": tencent_text()}))
        deps = make_deps(fetch_json=json_get, text_get=text_get)

        result = v3_sources_ext.fetch_a_share_quote(deps, "SH.600000")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "tencent/qt.gtimg.cn")
        self.assertEqual(result["chain"][0]["error"]["code"], "eastmoney/push2")
        self.assertIn("未返回", result["chain"][0]["error"]["message"])
        self.assertIn("secid=1.600000", json_get.calls[0])  # 真是打给东财 push2 的请求

    def test_tencent_bad_shape_degrades_to_sina(self):
        json_get = CallRecorder(lambda url, headers, timeout: v3_sources.envelope_error(
            "quote/network", "东财不可达（测试替身）"))
        text_get = TextRecorder(lambda url, headers, encoding, timeout: (
            {"ok": True, "value": sina_text()} if "sinajs" in url
            else {"ok": True, "value": 'v_sh600000="1~浦发银行";'}))  # 腾讯字段不足
        deps = make_deps(fetch_json=json_get, text_get=text_get)

        result = v3_sources_ext.fetch_a_share_quote(deps, "SH.600000")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "sina/hq.sinajs.cn")
        self.assertEqual([item["source"] for item in result["chain"]],
                         ["eastmoney/push2", "tencent/qt.gtimg.cn", "sina/hq.sinajs.cn"])
        self.assertFalse(result["chain"][1]["ok"])
        self.assertEqual(result["chain"][1]["error"]["code"], "tencent/qt.gtimg.cn")
        self.assertIn("字段不足", result["chain"][1]["error"]["message"])
        # 新浪量字段单位是股 → 换算成手；该形态无涨跌额字段 → None（不从价差推算）
        self.assertEqual(result["quote"]["volume"], 123456.78)
        self.assertIsNone(result["quote"]["change"])
        self.assertIsNone(result["quote"]["change_pct"])
        # 五档：新浪 raws 是「量,价」序且量单位股 → 档量 /100
        self.assertEqual(result["book"]["bid"][0], [10.49, 1.0])
        self.assertEqual(result["book"]["depth"], 5)

    def test_all_three_fail_returns_honest_error_envelope(self):
        json_get = CallRecorder(lambda url, headers, timeout: v3_sources.envelope_error(
            "quote/network", "东财断连（测试替身）"))
        text_get = TextRecorder(lambda url, headers, encoding, timeout: (
            v3_sources.envelope_error("quote/network", f"{url} → 测试替身拒绝")))
        deps = make_deps(fetch_json=json_get, text_get=text_get)

        result = v3_sources_ext.fetch_a_share_quote(deps, "SH.600000")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "quote/network")  # 最后一级的真实错误码
        self.assertEqual(result["tried"], list(v3_sources_ext.PUBLIC_QUOTE_SOURCES))
        self.assertEqual(len(result["chain"]), 3)
        self.assertTrue(all(item["ok"] is False for item in result["chain"]))
        self.assertTrue(all(isinstance(item["ms"], int) for item in result["chain"]))
        self.assertIn("全部失败", result["error"]["message"])
        self.assertIn("eastmoney/push2", result["error"]["message"])
        self.assertIn("sina/hq.sinajs.cn", result["error"]["message"])
        # 不编造数据：错误信封里没有报价/盘口字段
        self.assertNotIn("quote", result)
        self.assertNotIn("book", result)
        self.assertNotIn("source", result)

    def test_unsupported_ticker_makes_no_request(self):
        json_get = CallRecorder()
        text_get = TextRecorder()
        deps = make_deps(fetch_json=json_get, text_get=text_get)

        result = v3_sources_ext.fetch_a_share_quote(deps, "SH.60051")  # 5 位数字，不合法

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "quote/unsupported-market")
        self.assertEqual(json_get.calls, [])
        self.assertEqual(text_get.calls, [])


# ---------------------------------------------------------------------------
# ② 冷却 / 链预算：skipped 不许假装试过
# ---------------------------------------------------------------------------
class CooldownAndBudgetTests(SourcesExtTestBase):
    def test_cooling_source_is_skipped_without_any_request(self):
        # 先制造一次东财失败（进冷却），再发第二次请求
        json_get = CallRecorder(lambda url, headers, timeout: v3_sources.envelope_error(
            "quote/network", "东财断连（测试替身）"))
        text_get = TextRecorder(lambda url, headers, encoding, timeout: (
            {"ok": True, "value": tencent_text()}))
        deps = make_deps(fetch_json=json_get, text_get=text_get)

        first = v3_sources_ext.fetch_a_share_quote(deps, "SH.600000")
        self.assertTrue(first["ok"])
        self.assertEqual(len(json_get.calls), 1)

        second = v3_sources_ext.fetch_a_share_quote(deps, "SH.600000")

        self.assertTrue(second["ok"], second)
        self.assertEqual(second["source"], "tencent/qt.gtimg.cn")
        cooling = second["chain"][0]
        self.assertEqual(cooling["source"], "eastmoney/push2")
        self.assertFalse(cooling["ok"])
        self.assertEqual(cooling["error"]["code"], "quote/cooldown")
        self.assertIn("冷却", cooling["error"]["message"])
        # 语义关键点：ms=0 且**一个请求都没发**（skipped，不是「试过但失败」）
        self.assertEqual(cooling["ms"], 0)
        self.assertEqual(len(json_get.calls), 1)  # 仍是第一次那一次
        self.assertEqual(len(text_get.urls_with("qt.gtimg.cn")), 2)

    def test_chain_budget_exhausted_marks_rest_skipped_without_requests(self):
        json_get = CallRecorder()
        text_get = TextRecorder()
        deps = make_deps(fetch_json=json_get, text_get=text_get)

        with unittest.mock.patch.object(v3_sources_ext, "QUOTE_CHAIN_TIMEOUT", 0.0):
            result = v3_sources_ext.fetch_a_share_quote(deps, "SH.600000")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "chain/timeout")
        self.assertEqual(len(result["chain"]), 3)
        for item in result["chain"]:
            self.assertTrue(item.get("skipped"), item)
            self.assertEqual(item["ms"], 0, item)
            self.assertEqual(item["error"]["code"], "chain/timeout")
        self.assertIn("未尝试", result["error"]["message"])
        self.assertEqual(json_get.calls, [])   # 预算耗尽：一家都没打
        self.assertEqual(text_get.calls, [])

    def test_two_cooling_sources_leave_only_sina(self):
        v3_sources_ext._COOLDOWN["eastmoney/push2"] = time.monotonic() + 60
        v3_sources_ext._COOLDOWN["tencent/qt.gtimg.cn"] = time.monotonic() + 60
        json_get = CallRecorder()
        text_get = TextRecorder(lambda url, headers, encoding, timeout: (
            {"ok": True, "value": sina_text()}))
        deps = make_deps(fetch_json=json_get, text_get=text_get)

        result = v3_sources_ext.fetch_a_share_quote(deps, "SH.600000")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "sina/hq.sinajs.cn")
        self.assertEqual([(item["source"], item["ok"], item["ms"])
                          for item in result["chain"][:2]],
                         [("eastmoney/push2", False, 0), ("tencent/qt.gtimg.cn", False, 0)])
        self.assertEqual(result["chain"][0]["error"]["code"], "quote/cooldown")
        self.assertEqual(result["chain"][1]["error"]["code"], "quote/cooldown")
        self.assertTrue(result["chain"][2]["ok"])
        self.assertEqual(json_get.calls, [])
        self.assertEqual(text_get.urls_with("qt.gtimg.cn"), [])


# ---------------------------------------------------------------------------
# ③ 盘口字段：book_depth / depth_note，缺档不填 0
# ---------------------------------------------------------------------------
class OrderBookTests(SourcesExtTestBase):
    def setUp(self):
        super().setUp()
        self.json_get = CallRecorder(lambda url, headers, timeout: v3_sources.envelope_error(
            "quote/network", "东财断连（测试替身）"))
        self.text_get = TextRecorder(lambda url, headers, encoding, timeout: (
            {"ok": True, "value": tencent_text()}))
        self.deps = make_deps(fetch_json=self.json_get, text_get=self.text_get)

    def test_order_book_exposes_depth_fields_and_omits_missing_levels(self):
        result = v3_sources_ext.fetch_a_share_order_book(self.deps, "SH.600000")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["book_depth"], 4)
        self.assertIn("五档", result["depth_note"])
        self.assertIn("不伪造", result["depth_note"])
        self.assertIn("tencent/qt.gtimg.cn", result["depth_note"])
        bid = result["book"]["bid"]
        self.assertEqual(bid, [[10.49, 100.0], [10.48, 200.0], [10.47, 300.0], [10.45, 500.0]])
        # 上游 "-" 的买四整档省略：既不填 0 价、也不填 0 量
        self.assertTrue(all(level[0] > 0 for level in bid))
        self.assertTrue(all(level[1] != 0 for level in bid))
        self.assertEqual(len(result["book"]["ask"]), 5)

    def test_quote_does_not_carry_depth_fields(self):
        result = v3_sources_ext.fetch_a_share_quote(self.deps, "SH.600000")

        self.assertTrue(result["ok"], result)
        self.assertNotIn("book_depth", result)
        self.assertNotIn("depth_note", result)
        self.assertEqual(result["book"]["depth"], 4)  # 盘口本体仍在（归一化载荷）

    def test_no_valid_levels_means_source_failure_not_zero_filled(self):
        empty_book = [("-", "-")] * 5
        text_get = TextRecorder(lambda url, headers, encoding, timeout: (
            {"ok": True, "value": tencent_text(bid=empty_book, ask=empty_book)}))
        deps = make_deps(fetch_json=self.json_get, text_get=text_get)

        result = v3_sources_ext.fetch_a_share_order_book(deps, "SH.600000")

        self.assertFalse(result["ok"])
        self.assertNotIn("book_depth", result)
        self.assertNotIn("book", result)
        self.assertEqual(result["chain"][1]["source"], "tencent/qt.gtimg.cn")
        self.assertIn("有效档位", result["chain"][1]["error"]["message"])

    def test_quote_tolerates_empty_book_without_inventing_levels(self):
        empty_book = [("-", "-")] * 5
        text_get = TextRecorder(lambda url, headers, encoding, timeout: (
            {"ok": True, "value": tencent_text(bid=empty_book, ask=empty_book)}))
        deps = make_deps(fetch_json=self.json_get, text_get=text_get)

        result = v3_sources_ext.fetch_a_share_quote(deps, "SH.600000")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["book"], {"bid": [], "ask": [], "depth": 0,
                                          "volume_unit": "手"})


# ---------------------------------------------------------------------------
# ④ 北向：net_buy_disclosed（披露事实，不把占位 0 当真实值）
# ---------------------------------------------------------------------------
SUMMARY_ROWS = [
    {"交易日": "2026-09-18", "板块": "沪股通", "类型": "沪股通", "资金方向": "北向",
     "成交净买额": 0.0, "资金净流入": 12.3, "上涨数": 800, "下跌数": 600,
     "相关指数": "上证指数", "指数涨跌幅": 0.51},
    {"交易日": "2026-09-18", "板块": "港股通(沪)", "类型": "港股通", "资金方向": "南向",
     "成交净买额": 45.62, "资金净流入": 51.0, "上涨数": 300, "下跌数": 200,
     "相关指数": "恒生指数", "指数涨跌幅": -0.22},
]
HIST_ROWS = [
    {"日期": "2024-08-16", "当日成交净买额": 12.5, "买入成交额": 100.0, "卖出成交额": 87.5,
     "历史累计净买额": 18000.0, "当日资金流入": 12.5, "当日余额": 500.0, "持股市值": 20000.0},
    {"日期": "2026-09-17", "当日成交净买额": math.nan, "买入成交额": 900.0,
     "卖出成交额": 880.0, "历史累计净买额": math.nan, "当日资金流入": math.nan,
     "当日余额": math.nan, "持股市值": 25000.0},
    {"日期": "2026-09-18", "当日成交净买额": math.nan, "买入成交额": 910.0,
     "卖出成交额": 890.0, "历史累计净买额": math.nan, "当日资金流入": math.nan,
     "当日余额": math.nan, "持股市值": 25100.0},
]


class NorthboundTests(SourcesExtTestBase):
    def setUp(self):
        super().setUp()
        self.ak = FakeAkshare(stock_hsgt_fund_flow_summary_em=SUMMARY_ROWS,
                              stock_hsgt_hist_em=HIST_ROWS)
        self.deps = make_deps(akshare=self.ak)

    def test_placeholder_zero_is_flagged_not_sold_as_real(self):
        result = v3_sources_ext.fetch_northbound(self.deps, limit=5)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["errors"], [])
        north = [row for row in result["boards"] if row["direction"] == "北向"]
        south = [row for row in result["boards"] if row["direction"] == "南向"]
        self.assertEqual(len(north), 1)
        self.assertEqual(north[0]["net_buy"], 0.0)             # 上游原样透传
        self.assertIs(north[0]["net_buy_disclosed"], False)    # 但如实标注为未披露/占位
        self.assertIs(south[0]["net_buy_disclosed"], True)     # 南向不受影响
        self.assertEqual(south[0]["net_buy"], 45.62)
        disclosure = result["net_buy_disclosure"]
        self.assertIs(disclosure["northbound_daily_net_buy"], False)
        self.assertEqual(disclosure["last_disclosed_date"], "2024-08-16")
        self.assertIn("2024-08-19", disclosure["note"])
        self.assertIn("占位", disclosure["note"])

    def test_history_keeps_missing_net_buy_as_none(self):
        result = v3_sources_ext.fetch_northbound(self.deps, limit=2)

        self.assertTrue(result["ok"], result)
        sh = result["history"]["SH"]
        self.assertEqual(sh["total_rows"], 3)
        self.assertEqual(len(sh["rows"]), 2)
        self.assertEqual([row["date"] for row in sh["rows"]],
                         ["2026-09-17", "2026-09-18"])
        # 未披露的净买额是 None（不是 0），买入/卖出额照给
        self.assertIsNone(sh["rows"][1]["net_buy"])
        self.assertEqual(sh["rows"][1]["buy"], 910.0)
        self.assertEqual(sh["last_net_buy_date"], "2024-08-16")
        self.assertEqual(result["history"]["SZ"]["last_net_buy_date"], "2024-08-16")
        self.assertEqual(self.ak.called("stock_hsgt_hist_em"),
                         [{"symbol": "沪股通"}, {"symbol": "深股通"}])

    def test_missing_akshare_function_is_reported_per_part(self):
        ak = FakeAkshare(stock_hsgt_hist_em=HIST_ROWS)  # summary 函数缺失
        deps = make_deps(akshare=ak)

        result = v3_sources_ext.fetch_northbound(deps, limit=2)

        self.assertTrue(result["ok"], result)  # 部件级降级：历史部件仍可用
        self.assertIsNone(result["boards"])
        codes = {item["part"]: item["code"] for item in result["errors"]}
        self.assertEqual(codes["summary"], "akshare/missing-func")

    def test_all_parts_failed_is_error_envelope_with_real_errors(self):
        ak = FakeAkshare(stock_hsgt_fund_flow_summary_em=RuntimeError("上游 500"),
                         stock_hsgt_hist_em=RuntimeError("上游 500"))
        deps = make_deps(akshare=ak)

        result = v3_sources_ext.fetch_northbound(deps, limit=2)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "northbound/all-parts-failed")
        self.assertEqual(sorted(item["part"] for item in result["errors"]),
                         ["hist_sh", "hist_sz", "summary"])
        self.assertNotIn("boards", result)
        self.assertNotIn("history", result)


# ---------------------------------------------------------------------------
# ⑤ 宏观：NBS 主源与 OECD 第二源并列、caliber 区分
# ---------------------------------------------------------------------------
NBS_CPI_ROWS = [
    {"月份": "2026年07月份", "全国-同比增长": 0.5, "全国-当月": 100.2},
    {"月份": "2026年08月份", "全国-同比增长": 0.8, "全国-当月": 100.4},
]
NBS_PPI_ROWS = [{"月份": "2026年08月份", "当月同比增长": -1.8, "当月": 98.2}]


def oecd_object(values=(0.005, 0.008)):
    import pandas as pd
    frame = pd.DataFrame({"value": list(values)},
                         index=pd.to_datetime(["2026-07-01", "2026-08-01"]))
    frame.index.name = "date"
    return FakeOBBject(frame)


class MacroTests(SourcesExtTestBase):
    def setUp(self):
        super().setUp()
        self.ak = FakeAkshare(macro_china_cpi=NBS_CPI_ROWS)
        self.openbb = SimpleNamespace(obb=SimpleNamespace(
            economy=SimpleNamespace(cpi=lambda **kwargs: oecd_object())))
        self.deps = make_deps(akshare=self.ak, openbb=self.openbb)

    def test_default_response_is_nbs_only_and_does_not_touch_openbb(self):
        result = v3_sources_ext.fetch_macro(self.deps, "cpi", limit=2)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "akshare/macro_china_cpi")
        self.assertIn("国家统计局", result["caliber"])
        self.assertEqual(result["latest"], {"period": "2026-08", "value": 0.8,
                                            "index": 100.4})
        self.assertNotIn("oecd", result)

    def test_compare_oecd_puts_both_calibers_side_by_side(self):
        result = v3_sources_ext.fetch_macro(self.deps, "cpi", limit=2, compare="oecd")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "akshare/macro_china_cpi")
        self.assertIn("国家统计局", result["caliber"])
        oecd = result["oecd"]
        self.assertTrue(oecd["ok"], oecd)
        self.assertEqual(oecd["source"], "openbb/economy.cpi(provider=oecd)")
        self.assertIn("OECD", oecd["caliber"])
        # 两口径各自带标注、互不冒充：NBS 给同比 %，OECD 给小数 + 显式 ×100
        self.assertNotEqual(result["caliber"], oecd["caliber"])
        self.assertEqual(oecd["latest"]["value"], 0.008)
        self.assertAlmostEqual(oecd["latest"]["value_pct"], 0.8, places=10)
        self.assertEqual(oecd["latest"]["period"], "2026-08")
        self.assertEqual(result["calibers"]["nbs"], result["caliber"])
        self.assertEqual(result["calibers"]["oecd"], oecd["caliber"])
        self.assertEqual(result["latest"]["value"], 0.8)  # 主源数值未被第二源改写

    def test_openbb_failure_is_error_envelope_not_empty_array(self):
        broken = SimpleNamespace(obb=SimpleNamespace(economy=SimpleNamespace(
            cpi=raising("openbb 取数失败"))))
        deps = make_deps(akshare=self.ak, openbb=broken)

        result = v3_sources_ext.fetch_macro(deps, "cpi", limit=2, compare="oecd")

        self.assertTrue(result["ok"], result)          # 主源仍在 → 成功响应
        oecd = result["oecd"]
        self.assertFalse(oecd["ok"])
        self.assertTrue(oecd["error"]["code"].startswith("openbb/"), oecd["error"])
        self.assertIn("openbb 取数失败", oecd["error"]["message"])
        self.assertNotIn("rows", oecd)                 # 空数组不算「第二源可用」
        self.assertIsNone(result["calibers"]["oecd"])

    def test_indicator_without_oecd_series_says_so(self):
        deps = make_deps(akshare=FakeAkshare(macro_china_ppi=NBS_PPI_ROWS),
                         openbb=self.openbb)

        result = v3_sources_ext.fetch_macro(deps, "ppi", limit=2, compare="oecd")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["oecd"]["error"]["code"], "oecd/no-series")
        self.assertIn("没有该指标", result["oecd"]["error"]["message"])

    def test_nbs_failure_degrades_to_oecd_with_relabeled_caliber(self):
        deps = make_deps(akshare=FakeAkshare(
            macro_china_cpi=ConnectionError("NBS 断连（测试替身）")), openbb=self.openbb)

        result = v3_sources_ext.fetch_macro(deps, "cpi", limit=2)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "openbb/economy.cpi(provider=oecd)")
        self.assertIn("OECD", result["caliber"])
        self.assertNotIn("国家统计局", result["caliber"])   # 换标，不冒充 NBS
        self.assertEqual(result["nbs_error"]["code"], "akshare/macro_china_cpi")
        self.assertIn("NBS 断连", result["nbs_error"]["message"])

    def test_missing_openbb_capability_is_reported(self):
        deps = make_deps(akshare=self.ak, openbb=SimpleNamespace(
            obb=SimpleNamespace(economy=SimpleNamespace())))

        result = v3_sources_ext.fetch_macro(deps, "cpi", limit=2, compare="oecd")

        self.assertEqual(result["oecd"]["error"]["code"], "openbb/capability-missing")

    def test_openbb_import_timeout_env_override(self):
        deps = make_deps(env={v3_sources_ext.MACRO_OPENBB_IMPORT_TIMEOUT_ENV: "9.5"})
        self.assertEqual(v3_sources_ext.macro_openbb_import_timeout(deps), 9.5)
        bad = make_deps(env={v3_sources_ext.MACRO_OPENBB_IMPORT_TIMEOUT_ENV: "abc"})
        self.assertEqual(v3_sources_ext.macro_openbb_import_timeout(bad),
                         v3_sources_ext.MACRO_OPENBB_IMPORT_TIMEOUT)

    def test_unknown_indicator_is_readable_error(self):
        result = v3_sources_ext.fetch_macro(self.deps, "gdp")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "macro/unknown-indicator")
        self.assertIn("cpi", result["error"]["message"])

    def test_chinese_alias_resolves_to_same_indicator(self):
        result = v3_sources_ext.fetch_macro(self.deps, "货币供应量", limit=1)

        # 别名解析到 m2（本测试只给 cpi 桩 → 该函数缺失，如实报 akshare/missing-func）
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "akshare/missing-func")
        self.assertIn("macro_china_money_supply", result["error"]["message"])


# ---------------------------------------------------------------------------
# ⑥ 富途 -9 钩子边界：只服务沪深 A 股
# ---------------------------------------------------------------------------
class FutuError(Exception):
    def __init__(self, code=-9):
        super().__init__("futu 返回 -9（A 股无实时权限）")
        self.code = code


class HookBoundaryTests(SourcesExtTestBase):
    def setUp(self):
        super().setUp()
        self.json_get = CallRecorder(lambda url, headers, timeout: v3_sources.envelope_error(
            "quote/network", "东财断连（测试替身）"))
        self.text_get = TextRecorder(lambda url, headers, encoding, timeout: (
            {"ok": True, "value": tencent_text()}))
        self.deps = make_deps(fetch_json=self.json_get, text_get=self.text_get)
        self.handler = v3_sources_ext._make_futu_fallback(self.deps)

    def test_public_code_resolution_is_explicit(self):
        self.assertEqual(v3_sources_ext.a_share_public_code("SH.600519"), ("SH", "600519"))
        self.assertEqual(v3_sources_ext.a_share_public_code("600519.SH"), ("SH", "600519"))
        self.assertEqual(v3_sources_ext.a_share_public_code("600519"), ("SH", "600519"))
        self.assertEqual(v3_sources_ext.a_share_public_code("000001"), ("SZ", "000001"))
        for ticker in ("HK.00700", "US.NVDA", "BJ.430047", "430047", "830799", "AAPL", ""):
            self.assertIsNone(v3_sources_ext.a_share_public_code(ticker), ticker)
        # 紧凑形态 ``sh600519``：docstring 一直声称支持，但此前实测返回 None（缺口已上报）。
        # 2026-09-21 主 agent 补齐：``sh``/``sz`` + 6 位数字（大小写不敏感）先做紧凑前缀映射，
        # 其余形态仍交 ``detect_market``。这里是**契约变更**（能力补齐），不是放宽断言。
        self.assertEqual(v3_sources_ext.a_share_public_code("sh600519"), ("SH", "600519"))
        self.assertEqual(v3_sources_ext.a_share_public_code("SZ000001"), ("SZ", "000001"))
        self.assertEqual(v3_sources_ext.a_share_public_code("sh60051"), None)   # 5 位不是合法代码
        self.assertEqual(v3_sources_ext.a_share_public_code("bj430047"), None)  # 北交所不在免密链

    def test_hk_us_bj_are_reported_unsupported_without_requests(self):
        for ticker in ("HK.00700", "US.NVDA", "BJ.430047"):
            result = v3_sources_ext.fetch_a_share_quote(self.deps, ticker)
            self.assertFalse(result["ok"], ticker)
            self.assertEqual(result["error"]["code"], "quote/unsupported-market", ticker)
            self.assertIn("只覆盖沪/深", result["error"]["message"])

        self.assertEqual(self.json_get.calls, [])
        self.assertEqual(self.text_get.calls, [])

    def test_order_book_for_foreign_ticker_is_unsupported_too(self):
        result = v3_sources_ext.fetch_a_share_order_book(self.deps, "HK.00700")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "quote/unsupported-market")
        self.assertNotIn("book_depth", result)
        self.assertEqual(self.json_get.calls, [])
        self.assertEqual(self.text_get.calls, [])

    def test_single_sh_ticker_falls_back_and_keeps_upstream_error(self):
        payload = self.handler("rt_quote", ["SH.600000"], FutuError())

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["source"], "tencent/qt.gtimg.cn")
        self.assertEqual(payload["futu_fallback"]["upstream_error"]["code"], -9)
        self.assertIn("-9", payload["futu_fallback"]["reason"])
        self.assertEqual(payload["errors"], [])
        self.assertEqual([item["code"] for item in payload["quotes"]], ["600000"])
        self.assertEqual(payload["quotes"][0]["quote"]["price"], 10.50)
        self.assertEqual(len(payload["chain"]), 2)  # 单标的：链明细一并带上

    def test_mixed_list_degrades_sh_and_reports_the_rest(self):
        payload = self.handler("rt_quote", ["SH.600000", "HK.00700", "US.NVDA"], FutuError())

        self.assertTrue(payload["ok"], payload)
        self.assertEqual([item["code"] for item in payload["quotes"]], ["600000"])
        self.assertEqual([item["code"] for item in payload["errors"]],
                         ["HK.00700", "US.NVDA"])
        for item in payload["errors"]:
            self.assertEqual(item["error"]["code"], "quote/unsupported-market")
        # 不编造：没有 HK/US 的报价行，只有如实错误
        self.assertNotIn("00700", [item.get("code") for item in payload["quotes"]])

    def test_all_foreign_list_returns_none_so_caller_rethrows_minus9(self):
        self.assertIsNone(self.handler("rt_quote", ["HK.00700", "US.NVDA"], FutuError()))
        self.assertEqual(self.json_get.calls, [])
        self.assertEqual(self.text_get.calls, [])

    def test_non_quote_endpoint_is_not_taken_over(self):
        self.assertIsNone(self.handler("capital_flow", ["SH.600000"], FutuError()))
        self.assertIsNone(self.handler("kline", ["SH.600000"], FutuError()))
        self.assertEqual(self.json_get.calls, [])

    def test_order_book_endpoint_carries_depth_fields(self):
        payload = self.handler("rt_order_book", ["SH.600000"], FutuError())

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["book_depth"], 4)
        self.assertIn("五档", payload["depth_note"])
        self.assertEqual(payload["code"], "600000")
        self.assertEqual(payload["book"]["depth"], 4)

    def test_quote_endpoint_does_not_carry_depth_fields(self):
        payload = self.handler("rt_quote", ["SH.600000"], FutuError())

        self.assertNotIn("book_depth", payload)
        self.assertNotIn("depth_note", payload)

    def test_install_hook_is_idempotent_and_uninstallable(self):
        self.assertFalse(futu_data.public_fallback_active())
        futu_data.install_public_fallback(self.handler)
        self.assertTrue(futu_data.public_fallback_active())
        futu_data.install_public_fallback(self.handler)
        self.assertTrue(futu_data.public_fallback_active())
        futu_data.uninstall_public_fallback()
        self.assertFalse(futu_data.public_fallback_active())


# ---------------------------------------------------------------------------
# ⑦ 路由回归：/api/v3/northbound（_head 未导入缺陷的护栏）
# ---------------------------------------------------------------------------
class RouteRegressionTests(SourcesExtTestBase):
    def setUp(self):
        super().setUp()
        self.ak = FakeAkshare(stock_hsgt_fund_flow_summary_em=SUMMARY_ROWS,
                              stock_hsgt_hist_em=HIST_ROWS,
                              macro_china_cpi=NBS_CPI_ROWS)
        self.deps = make_deps(akshare=self.ak)
        self.app = FastAPI()
        v3_sources_ext.register(self.app, lambda name, payload: {"ok": True, "value": {}},
                                home=None, deps=self.deps)
        self.client = TestClient(self.app)

    def test_head_is_imported_contract(self):
        # 缺陷本体：``_head`` 漏在 v3_sources_ext 的 import 清单外 → _northbound_hist NameError
        self.assertTrue(hasattr(v3_sources_ext, "_head"))
        self.assertIs(v3_sources_ext._head, v3_sources._head)

    def test_northbound_route_returns_200_with_data(self):
        response = self.client.get("/api/v3/northbound", params={"limit": 2})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("ok"), body)
        self.assertNotEqual((body.get("error") or {}).get("code"), "northbound/internal")
        self.assertEqual(body["history"]["SH"]["total_rows"], 3)   # _head 真的被走到
        self.assertEqual(body["history"]["SH"]["last_net_buy_date"], "2024-08-16")
        self.assertIs(body["net_buy_disclosure"]["northbound_daily_net_buy"], False)

    def test_macro_route_returns_200(self):
        response = self.client.get("/api/v3/macro",
                                   params={"indicator": "cpi", "limit": 2})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("ok"), body)
        self.assertEqual(body["source"], "akshare/macro_china_cpi")

    def test_register_exposes_state_and_installs_hook(self):
        state = self.app.state.v3_sources_ext

        self.assertEqual(state["routes"], ("/api/v3/northbound", "/api/v3/macro"))
        self.assertEqual(state["ext_chains"], ("rt_quote", "rt_order_book",
                                               "northbound", "macro"))
        self.assertTrue(futu_data.public_fallback_active())
        # 扩展链不进缺省探测集（缺省 8 条被既有单测锁定）
        self.assertEqual([spec["key"] for spec in v3_sources_ext.EXT_CHAIN_SPECS],
                         ["rt_quote", "rt_order_book", "northbound", "macro"])
        self.assertNotIn("rt_quote", {spec["key"] for spec in v3_fallback.CHAIN_SPECS})

    def test_ext_probes_are_built_without_network(self):
        probes = v3_sources_ext.build_ext_probes(
            lambda name, payload: {"ok": True, "value": {}}, home=None, deps=self.deps)

        self.assertEqual(sorted(probes),
                         ["macro", "northbound", "rt_order_book", "rt_quote"])
        values = [probe[1]() for probe in probes["northbound"]]
        self.assertTrue(all(value.get("ok") for value in values), values)


class ExtChainSpecsTests(SourcesExtTestBase):
    def test_ext_chain_specs_are_wellformed(self):
        for spec in v3_sources_ext.EXT_CHAIN_SPECS:
            self.assertEqual(sorted(spec), ["fallback", "key", "label", "primary"])
            self.assertTrue(spec["primary"])
        self.assertEqual(v3_sources_ext.EXT_SPEC_BY_KEY["macro"]["primary"],
                         "akshare/macro_china_cpi")
        self.assertEqual(set(v3_sources_ext._QUOTE_FETCHERS),
                         set(v3_sources_ext.PUBLIC_QUOTE_SOURCES))


# ---------------------------------------------------------------------------
# 附：v3_fundamentals_sync 免密替代源（本轮新增）的离线回归
# 归属说明：本轮任务只允许改 v3_fundamentals_sync.py 与本文件，故新单测落在这里。
# ---------------------------------------------------------------------------
from server import v3_fundamentals_sync as sync_mod  # noqa: E402
from trading_core import store  # noqa: E402


def make_balance(**periods):
    import pandas as pd
    return pd.DataFrame.from_dict(periods, orient="index").T.infer_objects()


#: 复刻 SH.600028 的实测形状：Yahoo 只给资产负债表侧科目，回报侧一行没有 → roe/roa 缺
BALANCE_ONLY = make_balance(**{"2025-06-30": {"Stockholders Equity": 8.24565e11,
                                              "Total Assets": 2.142807e12},
                               "2026-06-30": {"Stockholders Equity": 8.40901e11,
                                              "Total Assets": 2.197234e12}})
EM_FRAME = FakeFrame([
    {"REPORT_DATE": "2025-06-30 00:00:00", "ROEJQ": 2.61, "ZZCJLL": 1.1140243657},
    {"REPORT_DATE": "2026-06-30 00:00:00", "ROEJQ": 3.06, "ZZCJLL": 1.3774106581},
    {"REPORT_DATE": "2026-03-31 00:00:00", "ROEJQ": 2.04, "ZZCJLL": 0.8892153063},
])
SINA_FRAME = FakeFrame([
    {"日期": "2025-06-30", "净资产收益率(%)": 2.60, "总资产净利润率(%)": 1.1140},
    {"日期": "2026-06-30", "净资产收益率(%)": 3.05, "总资产净利润率(%)": 1.3774},
])


def period_registry(mapping, code="600028"):
    """假披露注册表：按 ``REPORTDATE`` 分派 NOTICE_DATE（缺该期 → 空表）。"""

    def fetch(params):
        match = re.search(r"REPORTDATE='([^']+)'", str(params.get("filter", "")))
        period = match.group(1) if match else ""
        notice = mapping.get(period)
        data = [] if notice is None else [
            {"SECURITY_CODE": code, "NOTICE_DATE": f"{notice} 00:00:00"}]
        return {"result": {"pages": 1, "data": data}}

    return fetch


REGISTRY_600028 = period_registry({"2025-06-30": "2025-08-22",
                                   "2026-06-30": "2026-08-24"})


class IndicatorFetcher:
    """假替代源取数口：``(func_name, arg) -> frame``；按函数名分派并记录调用。"""

    def __init__(self, **frames):
        self.frames = frames
        self.calls = []

    def __call__(self, func_name, arg):
        self.calls.append((func_name, arg))
        value = self.frames.get(func_name)
        if value is None:
            raise RuntimeError(f"测试替身：{func_name} 未提供假响应")
        if isinstance(value, BaseException):
            raise value
        return value

    def funcs(self):
        return [name for name, _ in self.calls]


class FundamentalsIndicatorFallbackTests(SourcesExtTestBase):
    def setUp(self):
        super().setUp()
        sync_mod._REGISTRIES.clear()
        self.addCleanup(sync_mod._REGISTRIES.clear)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = store.connect(Path(self._tmp.name) / "trading.sqlite")
        self.addCleanup(self.conn.close)

    def sync(self, ticker="SH.600028", **kwargs):
        return sync_mod.sync(
            self.conn, market="SH", tickers=[ticker], periods=kwargs.pop("periods", None),
            fetcher=lambda t: (BALANCE_ONLY, None),
            registry_fetcher=kwargs.pop("registry_fetcher", REGISTRY_600028),
            sleep_seconds=0, **kwargs)

    def rows(self, period="2026-06-30"):
        return {row["field"]: row for row in self.conn.execute(
            "SELECT field, period_end, value, source, announced_at, announced_source"
            " FROM fundamentals WHERE symbol='SH.600028' AND period_end=?", (period,))}

    def test_yahoo_missing_ratios_are_filled_by_em_indicator(self):
        indicator = IndicatorFetcher(stock_financial_analysis_indicator_em=EM_FRAME)

        result = self.sync(indicator_fetcher=indicator)

        self.assertTrue(result["ok"], result)
        self.assertEqual(indicator.calls,
                         [("stock_financial_analysis_indicator_em", "600028.SH")])
        rows = self.rows()
        # 免密替代源补的两个比率：source 逐行如实，披露日沿用既有 NOTICE_DATE 口径
        self.assertEqual(rows["roe"]["value"], 3.06)
        self.assertEqual(rows["roa"]["value"], 1.38)
        self.assertEqual(rows["roe"]["source"], sync_mod.SOURCE_AK_EM_INDICATOR)
        self.assertEqual(rows["roe"]["announced_at"], "2026-08-24")
        self.assertEqual(rows["roe"]["announced_source"], sync_mod.SOURCE_NOTICE_DATE)
        # Yahoo 腿的科目保持原来源（不被替代源的 source 覆盖）
        self.assertEqual(rows["equity"]["source"], "yahoo/yfinance")
        self.assertEqual(rows["equity"]["value"], 8.40901e11)
        self.assertEqual(result["written"][0]["sources"],
                         {"yahoo/yfinance": 4, sync_mod.SOURCE_AK_EM_INDICATOR: 4})
        self.assertEqual(result["fallback"][0]["source"], sync_mod.SOURCE_AK_EM_INDICATOR)
        self.assertIn("加权净资产收益率", result["fallback"][0]["caliber"])
        self.assertEqual(result["fallback_skipped"], [])
        self.assertEqual(result["fallback_failures"], [])

    def test_yahoo_present_ratios_never_call_the_fallback(self):
        balance = make_balance(**{"2026-06-30": {"Stockholders Equity": 1.0e11,
                                                 "Total Assets": 1.0e12}})
        income = make_balance(**{"2026-06-30": {"Net Income": 5.0e9}})
        indicator = IndicatorFetcher(stock_financial_analysis_indicator_em=EM_FRAME)

        result = sync_mod.sync(
            self.conn, market="SH", tickers=["SH.600000"],
            fetcher=lambda t: (balance, income),
            registry_fetcher=period_registry({"2026-06-30": "2026-08-24"},
                                             code="600000"),
            sleep_seconds=0, indicator_fetcher=indicator)

        self.assertTrue(result["ok"], result)
        self.assertEqual(indicator.calls, [])      # 降级不是替换：Yahoo 有值就不碰替代源
        self.assertEqual(result["fallback"], [])
        row = self.conn.execute(
            "SELECT value, source FROM fundamentals"
            " WHERE symbol='SH.600000' AND field='roe'").fetchone()
        self.assertEqual(row["source"], "yahoo/yfinance")
        self.assertEqual(row["value"], 5.0)         # 净利/权益 = 5%

    def test_partial_gap_only_fills_the_missing_ratio(self):
        # Yahoo 只算得出 roe（有权益、无总资产 → roa 缺）：只补 roa，Yahoo 的 roe 不被覆盖
        balance = make_balance(**{"2026-06-30": {"Stockholders Equity": 1.0e11}})
        income = make_balance(**{"2026-06-30": {"Net Income": 5.0e9}})
        indicator = IndicatorFetcher(stock_financial_analysis_indicator_em=EM_FRAME)

        result = sync_mod.sync(
            self.conn, market="SH", tickers=["SH.600000"], periods=["20260630"],
            fetcher=lambda t: (balance, income),
            registry_fetcher=period_registry({"2026-06-30": "2026-08-24"}, code="600000"),
            sleep_seconds=0, indicator_fetcher=indicator)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["fallback"][0]["periods"], ["2026-06-30"])
        rows = {row["field"]: row for row in self.conn.execute(
            "SELECT field, value, source FROM fundamentals WHERE symbol='SH.600000'")}
        self.assertEqual(rows["roe"]["source"], "yahoo/yfinance")   # 降级不是替换
        self.assertEqual(rows["roe"]["value"], 5.0)
        self.assertEqual(rows["roa"]["source"], sync_mod.SOURCE_AK_EM_INDICATOR)
        self.assertEqual(rows["roa"]["value"], 1.38)

    def test_em_failure_falls_through_to_sina_with_attempts(self):
        indicator = IndicatorFetcher(
            stock_financial_analysis_indicator_em=RuntimeError("东财 500（测试替身）"),
            stock_financial_analysis_indicator=SINA_FRAME)

        result = self.sync(indicator_fetcher=indicator)

        self.assertTrue(result["ok"], result)
        self.assertEqual(indicator.funcs(),
                         ["stock_financial_analysis_indicator_em",
                          "stock_financial_analysis_indicator"])
        self.assertEqual(indicator.calls[1], ("stock_financial_analysis_indicator", "600028"))
        attempts = result["fallback"][0]["attempts"]
        self.assertEqual([(item["source"], item["ok"]) for item in attempts],
                         [(sync_mod.SOURCE_AK_EM_INDICATOR, False),
                          (sync_mod.SOURCE_AK_SINA_INDICATOR, True)])
        self.assertIn("东财 500", attempts[0]["error"])
        rows = self.rows()
        self.assertEqual(rows["roe"]["source"], sync_mod.SOURCE_AK_SINA_INDICATOR)
        self.assertEqual(rows["roe"]["value"], 3.05)

    def test_all_indicator_sources_failure_is_reported_and_nothing_faked(self):
        indicator = IndicatorFetcher(
            stock_financial_analysis_indicator_em=RuntimeError("东财 500（测试替身）"),
            stock_financial_analysis_indicator=RuntimeError("新浪超时（测试替身）"))

        result = self.sync(indicator_fetcher=indicator)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["fallback"], [])
        reason = result["fallback_failures"][0]["reason"]
        self.assertIn("东财 500", reason)
        self.assertIn("新浪超时", reason)
        self.assertEqual([row["field"] for row in self.conn.execute(
            "SELECT field FROM fundamentals WHERE symbol='SH.600028'"
            " AND field IN ('roe','roa')")], [])
        # Yahoo 腿的行照常落库（失败不牵连已成功的那条腿）
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM fundamentals WHERE symbol='SH.600028'"
            " AND source='yahoo/yfinance'").fetchone()[0], 4)

    def test_empty_indicator_table_is_not_success(self):
        indicator = IndicatorFetcher(stock_financial_analysis_indicator_em=FakeFrame([]),
                                     stock_financial_analysis_indicator=SINA_FRAME)

        result = self.sync(indicator_fetcher=indicator)

        self.assertTrue(result["ok"], result)
        first = result["fallback"][0]["attempts"][0]
        self.assertFalse(first["ok"])
        self.assertIn("空表不当作成功", first["error"])
        self.assertEqual(result["fallback"][0]["source"], sync_mod.SOURCE_AK_SINA_INDICATOR)

    def test_missing_notice_date_blocks_fallback_rows_too(self):
        indicator = IndicatorFetcher(stock_financial_analysis_indicator_em=EM_FRAME)

        result = self.sync(indicator_fetcher=indicator,
                           registry_fetcher=period_registry({}))

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["rows_written"], 0)
        self.assertEqual(result["written"], [])
        self.assertTrue(any("宁缺毋假" in item["reason"] for item in result["skipped"]))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM fundamentals").fetchone()[0], 0)

    def test_non_a_share_ticker_never_uses_the_a_share_indicator(self):
        hk_balance = make_balance(**{"2025-06-30": {"Stockholders Equity": 9.0e11,
                                                    "Total Assets": 1.2e12}})
        indicator = IndicatorFetcher(stock_financial_analysis_indicator_em=EM_FRAME)

        result = sync_mod.sync(
            self.conn, market="HK", tickers=["HK.00700"], periods=["20250630"],
            fetcher=lambda t: (hk_balance, None), sleep_seconds=0, today="2026-09-21",
            indicator_fetcher=indicator)

        self.assertTrue(result["ok"], result)
        self.assertEqual(indicator.calls, [])
        self.assertTrue(any("非 A 股" in item["reason"]
                            for item in result["fallback_skipped"]))
        self.assertEqual([row["field"] for row in self.conn.execute(
            "SELECT field FROM fundamentals WHERE field IN ('roe','roa')")], [])

    def test_dry_run_previews_fallback_rows_without_writing(self):
        indicator = IndicatorFetcher(stock_financial_analysis_indicator_em=EM_FRAME)

        result = self.sync(indicator_fetcher=indicator, dry_run=True)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["written"][0]["sources"],
                         {"yahoo/yfinance": 4, sync_mod.SOURCE_AK_EM_INDICATOR: 4})
        self.assertEqual(result["rows_written"], 8)
        self.assertEqual(result["fallback"][0]["periods"],
                         ["2025-06-30", "2026-06-30"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM fundamentals").fetchone()[0], 0)

    def test_rerun_is_idempotent_with_fallback_rows(self):
        indicator = IndicatorFetcher(stock_financial_analysis_indicator_em=EM_FRAME)

        first = self.sync(indicator_fetcher=indicator)
        count_first = self.conn.execute(
            "SELECT COUNT(*) FROM fundamentals").fetchone()[0]
        second = self.sync(indicator_fetcher=indicator)
        count_second = self.conn.execute(
            "SELECT COUNT(*) FROM fundamentals").fetchone()[0]

        self.assertEqual(first["rows_written"], second["rows_written"])
        self.assertEqual(count_first, count_second)
        row = self.conn.execute(
            "SELECT announced_at, source FROM fundamentals"
            " WHERE symbol='SH.600028' AND field='roe' AND period_end='2026-06-30'"
        ).fetchone()
        self.assertEqual(row["announced_at"], "2026-08-24")   # 重跑不改写披露日
        self.assertEqual(row["source"], sync_mod.SOURCE_AK_EM_INDICATOR)

    def test_fallback_disabled_keeps_yahoo_only(self):
        indicator = IndicatorFetcher(stock_financial_analysis_indicator_em=EM_FRAME)

        result = self.sync(indicator_fetcher=indicator, indicator_fallback=False)

        self.assertTrue(result["ok"], result)
        self.assertEqual(indicator.calls, [])
        self.assertEqual(result["fallback"], [])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM fundamentals WHERE field IN ('roe','roa')"
        ).fetchone()[0], 0)

    def test_missing_ratios_helper_only_lists_gaps(self):
        data = {"periods": {"2025-06-30": {"roe": 1.0, "roa": 2.0},
                            "2026-06-30": {"roe": None, "roa": 2.0},
                            "2026-03-31": {"roe": 1.0, "roa": None}}}
        self.assertEqual(sync_mod._missing_ratios(data, sorted(data["periods"])),
                         ["2026-03-31", "2026-06-30"])
        self.assertEqual(sync_mod._missing_ratios(data, ["2025-06-30"]), [])

    def test_indicator_channel_rejects_non_a_share(self):
        with self.assertRaises(RuntimeError) as caught:
            sync_mod.fetch_indicator_returns("US.NVDA", fetcher=IndicatorFetcher())
        self.assertIn("不是 A 股标的", str(caught.exception))

    def test_indicator_entries_skip_dirty_periods_and_blank_ratios(self):
        frame = FakeFrame([
            {"REPORT_DATE": None, "ROEJQ": 1.0, "ZZCJLL": 1.0},          # 无报告期 → 丢
            {"REPORT_DATE": "2026-06-30 00:00:00", "ROEJQ": "-", "ZZCJLL": None},  # 无值 → 丢
            {"REPORT_DATE": "2026-06-30 00:00:00", "ROEJQ": 3.06, "ZZCJLL": "-"},
        ])
        entries = sync_mod._indicator_entries(
            frame, sync_mod.INDICATOR_SOURCES[0], {"2026-06-30"})
        self.assertEqual(entries, {"2026-06-30": {"roe": 3.06, "roa": None}})

    def test_cli_flag_wires_fallback_toggle_offline(self):
        db = Path(self._tmp.name) / "cli.sqlite"
        with unittest.mock.patch.object(sync_mod, "sync",
                                        return_value={"ok": True}) as fake:
            with contextlib.redirect_stdout(io.StringIO()):  # main() 会打印摘要 JSON
                exit_code = sync_mod.main(["--market", "SH", "--tickers", "SH.600028",
                                           "--no-indicator-fallback", "--db", str(db)])
        self.assertEqual(exit_code, 0)
        self.assertIs(fake.call_args.kwargs["indicator_fallback"], False)
        with unittest.mock.patch.object(sync_mod, "sync",
                                        return_value={"ok": True}) as fake:
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = sync_mod.main(["--market", "SH", "--tickers", "SH.600028",
                                           "--db", str(db)])
        self.assertEqual(exit_code, 0)
        self.assertIs(fake.call_args.kwargs["indicator_fallback"], True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
