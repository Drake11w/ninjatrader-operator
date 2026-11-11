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


class HolidayTests(unittest.TestCase):
    def test_2026_calendar(self):
        got = {d.isoformat(): n.split(" (")[1].rstrip(")") for d, n in sessions.special_sessions(2026).items()}
        halt, early = "trading halts 12:00 CT", "early close 12:15 CT"
        self.assertEqual(got, {
            "2026-01-01": "closed", "2026-01-19": halt, "2026-02-16": halt, "2026-04-03": "closed",
            "2026-05-25": halt, "2026-06-19": halt, "2026-07-03": halt, "2026-09-07": halt,
            "2026-11-26": halt, "2026-11-27": early, "2026-12-24": early, "2026-12-25": "closed"})

    def test_observed_dates(self):
        s27 = sessions.special_sessions(2027)
        self.assertIn("Independence Day", s27[date(2027, 7, 5)])   # Sunday -> Monday
        self.assertIn("Christmas Day", s27[date(2027, 12, 24)])    # Saturday -> Friday
        self.assertNotIn(date(2027, 12, 23), s27)                  # no early close before an observed Friday
        self.assertIn("Juneteenth", s27[date(2027, 6, 18)])        # Saturday -> Friday
        self.assertIsNone(sessions.special_sessions(2022).get(date(2022, 1, 1)))  # Saturday New Year: not observed
        self.assertIn("New Year", sessions.special_sessions(2023)[date(2023, 1, 2)])  # Sunday -> Monday
        self.assertIn("day before Independence Day", sessions.special_sessions(2025)[date(2025, 7, 3)])
        self.assertTrue(all(d.weekday() < 5 for y in range(2020, 2040) for d in sessions.special_sessions(y)))

    def test_easter(self):
        self.assertEqual([sessions.easter(y) for y in (2024, 2025, 2026, 2027)],
                         [date(2024, 3, 31), date(2025, 4, 20), date(2026, 4, 5), date(2027, 3, 28)])

    def test_special_session_and_extra(self):
        self.assertEqual(sessions.special_session(date(2026, 11, 25)), "")
        self.assertIn("Thanksgiving", sessions.special_session(date(2026, 11, 26)))
        self.assertEqual(sessions.special_session(date(2026, 11, 25), ["2026-11-25"]), "listed in extra_holidays")

    def test_session_info_names_the_holiday(self):
        info = sessions.session_info(et("2026-11-26T10:00:00"))
        self.assertIn("Thanksgiving", info["special_session"])
        self.assertEqual(len(info["warnings"]), 2)
        self.assertIsNone(sessions.session_info(et("2026-11-25T10:00:00"))["special_session"])


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
