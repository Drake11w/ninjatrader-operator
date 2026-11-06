"""Tests the MCP tool functions against a fake add-on (no NinjaTrader needed)."""
import json
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NOW = lambda: datetime.now(timezone.utc).isoformat()
OLD = lambda: (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
state = {"ts": NOW}
posts = []


def payloads():
    ts = state["ts"]()
    return {
        "/status": {"timestamp": ts, "accounts": [
            {"name": "Sim101", "connection": "Sim", "cash_value": 50000, "realized_pnl": 12.5,
             "unrealized_pnl": 0, "positions": [{"instrument": "MNQ 12-26", "direction": "Long", "quantity": 1, "avg_price": 100}]}]},
        "/orders": {"timestamp": ts, "orders": [
            {"account": "Sim101", "name": "Stop loss", "state": "Accepted"},
            {"account": "Other", "name": "x", "state": "Working"}]},
        "/executions": {"timestamp": ts, "executions": [
            {"account": "Sim101", "execution_id": "e1", "time": "2026-11-05T14:30:00Z", "order_name": "ORB long", "is_entry": True, "price": 1},
            {"account": "Sim101", "execution_id": "e2", "time": "2026-11-05T15:00:00Z", "order_name": "ORB time exit", "is_exit": True, "price": 2}]},
        "/strategies": {"timestamp": ts, "strategies": [{"account": "Sim101", "name": "ORB", "state": "Realtime"}]},
        "/health": {"ok": True, "timestamp": ts, "uptime_seconds": 5, "connections": [
            {"account": "Sim101", "status": "Connected", "price_status": "Disconnected"}]},
        "/recovery": {"enabled": False, "last_event": "not started", "pending": []},
    }


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        data = payloads().get(path)
        self.send_response(200 if data else 404)
        self.end_headers()
        self.wfile.write(json.dumps(data or {"error": "nf"}).encode())

    def do_POST(self):
        posts.append((self.path, self.headers.get("X-NT-Token")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "action": self.path}).encode())

    def log_message(self, *a):
        pass


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        os.environ["NT_ADDON_URL"] = f"http://127.0.0.1:{cls.httpd.server_port}"
        import server
        cls.s = server
        server.BASE = os.environ["NT_ADDON_URL"]  # server.py reads the URL at import; another test may have imported it first

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        state["ts"] = NOW

    def test_accounts_and_positions(self):
        self.assertEqual(json.loads(self.s.get_accounts())["accounts"][0]["cash_value"], 50000)
        self.assertEqual(len(json.loads(self.s.get_positions("Sim101"))["accounts"]), 1)
        self.assertEqual(json.loads(self.s.get_positions("Nope"))["accounts"], [])

    def test_controls_guarded_and_audited(self):
        import tempfile
        os.environ["NT_AUDIT_FILE"] = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
        posts.clear()
        out = json.loads(self.s.flatten("Sim101", "MNQ 12-26", "test"))
        self.assertTrue(out["ok"])
        self.assertEqual(posts[0][0].split("?")[0], "/flatten")
        self.assertIn("instrument=MNQ", posts[0][0])
        # live account refused, nothing sent
        n = len(posts)
        bad = json.loads(self.s.flatten("LFE05079396550004"))
        self.assertFalse(bad["ok"])
        self.assertIn("locked", bad["error"])
        self.assertFalse(json.loads(self.s.flatten(""))["ok"])
        self.assertFalse(json.loads(self.s.arm_kill_switch("Sim101", 0))["ok"])
        self.assertFalse(json.loads(self.s.arm_kill_switch("Sim101", 99999))["ok"])
        self.assertEqual(len(posts), n)
        self.assertTrue(json.loads(self.s.arm_kill_switch("Sim101", 500))["ok"])
        self.assertIn("max_loss=500", posts[-1][0])
        self.assertTrue(json.loads(self.s.cancel_all_orders("Sim101"))["ok"])
        self.assertTrue(json.loads(self.s.disarm_kill_switch())["ok"])
        log = json.loads(self.s.get_audit_log())
        self.assertEqual([r["action"] for r in log],
                         ["flatten", "flatten", "flatten", "arm_kill_switch", "arm_kill_switch", "arm_kill_switch",
                          "cancel_all", "disarm_kill_switch"])
        self.assertEqual([r["ok"] for r in log][:3], [True, False, False])

    def test_allowlist(self):
        import tempfile
        os.environ["NT_AUDIT_FILE"] = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
        os.environ["NT_OPERATOR_ALLOWED_ACCOUNTS"] = "LFE1, LFE2"
        try:
            posts.clear()
            self.assertTrue(json.loads(self.s.flatten("LFE2"))["ok"])
            self.assertFalse(json.loads(self.s.flatten("LFE"))["ok"])
            self.assertFalse(json.loads(self.s.flatten("LFE3"))["ok"])
            self.assertEqual(len(posts), 1)
        finally:
            del os.environ["NT_OPERATOR_ALLOWED_ACCOUNTS"]

    def test_live_unlock_and_token(self):
        import tempfile
        os.environ["NT_AUDIT_FILE"] = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
        os.environ["NT_OPERATOR_ALLOW_LIVE"] = "1"
        os.environ["NT_OPERATOR_TOKEN"] = "secret"
        try:
            posts.clear()
            self.assertTrue(json.loads(self.s.flatten("LFE05079396550004"))["ok"])
            self.assertEqual(posts[0][1], "secret")
        finally:
            del os.environ["NT_OPERATOR_ALLOW_LIVE"], os.environ["NT_OPERATOR_TOKEN"]

    def test_live_small_gate_requires_exact_status_unlock_and_allowlist(self):
        check = self.s._check_live_small_deployment
        for key in ("NT_OPERATOR_ALLOW_LIVE_SMALL", "NT_OPERATOR_LIVE_SMALL_ACCOUNTS", "NT_OPERATOR_ALLOW_STRATEGY_START", "NT_OPERATOR_TOKEN"):
            os.environ.pop(key, None)
        os.environ["NT_OPERATOR_ALLOW_LIVE"] = "1"  # broad legacy unlock must not bypass this gate
        try:
            self.assertIsNotNone(check("LiveAcct", "live-small"))
            os.environ["NT_OPERATOR_ALLOW_LIVE_SMALL"] = "1"
            os.environ["NT_OPERATOR_LIVE_SMALL_ACCOUNTS"] = "LiveAcct, OtherAcct"
            os.environ["NT_OPERATOR_ALLOW_STRATEGY_START"] = "1"
            self.assertIsNotNone(check("LiveAcct", "live-small"))  # shared token is also mandatory
            os.environ["NT_OPERATOR_TOKEN"] = "test-secret"
            self.assertIsNone(check("LiveAcct", "live-small"))
            self.assertIsNotNone(check("LiveAcct", "sim"))
            self.assertIsNotNone(check("Sim101", "live-small"))
            self.assertIsNotNone(check("LiveAcct ", "live-small"))
            del os.environ["NT_OPERATOR_ALLOW_STRATEGY_START"]
            self.assertIsNotNone(check("LiveAcct", "live-small"))
        finally:
            for key in ("NT_OPERATOR_ALLOW_LIVE", "NT_OPERATOR_ALLOW_LIVE_SMALL",
                        "NT_OPERATOR_LIVE_SMALL_ACCOUNTS", "NT_OPERATOR_ALLOW_STRATEGY_START", "NT_OPERATOR_TOKEN"):
                os.environ.pop(key, None)

    def test_orders_filter(self):
        self.assertEqual(len(json.loads(self.s.get_orders())["orders"]), 2)
        self.assertEqual(len(json.loads(self.s.get_orders("Sim101"))["orders"]), 1)

    def test_executions_limit(self):
        rows = json.loads(self.s.get_executions(limit=1))["executions"]
        self.assertEqual([r["order_name"] for r in rows], ["ORB time exit"])

    def test_sync_and_read_journal(self):
        import tempfile
        os.environ["NT_JOURNAL_DB"] = os.path.join(tempfile.mkdtemp(), "j.sqlite")
        os.environ["NT_REGISTRY_DB"] = os.path.join(tempfile.mkdtemp(), "r.sqlite")
        first = json.loads(self.s.sync_journal("ORB", "v1"))
        self.assertEqual((first["new"], first["fetched"]), (2, 2))
        self.assertEqual(json.loads(self.s.sync_journal())["new"], 0)
        rows = json.loads(self.s.get_journal(strategy="ORB"))
        self.assertEqual(rows["count"], 2)

    def test_strategies(self):
        self.assertEqual(json.loads(self.s.get_strategies())["strategies"][0]["name"], "ORB")

    def test_health_flags_bad_feed(self):
        self.assertIn("Sim101", json.loads(self.s.get_health())["warning"])

    def test_recovery_status(self):
        status = json.loads(self.s.get_recovery_status())
        self.assertFalse(status["enabled"])
        self.assertEqual(status["pending"], [])

    def test_stale_warning(self):
        state["ts"] = OLD
        self.assertIn("STALE", json.loads(self.s.get_orders())["warning"])
        state["ts"] = NOW
        self.assertNotIn("warning", json.loads(self.s.get_orders()))

    def test_unreachable(self):
        old = self.s.BASE
        self.s.BASE = "http://127.0.0.1:1"
        try:
            self.assertIn("error", json.loads(self.s.get_orders()))
        finally:
            self.s.BASE = old


if __name__ == "__main__":
    unittest.main()
