import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import handoff
import registry

with open(os.path.join(ROOT, "examples", "two-leg-mnq.strategy.json"), encoding="utf-8") as f:
    EXAMPLE = json.load(f)


def cs_for(leg):
    ns = leg["ninjascript"]
    props = "\n".join(f"        [NinjaScriptProperty]\n        public int {k} {{ get; set; }}" for k in ns["inputs"])
    names = "\n".join(f'        // "{n}"' for n in ns["order_names"])
    return f"public class {ns['class']} : Strategy\n{{\n{props}\n{names}\n}}\n"


def make_bundle(root, approved=True, tamper=False):
    spec = copy.deepcopy(EXAMPLE)
    d = os.path.join(root, "bundle")
    os.makedirs(os.path.join(d, "strategies"))
    files = {}
    for leg in spec["legs"]:
        rel = f"strategies/{leg['ninjascript']['class']}.cs"
        text = cs_for(leg)
        with open(os.path.join(d, rel), "w", encoding="utf-8", newline="") as f:
            f.write(text)
        files[rel] = {"sha256": hashlib.sha256(text.encode()).hexdigest()}
        if tamper:
            with open(os.path.join(d, rel), "a", encoding="utf-8", newline="") as f:
                f.write("// edited\n")
    with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as f:
        json.dump(spec, f)
    manifest = {"spec_id": spec["id"], "spec_version": spec["version"], "files": files,
                "approval": {"handoff_approved": approved, "limits": "candidate for Sim only"}}
    with open(os.path.join(d, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    return d, spec


class InspectInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["NT_HOME"] = os.path.join(self.tmp.name, "nt")
        os.environ["NT_REGISTRY_DB"] = os.path.join(self.tmp.name, "reg.sqlite")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("NT_HOME", "NT_REGISTRY_DB")])

    def test_good_bundle_dry_run_writes_nothing(self):
        d, _ = make_bundle(self.tmp.name)
        r = handoff.install_bundle(d)
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["applied"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "nt")))

    def test_apply_copies_registers_and_backs_up(self):
        d, spec = make_bundle(self.tmp.name)
        strat = os.path.join(self.tmp.name, "nt", "bin", "Custom", "Strategies")
        os.makedirs(strat)
        old = os.path.join(strat, "PlaceholderLegA.cs")
        with open(old, "w") as f:
            f.write("old version")
        r = handoff.install_bundle(d, apply=True)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["registry"]["ok"])
        self.assertEqual(open(os.path.join(strat, "PlaceholderLegA.cs")).read(), cs_for(spec["legs"][0]))
        backup = [c for c in r["plan"]["copy"] if c.get("backed_up_to")]
        self.assertEqual(len(backup), 1)
        self.assertEqual(open(os.path.join(backup[0]["backed_up_to"], "PlaceholderLegA.cs")).read(), "old version")
        again = handoff.install_bundle(d, apply=True)  # idempotent: already registered, files identical
        self.assertTrue(again["ok"])
        self.assertTrue(all(c["already_identical"] for c in again["plan"]["copy"]))

    def test_refusals(self):
        d, _ = make_bundle(self.tmp.name, approved=False)
        self.assertTrue(any("approved" in e for e in handoff.install_bundle(d)["errors"]))
        self.assertFalse(handoff.install_bundle(d, apply=True)["applied"])

    def test_tampered_cs_is_refused(self):
        d, _ = make_bundle(self.tmp.name, tamper=True)
        r = handoff.install_bundle(d, apply=True)
        self.assertFalse(r["ok"])
        self.assertTrue(any("sha256" in e for e in r["errors"]))

    def test_unknown_input_is_refused(self):
        d, spec = make_bundle(self.tmp.name)
        spec["legs"][0]["ninjascript"]["inputs"]["Bogus"] = 1
        with open(os.path.join(d, "spec.json"), "w") as f:
            json.dump(spec, f)
        r = handoff.inspect_bundle(d)
        self.assertTrue(any("Bogus" in e for e in r["errors"]))


class EnableTests(unittest.TestCase):
    def test_requests_encode_inputs(self):
        reqs = handoff.enable_requests(EXAMPLE, date(2026, 11, 6))
        self.assertEqual(reqs[0]["instrument"], "MNQ 12-26")
        self.assertIn("UseSkipEventDays=true", reqs[0]["body"])
        self.assertIn("StopAtrMultiple=1.0", reqs[0]["body"])

    def fakes(self, fail_on=None, present=None):
        calls, state = [], {"strategies": list(present or [])}

        def fetch(path):
            return {"strategies": state["strategies"]}

        def post(path, params, body):
            calls.append((path, params["name"] if "name" in params else None, dict(params)))
            if path == "/strategy/enable":
                if params["class"] == fail_on:
                    return {"ok": False, "error": "boom"}
                state["strategies"].append({"account": params["account"], "name": params["name"], "state": "Realtime"})
            return {"ok": True}
        return calls, post, fetch

    def test_all_legs_start_and_verify(self):
        calls, post, fetch = self.fakes()
        r = handoff.enable_spec(EXAMPLE, "Sim101", post, fetch, date(2026, 11, 6))
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["verified_states"]), 2)

    def test_skip_defaults_is_sent_for_every_leg_by_default(self):
        calls, post, fetch = self.fakes()
        r = handoff.enable_spec(EXAMPLE, "Sim101", post, fetch, date(2026, 11, 6))
        self.assertTrue(r["ok"], r)
        enables = [c for c in calls if c[0] == "/strategy/enable"]
        self.assertEqual(len(enables), 2)
        self.assertTrue(all(c[2].get("skip_defaults") == "1" for c in enables))

    def test_failure_rolls_back_earlier_legs(self):
        calls, post, fetch = self.fakes(fail_on="PlaceholderLegB")
        r = handoff.enable_spec(EXAMPLE, "Sim101", post, fetch, date(2026, 11, 6))
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["rolled_back"]), 1)
        self.assertIn("/strategy/disable", [c[0] for c in calls])

    def test_conflict_blocks_start(self):
        spec = copy.deepcopy(EXAMPLE)
        spec["conflicts_with"] = ["MondayLongMNQ"]
        calls, post, fetch = self.fakes(present=[{"account": "Sim101", "name": "MondayLongMNQ", "state": "Realtime"}])
        r = handoff.enable_spec(spec, "Sim101", post, fetch)
        self.assertFalse(r["ok"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
