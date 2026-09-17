"""WP13 任务 0 护栏：futu_openapi 拆包后「历史命名空间可用 + 路径一律来自 paths 常量」。

拆包（WP13 任务 0）是纯机械重构，零行为变化；本文件是它的三条不变式：

1. **历史命名空间**：`from trading_datasource.futu_openapi import X` 在拆包前后都必须可用
   （含历史上经 `fo.base64` / `fo.hashes` / `fo.padding` 访问的 stdlib/加密库模块名）；
2. **禁止内联路径字面量**：除 `paths.py` 外，包内任何模块的**代码**（docstring 除外）
   不得出现 `/api/v1.0` 字面量——所有路径必须引用 `paths` 常量，否则「锁定测试绑定路径」
   的不变式会被绕过（WP12 质量审查遗留）；
3. **旧组路径零漂移**：`OpenApiMarket` / `OpenApiTrade` 的 31 个 (方法 → HTTP 方法 + 路径)
   与拆包前源码逐条一致（期望表由 `git show <拆包前提交>:…/futu_openapi.py` 经 AST 提取）。
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from trading_datasource import futu_openapi as fo  # noqa: E402
from trading_datasource.futu_openapi import paths  # noqa: E402

PKG = Path(fo.__file__).resolve().parent

#: 拆包前模块级公开名（逐条人工核对；含被外部引用的私有名与 stdlib 透出名）
HISTORICAL_NAMES = (
    # 常量
    "DEFAULT_HOST", "TOKEN_PATH", "NONCE_ALPHABET", "NONCE_PATTERN",
    # 异常族
    "OpenApiError", "TransportError", "UnexpectedResponse", "OrderConfirmRequired",
    # 认证与凭据
    "Pkce", "AppKeySigner", "CredentialStore", "default_credential_path",
    # 信封
    "json_body_bytes", "query_string", "parse_envelope", "parse_envelope_meta",
    "_safe_json_dict",
    # 客户端与校验基类
    "OpenApiClient", "_default_http", "_RestValidators",
    # 方法组
    "OpenApiMarket", "OpenApiTrade", "OpenApiScreen", "OpenApiPlate", "OpenApiShort",
    "OpenApiBasicData", "OpenApiIpo", "OpenApiWatchlist", "OpenApiDerivatives", "OpenApiF10",
    # 锁定面
    "WP12_F10_ENDPOINTS", "WP12_TRANSPORT_ENDPOINTS",
    # 路径常量（抽样覆盖各分组）
    "QUOTE_SNAPSHOT_PATH", "CUR_KLINE_PATH", "ACCOUNT_ORDERS_PATH",
    "ACCOUNT_ORDER_DETAIL_PATH", "AUTHORIZED_ACCOUNTS_PATH",
    "STOCK_SCREEN_PATH", "PLATE_LIST_PATH", "SHORT_DAILY_VOLUME_PATH",
    "F10_ANALYST_CONSENSUS_PATH", "F10_STATEMENTS_PATH",
    # stdlib / 加密库透出名（tests/test_wp8_push.py 经 fo.base64 等访问）
    "base64", "hashlib", "json", "os", "re", "secrets", "string", "tempfile", "time",
    "urllib", "http", "Path", "hashes", "padding", "rsa", "serialization", "Ed25519PrivateKey",
)

#: 拆包前 `OpenApiMarket` / `OpenApiTrade` 的 (方法 → HTTP 方法 + 路径模板)，AST 提取
LEGACY_PATH_EXPECTATIONS = {
    ("OpenApiMarket", "capital_distribution"): ("GET", "/api/v1.0/quote/{symbol}/capital-distribution"),
    ("OpenApiMarket", "capital_flow"): ("GET", "/api/v1.0/quote/{symbol}/capital-flow"),
    ("OpenApiMarket", "capital_flow_history"): ("GET", "/api/v1.0/quote/{symbol}/capital-flow/history"),
    ("OpenApiMarket", "cur_kline"): ("GET", "/api/v1.0/quote/{symbol}/cur-kline"),
    ("OpenApiMarket", "history_kline"): ("GET", "/api/v1.0/quote/{symbol}/history-kline"),
    ("OpenApiMarket", "market_snapshot"): ("POST", "/api/v1.0/quote/snapshot"),
    ("OpenApiMarket", "market_state"): ("POST", "/api/v1.0/quote/market-state"),
    ("OpenApiMarket", "option_chain"): ("GET", "/api/v1.0/quote/{symbol}/option-chain"),
    ("OpenApiMarket", "option_expiration"): ("GET", "/api/v1.0/quote/{symbol}/option-expiration"),
    ("OpenApiMarket", "option_screen"): ("POST", "/api/v1.0/quote/option-screen"),
    ("OpenApiMarket", "order_book"): ("POST", "/api/v1.0/quote/order-book"),
    ("OpenApiMarket", "rt_data"): ("GET", "/api/v1.0/quote/{symbol}/rt-data"),
    ("OpenApiMarket", "rt_ticker"): ("GET", "/api/v1.0/quote/{symbol}/rt-ticker"),
    ("OpenApiMarket", "search_community"): ("GET", "/api/v1.0/quote/find-community"),
    ("OpenApiMarket", "search_news"): ("GET", "/api/v1.0/quote/find-news"),
    ("OpenApiMarket", "stock_basicinfo"): ("POST", "/api/v1.0/quote/stock-basicinfo"),
    ("OpenApiMarket", "stock_quote"): ("POST", "/api/v1.0/quote/stock-quote"),
    ("OpenApiMarket", "trading_days"): ("GET", "/api/v1.0/quote/trading-days"),
    ("OpenApiTrade", "account_funds"): ("GET", "/api/v1.0/accounts/{acc_id}/funds"),
    ("OpenApiTrade", "authorized_accounts"): ("GET", "/api/v1.0/accounts/authorized_trd_accs"),
    ("OpenApiTrade", "cancel_order"): ("DELETE", "/api/v1.0/accounts/{acc_id}/orders/{order_id}"),
    ("OpenApiTrade", "history_deals"): ("GET", "/api/v1.0/accounts/{acc_id}/fills_history"),
    ("OpenApiTrade", "history_orders"): ("GET", "/api/v1.0/accounts/{acc_id}/orders_history"),
    ("OpenApiTrade", "max_trade_qty"): ("GET", "/api/v1.0/accounts/{acc_id}/acctradinginfo"),
    ("OpenApiTrade", "modify_order"): ("PUT", "/api/v1.0/accounts/{acc_id}/orders/{order_id}"),
    ("OpenApiTrade", "open_orders"): ("GET", "/api/v1.0/accounts/{acc_id}/orders"),
    ("OpenApiTrade", "order_confirm"): ("POST", "/api/v1.0/accounts/{acc_id}/order_confirm"),
    ("OpenApiTrade", "order_details"): ("POST", "/api/v1.0/accounts/{acc_id}/orders/detail"),
    ("OpenApiTrade", "place_order"): ("POST", "/api/v1.0/accounts/{acc_id}/orders"),
    ("OpenApiTrade", "positions"): ("GET", "/api/v1.0/accounts/{acc_id}/positions"),
    ("OpenApiTrade", "today_deals"): ("GET", "/api/v1.0/accounts/{acc_id}/order_fills"),
}

PREFIX = "/api/v1.0"


def _module_files():
    return sorted(p for p in PKG.rglob("*.py") if p.name != "paths.py")


def _docstring_nodes(tree):
    """收集所有 docstring 常量节点（用于把文档里的路径引用排除在扫描外）。"""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


def _literal_paths(path):
    """返回源码文件里「代码位置」的 /api/v1.0 字面量（docstring 除外）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstring_nodes(tree)
    hits = []
    for node in ast.walk(tree):
        if id(node) in docs:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value.startswith(PREFIX):
            hits.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            head = "".join(v.value for v in node.values
                           if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if head.startswith(PREFIX):
                hits.append((node.lineno, head))
    return hits


def _is_paths_ref(node):
    """``paths.X`` 或 ``paths.X.format(...)``。"""
    if isinstance(node, ast.Call):
        node = node.func
        if isinstance(node, ast.Attribute) and node.attr == "format":
            node = node.value
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
        and node.value.id == "paths"


def _path_arg(call):
    """从 self.client.request(...) 调用取路径实参的常量名（必须是 paths.X / paths.X.format(...)）。"""
    if not call.args:
        return None, "缺路径实参"
    if len(call.args) < 2:
        return None, "只有 1 个实参，路径位置缺失"
    arg = call.args[1]
    if _is_paths_ref(arg):
        node = arg.func.value if isinstance(arg, ast.Call) else arg
        return node.attr, None
    if isinstance(arg, ast.Name):
        # 允许 path 参数转发（如 F10 的 _page/_get 助手）；调用点由
        # PathForwardingHelperTests 逐处验证必须传 paths 常量。
        return arg.id, "forwarded"
    return None, f"路径实参未引用 paths 常量：{ast.unparse(arg)[:60]}"


def _request_calls(tree):
    """全部 ``<x>.client.request/request_meta`` 调用。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("request", "request_meta") \
                and isinstance(node.func.value, ast.Attribute) \
                and node.func.value.attr == "client":
            yield node


def _forwarding_helpers(tree):
    """返回 {助手方法名: 路径参数位置}——路径实参是其形参的私有助手。"""
    helpers = {}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
            params = [a.arg for a in fn.args.args]
            for call in _request_calls(fn):
                if len(call.args) >= 2 and isinstance(call.args[1], ast.Name):
                    name = call.args[1].id
                    if name in params:
                        # 方法形参含隐式 self → 调用点实参位置需减 1
                        helpers[fn.name] = params.index(name) - 1
    return helpers


class HistoricalNamespaceTests(unittest.TestCase):
    """① 拆包不得静默破坏任何历史导入。"""

    def test_historical_names_reexported(self):
        missing = [n for n in HISTORICAL_NAMES if not hasattr(fo, n)]
        self.assertEqual(missing, [], f"拆包丢失历史命名空间：{missing}")

    def test_submodule_layout_is_the_documented_one(self):
        for rel in ("errors.py", "auth.py", "envelope.py", "client.py", "validators.py",
                    "paths.py", "surface.py", "groups/market.py", "groups/trade.py",
                    "groups/dataplane.py", "groups/f10.py"):
            with self.subTest(module=rel):
                self.assertTrue((PKG / rel).is_file(), f"缺模块 {rel}")

    def test_paths_module_is_the_single_literal_home(self):
        self.assertGreaterEqual(len(_literal_paths(PKG / "paths.py")), 60,
                                "paths.py 应集中全部路径常量")


class NoInlinePathLiteralTests(unittest.TestCase):
    """② 除 paths.py 外，代码里不得再有 /api/v1.0 字面量。"""

    def test_no_inline_literals_in_package_modules(self):
        offenders = []
        for path in _module_files():
            for lineno, value in _literal_paths(path):
                offenders.append(f"{path.relative_to(PKG)}:{lineno} {value}")
        self.assertEqual(offenders, [], "发现内联路径字面量（应提升为 paths 常量）：\n" +
                         "\n".join(offenders))

    def test_every_request_call_uses_paths_constant(self):
        problems = []
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in _request_calls(tree):
                name, err = _path_arg(node)
                if err == "forwarded":
                    continue  # 由 PathForwardingHelperTests 逐调用点验证
                if err:
                    problems.append(f"{path.relative_to(PKG)}:{node.lineno} {err}")
                elif not hasattr(paths, name):
                    problems.append(f"{path.relative_to(PKG)}:{node.lineno} "
                                    f"paths.{name} 不存在")
        self.assertEqual(problems, [], "存在未绑定 paths 常量的请求路径：\n" + "\n".join(problems))


class PathForwardingHelperTests(unittest.TestCase):
    """②b 路径转发助手（如 F10 的 _page/_get）的每个调用点必须传 paths 常量。"""

    def test_forwarding_call_sites_pass_paths_constants(self):
        problems = []
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            helpers = _forwarding_helpers(tree)
            if not helpers:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                pos = helpers.get(node.func.attr)
                if pos is None:
                    continue
                args = list(node.args)
                arg = args[pos] if pos < len(args) else None
                if arg is None or not _is_paths_ref(arg):
                    problems.append(f"{path.relative_to(PKG)}:{node.lineno} "
                                    f"{node.func.attr}(...) 第 {pos + 1} 实参不是 paths 常量")
        self.assertEqual(problems, [], "路径转发助手的调用点未传 paths 常量：\n" + "\n".join(problems))


class LegacyGroupPathRegressionTests(unittest.TestCase):
    """③ 旧组 31 条 (方法 → HTTP 方法 + 路径) 与拆包前逐条一致。"""

    MODULES = {"OpenApiMarket": PKG / "groups" / "market.py",
               "OpenApiTrade": PKG / "groups" / "trade.py"}

    def _implemented(self, cls_name):
        tree = ast.parse(self.MODULES[cls_name].read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name)
        out = {}
        for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
            for node in _request_calls(fn):
                http = ast.literal_eval(node.args[0]) if node.args and \
                    isinstance(node.args[0], ast.Constant) else None
                name, err = _path_arg(node)
                if err == "forwarded":
                    continue  # 旧组无转发助手；出现即说明结构变了，由方法集合断言兜底
                out[fn.name] = (http, name)
                break
        return {k: v for k, v in out.items() if v[1]}

    def test_expected_table_size(self):
        self.assertEqual(len(LEGACY_PATH_EXPECTATIONS), 31,
                         "旧组端点应为 31 条（18 行情 + 13 交易）")

    def test_every_legacy_method_matches_pre_split_path(self):
        for cls_name in self.MODULES:
            implemented = self._implemented(cls_name)
            expected_methods = {m for (c, m) in LEGACY_PATH_EXPECTATIONS if c == cls_name}
            self.assertEqual(set(implemented), expected_methods,
                             f"{cls_name} 方法集合与拆包前不一致")
            for method, (http, const_name) in implemented.items():
                want_http, want_path = LEGACY_PATH_EXPECTATIONS[(cls_name, method)]
                with self.subTest(endpoint=f"{cls_name}.{method}"):
                    self.assertEqual(getattr(paths, const_name), want_path)
                    self.assertEqual(http, want_http)


class LockSurfaceIntegrityTests(unittest.TestCase):
    """④ 锁定面声明仍指向 paths 常量（值即常量值，防漂移）。"""

    def test_transport_surface_values_are_path_constants(self):
        known = {getattr(paths, n) for n in dir(paths) if n.endswith("_PATH")}
        for key, (http, path) in fo.WP12_TRANSPORT_ENDPOINTS.items():
            with self.subTest(endpoint=str(key)):
                self.assertIn(path, known, f"{key} 的路径不在 paths.py")
                self.assertIn(http, ("GET", "POST"))

    def test_f10_surface_values_are_path_constants(self):
        known = {getattr(paths, n) for n in dir(paths) if n.endswith("_PATH")}
        for key, (http, path) in fo.WP12_F10_ENDPOINTS.items():
            with self.subTest(endpoint=str(key)):
                self.assertIn(path, known, f"{key} 的路径不在 paths.py")


if __name__ == "__main__":
    unittest.main()
