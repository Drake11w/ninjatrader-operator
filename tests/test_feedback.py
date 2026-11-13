"""Feedback export: schema validity, golden bundles, idempotency, corrections, privacy, git guards, no network."""
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))
import feedback  # noqa: E402
import feedback_fixture as fx  # noqa: E402
import readiness  # noqa: E402

with open(os.path.join(ROOT, "feedback.schema.json"), encoding="utf-8") as _f:
    SCHEMA = json.load(_f)

TYPES = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}


def check(value, schema, path="$"):
    """Tiny JSON-Schema-subset checker (type, enum, const, pattern, required, properties, items). Returns errors."""
    errs = []
    types = schema.get("type")
    if types:
        types = [types] if isinstance(types, str) else types
        ok = False
        for t in types:
            if t == "number":
                ok = ok or (isinstance(value, (int, float)) and not isinstance(value, bool))
            elif t == "integer":
                ok = ok or (isinstance(value, int) and not isinstance(value, bool))
            else:
                ok = ok or isinstance(value, TYPES[t])
        if not ok:
            return [f"{path}: expected {types}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errs.append(f"{path}: expected {schema['const']!r}")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        errs.append(f"{path}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path}: missing {k!r}")
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                errs += check(value[k], sub, f"{path}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errs += check(v, schema["items"], f"{path}[{i}]")
    return errs


def check_csv(text, spec, name):
    rows = list(csv.reader(io.StringIO(text)))
    errs = []
    if not rows or rows[0] != spec["columns"]:
        return [f"{name}: header {rows[0] if rows else None} != {spec['columns']}"]
    for n, r in enumerate(rows[1:], 2):
        d = dict(zip(spec["columns"], r))
        for c in spec["columns"]:
            v = d[c]
            if v == "":
                if c in spec["numeric"] and c not in spec["nullable"]:
                    errs.append(f"{name}:{n} {c} empty")
                continue
            if c in spec["numeric"]:
                try:
                    float(v)
                except ValueError:
                    errs.append(f"{name}:{n} {c}={v!r} not numeric")
            if c in spec["enums"] and v not in spec["enums"][c]:
                errs.append(f"{name}:{n} {c}={v!r} not in {spec['enums'][c]}")
            if c in spec["patterns"] and not re.search(spec["patterns"][c], v):
                errs.append(f"{name}:{n} {c}={v!r} bad format")
    return errs


def validate_dir(path):
    errs = []
    for name, schema in SCHEMA["files"].items():
        with open(os.path.join(path, name), encoding="utf-8") as f:
            errs += check(json.load(f), schema, name)
    for name, spec in SCHEMA["csv"].items():
        with open(os.path.join(path, name), encoding="utf-8", newline="") as f:
            errs += check_csv(f.read(), spec, name)
    return errs


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "out")
        self.aliases = os.path.join(self.tmp.name, "aliases.json")
        self.handoff = fx.make_handoff(self.tmp.name)
        self.audit = os.path.join(self.tmp.name, "audit.jsonl")
        p = mock.patch.dict(os.environ, {"NT_AUDIT_FILE": self.audit})
        p.start()
        self.addCleanup(p.stop)

    def run_export(self, sc, **kw):
        rcon, jcon = fx.build(sc)
        try:
            args = dict(out_dir=self.out, alias_file=self.aliases, handoff_dir=self.handoff, as_of=fx.AS_OF,
                        operator_commit=None)
            args.update(kw)
            return feedback.export(rcon, jcon, "strat-a", "1.0.0", **args)
        finally:
            rcon.close()
            jcon.close()


class SchemaAndGoldens(Env):
    def test_schema_version_matches_code(self):
        self.assertEqual(SCHEMA["schema_version"], feedback.SCHEMA_VERSION)
        self.assertEqual(SCHEMA["csv"]["trades.csv"]["columns"], feedback.TRADE_COLUMNS)
        self.assertEqual(SCHEMA["csv"]["daily_pnl.csv"]["columns"], feedback.DAILY_COLUMNS)

    def test_goldens_validate_and_match_exporter_output(self):
        for sc in fx.SCENARIOS:
            with self.subTest(scenario=sc):
                res = self.run_export(sc, out_dir=os.path.join(self.out, sc))
                self.assertEqual(res["status"], "written", res)
                self.assertEqual(validate_dir(res["path"]), [])
                gold = os.path.join(fx.GOLDEN_DIR, sc)
                self.assertEqual(sorted(os.listdir(res["path"])), sorted(os.listdir(gold)))
                for name in os.listdir(gold):
                    with open(os.path.join(gold, name), "rb") as a, open(os.path.join(res["path"], name), "rb") as b:
                        self.assertEqual(a.read(), b.read(), f"{sc}/{name} differs from golden; "
                                         f"run tests/fixtures/feedback_fixture.py --update-goldens if intended")

    def test_manifest_hashes_match_files(self):
        res = self.run_export("ready")
        with open(os.path.join(res["path"], "MANIFEST.json"), encoding="utf-8") as f:
            m = json.load(f)
        for name, info in m["files"].items():
            with open(os.path.join(res["path"], name), "rb") as f:
                data = f.read()
            self.assertEqual(hashlib.sha256(data).hexdigest(), info["sha256"])
            self.assertEqual(len(data), info["bytes"])

    def test_scenarios_have_expected_readiness(self):
        want = {"ready": "ready", "not_ready": "not_ready", "nullable_fields": "not_ready", "empty_period": "not_ready"}
        for sc, state in want.items():
            res = self.run_export(sc, out_dir=os.path.join(self.out, sc))
            with open(os.path.join(res["path"], "verdict.json"), encoding="utf-8") as f:
                r = json.load(f)["readiness"]
            self.assertEqual(r["state"], state, sc)
        with open(os.path.join(self.out, "not_ready", "S900", "1.0.0", "latest.json"), encoding="utf-8") as f:
            snap = json.load(f)["snapshot"]
        with open(os.path.join(self.out, "not_ready", "S900", "1.0.0", snap, "verdict.json"), encoding="utf-8") as f:
            failing = [c["key"] for c in json.load(f)["readiness"]["criteria"] if c["status"] != "pass"]
        self.assertEqual(failing, ["untagged_fills"])

    def test_nullable_columns_and_exit_reasons(self):
        res = self.run_export("nullable_fields")
        with open(os.path.join(res["path"], "trades.csv"), encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertTrue(all(r["slippage_ticks"] == "" and r["mae_points"] == "" and r["regime"] == "" for r in rows))
        self.assertEqual([r["exit_reason"] for r in rows[:7]],
                         ["target", "stop", "unknown", "target", "other", "target", "strategy_exit"])
        self.assertTrue(all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["trading_date_ct"]) for r in rows))

    def test_empty_period_has_headers_only(self):
        res = self.run_export("empty_period")
        with open(os.path.join(res["path"], "trades.csv"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), ",".join(feedback.TRADE_COLUMNS))


class Idempotency(Env):
    def test_rerun_is_a_no_op(self):
        first = self.run_export("ready")
        again = self.run_export("ready", as_of=fx.AS_OF + timedelta(hours=1))
        self.assertEqual((first["status"], again["status"]), ("written", "unchanged"))
        self.assertEqual(first["path"], again["path"])
        self.assertEqual(len(os.listdir(os.path.join(self.out, "S900", "1.0.0"))), 2)  # one snapshot + latest.json

    def test_changed_data_writes_a_new_snapshot_and_moves_latest(self):
        self.run_export("ready")
        res = self.run_export("not_ready", as_of=fx.AS_OF + timedelta(days=1))
        self.assertEqual(res["status"], "written")
        self.assertIsNone(res["supersedes"])
        with open(os.path.join(self.out, "S900", "1.0.0", "latest.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["as_of"], "2026-10-31T12:00:00Z")
        self.assertEqual(len([d for d in os.listdir(os.path.join(self.out, "S900", "1.0.0")) if d != "latest.json"]), 2)

    def test_correction_names_the_snapshot_it_supersedes(self):
        self.run_export("ready")
        res = self.run_export("not_ready", as_of=fx.AS_OF + timedelta(hours=2), correction=True)
        with open(os.path.join(res["path"], "MANIFEST.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["supersedes"], "2026-10-30T12:00:00Z")

    def test_old_snapshots_are_never_edited(self):
        first = self.run_export("ready")
        with open(os.path.join(first["path"], "MANIFEST.json"), "rb") as f:
            before = f.read()
        self.run_export("not_ready", as_of=fx.AS_OF + timedelta(days=1), correction=True)
        with open(os.path.join(first["path"], "MANIFEST.json"), "rb") as f:
            self.assertEqual(f.read(), before)

    def test_same_as_of_with_different_content_is_refused(self):
        self.run_export("ready")
        res = self.run_export("not_ready")
        self.assertFalse(res["ok"])
        self.assertIn("immutable", res["error"])


class Privacy(Env):
    RAW = "APEX-123456-07"

    def test_raw_account_name_is_nowhere_in_the_bundle_or_audit(self):
        rcon, jcon = fx.build("ready")
        jcon.execute("UPDATE executions SET account=?", (self.RAW,))
        jcon.commit()
        with open(self.aliases, "w", encoding="utf-8") as f:
            json.dump({self.RAW: "acct-9"}, f)
        res = feedback.export(rcon, jcon, "strat-a", "1.0.0", account=self.RAW, out_dir=self.out,
                              alias_file=self.aliases, handoff_dir=self.handoff, as_of=fx.AS_OF, operator_commit=None)
        rcon.close()
        jcon.close()
        self.assertEqual(res["status"], "written", res)
        raw = self.RAW.encode()
        for dirpath, _, names in os.walk(self.out):
            for n in names:
                with open(os.path.join(dirpath, n), "rb") as f:
                    self.assertNotIn(raw, f.read(), n)
        with open(os.path.join(res["path"], "trades.csv"), encoding="utf-8") as f:
            self.assertIn("acct-9", f.read())
        with open(self.audit, "rb") as f:
            data = f.read()
        self.assertTrue(data.strip())
        self.assertNotIn(raw, data)
        self.assertEqual(json.loads(data.splitlines()[-1])["params"]["account"], "acct-9")

    def test_new_accounts_get_stable_aliases(self):
        a = feedback.resolve_aliases(["Sim101", "Zed"], self.aliases)
        b = feedback.resolve_aliases(["Sim101", "Another"], self.aliases)
        self.assertEqual(a["Sim101"], b["Sim101"])
        self.assertEqual(sorted(set(b.values())), ["acct-1", "acct-2", "acct-3"])


class ReadinessUnit(unittest.TestCase):
    def v(self, **kw):
        base = {"verdict": "promote", "edge_decayed": False, "reasons": [], "sample": {"trades": 36, "trading_days": 36},
                "expected": {"trades_per_year": {"value": 252}}, "checks": [
                    {"check": "edge_decay (all Sim trades)", "status": "pass"},
                    {"check": "expectancy_vs_claim", "status": "pass", "actual": 0.8, "threshold": ">=0.4"}]}
        base.update(kw)
        return base

    def ev(self, v=None, **kw):
        args = dict(status="sim", since="2026-08-01T00:00:00Z", now="2026-10-01T00:00:00Z",
                    backtest={"present": True, "claims": "matches"}, untagged_fills=0)
        args.update(kw)
        return readiness.evaluate(v or self.v(), **args)

    def test_ready(self):
        self.assertTrue(self.ev()["ready"])

    def test_missing_baseline(self):
        r = self.ev(backtest=None)
        self.assertFalse(r["ready"])
        self.assertIn("missing baseline", [c for c in r["criteria"] if c["key"] == "baseline"][0]["note"])

    def test_floors_report_progress(self):
        r = self.ev(self.v(sample={"trades": 18, "trading_days": 18}, verdict="keep_testing"))
        self.assertIn("sample_trades 18/30", r["summary"])

    def test_frequency_outside_band_fails(self):
        r = self.ev(self.v(expected={"trades_per_year": {"value": 60}}))
        self.assertEqual([c for c in r["criteria"] if c["key"] == "trade_frequency"][0]["status"], "fail")

    def freq(self, r):
        return [c for c in r["criteria"] if c["key"] == "trade_frequency"][0]

    def test_frequency_counts_sessions_without_a_trade(self):
        # A strategy that trades about every second session (claim 119/year): 20 trades on 20 days in 43 sessions.
        # Dividing by the 20 traded days read 252/year, 2.1x the claim, so it could never become ready.
        v = self.v(sample={"trades": 20, "trading_days": 20}, expected={"trades_per_year": {"value": 119.2}})
        c = self.freq(self.ev(v))
        self.assertEqual((c["status"], c["actual"]), ("pass", 0.98))

    def test_frequency_pending_until_enough_sessions(self):
        c = self.freq(self.ev(since="2026-09-21T12:00:00Z"))            # Sep 21-29: `now` is still Sep 30 in Chicago
        self.assertEqual((c["status"], c["progress"]), ("pending", "7/20 sessions"))

    def test_reject_is_a_suggestion_only(self):
        r = self.ev(self.v(verdict="reject", reasons=["drawdown"]))
        self.assertEqual(r["state"], "rejected")
        self.assertIn("suggestion only", r["suggested_action"])

    def test_overrides_only_stricter(self):
        t, notes = readiness.thresholds({"min_trades": 50, "min_days": 5, "freq_high": 2.0, "freq_low": 0.8})
        self.assertEqual((t["min_trades"], t["min_days"], t["freq_high"], t["freq_low"]), (50, 20, 1.5, 0.8))
        self.assertEqual(len(notes), 2)

    def test_untagged_fill_blocks(self):
        self.assertFalse(self.ev(untagged_fills=1)["ready"])


class GitGuards(unittest.TestCase):
    def test_handoff_and_feedback_are_ignored_and_untracked(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            lines = [x.strip() for x in f]
        self.assertIn("/handoff/", lines)
        self.assertIn("/feedback/", lines)
        try:
            out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git not available")
        if out.returncode != 0:
            self.skipTest("not a git checkout")
        bad = [p for p in out.stdout.splitlines() if p.startswith(("handoff/", "feedback/"))]
        self.assertEqual(bad, [], "handoff/ and feedback/ must never be committed to this repo")


class NoNetwork(Env):
    def test_export_never_calls_urlopen(self):
        import urllib.request
        with mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network call")) as m:
            res = self.run_export("ready")
        self.assertEqual(res["status"], "written")
        m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
