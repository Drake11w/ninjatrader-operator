import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import journal

ROWS = [
    {"account": "Sim101", "execution_id": "a1", "instrument": "MNQ 12-26", "time": "2026-11-05T14:30:00Z",
     "action": "Buy", "order_name": "ORB long", "quantity": 1, "price": 100.0, "commission": 0.5, "is_entry": True},
    {"account": "Sim101", "execution_id": "a2", "instrument": "MNQ 12-26", "time": "2026-11-05T15:00:00Z",
     "action": "Sell", "order_name": "ORB time exit", "quantity": 1, "price": 101.0, "commission": 0.5, "is_exit": True},
]


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.con = journal.connect(":memory:")

    def test_dedupe(self):
        self.assertEqual(journal.record(self.con, ROWS, "ORB", "v1"), {"new": 2, "already_recorded": 0})
        self.assertEqual(journal.record(self.con, ROWS), {"new": 0, "already_recorded": 2})
        self.assertEqual(len(journal.query(self.con)), 2)

    def test_tag_and_filters(self):
        journal.record(self.con, ROWS, "ORB", "v1")
        self.assertEqual(len(journal.query(self.con, strategy="ORB")), 2)
        self.assertEqual(journal.query(self.con, strategy="X"), [])
        self.assertEqual([r["execution_id"] for r in journal.query(self.con, since="2026-11-05T14:45:00Z")], ["a2"])
        self.assertEqual(journal.query(self.con)[0]["execution_id"], "a1")  # oldest first

    def test_skips_rows_without_id(self):
        self.assertEqual(journal.record(self.con, [{"account": "S", "price": 1}])["new"], 0)

    def test_persists_on_disk(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "sub", "j.sqlite")
        c1 = journal.connect(path); journal.record(c1, ROWS); c1.close()
        self.assertEqual(len(journal.query(journal.connect(path))), 2)


if __name__ == "__main__":
    unittest.main()
