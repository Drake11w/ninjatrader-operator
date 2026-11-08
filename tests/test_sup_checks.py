import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import sup_checks
import sup_profile

NOW = datetime(2026, 11, 6, 15, 0, 0, tzinfo=timezone.utc)


def prof(**kw):
    p = {"strategy_id": "s742", "version": "1.0.0", "leg": "orb", "instance_names": ["s742 1.0.0 orb"],
         "account": "Sim101", "authorized_accounts": ["Sim101", "LucidSim"], "grace_seconds": 60}
    p.update(kw)
    return sup_profile.normalize(p)


def ok(data, age=1, at=NOW):
    data = dict(data)
    data["timestamp"] = (at - timedelta(seconds=age)).isoformat()
    return {"ok": True, "fetched_at": NOW, "data": data, "error": None}


def fail(msg="refused"):
    return {"ok": False, "fetched_at": NOW, "data": None, "error": msg}


def health(status="Connected", price="Connected", acct="Sim101", at=NOW):
    return ok({"connections": [{"account": acct, "connection": "Sim", "status": status, "price_status": price}]},
              at=at)


def strats(*rows, age=1, at=NOW):
    base = dict(account="Sim101", name="s742 1.0.0 orb", instrument="MNQ 12-26", state="Realtime",
                position="Flat", quantity=0)
    return ok({"strategies": [{**base, **r} for r in rows]}, age, at)


def keys(findings):
    return sorted(f["key"] for f in findings)


def run(obs, profiles=None, state=None, now=NOW, disables=()):
    return sup_checks.evaluate(obs, profiles or [prof()], state or {}, now, disables)


# Monday 2026-11-09. 14:30 UTC = 08:30 CT; 21:30 UTC = 15:30 CT. Saturday 2026-11-07 14:30 UTC.
MON_0830 = datetime(2026, 11, 9, 14, 30, tzinfo=timezone.utc)
MON_1530 = datetime(2026, 11, 9, 21, 30, tzinfo=timezone.utc)
SAT = datetime(2026, 11, 7, 14, 30, tzinfo=timezone.utc)
WINDOW = {"days": [0, 1, 2, 3, 4], "from": "08:15", "until": "15:00"}


class ScheduleTests(unittest.TestCase):
    def test_missing_outside_active_window_is_not_reported(self):
        p = [prof(active_ct=WINDOW, grace_seconds=0)]
        f, st = run({"health": health(at=MON_1530), "strategies": strats(at=MON_1530)}, p, now=MON_1530)
        self.assertEqual(f, [])
        self.assertEqual(st["missing"], {})

    def test_missing_inside_active_window_is_reported(self):
        p = [prof(active_ct=WINDOW, grace_seconds=0)]
        f, _ = run({"health": health(at=MON_0830), "strategies": strats(at=MON_0830)}, p, now=MON_0830)
        self.assertEqual(keys(f), ["strategy_missing"])

    def test_stop_after_window_end_is_not_unexpected_and_forgotten(self):
        p = [prof(active_ct=WINDOW)]
        _, st = run({"health": health(at=MON_0830), "strategies": strats({}, at=MON_0830)}, p, now=MON_0830)
        self.assertIn("Sim101|s742 1.0.0 orb", st["running"])
        f, st = run({"health": health(at=MON_1530), "strategies": strats({"state": "Terminated"}, at=MON_1530)},
                    p, st, now=MON_1530)
        self.assertEqual(f, [])
        self.assertEqual(st["running"], {})

    def test_wrong_account_still_reported_outside_window(self):
        p = [prof(active_ct=WINDOW)]
        f, _ = run({"health": health(at=MON_1530), "strategies": strats({"account": "Other"}, at=MON_1530)},
                   p, now=MON_1530)
        self.assertIn("wrong_account", keys(f))

    def test_profile_without_window_is_always_active(self):
        self.assertTrue(sup_profile.is_active(prof(), MON_1530))

    def test_window_validation(self):
        bad = [{"from": "15:00", "until": "08:00"}, {"from": "8am", "until": "15:00"},
               {"days": [7], "from": "08:00", "until": "15:00"}, {"from": "08:00", "until": "15:00", "x": 1}, []]
        for w in bad:
            self.assertTrue(sup_profile.validate({**prof(), "active_ct": w}), w)
        self.assertEqual(sup_profile.validate({**prof(), "active_ct": WINDOW}), [])


class WeekendTests(unittest.TestCase):
    def test_closed_ninjatrader_on_weekend_is_quiet(self):
        obs = {"health": fail(), "strategies": fail(), "process": {"nt_running": False, "login_window": False}}
        st = {}
        for _ in range(5):
            f, st = run(obs, state=st, now=SAT)
        self.assertEqual(f, [])

    def test_disconnected_and_missing_on_weekend_is_quiet(self):
        f, _ = run({"health": health("Disconnected", at=SAT), "strategies": strats(at=SAT)},
                   [prof(grace_seconds=0)], now=SAT)
        self.assertEqual(f, [])

    def test_wrong_account_still_reported_on_weekend(self):
        f, _ = run({"health": health(at=SAT), "strategies": strats({"account": "Other"}, at=SAT)}, now=SAT)
        self.assertIn("wrong_account", keys(f))


class CauseTests(unittest.TestCase):
    def unreachable(self, process):
        obs = {"health": fail(), "strategies": fail()}
        if process is not None:
            obs["process"] = process
        st = {}
        for _ in range(3):
            f, st = run(obs, state=st)
        self.assertEqual(keys(f), ["addon_unreachable"])  # one incident, not one per endpoint
        return f[0]

    def test_not_running(self):
        f = self.unreachable({"nt_running": False, "login_window": False})
        self.assertEqual(f["evidence"]["cause"], "nt_not_running")
        self.assertTrue(f["trigger"].startswith("NinjaTrader is not running"))

    def test_login_window(self):
        f = self.unreachable({"nt_running": True, "login_window": True})
        self.assertEqual(f["evidence"]["cause"], "login_window")
        self.assertIn("log in on the PC", f["trigger"])

    def test_addon_broken(self):
        self.assertEqual(self.unreachable({"nt_running": True, "login_window": False})["evidence"]["cause"],
                         "addon_not_answering")

    def test_unknown_without_process_facts(self):
        self.assertEqual(self.unreachable(None)["evidence"]["cause"], "unknown")


class HealthyTests(unittest.TestCase):
    def test_all_normal_gives_no_findings(self):
        f, st = run({"health": health(), "strategies": strats({})})
        self.assertEqual(f, [])
        self.assertIn("Sim101|s742 1.0.0 orb", st["running"])
        self.assertTrue(sup_checks.data_current({"health": health(), "strategies": strats({})}, NOW))


class ReachabilityTests(unittest.TestCase):
    def test_unreachable_needs_three_cycles(self):
        obs = {"health": fail(), "strategies": fail()}
        state = {}
        for i in range(2):
            f, state = run(obs, state=state)
            self.assertEqual(f, [])
        f, state = run(obs, state=state)
        self.assertIn("addon_unreachable", keys(f))
        self.assertFalse(sup_checks.data_current(obs, NOW))

    def test_success_resets_counter(self):
        state = {}
        for _ in range(2):
            _, state = run({"health": fail(), "strategies": fail()}, state=state)
        _, state = run({"health": health(), "strategies": strats({})}, state=state)
        f, _ = run({"health": fail(), "strategies": fail()}, state=state)
        self.assertEqual(f, [])

    def test_stale_data_is_flagged_and_not_trusted(self):
        obs = {"health": ok({"connections": []}, age=120), "strategies": strats({}, age=120)}
        f, _ = run(obs)
        self.assertEqual(keys(f), ["data_stale", "data_stale"])
        self.assertFalse(sup_checks.data_current(obs, NOW))

    def test_missing_timestamp_counts_as_stale(self):
        o = {"ok": True, "fetched_at": NOW, "data": {"connections": []}, "error": None}
        f, _ = run({"health": o, "strategies": strats({})})
        self.assertIn("data_stale", keys(f))


class ConnectionTests(unittest.TestCase):
    def test_account_disconnected_blames_infrastructure_not_strategy(self):
        f, _ = run({"health": health(status="Disconnected"), "strategies": strats({"state": "Terminated"})})
        self.assertEqual(keys(f), ["account_disconnected"])

    def test_feed_disconnected(self):
        f, _ = run({"health": health(price="Disconnected"), "strategies": strats({})})
        self.assertEqual(keys(f), ["feed_disconnected"])

    def test_no_connection_entry(self):
        f, _ = run({"health": ok({"connections": []}), "strategies": strats({})})
        self.assertEqual(keys(f), ["account_disconnected"])


class StrategyTests(unittest.TestCase):
    def test_missing_after_grace_only(self):
        obs = {"health": health(), "strategies": strats()}
        f, st = run(obs)
        self.assertEqual(f, [])
        t = NOW + timedelta(seconds=61)
        f, st = run({"health": health(at=t), "strategies": strats(at=t)}, state=st, now=t)
        self.assertEqual(keys(f), ["strategy_missing"])

    def test_not_running_after_grace(self):
        obs = {"health": health(), "strategies": strats({"state": "Historical"})}
        f, st = run(obs)
        self.assertEqual(f, [])
        t = NOW + timedelta(seconds=90)
        f, _ = run({"health": health(at=t), "strategies": strats({"state": "Historical"}, at=t)}, state=st, now=t)
        self.assertEqual(keys(f), ["strategy_not_running"])

    def test_unexpected_stop_is_immediate(self):
        _, st = run({"health": health(), "strategies": strats({})})
        t = NOW + timedelta(seconds=10)
        f, _ = run({"health": health(at=t), "strategies": strats({"state": "Terminated"}, at=t)}, state=st, now=t)
        self.assertEqual(keys(f), ["strategy_unexpected_stop"])

    def test_operator_disable_is_not_an_incident(self):
        _, st = run({"health": health(), "strategies": strats({})})
        t = NOW + timedelta(seconds=10)
        f, st = run({"health": health(at=t), "strategies": strats({"state": "Terminated"}, at=t)}, state=st,
                    now=t, disables={"s742 1.0.0 orb"})
        self.assertEqual(f, [])
        self.assertNotIn("Sim101|s742 1.0.0 orb", st["running"])

    def test_wrong_account_is_critical(self):
        rows = strats({"account": "Live1"})
        f, _ = run({"health": health(), "strategies": rows}, profiles=[prof(monitored=False)])
        self.assertEqual(keys(f), ["wrong_account"])
        self.assertEqual(f[0]["severity"], 3)

    def test_authorized_second_account_is_fine(self):
        f, _ = run({"health": health(), "strategies": strats({"account": "LucidSim"})},
                   profiles=[prof(monitored=False)])
        self.assertEqual(f, [])

    def test_unknown_strategy_on_monitored_account_is_warning(self):
        f, _ = run({"health": health(), "strategies": strats({}, {"name": "mystery"})})
        self.assertEqual(keys(f), ["unknown_strategy"])
        self.assertEqual(f[0]["severity"], 1)

    def test_unreachable_strategies_do_not_clear_running_marks(self):
        _, st = run({"health": health(), "strategies": strats({})})
        f, st2 = run({"health": health(), "strategies": fail()}, state=st)
        self.assertEqual(st2["running"], st["running"])


class ProfileTests(unittest.TestCase):
    def write(self, obj):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        self.addCleanup(os.remove, path)
        return path

    def test_load_valid(self):
        p = sup_profile.load(self.write({"profiles": [prof()]}))
        self.assertEqual(p[0]["expected_state"], "Realtime")

    def test_rejects_account_not_authorized(self):
        bad = prof(account="Other")
        with self.assertRaises(ValueError):
            sup_profile.load(self.write({"profiles": [bad]}))

    def test_rejects_duplicate_instance_names(self):
        with self.assertRaises(ValueError):
            sup_profile.load(self.write({"profiles": [prof(), prof(strategy_id="other")]}))

    def test_missing_file(self):
        with self.assertRaises(ValueError):
            sup_profile.load("C:/nope/does-not-exist.json")


if __name__ == "__main__":
    unittest.main()
