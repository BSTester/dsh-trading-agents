"""``server/v3_credentials.py`` 契约测试（页面化密钥配置）。

离线：临时 home + 注入 ``get_env`` / ``http`` 替身，不打网络、不读真实 ``~/.dsh``。
钉死的纪律：
  * 凭据文件 0600，原子写；
  * **任何响应都不含明文密钥**（status / save / clear / test 四条路径逐条断言）；
  * 环境变量优先于文件；
  * 未配置时 test 不发请求（no-token）；
  * 保存前校验（空/过短/含空白一律拒绝，且**文件零改动**）。
"""
import json
import os
import stat
import tempfile
import unittest

import sys
from pathlib import Path

PLATFORM = str(Path(__file__).resolve().parents[1])
if PLATFORM not in sys.path:
    sys.path.insert(0, PLATFORM)

from server import v3_credentials as creds  # noqa: E402

SECRET = "0123456789abcdef0123456789abcdef"


class CredentialsTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="v3cred-")

    def tearDown(self):
        for name in os.listdir(self.home):
            os.unlink(os.path.join(self.home, name))
        os.rmdir(self.home)

    # ── 读取/状态 ───────────────────────────────────────────────────────────
    def test_status_without_file_reports_absent_and_creates_nothing(self):
        body = creds.status(self.home)
        self.assertTrue(body["ok"])
        entry = body["keys"][0]
        self.assertFalse(entry["present"])
        self.assertIsNone(entry["value"] if "value" in entry else None)
        self.assertFalse(os.path.exists(creds.credential_path(self.home)))

    # ── 保存 ────────────────────────────────────────────────────────────────
    def test_save_writes_0600_and_never_echoes_value(self):
        body = creds.save(self.home, "tushare_token", SECRET)
        self.assertTrue(body["ok"])
        entry = body["keys"][0]
        self.assertTrue(entry["present"])
        self.assertEqual(entry["source"], "页面配置（v3-credentials.json）")
        self.assertEqual(entry["hint"], "…" + SECRET[-4:])
        self.assertNotIn(SECRET, json.dumps(body, ensure_ascii=False))
        mode = stat.S_IMODE(os.stat(creds.credential_path(self.home)).st_mode)
        self.assertEqual(mode, 0o600)

    def test_save_rejects_invalid_values_without_touching_file(self):
        for bad in ["", "   ", "short", "has space 1234567890"]:
            body = creds.save(self.home, "tushare_token", bad)
            self.assertFalse(body["ok"], bad)
            self.assertEqual(body["error"]["code"], "v3-credentials/invalid")
            self.assertFalse(os.path.exists(creds.credential_path(self.home)))
        body = creds.save(self.home, "unknown_key", SECRET)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "v3-credentials/invalid")

    # ── 优先级与清除 ────────────────────────────────────────────────────────
    def test_env_wins_over_file(self):
        creds.save(self.home, "tushare_token", SECRET)
        value, source = creds.resolve(self.home, "tushare_token",
                                      lambda name: "env-token-1234567890" if name == "TUSHARE_TOKEN" else None)
        self.assertEqual(value, "env-token-1234567890")
        self.assertIn("环境变量", source)

    def test_clear_removes_file_entry_but_env_still_wins(self):
        creds.save(self.home, "tushare_token", SECRET)
        body = creds.clear(self.home, "tushare_token")
        self.assertTrue(body["ok"])
        self.assertFalse(body["keys"][0]["present"])
        self.assertNotIn(SECRET, json.dumps(body, ensure_ascii=False))
        value, _ = creds.resolve(self.home, "tushare_token", lambda name: "env-1234567890abcd")
        self.assertEqual(value, "env-1234567890abcd")

    def test_corrupt_file_is_treated_as_absent(self):
        with open(creds.credential_path(self.home), "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        body = creds.status(self.home)
        self.assertFalse(body["keys"][0]["present"])

    # ── 连通性测试 ──────────────────────────────────────────────────────────
    def test_test_without_token_sends_nothing(self):
        calls = []
        body = creds.test_tushare(self.home, http=lambda b, t: calls.append(b) or {"code": 0})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "tushare/no-token")
        self.assertEqual(calls, [], "未配置时不应发出任何请求")

    def test_test_reports_ok_latency_and_api_error(self):
        creds.save(self.home, "tushare_token", SECRET)
        body = creds.test_tushare(self.home, http=lambda b, t: {"code": 0, "data": {"items": [[1], [2]]}})
        self.assertTrue(body["ok"])
        self.assertEqual(body["rows"], 2)
        self.assertNotIn(SECRET, json.dumps(body, ensure_ascii=False))

        failed = creds.test_tushare(self.home, http=lambda b, t: {"code": 2002, "msg": "权限不足"})
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"]["code"], "tushare/api")
        self.assertIn("权限不足", failed["error"]["message"])

    def test_test_network_error_is_normalized(self):
        creds.save(self.home, "tushare_token", SECRET)

        def boom(_body, _timeout):
            raise TimeoutError("timed out")

        body = creds.test_tushare(self.home, http=boom)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "tushare/network")


if __name__ == "__main__":
    unittest.main()
