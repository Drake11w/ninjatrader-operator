import os
import sys
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sessions

ET = ZoneInfo("America/New_York")


def et(s):
    return datetime.fromisoformat(s).replace(tzinfo=ET)


class SessionTests(unittest.TestCase):
    def test_third_friday(self):
        self.assertEqual(sessions.third_friday(2026, 12), date(2026, 12, 18))
        self.assertEqual(sessions.third_friday(2026, 3), date(2026, 3, 20))

    def test_front_contract_and_roll(self):
        f = sessions.front_contract(date(2026, 11, 5))
        self.assertEqual((f["contract"], f["roll_date"], f["days_to_roll"]), ("12-26", "2026-12-10", 35))
        self.assertEqual(sessions.front_contract(date(2026, 12, 10))["contract"], "03-27")  # rolled on the roll date
        self.assertEqual(sessions.front_contract(date(2026, 12, 9))["contract"], "12-26")

    def test_rth(self):
        s = sessions.session_info(et("2026-11-05T10:00:00"))  # Thursday
        self.assertEqual((s["state"], s["rth_closes_in"]), ("rth", "6h 0m"))

    def test_before_open_and_overnight(self):
        s = sessions.session_info(et("2026-11-05T08:00:00"))
        self.assertEqual((s["state"], s["rth_opens_in"], s["market_open"]), ("globex_extended_hours", "1h 30m", True))

    def test_maintenance_break(self):
        s = sessions.session_info(et("2026-11-05T17:30:00"))
        self.assertEqual((s["state"], s["market_open"], s["rth_opens_in"]), ("maintenance_break", False, "16h 0m"))

    def test_after_close_same_evening_open(self):
        self.assertEqual(sessions.session_info(et("2026-11-05T19:00:00"))["state"], "globex_extended_hours")

    def test_weekend(self):
        s = sessions.session_info(et("2026-11-07T12:00:00"))  # Saturday
        self.assertEqual((s["state"], s["rth_opens_in"]), ("weekend_closed", "1d 21h 30m"))
        self.assertEqual(sessions.session_info(et("2026-11-06T17:01:00"))["state"], "weekend_closed")  # Friday
        self.assertEqual(sessions.session_info(et("2026-11-08T18:00:00"))["state"], "globex_extended_hours")  # Sunday

    def test_instrument_check(self):
        now = et("2026-11-05T10:00:00")
        self.assertTrue(sessions.session_info(now, "MNQ 12-26")["instrument_check"]["is_front_month"])
        self.assertFalse(sessions.session_info(now, "MNQ 09-26")["instrument_check"]["is_front_month"])

    def test_utc_input_converted(self):
        s = sessions.session_info(datetime.fromisoformat("2026-11-05T15:00:00+00:00"))  # 10:00 ET
        self.assertEqual(s["state"], "rth")


if __name__ == "__main__":
    unittest.main()
