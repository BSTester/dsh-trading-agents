"""指令目录：白名单校验、nonce 幂等、原子写、轮询消费、processed 去重。"""
import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import commands, daemon  # noqa: E402


class CommandsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = str(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_and_poll(self):
        nonce = commands.write_command(self.home, "kill", {"nonce": "n1"})
        self.assertTrue(nonce)
        handled = commands.poll(self.home, handler=lambda cmd: {"ok": True})
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0]["type"], "kill")
        self.assertEqual(commands.poll(self.home, handler=lambda c: c), [])  # processed 去重

    def test_unknown_type_rejected(self):
        with self.assertRaises(ValueError):
            commands.write_command(self.home, "rm_rf", {})


if __name__ == "__main__":
    unittest.main()
