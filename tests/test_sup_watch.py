import unittest
from datetime import datetime, timedelta, timezone

import sup_incidents as si
import sup_watch

T0 = datetime(2026, 11, 6, 15, 0, 0, tzinfo=timezone.utc)


def finding(sev=2, key="strategy_unexpected_stop"):
    return {"key": key, "severity": sev, "account": "A", "strategy": "s", "trigger": "stopped", "evidence": {},
            "caused_by": ""}


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.con = si.connect(":memory:")

    def beat(self, t):
        si.set_state(self.con, "heartbeat", t.isoformat())

    def test_never_started_alerts_once(self):
        self.assertIn("never started", sup_watch.check(self.con, T0)[0])
        self.assertEqual(sup_watch.check(self.con, T0 + timedelta(minutes=5)), [])

    def test_stale_heartbeat_alerts_once_then_recovery(self):
        self.beat(T0 - timedelta(seconds=300))
        self.assertIn("NOT RUNNING", sup_watch.check(self.con, T0)[0])
        self.assertEqual(sup_watch.check(self.con, T0 + timedelta(seconds=30)), [])
        self.beat(T0 + timedelta(seconds=60))
        self.assertEqual(sup_watch.check(self.con, T0 + timedelta(seconds=61)), ["Supervisor is running again."])

    def test_healthy_and_quiet(self):
        self.beat(T0)
        self.assertEqual(sup_watch.check(self.con, T0), [])

    def test_incident_reported_once_and_escalation_realerts(self):
        self.beat(T0)
        si.apply_findings(self.con, [finding(2)], T0)
        self.assertEqual(len(sup_watch.check(self.con, T0)), 1)
        self.assertEqual(sup_watch.check(self.con, T0), [])
        si.apply_findings(self.con, [finding(3)], T0)
        out = sup_watch.check(self.con, T0)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("SEV3"))

    def test_warning_level_is_not_pushed(self):
        self.beat(T0)
        si.apply_findings(self.con, [finding(1, "unknown_strategy")], T0)
        self.assertEqual(sup_watch.check(self.con, T0), [])


if __name__ == "__main__":
    unittest.main()
