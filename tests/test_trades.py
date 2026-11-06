import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trades


def fill(i, t, action, qty, price, comm=0.5, instr="MNQ 12-26"):
    return {"account": "S", "execution_id": str(i), "instrument": instr, "time": f"2026-11-05T{t}Z",
            "action": action, "quantity": qty, "price": price, "commission": comm, "order_name": "x"}


class TradeTests(unittest.TestCase):
    def test_simple_long_and_short(self):
        r = trades.build_trades([fill(1, "14:30:00", "Buy", 1, 100), fill(2, "15:00:00", "Sell", 1, 101),
                                 fill(3, "16:00:00", "SellShort", 1, 110), fill(4, "16:10:00", "BuyToCover", 1, 112)])
        a, b = r["trades"]
        self.assertEqual((a["side"], a["gross_pnl"], a["net_pnl"], a["duration_sec"]), ("long", 2.0, 1.0, 1800))
        self.assertEqual((b["side"], b["gross_pnl"]), ("short", -4.0))
        self.assertEqual(r["open"], [])

    def test_partial_exit_and_open_remainder(self):
        r = trades.build_trades([fill(1, "14:00:00", "Buy", 2, 100, 1.0), fill(2, "14:10:00", "Sell", 1, 102, 0.5)])
        t = r["trades"][0]
        self.assertEqual((t["quantity"], t["commission"], t["net_pnl"]), (1, 1.0, 3.0))
        self.assertEqual(r["open"][0]["quantity"], 1)

    def test_remainder_commission_not_double_counted(self):
        r = trades.build_trades([fill(1, "14:00:00", "Buy", 2, 100, 1.0), fill(2, "14:10:00", "Sell", 1, 100, 0),
                                 fill(3, "14:20:00", "Sell", 1, 100, 0)])
        self.assertEqual([t["commission"] for t in r["trades"]], [0.5, 0.5])

    def test_reversal(self):
        r = trades.build_trades([fill(1, "14:00:00", "Buy", 1, 100), fill(2, "14:05:00", "Sell", 2, 99)])
        self.assertEqual(len(r["trades"]), 1)
        self.assertEqual((r["open"][0]["side"], r["open"][0]["quantity"]), ("short", 1))

    def test_unknown_instrument_and_action_warn(self):
        r = trades.build_trades([fill(1, "14:00:00", "Buy", 1, 1, instr="ZZ 12-26"), fill(2, "14:01:00", "Bogus", 1, 1)])
        self.assertEqual(len(r["warnings"]), 2)

    def test_performance(self):
        ts = trades.build_trades([fill(1, "14:00:00", "Buy", 1, 100, 0), fill(2, "14:10:00", "Sell", 1, 110, 0),
                                  fill(3, "15:00:00", "Buy", 1, 100, 0), fill(4, "15:10:00", "Sell", 1, 95, 0)])["trades"]
        p = trades.performance(ts)
        self.assertEqual((p["trades"], p["net_pnl"], p["win_rate"], p["profit_factor"]), (2, 10.0, 0.5, 2.0))
        self.assertEqual((p["avg_win"], p["avg_loss"], p["expectancy"], p["max_drawdown"]), (20.0, -10.0, 5.0, 10.0))
        self.assertEqual(trades.performance([])["trades"], 0)


if __name__ == "__main__":
    unittest.main()
