import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import registry
import verdict

SPEC = {"id": "s742", "version": "1.0.0", "expected_metrics": {"win_rate": 0.5, "expectancy": 40.0,
                                                               "trades_per_year": 60}}


def mk(pnls, start_day=1):
    out = []
    for i, p in enumerate(pnls):
        day = start_day + i
        m, d = 1 + (day - 1) // 28, 1 + (day - 1) % 28
        out.append({"entry_time": f"2026-{m + 0:02d}-{d:02d}T14:30:00Z", "exit_time": f"2026-{m:02d}-{d:02d}T15:00:00Z",
                    "net_pnl": p, "duration_sec": 1800})
    return out


class VerdictTests(unittest.TestCase):
    def test_no_trades_keeps_testing(self):
        r = verdict.evaluate(SPEC, [])
        self.assertEqual(r["verdict"], "keep_testing")

    def test_small_sample_cannot_promote(self):
        r = verdict.evaluate(SPEC, mk([50, 50, 50, 50, 50]))
        self.assertEqual(r["verdict"], "keep_testing")
        self.assertIn("eta", r)

    def test_promote_on_good_large_sample(self):
        pnls = ([60, 60, -20] * 12)[:36]
        r = verdict.evaluate(SPEC, mk(pnls))
        self.assertEqual(r["verdict"], "promote", r["reasons"])

    def test_drawdown_stop_rejects_even_with_few_trades(self):
        r = verdict.evaluate(SPEC, mk([-500, -500, -500, -400]))
        self.assertEqual(r["verdict"], "reject")
        self.assertIn("drawdown", r["reasons"][0])

    def test_losing_after_costs_rejects(self):
        pnls = [-30, -30, 20] * 8
        r = verdict.evaluate(SPEC, mk(pnls), criteria={"drawdown_limit": 100000})
        self.assertEqual(r["verdict"], "reject")

    def test_win_rate_far_below_claim_rejects(self):
        pnls = [200, -10, -10, -10] * 8  # profitable but wins 25% vs claimed 50%
        r = verdict.evaluate(SPEC, mk(pnls), criteria={"drawdown_limit": 100000})
        self.assertEqual(r["verdict"], "reject")
        self.assertTrue(any("win rate" in x for x in r["reasons"]))

    def test_since_filters_old_trades(self):
        r = verdict.evaluate(SPEC, mk([-500, -500, -500, -400]), since="2027-01-01T00:00:00Z")
        self.assertEqual(r["sample"]["trades"], 0)

    def test_backtest_claim_beats_spec_claim(self):
        r = verdict.evaluate(SPEC, mk([10] * 3), backtest_metrics={"expectancy": 99.0})
        self.assertEqual(r["expected"]["expectancy"]["source"], "backtest")

    def test_log_roundtrip(self):
        con = registry.connect(":memory:")
        r = verdict.evaluate(SPEC, mk([10]))
        r.update()
        verdict.record(con, "s742", "1.0.0", "sim", r)
        h = verdict.history(con, "s742")
        self.assertEqual(h[0]["verdict"], "keep_testing")


if __name__ == "__main__":
    unittest.main()


class DecayTests(unittest.TestCase):
    BT = {"expectancy": 50.0, "trades": 100, "net_pnl": 5000.0}
    LOOSE = {"drawdown_limit": 100000, "reject_profit_factor": 0}

    def test_decay_detected(self):
        r = verdict.evaluate(SPEC, mk([-20, 30, -25, 10, -30] * 6), self.BT, self.LOOSE)
        self.assertTrue(r["edge_decayed"])
        self.assertEqual(r["verdict"], "reject")

    def test_recent_window_decay(self):
        pnls = [80, 60, 70, 90] * 8 + [-40, -30, -50, -20] * 5
        r = verdict.evaluate(SPEC, mk(pnls), self.BT, self.LOOSE)
        self.assertTrue(r["edge_decayed"])

    def test_healthy_not_decayed(self):
        r = verdict.evaluate(SPEC, mk([80, -30, 60, 40] * 8), self.BT, self.LOOSE)
        self.assertFalse(r["edge_decayed"])

    def test_spec_claim_alone_is_not_a_reference(self):
        r = verdict.evaluate(SPEC, mk([-20, 30, -25, 10, -30] * 6), None, self.LOOSE)
        self.assertFalse(r["edge_decayed"])

    def test_too_few_trades(self):
        r = verdict.evaluate(SPEC, mk([-50] * 5), self.BT, self.LOOSE)
        self.assertFalse(r["edge_decayed"])
