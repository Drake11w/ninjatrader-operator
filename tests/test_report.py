import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import report

RULES = {"starting_balance": 50000, "daily_loss_limit": 1000, "profit_target": 3000, "timezone": "UTC",
         "session_start_hour": 0}


def t(day, hour, pnl):
    return {"instrument": "MNQ 12-26", "side": "long", "quantity": 1, "entry_price": 100, "exit_price": 101,
            "entry_time": f"{day}T{hour:02d}:00:00Z", "exit_time": f"{day}T{hour:02d}:30:00Z",
            "net_pnl": pnl, "duration_sec": 1800, "strategy": "ORB"}


TRADES = [t("2026-11-02", 14, 100), t("2026-11-03", 14, -400), t("2026-11-03", 15, 250), t("2026-11-10", 14, 999)]


class ReportTests(unittest.TestCase):
    def build(self, **kw):
        return report.build_report(TRADES, [], tz_name="UTC", start_hour=0, **kw)

    def test_day_report(self):
        md = self.build(period="day", day="2026-11-03")
        self.assertIn("# Trading report: 2026-11-03", md)
        self.assertIn("**-$150.00** over 2 trades", md)
        self.assertNotIn("$999", md)

    def test_week_report_by_day_and_bounds(self):
        md = self.build(period="week", day="2026-11-04")
        self.assertIn("2026-11-02 to 2026-11-08", md)
        self.assertIn("| 2026-11-03 | 2 | -$150.00 |", md)
        self.assertNotIn("2026-11-10", md)

    def test_rules_section_uses_period_end(self):
        md = self.build(period="day", day="2026-11-03", rules_cfg=RULES)
        self.assertIn("## Prop rules (as of 2026-11-03): OK", md)
        self.assertIn("lost $150.00 of $1,000.00", md)
        self.assertIn("-$50.00 of $3,000.00", md)

    def test_empty_period(self):
        md = self.build(period="day", day="2026-11-20")
        self.assertIn("No closed trades", md)

    def test_open_positions_listed(self):
        op = [{"side": "long", "quantity": 1, "instrument": "MNQ 12-26", "entry_price": 1, "entry_time": "x"}]
        self.assertIn("## Open positions", report.build_report([], op, "day", "2026-11-03", tz_name="UTC", start_hour=0))


if __name__ == "__main__":
    unittest.main()
