"""Offline regression tests for the canonical local quant simulator."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "plugins" / "engine" / "python"
sys.path.insert(0, str(PYTHON))
import backtest
import engine
sys.path.pop(0)


def bars(opens=(10, 10, 10), closes=None):
    closes = opens if closes is None else closes
    return pd.DataFrame({
        "date": pd.date_range("2026-01-05", periods=len(opens), freq="B").strftime("%Y-%m-%d"),
        "open": opens,
        "high": np.maximum(opens, closes) + 1,
        "low": np.minimum(opens, closes) - 1,
        "close": closes,
        "volume": [100000] * len(opens),
    })


class IsolatedLedger(unittest.TestCase):
    def setUp(self):
        self.home = ROOT / (".quant-test-" + uuid4().hex)
        self.home.mkdir()
        self.addCleanup(shutil.rmtree, self.home)
        for name, value in (
            ("DSH", self.home),
            ("MODE_FILE", self.home / "trading-account-mode"),
            ("LEDGER", self.home / "quant-ledger.json"),
        ):
            p = patch.object(engine, name, value)
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict(os.environ, {"DSH_HOME": str(self.home), "HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        network = patch.object(engine, "load_data", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def signal(self, signal="BUY", price=10, atr=1, date="2026-01-06", ticker="600519"):
        return {"ticker": ticker, "strategy": "rsi", "date": date,
                "signal": signal, "price": price, "atr": atr}

    def ledger(self, cash=1_000_000, positions=None):
        return {"cash": cash, "positions": positions or {}, "history": []}

    def position(self, date="2026-01-05"):
        return {"shares": 100, "entry": 10, "stop": 8, "date": date, "entry_fee": 1.3}

    def store(self, ledger):
        engine.LEDGER.write_text(json.dumps({"sim": ledger}))

    def decide(self, signal=None, apply=False):
        with patch.object(engine, "compute_signal", return_value=signal or self.signal()), \
                patch.object(engine, "latest_price", return_value=10):
            return engine.decide("600519", "rsi", apply_fill=apply)

    def test_missing_mode_defaults_sim_invalid_mode_fails_closed(self):
        self.assertEqual(engine.read_mode(), "sim")
        for invalid in ("", "paper", "LIVE!", "sim\nlive"):
            engine.MODE_FILE.write_text(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                engine.read_mode()

    def test_uppercase_account_modes_are_invalid(self):
        for invalid in ("SIM", "LIVE", "Sim", "Live"):
            engine.MODE_FILE.write_text(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                engine.read_mode()

    def test_dsh_home_is_respected_on_import(self):
        spec = importlib.util.spec_from_file_location("isolated_engine", PYTHON / "engine.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.LEDGER, self.home / "quant-ledger.json")
        self.assertEqual(module.MODE_FILE, self.home / "trading-account-mode")

    def test_risk_budget_counts_cash_once(self):
        result = self.decide()
        self.assertEqual(result["order"]["shares"], 5000)
        self.assertEqual(result["execution_source"], "local_simulation")
        self.assertEqual(result["order"]["execution_source"], "local_simulation")

    def test_risk_budget_includes_existing_position_value_once(self):
        self.store(self.ledger(500_000, {"000001": {"shares": 50_000, "entry": 10}}))
        self.assertEqual(self.decide()["order"]["shares"], 5000)

    def test_cash_caps_lots_including_buy_costs(self):
        self.store(self.ledger(cash=1000))
        result = self.decide(self.signal(price=1, atr=0.001), apply=True)
        self.assertEqual(result["order"]["shares"], 900)
        self.assertAlmostEqual(engine.load_ledger("sim")["cash"], 98.83)

    def test_stop_triggers_for_hold_and_buy(self):
        self.store(self.ledger(1000, {"600519": self.position()}))
        for signal in ("HOLD", "BUY", "SELL"):
            with self.subTest(signal=signal):
                result = self.decide(self.signal(signal, price=7))
                self.assertEqual(result["order"]["action"], "SELL")
                self.assertIn("stop", result["order"]["reason"].lower())

    def test_t_plus_one_blocks_signal_and_stop_on_entry_day(self):
        self.store(self.ledger(1000, {"600519": self.position(date="2026-01-06")}))
        for signal in ("SELL", "HOLD"):
            with self.subTest(signal=signal):
                self.assertIsNone(self.decide(self.signal(signal, price=7), apply=True)["order"])
        self.assertIn("600519", engine.load_ledger("sim")["positions"])

    def test_missing_entry_date_cannot_be_sold(self):
        self.store(self.ledger(1000, {"600519": self.position(date="")}))
        self.assertIsNone(self.decide(self.signal("SELL"))["order"])

    def test_fills_have_date_provenance_and_net_return(self):
        self.decide(apply=True)
        ledger = engine.load_ledger("sim")
        buy = ledger["history"][0]
        self.assertEqual(buy["date"], "2026-01-06")
        self.assertEqual(buy["execution_source"], "local_simulation")
        self.decide(self.signal("SELL", date="2026-01-07"), apply=True)
        sell = engine.load_ledger("sim")["history"][-1]
        expected = (1 - backtest.COMMISSION - backtest.SLIPPAGE - backtest.STAMP_TAX) / (
            1 + backtest.COMMISSION + backtest.SLIPPAGE) - 1
        self.assertAlmostEqual(sell["return"], round(expected, 4))

    def test_live_decision_and_fill_are_refused_before_fetch_or_write(self):
        engine.MODE_FILE.write_text("live")
        for apply in (False, True):
            with self.subTest(apply=apply), self.assertRaisesRegex(ValueError, "sim"):
                engine.decide("600519", "rsi", apply_fill=apply)
        order = {"action": "BUY", "ticker": "600519", "shares": 100,
                 "price": 10, "stop": 8, "date": "2026-01-06"}
        with self.assertRaisesRegex(ValueError, "sim"):
            engine.fill_order("live", self.ledger(), order)
        self.assertFalse(engine.LEDGER.exists())

    def test_live_ledger_load_and_save_refuse_synthetic_account(self):
        with self.assertRaisesRegex(ValueError, "sim"):
            engine.load_ledger("live")
        with self.assertRaisesRegex(ValueError, "sim"):
            engine.save_ledger("live", self.ledger())
        self.assertFalse(engine.LEDGER.exists())

    def test_live_report_has_no_synthetic_balance(self):
        engine.MODE_FILE.write_text("live")
        engine.LEDGER.write_text(json.dumps({"live": self.ledger(999)}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            engine.report()
        report = json.loads(out.getvalue())
        self.assertIsNone(report["cash"])
        self.assertIsNone(report["equity"])
        self.assertEqual(report["execution_source"], "local_simulation")
        self.assertEqual(report["status"], "unavailable")

    def test_quote_errors_propagate(self):
        with patch.object(engine, "load_data", side_effect=RuntimeError("quote unavailable")):
            with self.assertRaisesRegex(RuntimeError, "quote unavailable"):
                engine.latest_price("600519")

    def test_atr_accounts_for_previous_close_gap(self):
        df = bars((10, 20), (10, 20))
        with patch.object(engine, "load_data", return_value=df), \
                patch.object(engine, "rsi_signal", return_value=pd.Series([0, 0])):
            self.assertEqual(engine.compute_signal("600519", "rsi")["atr"], 6.5)

    def test_fill_rejects_insufficient_cash_and_same_day_sale_without_mutation(self):
        ledger = self.ledger(1000)
        order = {"action": "BUY", "ticker": "600519", "shares": 100,
                 "price": 10, "stop": 8, "date": "2026-01-06"}
        before = json.dumps(ledger)
        with self.assertRaises(ValueError):
            engine.fill_order("sim", ledger, order)
        self.assertEqual(json.dumps(ledger), before)
        ledger = self.ledger(1000, {"600519": self.position("2026-01-06")})
        before = json.dumps(ledger)
        with self.assertRaisesRegex(ValueError, "T\\+1"):
            engine.fill_order("sim", ledger, {**order, "action": "SELL"})
        self.assertEqual(json.dumps(ledger), before)

    def test_save_is_atomic_and_preserves_other_mode(self):
        original = {"sim": self.ledger(), "live": {"opaque": "untouched"}}
        engine.LEDGER.write_text(json.dumps(original))
        with patch("os.replace", side_effect=OSError("disk failure")) as replace:
            with self.assertRaisesRegex(OSError, "disk failure"):
                engine.save_ledger("sim", self.ledger(900))
            replace.assert_called_once()
        self.assertEqual(json.loads(engine.LEDGER.read_text()), original)
        engine.save_ledger("sim", self.ledger(900))
        saved = json.loads(engine.LEDGER.read_text())
        self.assertEqual(saved["live"], original["live"])
        self.assertEqual(saved["sim"]["cash"], 900)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()),
                         ["quant-ledger.json", "quant-ledger.lock"])

    def test_busy_shared_mode_lock_preserves_ledger_and_lock_owner(self):
        self.store(self.ledger())
        original = engine.LEDGER.read_bytes()
        lock = self.home / "trading-workbench.lock"
        lock.write_text("other-owner")
        with self.assertRaisesRegex(ValueError, "busy"):
            self.decide(apply=True)
        self.assertEqual(engine.LEDGER.read_bytes(), original)
        self.assertEqual(lock.read_text(), "other-owner")

    def test_mode_flip_at_commit_lock_acquisition_refuses_write(self):
        self.store(self.ledger())
        original = engine.LEDGER.read_bytes()
        lock = self.home / "trading-workbench.lock"
        original_open = Path.open

        def open_with_mode_flip(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            if path == lock:
                engine.MODE_FILE.write_text("live")
            return handle

        with patch.object(Path, "open", open_with_mode_flip):
            with self.assertRaisesRegex(ValueError, "sim"):
                engine.save_ledger("sim", self.ledger(900))
        self.assertEqual(engine.LEDGER.read_bytes(), original)
        self.assertFalse(lock.exists())

    def test_shared_mode_lock_covers_replace_but_not_quotes(self):
        lock = self.home / "trading-workbench.lock"
        original_replace = os.replace

        def observed_signal(*args, **kwargs):
            self.assertFalse(lock.exists())
            return self.signal()

        def observed_replace(source, destination):
            self.assertTrue(lock.exists())
            self.assertEqual(engine.read_mode(), "sim")
            return original_replace(source, destination)

        with patch.object(engine, "compute_signal", side_effect=observed_signal), \
                patch("os.replace", side_effect=observed_replace):
            result = engine.decide("600519", "rsi", apply_fill=True)
        self.assertTrue(result["applied"])
        self.assertFalse(lock.exists())

    def test_apply_serializes_complete_read_modify_write(self):
        self.store(self.ledger())

        def delayed_signal(ticker, *args, **kwargs):
            time.sleep(0.03)
            return self.signal(ticker=ticker)

        with patch.object(engine, "compute_signal", side_effect=delayed_signal), \
                patch.object(engine, "latest_price", return_value=10), \
                ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda t: engine.decide(t, "rsi", True), ["600519", "000001"]))
        ledger = engine.load_ledger("sim")
        self.assertEqual(set(ledger["positions"]), {"600519", "000001"})
        self.assertEqual(len(ledger["history"]), 2)
        spent = sum(r["order"]["shares"] * 10 * (1 + backtest.COMMISSION + backtest.SLIPPAGE)
                    for r in results)
        self.assertAlmostEqual(ledger["cash"], 1_000_000 - spent)

    def test_mode_change_during_decision_prevents_apply(self):
        def changed_mode(*args, **kwargs):
            engine.MODE_FILE.write_text("live")
            return self.signal()

        with patch.object(engine, "compute_signal", side_effect=changed_mode):
            with self.assertRaisesRegex(ValueError, "sim"):
                engine.decide("600519", "rsi", True)
        self.assertFalse(engine.LEDGER.exists())

    def test_compatibility_engine_cli_respects_isolated_live_mode(self):
        engine.MODE_FILE.write_text("live")
        for package in ("engine", "quant"):
            result = subprocess.run(
                [sys.executable, "-c",
                 "import runpy,socket,sys; "
                 "socket.socket=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('Network forbidden')); "
                 "script=sys.argv.pop(1); sys.path.insert(0,str(__import__('pathlib').Path(script).parent)); "
                 "runpy.run_path(script,run_name='__main__')",
                 str(ROOT / "plugins" / package / "python" / "engine.py"),
                 "decide", "--ticker", "600519", "--apply"],
                capture_output=True, text=True, env=os.environ.copy(), timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("sim", result.stderr.lower())
        self.assertFalse(engine.LEDGER.exists())


class BacktestRegression(unittest.TestCase):
    def run_signals(self, df, signals):
        return backtest.run(df, lambda frame: pd.Series(signals, index=frame.index))

    def test_signal_fills_at_next_open_not_current_close(self):
        trades, _, _ = self.run_signals(bars((10, 12, 13), (11, 12, 13)), [1, -1, 0])
        self.assertEqual([(t["type"], t["date"], t["price"]) for t in trades],
                         [("buy", "2026-01-06", 12), ("sell", "2026-01-07", 13)])
        self.assertEqual(trades[0]["signal_date"], "2026-01-05")

    def test_last_bar_signal_has_no_fill(self):
        trades, _, final = self.run_signals(bars(), [0, 0, 1])
        self.assertEqual(trades, [])
        self.assertEqual(final, 1_000_000)

    def test_cost_aware_lots_do_not_overdraw_cash(self):
        trades, curve, _ = self.run_signals(bars(), [1, 0, 0])
        buy = trades[0]
        self.assertEqual(buy["shares"], 99_800)
        self.assertGreaterEqual(1_000_000 - buy["shares"] * buy["price"] - buy["fee"], 0)
        self.assertEqual(curve[0], 1_000_000)

    def test_sell_return_is_net_of_both_sides_costs(self):
        trades, _, _ = self.run_signals(bars(), [1, -1, 0])
        buy, sell = trades
        basis = buy["shares"] * buy["price"] + buy["fee"]
        proceeds = sell["shares"] * sell["price"] - sell["fee"]
        self.assertAlmostEqual(sell["return"], round(proceeds / basis - 1, 4))
        self.assertEqual(sell["hold_days"], 1)
        self.assertEqual(buy["status"], "closed")

    def test_end_position_marked_open_not_liquidated(self):
        trades, _, final = self.run_signals(bars(), [1, 0, 0])
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["status"], "open")
        self.assertEqual(trades[0]["execution_source"], "local_simulation")
        self.assertAlmostEqual(final, 1_000_000 - trades[0]["fee"])

    def test_invalid_data_rejected_before_signal(self):
        cases = [bars().iloc[:0], bars().drop(columns="open")]
        for column, value in (("close", 0), ("high", np.nan), ("open", np.inf),
                              ("low", 20), ("volume", -1), ("date", "not-a-date")):
            frame = bars()
            frame[column] = frame[column].astype(object)
            frame.loc[1, column] = value
            cases.append(frame)
        duplicate_day = bars()
        duplicate_day.loc[1, "date"] = duplicate_day.loc[0, "date"]
        cases.extend([duplicate_day, bars().iloc[::-1]])
        for frame in cases:
            with self.subTest(frame=frame.to_dict()), self.assertRaises(ValueError):
                self.run_signals(frame, [0] * len(frame))

    def test_invalid_signals_rejected(self):
        for values in ([1], [0, np.nan, 0], [0, 2, 0], [0, 0.5, 0]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                backtest.run(bars(), lambda frame: pd.Series(values))

    def test_synthetic_ohlc_is_valid(self):
        df = backtest.load_data("TEST", "2024-01-01", "synth")
        self.assertTrue((df["high"] >= df[["open", "close"]].max(axis=1)).all())
        self.assertTrue((df["low"] <= df[["open", "close"]].min(axis=1)).all())

    def test_synthetic_backtest_cli_compatibility(self):
        outputs = []
        for package in ("engine", "quant"):
            result = subprocess.run(
                [sys.executable, str(ROOT / "plugins" / package / "python" / "backtest.py"),
                 "--ticker", "TEST", "--source", "synth"],
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["execution_source"], "local_simulation")
            self.assertIn("open_positions", output)
            self.assertEqual(output["end_position_policy"], "mark_to_market_no_liquidation")
            outputs.append(output)
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
