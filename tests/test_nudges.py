"""Nudge list: appears only while readiness holds, read-only, no network."""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))
import feedback_fixture as fx  # noqa: E402
import journal  # noqa: E402
import nudges  # noqa: E402


def snapshot(rcon):
    return (rcon.execute("SELECT id, version, status FROM specs").fetchall(),
            rcon.execute("SELECT COUNT(*) FROM status_history").fetchone()[0])


class NudgeTests(unittest.TestCase):
    def run_nudges(self, sc, mutate=None):
        rcon, jcon = fx.build(sc)
        self.addCleanup(rcon.close)
        self.addCleanup(jcon.close)
        if mutate:
            mutate(rcon, jcon)
        before = snapshot(rcon)
        import urllib.request
        with mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network call")):
            out = nudges.list_nudges(rcon, jcon, as_of=fx.AS_OF)
        self.assertEqual(snapshot(rcon), before, "list_nudges changed the registry")
        return out

    def test_ready_version_is_nudged(self):
        out = self.run_nudges("ready")
        self.assertEqual([(n["id"], n["version"]) for n in out["nudges"]], [("strat-a", "1.0.0")])
        self.assertEqual(out["nudges"][0]["summary"], "ready")

    def test_not_ready_and_empty_versions_are_not_nudged(self):
        for sc in ("not_ready", "nullable_fields", "empty_period"):
            self.assertEqual(self.run_nudges(sc)["nudges"], [], sc)

    def test_nudge_disappears_when_readiness_lapses(self):
        self.assertEqual(len(self.run_nudges("ready")["nudges"]), 1)

        def untagged_fill(rcon, jcon):
            journal.record(jcon, [{"account": fx.ACCOUNT, "execution_id": "untagged-9", "instrument": "MNQ 12-26",
                                   "time": "2026-10-05T16:00:00Z", "action": "Buy", "order_name": "Manual",
                                   "quantity": 1, "price": 20010.0, "commission": 0.62, "is_entry": True,
                                   "is_exit": False}], tagger=lambda e: None)
        self.assertEqual(self.run_nudges("ready", untagged_fill)["nudges"], [])

    def test_version_outside_sim_is_not_listed(self):
        def to_live_small(rcon, jcon):
            rcon.execute("UPDATE specs SET status='live-small'")
            rcon.commit()
        out = self.run_nudges("ready", to_live_small)
        self.assertEqual((out["nudges"], out["checked"]), ([], 0))


if __name__ == "__main__":
    unittest.main()
