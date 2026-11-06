import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import comparison


def row(res, k):
    return next(r for r in res["rows"] if r["metric"] == k)


def perf(n=40, **kw):
    p = {"trades": n, "trading_days": 40, "expectancy": 20.0, "win_rate": 0.5, "profit_factor": 1.5,
         "avg_win": 100.0, "avg_loss": -60.0}
    p.update(kw)
    return p


BT = {"expectancy": 20.0, "win_rate": 0.5, "profit_factor": 1.5, "avg_win": 100.0, "avg_loss": -60.0,
      "trades_per_year": 252.0}


class CompareTests(unittest.TestCase):
    def test_consistent(self):
        r = comparison.compare(perf(), BT, [{"duration_sec": 600}], [600])
        self.assertEqual(r["summary"], "consistent")
        self.assertEqual(row(r, "expectancy")["flag"], "ok")

    def test_worse_and_better_directions(self):
        r = comparison.compare(perf(expectancy=2.0, avg_loss=-120.0, avg_win=200.0), BT, [], [])
        self.assertEqual(row(r, "expectancy")["flag"], "worse")
        self.assertEqual(row(r, "avg_loss")["flag"], "worse")   # more negative
        self.assertEqual(row(r, "avg_win")["flag"], "better")
        self.assertEqual(r["summary"], "diverging")

    def test_win_rate_is_absolute(self):
        r = comparison.compare(perf(win_rate=0.35), BT, [], [])
        self.assertEqual(row(r, "win_rate")["flag"], "worse")
        r = comparison.compare(perf(win_rate=0.45), BT, [], [])
        self.assertEqual(row(r, "win_rate")["flag"], "ok")

    def test_low_sample_suppresses_flags(self):
        r = comparison.compare(perf(n=5, expectancy=-50.0), BT, [], [])
        self.assertEqual(r["summary"], "low_sample")
        self.assertEqual(row(r, "expectancy")["flag"], "low_sample")
        self.assertTrue(r["notes"])

    def test_no_backtest(self):
        r = comparison.compare(perf(), {}, [], [])
        self.assertEqual(r["summary"], "no_backtest")
        self.assertEqual(row(r, "expectancy")["flag"], "n/a")

    def test_holding_time_shift_has_no_direction(self):
        r = comparison.compare(perf(), BT, [{"duration_sec": 3000}] * 20, [600] * 30)
        self.assertEqual(row(r, "avg_duration_min")["flag"], "shift")

    def test_pace_needs_enough_days(self):
        r = comparison.compare(perf(trading_days=3), BT, [], [])
        self.assertIsNone(row(r, "trades_per_year")["sim"])


if __name__ == "__main__":
    unittest.main()
