import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import history as h


class HistoryTests(unittest.TestCase):
    def test_chunks_cover_range_without_overlap(self):
        c = h.chunks(date(2026, 1, 1), date(2026, 3, 15), 30)
        self.assertEqual(c[0][0], date(2026, 1, 1))
        self.assertEqual(c[-1][1], date(2026, 3, 15))
        for (_, b), (a, _) in zip(c, c[1:]):
            self.assertEqual((a - b).days, 1)
        self.assertEqual(h.chunks(date(2026, 1, 1), date(2026, 1, 1), 30), [(date(2026, 1, 1), date(2026, 1, 1))])

    def test_download_merges_and_dedupes(self):
        calls = []

        def call(p):
            calls.append(p)
            return {"ok": True, "bars": [[p["from"] + "T09:00:00", 1, 2, 0.5, 1.5, 10]]}
        with tempfile.TemporaryDirectory() as d:
            r = h.download("MNQ 12-26", date(2026, 1, 1), date(2026, 2, 5), out=d, call=call)
            self.assertTrue(r["ok"])
            self.assertEqual((len(calls), r["total_rows"]), (2, 2))
            r2 = h.download("MNQ 12-26", date(2026, 1, 1), date(2026, 1, 2), out=d, call=call)  # same bar again
            self.assertEqual(r2["total_rows"], 2)
            lines = open(r["path"]).read().splitlines()
            self.assertEqual(lines[0], "time,open,high,low,close,volume")
            self.assertEqual(len(lines), 3)

    def test_error_keeps_partial_and_reports(self):
        n = []

        def call(p):
            n.append(1)
            return {"ok": True, "bars": [[p["from"] + "T09:00:00", 1, 2, 3, 4, 5]]} if len(n) == 1 else {"ok": False, "error": "boom"}
        with tempfile.TemporaryDirectory() as d:
            r = h.download("MNQ 12-26", date(2026, 1, 1), date(2026, 3, 1), out=d, call=call)
            self.assertFalse(r["ok"])
            self.assertIn("boom", r["error"])
            self.assertEqual(len(open(r["path"]).read().splitlines()), 2)

    def test_truncated_reply_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            r = h.download("MNQ 12-26", date(2026, 1, 1), date(2026, 1, 2), out=d, call=lambda p: {"ok": True, "bars": [], "truncated": True})
            self.assertFalse(r["ok"])

    def test_validation(self):
        self.assertFalse(h.download("X", date(2026, 1, 1), date(2026, 1, 2), type_="Hour")["ok"])
        self.assertFalse(h.download("X", date(2026, 1, 2), date(2026, 1, 1))["ok"])
        self.assertIn("MNQ 12-26", h.file_path("MNQ 12-26", "Minute", 1, "d"))


if __name__ == "__main__":
    unittest.main()
