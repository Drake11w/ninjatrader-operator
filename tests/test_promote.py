"""Promotion: stage order, stop-on-failure with no override, resume, lock, Sim ceiling, report, no network."""
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import promote  # noqa: E402
import registry  # noqa: E402

with open(os.path.join(ROOT, "examples", "orb-mnq.strategy.json"), encoding="utf-8") as f:
    SPEC = json.load(f)
SID, VER = SPEC["id"], SPEC["version"]


class Fakes:
    def __init__(self, rcon):
        self.rcon = rcon
        self.calls = []
        self.bt_ok = True
        self.enable_ok = True
        self.claims = "matches"

    def io(self):
        def inspect(path):
            self.calls.append("inspect")
            return {"ok": True, "spec": SPEC, "legs": [{"class": "C", "sha256": "ab" * 32}], "errors": []}

        def install(path):
            self.calls.append("install")
            return {"ok": registry.submit(self.rcon, copy.deepcopy(SPEC))["ok"]}

        def backtest(spec, path):
            self.calls.append("backtest")
            return {"ok": True, "csv": "a,b\n1,2\n"} if self.bt_ok else {"ok": False, "error": "analyzer window busy"}

        def enable(spec, account):
            self.calls.append("enable:" + account)
            return {"ok": True} if self.enable_ok else {"ok": False, "error": "add-on refused"}
        return {"inspect": inspect, "install": install, "backtest": backtest, "enable": enable}


class PromoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "feedback")
        self.alias = os.path.join(self.tmp.name, "aliases.json")
        self.rcon = registry.connect(":memory:")
        self.addCleanup(self.rcon.close)
        self.fakes = Fakes(self.rcon)
        for p in (mock.patch.dict(os.environ, {"NT_AUDIT_FILE": os.path.join(self.tmp.name, "audit.jsonl")}),
                  mock.patch("backtest.import_run", return_value={"ok": True, "run_id": 7}),
                  mock.patch("promote._latest_backtest",
                             side_effect=lambda *a: {"present": True, "claims": self.fakes.claims, "run_id": 7}),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("network call"))):
            p.start()
            self.addCleanup(p.stop)

    def run_promote(self, account="Sim101"):
        return promote.promote(self.rcon, SID, VER, account, "bundle", self.fakes.io(), self.out, self.alias)

    def status(self):
        return registry.get_spec(self.rcon, SID, VER).get("status")

    def results(self, out):
        return {s["stage"]: s["result"] for s in out["stages"]}

    def test_happy_path_reaches_sim_and_writes_report(self):
        out = self.run_promote()
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.status(), "sim")
        self.assertEqual(self.fakes.calls, ["inspect", "install", "backtest", "enable:Sim101"])
        self.assertEqual(set(self.results(out).values()), {"pass"})
        with open(out["report"], encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(json.loads(text)["attempts"], 1)
        self.assertNotIn("Sim101", text)

    def test_non_sim_account_is_refused(self):
        out = self.run_promote("LiveAcct")
        self.assertFalse(out["ok"])
        self.assertEqual(self.fakes.calls, [])
        self.assertEqual(self.status(), None)

    def test_failed_backtest_stops_with_no_override(self):
        self.fakes.bt_ok = False
        out = self.run_promote()
        self.assertEqual((out["ok"], out["failed_stage"], self.status()), (False, "backtest", "candidate"))
        self.assertEqual(self.results(out)["spec_check"], "pending")
        self.assertEqual(self.results(out)["sim"], "pending")
        self.assertFalse([c for c in self.fakes.calls if c.startswith("enable")])
        again = self.run_promote()          # nothing to bypass the failure with
        self.assertEqual((again["ok"], again["failed_stage"]), (False, "backtest"))
        self.assertEqual(self.status(), "candidate")

    def test_failed_spec_check_never_starts_sim(self):
        self.fakes.claims = "differs"
        out = self.run_promote()
        self.assertEqual((out["failed_stage"], self.status()), ("spec_check", "backtest"))
        self.assertFalse([c for c in self.fakes.calls if c.startswith("enable")])

    def test_resume_skips_completed_stages(self):
        self.fakes.enable_ok = False
        out = self.run_promote()
        self.assertEqual((out["failed_stage"], self.status()), ("sim", "backtest"))
        self.fakes.calls.clear()
        self.fakes.enable_ok = True
        out = self.run_promote()
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.fakes.calls, ["enable:Sim101"])
        self.assertEqual(self.status(), "sim")
        self.assertEqual(json.load(open(out["report"], encoding="utf-8"))["attempts"], 2)

    def test_second_run_is_a_noop_once_in_sim(self):
        self.run_promote()
        self.fakes.calls.clear()
        out = self.run_promote()
        self.assertTrue(out["ok"])
        self.assertEqual(self.fakes.calls, [])

    def test_live_small_is_never_touched(self):
        registry.submit(self.rcon, copy.deepcopy(SPEC))
        for s in ("backtest", "sim", "live-small"):
            registry.set_status(self.rcon, SID, VER, s)
        out = self.run_promote()
        self.assertTrue(out["ok"])
        self.assertEqual((self.fakes.calls, self.status()), ([], "live-small"))

    def test_second_invocation_reports_busy(self):
        lock = os.path.join(self.out, "promote.lock")
        self.assertIsNone(promote.acquire_lock(lock))
        out = self.run_promote()
        self.assertEqual((out["ok"], out.get("busy")), (False, True))
        promote.release_lock(lock)
        self.assertTrue(self.run_promote()["ok"])

    def test_lock_released_after_failure(self):
        self.fakes.bt_ok = False
        self.run_promote()
        self.assertFalse(os.path.exists(os.path.join(self.out, "promote.lock")))

    def test_plan_stages_is_pure(self):
        plan = promote.plan_stages("backtest", None, {}, "Sim101")
        self.assertEqual([(p["stage"], p["state"]) for p in plan],
                         [("export", "done"), ("install", "done"), ("backtest", "done"), ("spec_check", "todo"),
                          ("sim", "todo")])
        self.assertEqual(promote.spec_check({"present": False})[0], False)
        self.assertEqual(promote.spec_check({"present": True, "claims": "differs"})[0], False)
        self.assertEqual(promote.spec_check({"present": True, "claims": "matches"})[0], True)


if __name__ == "__main__":
    unittest.main()
