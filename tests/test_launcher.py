import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import launcher
import registry

# 2026-11-09 is a Monday. 14:00 UTC = 08:00 CT.
MON_8CT = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)
MON_6CT = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)   # 06:00 CT, before enable_from
FRI_1559 = datetime(2026, 11, 13, 21, 59, tzinfo=timezone.utc)
FRI_1600 = datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc)
SUN_1644 = datetime(2026, 11, 15, 22, 44, tzinfo=timezone.utc)
SUN_1645 = datetime(2026, 11, 15, 22, 45, tzinfo=timezone.utc)
MON_16CT = datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)  # after enable_until
SAT_8CT = datetime(2026, 11, 14, 14, 0, tzinfo=timezone.utc)
ACC = "LiveAcct"
SPEC = {"id": "sx", "version": "1.0.0", "instrument": {"root": "MNQ"}, "conflicts_with": [],
        "legs": [{"name": "a", "ninjascript": {"class": "A", "inputs": {}}},
                 {"name": "b", "ninjascript": {"class": "B", "inputs": {}}}]}
PROFILES = [{"strategy_id": "sx", "version": "1.0.0", "leg": "a", "account": ACC},
            {"strategy_id": "sx", "version": "1.0.0", "leg": "b", "account": ACC}]
GOOD_HEALTH = {"connections": [{"account": ACC, "status": "Connected", "price_status": "Connected"}]}
GATE_ENV = {"NT_OPERATOR_ALLOW_LIVE_SMALL": "1", "NT_OPERATOR_LIVE_SMALL_ACCOUNTS": ACC,
            "NT_OPERATOR_ALLOW_STRATEGY_START": "1", "NT_OPERATOR_TOKEN": "t"}


class Io:
    def __init__(self, running=True, health=GOOD_HEALTH, strategies=(), positions=(), login=False, acct=ACC):
        self.acct = acct
        self.is_running, self.health, self.strats, self.pos = running, health, list(strategies), list(positions)
        self.login = login
        self.sup_alive = False
        self.started, self.posts, self.alerts = [], [], []

    def dict(self):
        def fetch(p):
            if p == "/health":
                return self.health
            if p == "/strategies":
                return {"strategies": [{"name": n, "account": self.acct, "state": "Realtime"} for n in self.strats]}
            if p == "/status":
                return {"accounts": [{"name": self.acct, "positions": self.pos}]}
        def post(p, q, b):
            self.posts.append((p, q))
            self.strats.append(q["name"])
            return {"ok": True}
        return {"nt_running": lambda: self.is_running, "start_nt": self.started.append, "fetch": fetch,
                "post": post, "alert": self.alerts.append, "login_window_open": lambda: self.login,
                "supervisor_alive": lambda: self.sup_alive}


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"NT_AUDIT_FILE": os.path.join(self.tmp.name, "audit.jsonl"),
                                                "NT_LAUNCHER_STATE": os.path.join(self.tmp.name, "state.json"),
                                                "NT_SUPERVISOR_ALERT_LOG": os.path.join(self.tmp.name, "a.log"),
                                                **GATE_ENV})
        self.env.start()
        self.con = registry.connect(":memory:")
        self.con.execute("INSERT INTO specs(id,version,status,spec_json) VALUES('sx','1.0.0','live-small',?)",
                         (__import__("json").dumps(SPEC),))
        self.cfg = {**launcher.DEFAULT_CONFIG, "enabled": True, "nt_exe": __file__}
        mock.patch.object(launcher.sup_alerts, "toast").start()
        mock.patch.object(registry, "list_specs",
                          lambda c, status="": [{"id": "sx", "version": "1.0.0"}] if status == "live-small" else []).start()
        mock.patch.object(registry, "get_spec", lambda c, i, v: {"status": "live-small", "spec": SPEC}).start()

    def tearDown(self):
        mock.patch.stopall()
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def tick(self, io, now=MON_8CT, apply=True, state=None, cfg=None):
        state = {} if state is None else state
        return launcher.tick(cfg or self.cfg, state, self.con, PROFILES, now, apply, io.dict()), state

    def test_disabled_by_default(self):
        io = Io(running=False)
        out, _ = self.tick(io, cfg={**self.cfg, "enabled": False})
        self.assertIn("disabled", out[0])
        self.assertEqual(io.started, [])

    def test_weekend_is_quiet(self):
        for now in (FRI_1600, SAT_8CT, SUN_1644):
            io = Io(running=False)
            out, _ = self.tick(io, now=now)
            self.assertIn("weekend", out[0])
            self.assertEqual(io.started, [])

    def test_resumes_sunday_445_launches_but_does_not_enable(self):
        io = Io(running=False)
        self.tick(io, now=SUN_1645)
        self.assertEqual(len(io.started), 1)
        io = Io()
        out, _ = self.tick(io, now=SUN_1645)
        self.assertEqual(io.posts, [])
        self.assertIn("outside the enable window", out[-1])

    def test_friday_before_close_still_runs(self):
        io = Io(running=False)
        self.tick(io, now=FRI_1559)
        self.assertEqual(len(io.started), 1)

    def test_overnight_crash_is_recovered_but_not_enabled(self):
        io = Io(running=False)
        self.tick(io, now=MON_6CT)
        self.assertEqual(len(io.started), 1)
        io = Io()
        self.tick(io, now=MON_6CT)
        self.assertEqual(io.posts, [])

    def test_stuck_at_login_alerts_once_even_outside_enable_window(self):
        io = Io(health={"error": "down"})
        later = datetime(2026, 11, 9, 12, 30, tzinfo=timezone.utc)  # Monday 06:30 CT, before enable_from
        st = {"day": "2026-11-09", "launched_at": datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc).isoformat()}
        self.tick(io, now=later, state=st)
        self.assertEqual(len(io.alerts), 1)
        self.tick(io, now=later, state=st)
        self.assertEqual(len(io.alerts), 1)
        io = Io(health={"error": "down"})  # not yet 10 minutes
        self.tick(io, now=datetime(2026, 11, 9, 12, 5, tzinfo=timezone.utc), state=dict(st))
        self.assertEqual(io.alerts, [])

    def test_login_window_alerts_at_once_without_launch_record(self):
        # NinjaTrader started by a reboot or by hand, sitting at its login window: no launched_at, still alerts.
        io = Io(health={"error": "down"}, login=True)
        out, st = self.tick(io, now=MON_6CT)
        self.assertEqual(len(io.alerts), 1)
        self.assertIn("login window", io.alerts[0])
        self.assertTrue(any("login window" in line for line in out))
        self.tick(io, now=MON_6CT, state=st)
        self.assertEqual(len(io.alerts), 1)

    def test_running_supervisor_reports_stuck_login_instead(self):
        io = Io(health={"error": "down"}, login=True)
        io.sup_alive = True
        out, st = self.tick(io, now=MON_6CT)
        self.assertEqual(io.alerts, [])
        self.assertTrue(st["stuck_alerted"])
        self.assertTrue(any("supervisor" in line for line in out))

    def test_heartbeat_written_even_when_refusing_to_run(self):
        cfg = os.path.join(self.tmp.name, "launcher.json")
        with open(cfg, "w") as f:
            f.write('{"enabled": true, "enable_from": "7:3O"}')
        with mock.patch.dict(os.environ, {"NT_LAUNCHER_CONFIG": cfg}):
            self.assertEqual(launcher.main(["--apply"]), 2)
            hb = launcher.nt_local.read_launcher_heartbeat()
        self.assertFalse(hb["ok"])
        self.assertIn("bad time", hb["detail"])

    def test_login_window_not_alerted_in_plan_mode(self):
        io = Io(health={"error": "down"}, login=True)
        self.tick(io, now=MON_6CT, apply=False)
        self.assertEqual(io.alerts, [])

    def test_stuck_alert_survives_midnight(self):
        # Relaunched at 23:50 CT Sunday, still stuck at 00:30 CT Monday: the alert must still fire.
        st = {"day": "2026-11-08", "launched_at": datetime(2026, 11, 9, 5, 50, tzinfo=timezone.utc).isoformat()}
        io = Io(health={"error": "down"})
        self.tick(io, now=datetime(2026, 11, 9, 6, 30, tzinfo=timezone.utc), state=st)
        self.assertEqual(st["day"], "2026-11-09")
        self.assertEqual(len(io.alerts), 1)

    def test_stuck_alert_rearms_after_recovery(self):
        st = {"day": "2026-11-09"}
        io = Io(health={"error": "down"}, login=True)
        self.tick(io, now=MON_6CT, state=st)
        io.health = GOOD_HEALTH
        io.login = False
        self.tick(io, now=MON_6CT, state=st)
        self.assertNotIn("stuck_alerted", st)
        io.health, io.login = {"error": "down"}, True
        self.tick(io, now=MON_6CT, state=st)
        self.assertEqual(len(io.alerts), 2)

    def test_no_enable_after_window(self):
        io = Io()
        self.tick(io, now=MON_16CT)
        self.assertEqual(io.posts, [])

    def test_launches_nt_once(self):
        io = Io(running=False)
        out, st = self.tick(io)
        self.assertEqual(io.started, [__file__])
        self.tick(io, state=st)
        self.assertEqual(len(io.started), 1)  # relaunch backoff

    def test_plan_mode_changes_nothing(self):
        io = Io(running=False)
        self.tick(io, apply=False)
        self.assertEqual(io.started, [])
        io = Io()
        self.tick(io, apply=False)
        self.assertEqual(io.posts, [])

    def test_enables_all_legs_then_not_again(self):
        io = Io()
        out, st = self.tick(io)
        self.assertEqual(sorted(q["name"] for _, q in io.posts), ["sx 1.0.0 a", "sx 1.0.0 b"])
        self.assertTrue(all(q["account"] == ACC for _, q in io.posts))
        n = len(io.posts)
        self.tick(io, state=st)
        self.assertEqual(len(io.posts), n)

    def test_stopped_by_hand_is_not_restarted_same_day(self):
        io = Io()
        _, st = self.tick(io)
        io.strats.clear()
        out, _ = self.tick(io, state=st)
        self.assertTrue(any("not restarting" in l for l in out))
        self.assertEqual(len(io.posts), 2)

    def test_waits_for_connection(self):
        bad = {"connections": [{"account": ACC, "status": "Disconnected", "price_status": "Connected"}]}
        io = Io(health=bad)
        out, _ = self.tick(io)
        self.assertEqual(io.posts, [])
        self.assertTrue(any("waiting" in l for l in out))
        io = Io(health={"error": "down"})
        self.tick(io)
        self.assertEqual(io.posts, [])

    def test_refusals(self):
        for io in (Io(positions=[{"instrument": "MNQ 12-26", "quantity": 1}]), Io(strategies=["other thing"])):
            self.tick(io)
            self.assertEqual(io.posts, [])
        with mock.patch.dict(os.environ, {"NT_OPERATOR_ALLOW_STRATEGY_START": "0"}):
            io = Io()
            self.tick(io)
            self.assertEqual(io.posts, [])

    def test_partial_presence_left_alone(self):
        io = Io(strategies=["sx 1.0.0 a"])
        self.tick(io)
        self.assertEqual(io.posts, [])

    def test_live_gate_matches_server(self):
        self.assertIsNone(launcher.live_gate(ACC, "live-small"))
        self.assertIn("human", launcher.live_gate(ACC, "live"))
        self.assertIn("LIVE_SMALL_ACCOUNTS", launcher.live_gate("Other", "live-small"))
        self.assertIsNone(launcher.live_gate("Sim101", "live-small"))

    def test_account_comes_from_single_profile(self):
        split = PROFILES + [{**PROFILES[0], "account": "Other"}]
        t, problems = launcher.live_targets(self.con, split)
        self.assertEqual(t, [])
        self.assertEqual(len(problems), 1)


    def _sim_setup(self, account):
        mock.patch.object(registry, "list_specs",
                          lambda c, status="": [{"id": "sx", "version": "1.0.0"}] if status == "sim" else []).start()
        mock.patch.object(registry, "get_spec", lambda c, i, v: {"status": "sim", "spec": SPEC}).start()
        profiles = [{**p, "account": account} for p in PROFILES]
        health = {"connections": [{"account": account, "status": "Connected", "price_status": "Connected"}]}
        return profiles, Io(health=health, acct=account)

    def test_sim_stage_enabled_on_sim_account(self):
        profiles, io = self._sim_setup("Sim101")
        with mock.patch.dict(os.environ, {"NT_OPERATOR_ALLOW_LIVE_SMALL": "0"}):
            out = launcher.tick(self.cfg, {}, self.con, profiles, MON_8CT, True, io.dict())
        self.assertEqual([q["name"] for _, q in io.posts], ["sx 1.0.0 a", "sx 1.0.0 b"], out)
        self.assertTrue(all(q["account"] == "Sim101" for _, q in io.posts))

    def test_sim_stage_never_on_real_account(self):
        profiles, io = self._sim_setup(ACC)
        out = launcher.tick(self.cfg, {}, self.con, profiles, MON_8CT, True, io.dict())
        self.assertEqual(io.posts, [])
        self.assertTrue(any("Sim* account" in line for line in out), out)

    def test_sim_stage_off_in_config(self):
        profiles, io = self._sim_setup("Sim101")
        out = launcher.tick({**self.cfg, "enable_sim": False}, {}, self.con, profiles, MON_8CT, True, io.dict())
        self.assertEqual(io.posts, [])
        self.assertIn("no live strategy to start", out[-1])


if __name__ == "__main__":
    unittest.main()
