import unittest
from datetime import datetime, timezone

import sup_behavior as sb

UTC = timezone.utc


class PaceTests(unittest.TestCase):
    def test_trading_days_counts_weekdays_before_today(self):
        # Mon 2026-11-02 .. Fri 2026-11-06 inclusive start, now = Mon 2026-11-09 -> 5 days
        self.assertEqual(sb.trading_days(datetime(2026, 11, 2, tzinfo=UTC), datetime(2026, 11, 9, 9, tzinfo=UTC)), 5)
        self.assertEqual(sb.trading_days(datetime(2026, 11, 7, tzinfo=UTC), datetime(2026, 11, 9, 9, tzinfo=UTC)), 0)

    def test_poisson_cdf(self):
        self.assertAlmostEqual(sb.poisson_cdf(0, 2.0), 0.1353, places=3)
        self.assertEqual(sb.poisson_cdf(-1, 2.0), 0.0)

    def test_too_early_gives_nothing(self):
        self.assertIsNone(sb.pace_finding(0, 3, 0.25))  # expected 0.75 < 5

    def test_normal_pace_ok(self):
        self.assertIsNone(sb.pace_finding(10, 40, 0.25))  # expected 10

    def test_far_fewer_trades_flagged(self):
        f = sb.pace_finding(0, 60, 0.25)  # expected 15, saw 0
        self.assertEqual(f["direction"], "fewer")
        self.assertLess(f["p_value"], 0.005)

    def test_far_more_trades_flagged(self):
        f = sb.pace_finding(40, 40, 0.25)  # expected 10, saw 40
        self.assertEqual(f["direction"], "more")


class HoldingTests(unittest.TestCase):
    REF = list(range(600, 1800, 20))  # 60 trades, 600..1780 s

    def test_needs_enough_samples(self):
        self.assertIsNone(sb.holding_finding([30] * 5, self.REF))
        self.assertIsNone(sb.holding_finding([30] * 20, self.REF[:10]))

    def test_normal_holding_ok(self):
        self.assertIsNone(sb.holding_finding([900, 1000, 1200] * 6, self.REF))

    def test_far_outside_band_flagged(self):
        f = sb.holding_finding([60] * 18, self.REF)
        self.assertEqual(f["outside"], 18)
        self.assertLess(f["sim_median_seconds"], f["backtest_median_seconds"])

    def test_single_outlier_not_flagged(self):
        self.assertIsNone(sb.holding_finding([900] * 17 + [5], self.REF))


if __name__ == "__main__":
    unittest.main()
