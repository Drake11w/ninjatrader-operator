import unittest
from datetime import datetime, timedelta, timezone

import sup_incidents as si

T0 = datetime(2026, 11, 6, 15, 0, 0, tzinfo=timezone.utc)


def f(key="strategy_not_running", sev=2, account="Sim101", strategy="s", trigger="t"):
    return {"key": key, "severity": sev, "account": account, "strategy": strategy, "trigger": trigger,
            "evidence": {"x": 1}, "caused_by": ""}


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.con = si.connect(":memory:")

    def tick(self, findings, n, allow=True):
        return si.apply_findings(self.con, findings, T0 + timedelta(seconds=10 * n), allow)

    def test_open_then_dedupe(self):
        e = self.tick([f()], 0)
        self.assertEqual([x["kind"] for x in e], ["opened"])
        self.assertEqual(self.tick([f()], 1), [])
        rows = si.list_incidents(self.con)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "INC-20261106-0001")
        self.assertEqual(rows[0]["actions_json"], "[]")

    def test_escalation_recorded(self):
        self.tick([f(sev=1)], 0)
        e = self.tick([f(sev=3)], 1)
        self.assertEqual(e[0]["kind"], "escalated")
        row = si.list_incidents(self.con)[0]
        self.assertEqual((row["severity"], row["needs_human"]), (3, 1))

    def test_severity_never_drops_while_open(self):
        self.tick([f(sev=3)], 0)
        self.tick([f(sev=1)], 1)
        self.assertEqual(si.list_incidents(self.con)[0]["severity"], 3)

    def test_resolves_only_after_three_clear_cycles(self):
        self.tick([f()], 0)
        self.assertEqual(self.tick([], 1), [])
        self.assertEqual(self.tick([], 2), [])
        e = self.tick([], 3)
        self.assertEqual(e[0]["kind"], "resolved")
        self.assertEqual(si.list_incidents(self.con, "active"), [])

    def test_flapping_resets_counter(self):
        self.tick([f()], 0)
        self.tick([], 1)
        self.tick([], 2)
        self.tick([f()], 3)
        self.assertEqual(self.tick([], 4), [])
        self.assertEqual(len(si.list_incidents(self.con, "active")), 1)

    def test_stale_data_cannot_resolve(self):
        self.tick([f()], 0)
        for n in range(1, 8):
            self.tick([], n, allow=False)
        self.assertEqual(len(si.list_incidents(self.con, "active")), 1)

    def test_recurrence_after_resolution_is_new_incident(self):
        self.tick([f()], 0)
        for n in range(1, 4):
            self.tick([], n)
        self.tick([f()], 5)
        self.assertEqual(len(si.list_incidents(self.con)), 2)

    def test_distinct_conditions_are_separate(self):
        self.tick([f(strategy="a"), f(strategy="b")], 0)
        self.assertEqual(len(si.list_incidents(self.con)), 2)

    def test_acknowledge_and_events(self):
        self.tick([f()], 0)
        iid = si.list_incidents(self.con)[0]["id"]
        self.assertTrue(si.acknowledge(self.con, iid, T0, "seen"))
        self.assertFalse(si.acknowledge(self.con, iid, T0))
        self.assertEqual([e["kind"] for e in si.events_for(self.con, iid)], ["opened", "acknowledged"])
        self.tick([f()], 1)  # acknowledged incident still dedupes
        self.assertEqual(len(si.list_incidents(self.con)), 1)

    def test_state_roundtrip(self):
        si.set_state(self.con, "k", {"a": [1]})
        si.set_state(self.con, "k", {"a": [2]})
        self.assertEqual(si.get_state(self.con, "k"), {"a": [2]})
        self.assertIsNone(si.get_state(self.con, "none"))


if __name__ == "__main__":
    unittest.main()
