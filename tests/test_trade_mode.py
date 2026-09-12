import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "trade_mode.py"


class TradeModeTests(unittest.TestCase):
    def run_script(self, home, *args):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), *args],
                              env={**os.environ, "DSH_HOME": str(home), "HOME": str(home), "USERPROFILE": str(home)},
                              capture_output=True, text=True)

    def test_missing_defaults_sim_and_live_is_not_a_model_confirmation_flag(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(self.run_script(home).stdout.strip(), "sim")
            result = self.run_script(home, "live")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(home) / "trading-account-mode").exists())

    def test_invalid_file_is_not_silently_sim(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "trading-account-mode").write_text("corrupt")
            self.assertNotEqual(self.run_script(home).returncode, 0)

    def test_sim_recovery_refuses_in_flight_call(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "trading-account-mode").write_text("live")
            (Path(home) / "trading-call-test.active").write_text("busy")
            self.assertNotEqual(self.run_script(home, "sim").returncode, 0)
            (Path(home) / "trading-call-test.active").unlink()
            self.assertEqual(self.run_script(home, "sim").returncode, 0)
            self.assertEqual(self.run_script(home).stdout.strip(), "sim")


if __name__ == "__main__":
    unittest.main()
