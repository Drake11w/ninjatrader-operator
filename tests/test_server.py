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
    }


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        data = payloads().get(path)
        self.send_response(200 if data else 404)
        self.end_headers()
        self.wfile.write(json.dumps(data or {"error": "nf"}).encode())

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

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        state["ts"] = NOW

    def test_accounts_and_positions(self):
        self.assertEqual(json.loads(self.s.get_accounts())["accounts"][0]["cash_value"], 50000)
        self.assertEqual(len(json.loads(self.s.get_positions("Sim101"))["accounts"]), 1)
        self.assertEqual(json.loads(self.s.get_positions("Nope"))["accounts"], [])

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
