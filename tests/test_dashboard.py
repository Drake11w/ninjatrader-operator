import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import dashboard
import journal
import registry

with open(os.path.join(ROOT, "examples", "two-leg-mnq.strategy.json"), encoding="utf-8") as f:
    SPEC = json.load(f)

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)


def fake(path):
    if path.startswith("/status"):
        return {"accounts": [{"name": "Sim101", "connection": "Sim", "cash_value": 50100, "realized_pnl": 100,
                              "unrealized_pnl": 0, "positions": []}]}
    if path.startswith("/health"):
        return {"ok": True, "connections": []}
    if path.startswith("/strategies"):
        n = f"{SPEC['id']} {SPEC['version']} {SPEC['legs'][0]['name']}"
        return {"strategies": [{"account": "Sim101", "name": n, "state": "Realtime", "position": "Flat", "quantity": 0}]}
    if path.startswith("/executions"):
        names = SPEC["legs"][0]["ninjascript"]["order_names"]
        return {"executions": [
            {"account": "Sim101", "execution_id": "a", "instrument": "MNQ 12-26", "time": "2026-11-09T15:00:00Z",
             "action": "Buy", "order_name": names[0], "quantity": 1, "price": 100.0, "commission": 0.5, "is_entry": True},
            {"account": "Sim101", "execution_id": "b", "instrument": "MNQ 12-26", "time": "2026-11-09T16:00:00Z",
             "action": "Sell", "order_name": names[1], "quantity": 1, "price": 110.0, "commission": 0.5, "is_exit": True}]}
    return {"error": "x"}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["NT_REGISTRY_DB"] = os.path.join(self.tmp.name, "r.sqlite")
        os.environ["NT_JOURNAL_DB"] = os.path.join(self.tmp.name, "j.sqlite")
        os.environ["NT_RULES_FILE"] = os.path.join(self.tmp.name, "none.json")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("NT_REGISTRY_DB", "NT_JOURNAL_DB", "NT_RULES_FILE")])

    def activate(self):
        con = registry.connect()
        registry.submit(con, SPEC)
        registry.set_status(con, SPEC["id"], SPEC["version"], "backtest")
        registry.set_status(con, SPEC["id"], SPEC["version"], "sim")
        con.execute("UPDATE status_history SET at='2026-11-08T00:00:00Z' WHERE to_status='sim'")
        con.commit()
        con.close()

    def test_empty(self):
        d = dashboard.collect(fetch_fn=fake, now=NOW)
        self.assertEqual(d["summary"]["total"], 0)
        self.assertTrue(d["nt"]["reachable"])

    def test_active_strategy_card(self):
        self.activate()
        d = dashboard.collect(fetch_fn=fake, now=NOW)
        s = d["strategies"][0]
        self.assertEqual(s["status"], "sim")
        self.assertEqual(s["running_seconds"], int((NOW - datetime(2026, 11, 8, tzinfo=timezone.utc)).total_seconds()))
        self.assertEqual(s["performance"]["trades"], 1)
        self.assertAlmostEqual(s["performance"]["net_pnl"], 19.0)  # 10 pts * $2 - $1 commission
        self.assertTrue(s["nt_running"])
        self.assertEqual(s["verdict"]["verdict"], "keep_testing")
        self.assertIsNone(s["approval"])
        self.assertEqual(d["summary"]["active"], 1)
        self.assertEqual(d["journal_synced_new_fills"], 2)
        json.dumps(d, default=str)

    def test_approval_shown(self):
        self.activate()
        con = registry.connect()
        registry.record_approval(con, SPEC["id"], SPEC["version"], True, "Sim only", "bundle")
        con.close()
        s = dashboard.collect(fetch_fn=fake, now=NOW)["strategies"][0]
        self.assertTrue(s["approval"]["approved"])

    def test_supervisor_panel_absent_then_present(self):
        os.environ["NT_SUPERVISOR_DB"] = os.path.join(self.tmp.name, "s.sqlite")
        self.addCleanup(lambda: os.environ.pop("NT_SUPERVISOR_DB", None))
        d = dashboard.collect(fetch_fn=fake, now=NOW)
        self.assertFalse(d["supervisor"]["available"])
        self.assertFalse(os.path.exists(os.environ["NT_SUPERVISOR_DB"]))  # dashboard never creates it
        import sup_incidents
        con = sup_incidents.connect()
        sup_incidents.set_state(con, "heartbeat", NOW.isoformat())
        sup_incidents.set_state(con, "perf_cache", {"at": NOW.isoformat(), "findings": [{}], "skipped": None,
                                                    "notes": ["x: not judged"]})
        sup_incidents.apply_findings(con, [{"key": "trade_pace", "severity": 1, "account": "Sim101",
                                            "strategy": "s", "trigger": "slow", "evidence": {}}], NOW)
        con.close()
        sp = dashboard.collect(fetch_fn=fake, now=NOW)["supervisor"]
        self.assertTrue(sp["available"])
        self.assertFalse(sp["heartbeat_stale"])
        self.assertEqual(sp["incidents"][0]["key"], "trade_pace")
        self.assertEqual(sp["perf"]["notes"], ["x: not judged"])
        self.assertEqual(sp["recent_events"][0]["kind"], "opened")
        json.dumps(sp, default=str)

    def test_nt_down_still_works(self):
        self.activate()
        d = dashboard.collect(fetch_fn=lambda p: {"error": "down"}, now=NOW)
        self.assertFalse(d["nt"]["reachable"])
        self.assertEqual(len(d["strategies"]), 1)


if __name__ == "__main__":
    unittest.main()
