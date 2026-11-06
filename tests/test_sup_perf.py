import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import journal
import registry
import sup_daemon
import sup_incidents
import sup_perf

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
PROFILE = {"strategy_id": "s742", "version": "1.0.0", "account": "A", "monitored": True}


def verdict_result(**kw):
    base = {"verdict": "keep_testing", "edge_decayed": False, "reasons": [], "warnings": [],
            "sample": {"trades": 5}, "expected": {"trades_per_year": {"value": 252.0, "source": "backtest"}},
            "since": "2026-11-02T00:00:00Z", "trades": [], "next_step": "x"}
    base.update(kw)
    return base


class CollectTests(unittest.TestCase):
    def run_collect(self, result, execs_ok=True, status="live-small", ref=None):
        obs = {"ok": execs_ok, "data": {"executions": []}, "error": None if execs_ok else "down"}
        with mock.patch("sup_perf.sup_fetch.fetch", return_value=obs), \
             mock.patch("sup_perf.registry.get_spec", return_value={"status": status}), \
             mock.patch("sup_perf.registry.order_name_map", return_value={}), \
             mock.patch("sup_perf.verdict.evaluate_registered", return_value=result), \
             mock.patch("sup_perf._ref_durations", return_value=ref or []):
            return sup_perf.collect([PROFILE], NOW, "", journal_con_rcon[0], journal_con_rcon[1])

    def setUp(self):
        global journal_con_rcon
        journal_con_rcon = (registry.connect(":memory:"), journal.connect(":memory:"))

    def keys(self, r):
        return sorted(f["key"] for f in r["findings"])

    def test_executions_unavailable_means_skipped_not_clear(self):
        r = self.run_collect(verdict_result(), execs_ok=False)
        self.assertIsNone(r["findings"])

    def test_healthy_gives_no_findings(self):
        trades = [{"duration_sec": 900}] * 14
        r = self.run_collect(verdict_result(trades=trades))
        self.assertEqual(r["findings"], [])

    def test_decay_is_warning_level_with_recommendation_only(self):
        r = self.run_collect(verdict_result(edge_decayed=True, verdict="reject", reasons=["edge decay"]))
        f = r["findings"][0]
        self.assertEqual((f["key"], f["severity"]), ("performance_drift", 1))
        self.assertIn("recommendation_only", f["evidence"])

    def test_pace_too_low_flagged(self):
        r = self.run_collect(verdict_result(trades=[]))  # 252/yr = 1/day, 14 trading days expected, saw 0
        self.assertIn("trade_pace", self.keys(r))

    def test_unpairable_fills_suppress_other_checks(self):
        r = self.run_collect(verdict_result(warnings=["skipped execution abc: unknown action ''"]))
        self.assertEqual(self.keys(r), ["fills_unpairable"])

    def test_inactive_status_not_judged(self):
        r = self.run_collect(verdict_result(), status="backtest")
        self.assertEqual(r["findings"], [])
        self.assertTrue(r["notes"])

    def test_holding_shift_flagged(self):
        trades = [{"duration_sec": 60}] * 20
        r = self.run_collect(verdict_result(trades=trades, expected={}), ref=list(range(600, 1800, 20)))
        self.assertEqual(self.keys(r), ["holding_time_shift"])


class CacheTests(unittest.TestCase):
    def test_cache_interval_and_keep_previous_on_skip(self):
        con = sup_incidents.connect(":memory:")
        f = [{"key": "trade_pace", "severity": 1, "account": "A", "strategy": "s", "trigger": "t", "evidence": {},
              "caused_by": ""}]
        calls = []

        def collect(p, n):
            calls.append(n)
            return {"findings": f} if len(calls) == 1 else {"findings": None, "skipped": "down"}

        self.assertEqual(sup_daemon.perf_findings(con, [], NOW, collect), f)
        self.assertEqual(sup_daemon.perf_findings(con, [], NOW + timedelta(seconds=100), collect), f)
        self.assertEqual(len(calls), 1)  # cached
        self.assertEqual(sup_daemon.perf_findings(con, [], NOW + timedelta(seconds=400), collect), f)
        self.assertEqual(len(calls), 2)  # refreshed, skipped, previous kept

    def test_exception_keeps_previous(self):
        con = sup_incidents.connect(":memory:")

        def boom(p, n):
            raise RuntimeError("x")

        self.assertEqual(sup_daemon.perf_findings(con, [], NOW, boom), [])
        self.assertEqual(sup_incidents.get_state(con, "perf_cache")["skipped"], "error: x")


if __name__ == "__main__":
    unittest.main()
