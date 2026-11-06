import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rules

RULES = {"starting_balance": 50000, "profit_target": 3000, "daily_loss_limit": 1000,
         "trailing_drawdown": {"amount": 2000, "mode": "eod", "lock_at_start": True},
         "consistency_pct": 0.5, "warn_at": 0.8, "timezone": "UTC", "session_start_hour": 0}


def trade(day, pnl):
    return {"exit_time": f"{day}T15:00:00Z", "net_pnl": pnl}


def at(day):
    return datetime.fromisoformat(f"{day}T20:00:00+00:00")


class RuleTests(unittest.TestCase):
    def test_daily_loss_status(self):
        r = rules.evaluate([trade("2026-11-05", -850)], RULES, at("2026-11-05"))
        d = r["rules"]["daily_loss_limit"]
        self.assertEqual((d["distance"], d["status"]), (150.0, "warning"))
        r = rules.evaluate([trade("2026-11-05", -1000)], RULES, at("2026-11-05"))
        self.assertEqual(r["rules"]["daily_loss_limit"]["status"], "breach")
        self.assertEqual(r["overall"], "breach")

    def test_daily_loss_resets_next_day(self):
        r = rules.evaluate([trade("2026-11-04", -900)], RULES, at("2026-11-05"))
        self.assertEqual(r["rules"]["daily_loss_limit"]["loss_today"], 0)

    def test_eod_trailing_and_lock(self):
        # +1500 on day 1 -> peak 51500, floor 49500; lock caps floor at 50000 only once peak-2000 > 50000
        r = rules.evaluate([trade("2026-11-04", 1500)], RULES, at("2026-11-05"))
        t = r["rules"]["trailing_drawdown"]
        self.assertEqual((t["peak_balance"], t["floor"], t["distance"]), (51500.0, 49500.0, 2000.0))
        r = rules.evaluate([trade("2026-11-03", 4000)], RULES, at("2026-11-05"))
        t = r["rules"]["trailing_drawdown"]
        self.assertEqual((t["floor"], t["distance"]), (50000.0, 4000.0))  # locked at starting balance

    def test_lock_floor_absolute(self):
        cfg = {**RULES, "trailing_drawdown": {"amount": 2000, "mode": "eod", "lock_floor": 50100}}
        r = rules.evaluate([trade("2026-11-03", 4000)], cfg, at("2026-11-05"))
        t = r["rules"]["trailing_drawdown"]
        self.assertEqual((t["floor"], t["distance"]), (50100.0, 3900.0))  # floor stops rising at 50,100
        r = rules.evaluate([trade("2026-11-03", 1000)], cfg, at("2026-11-05"))
        self.assertEqual(r["rules"]["trailing_drawdown"]["floor"], 49000.0)  # still trailing below the lock

    def test_example_rules_file_loads(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rules.example.json")
        cfg = rules.load_rules(path)
        self.assertEqual(cfg["trailing_drawdown"]["lock_floor"], 50100)
        self.assertNotIn("daily_loss_limit", cfg)
        r = rules.evaluate([], cfg, at("2026-11-05"))
        self.assertEqual(r["rules"]["trailing_drawdown"]["floor"], 48000.0)

    def test_today_does_not_raise_eod_peak(self):
        r = rules.evaluate([trade("2026-11-05", 1500)], RULES, at("2026-11-05"))
        self.assertEqual(r["rules"]["trailing_drawdown"]["peak_balance"], 50000.0)

    def test_intraday_peak_counts_open_day(self):
        cfg = {**RULES, "trailing_drawdown": {"amount": 2000, "mode": "intraday", "lock_at_start": False}}
        r = rules.evaluate([trade("2026-11-05", 1500), trade("2026-11-05", -500)], cfg, at("2026-11-05"))
        t = r["rules"]["trailing_drawdown"]
        self.assertEqual((t["peak_balance"], t["floor"], t["distance"]), (51500.0, 49500.0, 1500.0))

    def test_unrealized_counts_against_limits(self):
        r = rules.evaluate([], RULES, at("2026-11-05"), unrealized=-1200)
        self.assertEqual(r["rules"]["daily_loss_limit"]["status"], "breach")

    def test_profit_target_and_consistency(self):
        r = rules.evaluate([trade("2026-11-03", 1800), trade("2026-11-04", 600)], RULES, at("2026-11-05"))
        self.assertEqual(r["rules"]["profit_target"]["remaining"], 600.0)
        c = r["rules"]["consistency"]
        self.assertEqual((c["best_day"], c["share"], c["status"]), ("2026-11-03", 0.75, "breach"))
        self.assertEqual(c["profit_needed_to_comply"], 1200.0)  # 1800/0.5 - 2400

    def test_session_start_rollover(self):
        tz = None
        self.assertEqual(rules.trading_day("2026-11-05T19:00:00Z", tz, 18), "2026-11-06")
        self.assertEqual(rules.trading_day("2026-11-05T17:59:00Z", tz, 18), "2026-11-05")

    def test_load_rules_errors(self):
        with self.assertRaises(FileNotFoundError):
            rules.load_rules(os.path.join(os.path.dirname(__file__), "nope.json"))


if __name__ == "__main__":
    unittest.main()
