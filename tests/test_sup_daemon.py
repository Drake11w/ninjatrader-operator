import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from unittest import mock

import nt_local
import sup_alerts
import sup_checks
import sup_daemon
import sup_fetch
import sup_incidents
import sup_profile

NAME = "s742 1.0.0 orb"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class FakeAddon(BaseHTTPRequestHandler):
    health = None
    strategies = None
    posts = []

    def do_GET(self):
        body = {"/health": FakeAddon.health, "/strategies": FakeAddon.strategies}.get(self.path)
        if body is None:
            self.send_response(503)
            self.end_headers()
            return
        data = json.dumps({**body, "timestamp": now_iso()}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        FakeAddon.posts.append(self.path)
        self.send_response(500)
        self.end_headers()

    def log_message(self, *a):
        pass


class DaemonTests(unittest.TestCase):
    def setUp(self):
        FakeAddon.posts = []
        FakeAddon.health = {"connections": [{"account": "Sim101", "status": "Connected",
                                             "price_status": "Connected"}]}
        FakeAddon.strategies = {"strategies": [{"account": "Sim101", "name": NAME, "state": "Realtime"}]}
        self.srv = HTTPServer(("127.0.0.1", 0), FakeAddon)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        self.tmp = tempfile.mkdtemp()
        os.environ["NT_SUPERVISOR_ALERT_LOG"] = os.path.join(self.tmp, "alerts.log")
        os.environ["NT_SUPERVISOR_ATTENTION"] = os.path.join(self.tmp, "attention.json")
        os.environ.pop("NT_SUPERVISOR_TOAST", None)
        # These tests run on the wall clock and are about the daemon's mechanics, not the trading calendar or this
        # PC's processes: pin both so they behave the same on a weekend and with NinjaTrader open or closed.
        mock.patch.object(sup_checks.sessions, "weekend_closed", lambda now: False).start()
        mock.patch.object(nt_local, "process_state", lambda: {"nt_running": True, "login_window": True}).start()
        self.con = sup_incidents.connect(":memory:")
        self.profiles = [sup_profile.normalize({
            "strategy_id": "s742", "version": "1.0.0", "instance_names": [NAME], "account": "Sim101",
            "authorized_accounts": ["Sim101"], "grace_seconds": 0})]

    def tearDown(self):
        mock.patch.stopall()
        self.srv.shutdown()
        self.srv.server_close()
        for k in ("NT_SUPERVISOR_ALERT_LOG", "NT_SUPERVISOR_ATTENTION"):
            os.environ.pop(k, None)

    def cycle(self, now=None, audit=None):
        return sup_daemon.run_once(self.con, self.profiles, sup_fetch.observe(self.base), now, audit)

    def test_healthy_cycle_no_incidents_and_never_posts(self):
        self.assertEqual(self.cycle(), [])
        self.assertEqual(FakeAddon.posts, [])
        flag = json.load(open(sup_alerts.flag_path()))
        self.assertEqual(flag["count"], 0)

    def test_unexpected_stop_creates_incident_alert_and_flag(self):
        self.cycle()
        FakeAddon.strategies = {"strategies": [{"account": "Sim101", "name": NAME, "state": "Terminated"}]}
        ev = self.cycle()
        self.assertEqual([e["key"] for e in ev], ["strategy_unexpected_stop"])
        self.assertIn("strategy_unexpected_stop", open(sup_alerts.log_path()).read())
        flag = json.load(open(sup_alerts.flag_path()))
        self.assertEqual(flag["count"], 1)
        row = sup_incidents.list_incidents(self.con)[0]
        self.assertIsNotNone(row["notified_at"])

    def test_operator_disable_in_audit_is_not_an_incident(self):
        self.cycle()
        FakeAddon.strategies = {"strategies": [{"account": "Sim101", "name": NAME, "state": "Terminated"}]}
        audit = [{"action": "disable_strategy", "ok": True, "time": now_iso(), "params": {"name": NAME}}]
        self.assertEqual(self.cycle(audit=audit), [])

    def test_addon_down_is_reported_and_never_resolves_on_stale(self):
        FakeAddon.health = None
        FakeAddon.strategies = None
        for _ in range(3):
            ev = self.cycle()
        self.assertEqual([e["key"] for e in ev], ["addon_unreachable"])
        self.assertIn("login window", ev[0]["trigger"])
        self.assertEqual(ev[0]["evidence"]["cause"], "login_window")
        FakeAddon.health = {"connections": [{"account": "Sim101", "status": "Connected",
                                             "price_status": "Connected"}]}
        FakeAddon.strategies = {"strategies": [{"account": "Sim101", "name": NAME, "state": "Realtime"}]}
        for _ in range(3):
            self.cycle()
        self.assertEqual(sup_incidents.list_incidents(self.con, "active"), [])

    def test_restart_discards_state_and_logs(self):
        self.cycle()
        sup_daemon.start(self.con)
        self.assertEqual(sup_incidents.get_state(self.con, "check_state"), {})
        self.assertIn("supervisor_restarted", open(sup_alerts.log_path()).read())

    def test_status_reports_stale_heartbeat(self):
        self.assertTrue(sup_daemon.status(self.con)["heartbeat_stale"])
        self.cycle()
        self.assertFalse(sup_daemon.status(self.con)["heartbeat_stale"])
        later = datetime.now(timezone.utc) + timedelta(seconds=120)
        self.assertTrue(sup_daemon.status(self.con, later)["heartbeat_stale"])

    def test_fetch_failure_is_data_not_exception(self):
        o = sup_fetch.fetch("/health", "http://127.0.0.1:9", timeout=1)
        self.assertFalse(o["ok"])
        self.assertTrue(o["error"])

    def test_fetch_module_has_no_post(self):
        src = open(sup_fetch.__file__, encoding="utf-8").read()
        self.assertNotIn('method="POST"', src)
        self.assertNotIn("method='POST'", src)


if __name__ == "__main__":
    unittest.main()
