"""``server/v3_credentials.py`` 契约测试（页面化密钥配置）。

离线：临时 home，不打网络、不读真实 ``~/.dsh``。

**2026-09-21 数据源政策后的新契约**（原 tushare_token 用例随 Tushare Pro 一并移除——
政策：除富途（授权使用）外数据渠道一律免密钥，需要 token 的引用一律删除）：
  * ``KEYS`` 注册表为空 → status 返回 ``keys=[]`` 且 note 写明免密钥政策；
  * save / clear / validate 对**任何** key 一律拒绝（unknown-key / invalid），
    且**不落盘**（文件保持不存在）；
  * resolve 未知 key → ``(None, None)``。

通用机制（0600 原子写 / 掩码 / 环境变量优先）随政策休眠：为它保留旧形态测试只会
测试一个没有注册键的空壳，因此不保留。
"""
import os
import tempfile
import unittest

import sys
from pathlib import Path

PLATFORM = str(Path(__file__).resolve().parents[1])
if PLATFORM not in sys.path:
    sys.path.insert(0, PLATFORM)

from server import v3_credentials as creds  # noqa: E402


class CredentialsPolicyTest(unittest.TestCase):
    """数据源政策后的凭据注册表契约：空注册表 + 拒绝一切数据类凭据。"""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="v3cred-")

    def tearDown(self):
        for name in os.listdir(self.home):
            os.unlink(os.path.join(self.home, name))
        os.rmdir(self.home)

    def test_registry_is_empty_by_policy(self):
        """注册表必须为空：数据渠道一律免密钥（富途凭据不在本模块）。"""
        self.assertEqual(creds.KEYS, {}, "数据源政策：不得再注册需要 token 的数据渠道")

    def test_status_returns_empty_keys_with_policy_note(self):
        body = creds.status(self.home)
        self.assertTrue(body["ok"])
        self.assertEqual(body["keys"], [])
        self.assertIn("免密钥", body.get("note", ""))
        self.assertFalse(os.path.exists(creds.credential_path(self.home)),
                         "空注册表的 status 不该创建凭据文件")

    def test_status_treats_corrupt_file_as_absent(self):
        with open(creds.credential_path(self.home), "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        body = creds.status(self.home)
        self.assertTrue(body["ok"])
        self.assertEqual(body["keys"], [])

    def test_resolve_unknown_key_is_none(self):
        self.assertEqual(creds.resolve(self.home, "tushare_token"), (None, None))
        self.assertEqual(creds.resolve(self.home, "anything", os.environ.get), (None, None))

    def test_save_rejects_any_key_without_writing_file(self):
        for key in ("tushare_token", "deepseek_api_key", "anything"):
            body = creds.save(self.home, key, "0123456789abcdef0123456789abcdef")
            self.assertFalse(body["ok"], key)
            self.assertEqual(body["error"]["code"], "v3-credentials/invalid", key)
        self.assertFalse(os.path.exists(creds.credential_path(self.home)),
                         "拒绝的保存不得落盘")

    def test_clear_rejects_unknown_key(self):
        body = creds.clear(self.home, "tushare_token")
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "v3-credentials/unknown-key")


if __name__ == "__main__":
    unittest.main()
