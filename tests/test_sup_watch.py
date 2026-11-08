import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import nt_local

import sup_incidents as si
import sup_watch

T0 = datetime(2026, 11, 6, 15, 0, 0, tzinfo=timezone.utc)


def finding(sev=2, key="strategy_unexpected_stop"):
    return {"key": key, "severity": sev, "account": "A", "strategy": "s", "trigger": "stopped", "evidence": {},
            "caused_by": ""}


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.con = si.connect(":memory:")
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = os.path.join(self.tmp.name, "launcher.json")
        self.env = mock.patch.dict(os.environ, {"NT_LAUNCHER_CONFIG": self.cfg,
                                                "NT_LAUNCHER_STATE": os.path.join(self.tmp.name, "state.json")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def launcher(self, enabled=True):
        with open(self.cfg, "w") as f:
            json.dump({"enabled": enabled}, f)

    def test_launcher_off_is_not_watched(self):
        self.beat(T0)
        self.launcher(False)
        self.assertEqual(sup_watch.check(self.con, T0), [])

    def test_launcher_never_ran(self):
        self.beat(T0)
        self.launcher()
        out = sup_watch.check(self.con, T0)
        self.assertEqual(len(out), 1)
        self.assertIn("never run", out[0])
        self.assertEqual(sup_watch.check(self.con, T0), [])

    def test_launcher_stale_then_recovers(self):
        self.beat(T0)
        self.launcher()
        nt_local.write_launcher_heartbeat(T0 - timedelta(minutes=80), True, "ok")
        self.assertIn("has not run for 80 min", sup_watch.check(self.con, T0)[0])
        self.assertEqual(sup_watch.check(self.con, T0), [])
        nt_local.write_launcher_heartbeat(T0, True, "ok")
        self.assertEqual(sup_watch.check(self.con, T0), ["Launcher is running again."])
        self.assertEqual(sup_watch.check(self.con, T0), [])

    def test_launcher_failed_run_is_reported(self):
        self.beat(T0)
        self.launcher()
        nt_local.write_launcher_heartbeat(T0, False, "refusing to run: bad time 7:3O")
        self.assertIn("bad time", sup_watch.check(self.con, T0)[0])

    def test_unparseable_launcher_config_counts_as_enabled(self):
        with open(self.cfg, "w") as f:
            f.write("{not json")
        self.assertTrue(nt_local.launcher_enabled())

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

    def test_push_toasts_and_posts_only_when_url_set(self):
        import os
        from unittest import mock
        with mock.patch.object(sup_watch.sup_alerts, "toast") as toast,                 mock.patch.object(sup_watch.urllib.request, "urlopen") as uo:
            os.environ.pop("NT_WATCH_PUSH_URL", None)
            sup_watch.push("x")
            toast.assert_called_once()
            uo.assert_not_called()
            os.environ["NT_WATCH_PUSH_URL"] = "https://ntfy.example/topic"
            self.addCleanup(lambda: os.environ.pop("NT_WATCH_PUSH_URL", None))
            sup_watch.push("x")
            uo.assert_called_once()

    def test_push_never_raises(self):
        from unittest import mock
        with mock.patch.object(sup_watch.sup_alerts, "toast", side_effect=OSError):
            try:
                sup_watch.push("x")
            except OSError:
                self.fail("push raised")


if __name__ == "__main__":
    unittest.main()
