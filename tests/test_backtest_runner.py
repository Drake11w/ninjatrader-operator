import json
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backtest
import backtest_runner as br


def trade(entry, exit_, profit, qty=1, side="Long"):
    return {"entry_time": entry, "exit_time": exit_, "side": side, "qty": qty, "entry_price": 100.0, "exit_price": 101.0,
            "entry_name": "ORB long", "exit_name": "Stop loss", "profit": profit, "commission": 0}


class WindowTests(unittest.TestCase):
    def test_windows_cover_every_day_once_in_order(self):
        w = br.windows(date(2020, 1, 2), date(2026, 10, 16))
        self.assertEqual(w[0]["start"], date(2020, 1, 2))
        self.assertEqual(w[-1]["end"], date(2026, 10, 16))
        for a, b in zip(w, w[1:]):
            self.assertEqual((b["start"] - a["end"]).days, 1)
            self.assertNotEqual(a["contract"], b["contract"])
        self.assertEqual(len(w), 28)
        self.assertEqual(w[0]["instrument"], "MNQ 03-20")

    def test_in_window_uses_entry_date(self):
        w = {"start": date(2026, 9, 11), "end": date(2026, 12, 9)}
        self.assertTrue(br.in_window(trade("2026-09-11 09:02:00", "x", 1), w))
        self.assertFalse(br.in_window(trade("2026-09-10 09:02:00", "x", 1), w))
        self.assertFalse(br.in_window(trade("2026-12-10 09:02:00", "x", 1), w))


class RunLegTests(unittest.TestCase):
    spec = {"id": "s1", "costs": {"slippage_ticks": 2, "commission_per_side": 0.62}}
    leg = {"leg": "orb", "class": "OrbEvalMNQ", "inputs": {"A": True}}
    wins = [{"contract": "12-26", "instrument": "MNQ 12-26", "start": date(2026, 9, 11), "end": date(2026, 12, 9)},
            {"contract": "03-27", "instrument": "MNQ 03-27", "start": date(2026, 12, 10), "end": date(2027, 3, 10)}]

    def test_caches_and_filters_to_window(self):
        calls = []

        def run(cls, inputs, w, slip):
            calls.append(w["instrument"])
            return {"ok": True, "trades": [trade("2026-09-10 09:00:00", "x", 5), trade("2026-10-01 09:00:00", "x", 7)]}
        with tempfile.TemporaryDirectory() as d:
            a = br.run_leg(self.spec, self.leg, self.wins[:1], d, run=run, log=lambda *_: None)
            self.assertEqual([t["profit"] for t in a["trades"]], [7])   # the 09-10 trade is outside the window
            b = br.run_leg(self.spec, self.leg, self.wins[:1], d, run=run, log=lambda *_: None)
            self.assertEqual(len(calls), 1)                              # second pass came from the cache
            self.assertEqual(len(b["trades"]), 1)

    def test_failure_stops_and_is_reported_not_cached(self):
        def run(cls, inputs, w, slip):
            return {"ok": False, "error": "no Strategy Analyzer window is open"}
        with tempfile.TemporaryDirectory() as d:
            r = br.run_leg(self.spec, self.leg, self.wins, d, run=run, log=lambda *_: None)
            self.assertEqual(r["failed"]["window"], "MNQ 12-26")
            self.assertEqual(os.listdir(d), [])

    def test_empty_window_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            r = br.run_leg(self.spec, self.leg, self.wins[:1], d, run=lambda *a: {"ok": True, "trades": []}, log=lambda *_: None)
            self.assertEqual(r["empty_windows"], ["MNQ 12-26 2026-09-11"])


class RunWindowTests(unittest.TestCase):
    def test_polls_until_done_and_sends_inputs(self):
        sent = {}
        states = iter([{"ok": True, "state": "running"}, {"ok": True, "state": "done", "trades": [trade("a", "b", 1)], "seconds": 3}])

        def call(path, params=None, body=None):
            if path == "/backtest/run":
                sent.update(params, body=body)
                return {"ok": True, "job": 9}
            return next(states)
        w = {"instrument": "MNQ 12-26", "start": date(2026, 9, 11), "end": date(2026, 9, 30)}
        r = br.run_window("OrbEvalMNQ", {"UseX": True, "Name": "a b"}, w, 2, call=call, sleep=lambda s: None)
        self.assertTrue(r["ok"])
        self.assertEqual(sent["class"], "OrbEvalMNQ")
        self.assertEqual(sent["instrument"], "MNQ 12-26")
        self.assertIn("UseX=True", sent["body"])
        self.assertIn("Name=a%20b", sent["body"])

    def test_failed_job_is_an_error(self):
        def call(path, params=None, body=None):
            return {"ok": True, "job": 1} if path == "/backtest/run" else {"ok": True, "state": "failed", "error": "boom"}
        r = br.run_window("X", {}, {"instrument": "i", "start": date(2026, 1, 1), "end": date(2026, 1, 2)}, 2,
                          call=call, sleep=lambda s: None)
        self.assertEqual(r, {"ok": False, "error": "boom"})


class CsvTests(unittest.TestCase):
    def test_round_trips_through_the_backtest_parser_with_commission_applied(self):
        ts = [trade("2026-09-14 09:02:00", "2026-09-14 09:40:00", 100.0, qty=2),
              trade("2026-09-15 09:03:00", "2026-09-15 14:55:00", -50.0)]
        text = br.to_trades_csv(ts, 0.62)
        p = backtest.parse(text, "America/Chicago")
        self.assertEqual(p["kind"], "trades")
        self.assertEqual(len(p["trades"]), 2)
        nets = [t["net_pnl"] for t in p["trades"]]
        self.assertEqual(nets, [round(100 - 2.48, 2), round(-50 - 1.24, 2)])
        self.assertEqual(p["trades"][0]["quantity"], 2)


if __name__ == "__main__":
    unittest.main()
