"""Owner approval and scheduled approvals: secret gating, status change, tick execution/voiding, no network."""
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))
import approvals  # noqa: E402
import control  # noqa: E402
import feedback_fixture as fx  # noqa: E402
import journal  # noqa: E402
import launcher  # noqa: E402

SECRET = "owner-pass-1"
ENV = {"NT_OPERATOR_TOKEN": "tok", approvals.SECRET_HASH_ENV: approvals.secret_hash(SECRET)}
NOW = fx.AS_OF
NB = (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
UNTAGGED = {"account": fx.ACCOUNT, "execution_id": "u-9", "instrument": "MNQ 12-26", "time": "2026-10-05T16:00:00Z",
            "action": "Buy", "order_name": "Manual", "quantity": 1, "price": 20010.0, "commission": 0.62,
            "is_entry": True, "is_exit": False}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"NT_AUDIT_FILE": os.path.join(self.tmp.name, "audit.jsonl")})
        p.start()
        self.addCleanup(p.stop)
        u = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network call"))
        u.start()
        self.addCleanup(u.stop)

    def make(self, sc="ready"):
        rcon, jcon = fx.build(sc)
        self.addCleanup(rcon.close)
        self.addCleanup(jcon.close)
        return rcon, jcon

    def status(self, rcon):
        return rcon.execute("SELECT status FROM specs").fetchone()[0]

    def audit_text(self):
        with open(control.audit_path(), encoding="utf-8") as f:
            return f.read()


class ApproveTests(Base):
    def go(self, rcon, jcon, secret=SECRET, reason="looks good", **kw):
        return approvals.approve(rcon, jcon, "strat-a", "1.0.0", "acct-1", reason, secret, now=NOW, env=ENV, **kw)

    def test_secret_gates_the_status_change(self):
        rcon, jcon = self.make()
        for bad in ("", "wrong"):
            self.assertFalse(self.go(rcon, jcon, secret=bad)["ok"])
            self.assertEqual(self.status(rcon), "sim")
        r = approvals.approve(rcon, jcon, "strat-a", "1.0.0", "acct-1", "x", SECRET, now=NOW, env={})
        self.assertFalse(r["ok"])
        self.assertEqual(self.status(rcon), "sim")

    def test_approve_moves_to_live_small_and_records_row(self):
        rcon, jcon = self.make()
        r = self.go(rcon, jcon)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.status(rcon), "live-small")
        row = rcon.execute("SELECT * FROM live_small_approvals").fetchone()
        self.assertEqual((row["account_alias"], row["source"]), ("acct-1", "immediate"))
        self.assertEqual(json.loads(row["snapshot_json"])["verdict"], "promote")
        text = self.audit_text()
        self.assertIn("approve_live_small", text)
        self.assertNotIn(SECRET, text)

    def test_not_ready_refused_unless_acknowledged(self):
        rcon, jcon = self.make("not_ready")
        self.assertFalse(self.go(rcon, jcon)["ok"])
        self.assertEqual(self.status(rcon), "sim")
        self.assertTrue(self.go(rcon, jcon, acknowledge_not_ready=True)["ok"])
        snap = json.loads(rcon.execute("SELECT snapshot_json FROM live_small_approvals").fetchone()[0])
        self.assertTrue(snap["acknowledged_not_ready"])

    def test_reason_alias_and_stage_required(self):
        rcon, jcon = self.make()
        self.assertFalse(self.go(rcon, jcon, reason="  ")["ok"])
        r = approvals.approve(rcon, jcon, "strat-a", "1.0.0", "Sim 101!", "r", SECRET, now=NOW, env=ENV)
        self.assertFalse(r["ok"])
        rcon.execute("UPDATE specs SET status='backtest'")
        rcon.commit()
        r = self.go(rcon, jcon)
        self.assertFalse(r["ok"])
        self.assertIn("only a 'sim' version", r["error"])

    def test_approve_supersedes_pending_schedule(self):
        rcon, jcon = self.make()
        s = approvals.schedule(rcon, jcon, "strat-a", "1.0.0", "acct-1", "later", SECRET, NB, now=NOW, env=ENV)
        self.assertTrue(s["ok"], s)
        self.assertTrue(self.go(rcon, jcon)["ok"])
        self.assertEqual(approvals.list_schedules(rcon)[0]["state"], "superseded")


class ScheduleTests(Base):
    def sched(self, rcon, jcon, nb=NB, secret=SECRET, **kw):
        return approvals.schedule(rcon, jcon, "strat-a", "1.0.0", "acct-1", "go", secret, nb, now=NOW, env=ENV, **kw)

    def test_requires_secret_and_valid_times(self):
        rcon, jcon = self.make()
        self.assertFalse(self.sched(rcon, jcon, secret="nope")["ok"])
        self.assertFalse(self.sched(rcon, jcon, nb="tomorrow-ish")["ok"])
        self.assertFalse(self.sched(rcon, jcon, expires_at=NB)["ok"])
        self.assertFalse(self.sched(rcon, jcon, expires_at="2027-01-01T00:00:00Z")["ok"])
        self.assertEqual(approvals.list_schedules(rcon), [])

    def test_one_pending_per_version_and_cancel(self):
        rcon, jcon = self.make()
        a, b = self.sched(rcon, jcon), self.sched(rcon, jcon)
        states = {r["schedule_id"]: r["state"] for r in approvals.list_schedules(rcon)}
        self.assertEqual((states[a["schedule_id"]], states[b["schedule_id"]]), ("superseded", "pending"))
        self.assertFalse(approvals.cancel(rcon, b["schedule_id"], "bad", now=NOW, env=ENV)["ok"])
        self.assertTrue(approvals.cancel(rcon, b["schedule_id"], SECRET, now=NOW, env=ENV)["ok"])
        self.assertFalse(approvals.cancel(rcon, b["schedule_id"], SECRET, now=NOW, env=ENV)["ok"])
        self.assertEqual(self.status(rcon), "sim")

    def test_default_expiry(self):
        rcon, jcon = self.make()
        r = self.sched(rcon, jcon)
        self.assertEqual(r["expires_at"], (NOW + timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ"))


class RunDueTests(Base):
    def make_pending(self, sc="ready"):
        rcon, jcon = self.make(sc)
        approvals.schedule(rcon, jcon, "strat-a", "1.0.0", "acct-1", "go", SECRET, NB, now=NOW, env=ENV)
        return rcon, jcon

    def test_not_due_is_left_alone(self):
        rcon, jcon = self.make_pending()
        self.assertEqual(approvals.run_due(rcon, jcon, NOW), [])
        self.assertEqual(self.status(rcon), "sim")

    def test_executes_when_ready_without_secret(self):
        rcon, jcon = self.make_pending()
        ev = approvals.run_due(rcon, jcon, NOW + timedelta(hours=2))
        self.assertEqual([e["action"] for e in ev], ["executed"])
        self.assertEqual(self.status(rcon), "live-small")
        self.assertEqual(rcon.execute("SELECT source FROM live_small_approvals").fetchone()[0], "scheduled")
        self.assertEqual(approvals.list_schedules(rcon)[0]["state"], "executed")
        self.assertEqual(approvals.run_due(rcon, jcon, NOW + timedelta(hours=3)), [])

    def test_plan_mode_writes_nothing(self):
        rcon, jcon = self.make_pending()
        ev = approvals.run_due(rcon, jcon, NOW + timedelta(hours=2), apply=False)
        self.assertEqual(ev[0]["action"], "would_execute")
        self.assertEqual((self.status(rcon), approvals.list_schedules(rcon)[0]["state"]), ("sim", "pending"))

    def test_voids_when_readiness_lapsed(self):
        rcon, jcon = self.make_pending()
        journal.record(jcon, [UNTAGGED], tagger=lambda e: None)
        ev = approvals.run_due(rcon, jcon, NOW + timedelta(hours=2))
        self.assertEqual(ev[0]["action"], "voided")
        self.assertIn("readiness lapsed", ev[0]["reason"])
        self.assertEqual(self.status(rcon), "sim")
        self.assertIn("scheduled_approval_voided", self.audit_text())

    def test_voids_when_expired_or_left_sim(self):
        rcon, jcon = self.make_pending()
        ev = approvals.run_due(rcon, jcon, NOW + timedelta(days=3))
        self.assertIn("expired", ev[0]["reason"])
        rcon, jcon = self.make_pending()
        rcon.execute("UPDATE specs SET status='retired'")
        rcon.commit()
        ev = approvals.run_due(rcon, jcon, NOW + timedelta(hours=2))
        self.assertEqual(ev[0]["action"], "voided")


class LauncherStepTests(Base):
    def test_step_executes_and_alerts(self):
        rcon, jcon = self.make()
        approvals.schedule(rcon, jcon, "strat-a", "1.0.0", "acct-1", "go", SECRET, NB, now=NOW, env=ENV)
        alerts = []
        lines = launcher.scheduled_approvals_step(rcon, NOW + timedelta(hours=2), True, {"journal_con": jcon},
                                                  alerts.append)
        self.assertIn("executed", lines[0])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(self.status(rcon), "live-small")

    def test_step_failure_does_not_raise(self):
        rcon, jcon = self.make()
        approvals.schedule(rcon, jcon, "strat-a", "1.0.0", "acct-1", "go", SECRET, NB, now=NOW, env=ENV)

        def boom(*a):
            raise RuntimeError("x")
        lines = launcher.scheduled_approvals_step(rcon, NOW + timedelta(hours=2), True,
                                                  {"journal_con": jcon, "readiness_fn": boom}, lambda m: None)
        self.assertIn("step failed", lines[0])
        self.assertEqual(self.status(rcon), "sim")


if __name__ == "__main__":
    unittest.main()
