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
