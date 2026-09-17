"""WP12 代码质量审查「重要 1」：``openapi_ready`` 双实现等价矩阵。

core 的 ``trading_core.research_sync.openapi_ready`` 是服务端
``server.futu_data.openapi_ready`` 的**刻意镜像**（core 不得 import ``server`` 包，
依赖方向固定），两份实现的 docstring 各自声明「判据如有变更需两处同步」，但此前
**没有测试强制**——漂移后果是静默降级：作业认为无凭据而软跳过（研究数据不积累），
或认为有凭据、然后白烧一次链路才在签名期失败。

本文件用**同一凭据文件矩阵**喂两个实现，断言逐例同真值（手法沿用
``tests/test_wp12_locks.py::test_dataplane_ttls_mirror_server`` 的镜像断言：
两处都 import，逐例比对）。

矩阵逐条覆盖两份实现 docstring 声明的判据：oauth 有 token / appkey 私钥可加载 /
文件缺失 / 坏 JSON / mode 未配置 / 私钥文件缺失 / 坏 PEM / 算法不支持。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "platform"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey)

from server import futu_data  # noqa: E402  （服务端实现；测试内可 import，core 不可）
from trading_core import research_sync  # noqa: E402


def _pem(key):
    """cryptography 私钥 → PKCS8 PEM（与 tests/test_wp8_openapi_client.py::_pem 同规）。"""
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


class OpenApiReadyEquivalenceTests(unittest.TestCase):
    """同一矩阵下两个实现必须同真值——任一处判据漂移都必须让本矩阵变红。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    # ---- 凭据夹具 ----

    def _write(self, **fields):
        path = self.home / "futu-openapi.json"
        path.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")
        return path

    def _write_raw(self, text):
        path = self.home / "futu-openapi.json"
        path.write_text(text, encoding="utf-8")
        return path

    def _good_key_file(self):
        path = self.home / "app.key"
        path.write_bytes(_pem(Ed25519PrivateKey.generate()))
        return path

    def _broken_key_file(self):
        path = self.home / "broken.key"
        path.write_bytes(b"-----BEGIN PRIVATE KEY-----\nnot-base64\n"
                         b"-----END PRIVATE KEY-----\n")
        return path

    # ---- 断言：逐例同真值 ----

    def _assert_same_truth(self, path, expected, label):
        core = research_sync.openapi_ready(path)
        server = futu_data.openapi_ready(path)
        self.assertEqual(core, expected, f"{label}：core 判据与期望不符")
        self.assertEqual(server, expected, f"{label}：server 判据与期望不符")
        self.assertEqual(core, server, f"{label}：两实现真值漂移")

    # ---- 矩阵 ----

    def test_missing_file_unusable(self):
        self._assert_same_truth(self.home / "absent.json", False, "凭据文件缺失")

    def test_corrupt_json_unusable(self):
        self._assert_same_truth(self._write_raw("{not json"), False, "凭据 JSON 损坏")

    def test_oauth_access_token_usable(self):
        path = self._write(mode="oauth", access_token="tok", refresh_token="rtok")
        self._assert_same_truth(path, True, "oauth 有 access_token")

    def test_oauth_refresh_token_only_usable(self):
        self._assert_same_truth(self._write(mode="oauth", refresh_token="rtok"),
                                True, "oauth 仅 refresh_token")

    def test_oauth_without_any_token_unusable(self):
        self._assert_same_truth(self._write(mode="oauth", client_id="cid"),
                                False, "oauth 无任何 token")

    def test_appkey_with_loadable_key_usable(self):
        path = self._write(mode="appkey", app_key="ak",
                           private_key_path=str(self._good_key_file()))
        self._assert_same_truth(path, True, "appkey 私钥可加载")

    def test_appkey_missing_key_file_unusable(self):
        path = self._write(mode="appkey", app_key="ak",
                           private_key_path=str(self.home / "absent.key"))
        self._assert_same_truth(path, False, "appkey 私钥文件缺失")

    def test_appkey_broken_pem_unusable(self):
        path = self._write(mode="appkey", app_key="ak",
                           private_key_path=str(self._broken_key_file()))
        self._assert_same_truth(path, False, "appkey 坏 PEM")

    def test_appkey_without_key_path_unusable(self):
        self._assert_same_truth(self._write(mode="appkey", app_key="ak"),
                                False, "appkey 缺 private_key_path")

    def test_appkey_unknown_algorithm_unusable(self):
        path = self._write(mode="appkey", app_key="ak",
                           private_key_path=str(self._good_key_file()),
                           algorithm="HS256")
        self._assert_same_truth(path, False, "appkey 算法不支持")

    def test_unknown_mode_unusable(self):
        self._assert_same_truth(self._write(mode="apikey", app_key="ak"),
                                False, "mode 未配置/未知")


if __name__ == "__main__":
    unittest.main()
