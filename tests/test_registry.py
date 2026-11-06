import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import registry

with open(os.path.join(ROOT, "examples", "orb-mnq.strategy.json"), encoding="utf-8") as f:
    EXAMPLE = json.load(f)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.con = registry.connect(":memory:")
        self.sid, self.ver = EXAMPLE["id"], EXAMPLE["version"]

    def test_submit_and_duplicate(self):
        r = registry.submit(self.con, EXAMPLE)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "candidate")
        self.assertFalse(registry.submit(self.con, EXAMPLE)["ok"])
        v2 = copy.deepcopy(EXAMPLE)
        v2["version"] = "1.0.1"
        self.assertTrue(registry.submit(self.con, v2)["ok"])
        self.assertEqual(len(registry.list_specs(self.con, self.sid)), 2)

    def test_invalid_spec_rejected(self):
        bad = copy.deepcopy(EXAMPLE)
        bad["exit"].pop("stop")
        r = registry.submit(self.con, bad)
        self.assertFalse(r["ok"])
        self.assertEqual(registry.list_specs(self.con), [])

    def test_spec_status_ignored_with_warning(self):
        s = copy.deepcopy(EXAMPLE)
        s["status"] = "live"
        r = registry.submit(self.con, s)
        self.assertEqual(r["status"], "candidate")
        self.assertIn("warning", r)

    def test_lifecycle(self):
        registry.submit(self.con, EXAMPLE)
        self.assertFalse(registry.set_status(self.con, self.sid, self.ver, "live")["ok"])  # no skipping
        self.assertFalse(registry.set_status(self.con, self.sid, self.ver, "candidate")["ok"])  # no repeat/backward
        for st in ("backtest", "sim", "live-small", "live"):
            self.assertTrue(registry.set_status(self.con, self.sid, self.ver, st)["ok"], st)
        self.assertFalse(registry.set_status(self.con, self.sid, self.ver, "bogus")["ok"])
        self.assertTrue(registry.set_status(self.con, self.sid, self.ver, "retired", "edge decayed")["ok"])
        self.assertFalse(registry.set_status(self.con, self.sid, self.ver, "live")["ok"])  # terminal
        hist = registry.get_spec(self.con, self.sid, self.ver)["history"]
        self.assertEqual([h["to_status"] for h in hist],
                         ["candidate", "backtest", "sim", "live-small", "live", "retired"])

    def test_terminal_needs_note_and_unknown(self):
        registry.submit(self.con, EXAMPLE)
        self.assertFalse(registry.set_status(self.con, self.sid, self.ver, "rejected")["ok"])
        self.assertTrue(registry.set_status(self.con, self.sid, self.ver, "rejected", "no edge")["ok"])
        self.assertFalse(registry.set_status(self.con, "nope", "1.0.0", "backtest")["ok"])
        self.assertIn("error", registry.get_spec(self.con, "nope", "1.0.0"))

    def test_filter_by_status(self):
        registry.submit(self.con, EXAMPLE)
        registry.set_status(self.con, self.sid, self.ver, "backtest")
        self.assertEqual(len(registry.list_specs(self.con, status="backtest")), 1)
        self.assertEqual(registry.list_specs(self.con, status="candidate"), [])


if __name__ == "__main__":
    unittest.main()
