import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import registry
import strategy_spec

with open(os.path.join(ROOT, "examples", "two-leg-mnq.strategy.json"), encoding="utf-8") as f:
    EXAMPLE = json.load(f)


def broken(mutate):
    spec = copy.deepcopy(EXAMPLE)
    mutate(spec)
    return strategy_spec.validate(spec)


class SpecV2Tests(unittest.TestCase):
    def test_example_is_valid_and_summarised(self):
        self.assertEqual(strategy_spec.validate(EXAMPLE), [])
        s = strategy_spec.summarize(EXAMPLE)
        self.assertEqual((s["lucid_id"], s["contracts_total"], s["timezone"]), ("S123", 2, "America/Chicago"))
        self.assertEqual([l["ninjascript"] for l in s["legs"]], ["PlaceholderLegA", "PlaceholderLegB"])
        self.assertEqual(s["legs"][0]["target"], "none")

    def test_unknown_schema_version(self):
        self.assertTrue(any("schema_version" in e for e in strategy_spec.validate({"schema_version": 3})))

    def test_v1_still_valid(self):
        with open(os.path.join(ROOT, "examples", "orb-mnq.strategy.json"), encoding="utf-8") as f:
            self.assertEqual(strategy_spec.validate(json.load(f)), [])

    def test_legs_required_and_unique(self):
        self.assertTrue(broken(lambda s: s.update(legs=[])))
        self.assertTrue(any("unique" in e for e in broken(lambda s: s["legs"][1].update(name=s["legs"][0]["name"]))))

    def test_every_leg_needs_stop_and_ninjascript(self):
        self.assertTrue(any("legs[1].exit.stop" in e for e in broken(lambda s: s["legs"][1]["exit"].pop("stop"))))
        self.assertTrue(any("ninjascript" in e for e in broken(lambda s: s["legs"][0].pop("ninjascript"))))
        self.assertTrue(any("class" in e for e in broken(lambda s: s["legs"][0]["ninjascript"].update({"class": ""}))))
        self.assertTrue(broken(lambda s: s["legs"][0]["ninjascript"].update(inputs={"x": [1]})))

    def test_errors_name_the_leg(self):
        errs = broken(lambda s: s["legs"][1]["session"].update(entry_end="08:00"))
        self.assertTrue(any(e.startswith("legs[1].session") for e in errs))

    def test_enter_at_moment_allowed_but_not_backwards(self):
        self.assertEqual(broken(lambda s: s["legs"][0]["session"].update(entry_start="08:30", entry_end="08:30")), [])
        self.assertTrue(broken(lambda s: s["legs"][0]["session"].update(flat_by="08:00")))

    def test_days_timezone_lucid_id(self):
        self.assertTrue(broken(lambda s: s["legs"][0].update(days=["monday"])))
        self.assertTrue(broken(lambda s: s["legs"][0].update(days=["mon", "mon"])))
        self.assertTrue(broken(lambda s: s.update(timezone="UTC")))
        self.assertEqual(broken(lambda s: s.update(timezone="America/New_York")), [])
        self.assertTrue(broken(lambda s: s.update(lucid_id="123")))

    def test_metric_fractions_and_unknown_keys(self):
        self.assertTrue(any("fraction" in e for e in broken(lambda s: s["expected_metrics"].update(eval_pass=82))))
        self.assertTrue(broken(lambda s: s["expected_metrics"].update(sharpe=3)))
        self.assertTrue(broken(lambda s: s.update(backtest={"start": "2026-01-01", "end": "2025-01-01"})))

    def test_costs_and_hypothesis_required(self):
        self.assertTrue(broken(lambda s: s.pop("costs")))
        self.assertTrue(broken(lambda s: s.update(hypothesis=" ")))

    def test_registry_accepts_v2_and_summarises(self):
        con = registry.connect(":memory:")
        self.assertTrue(registry.submit(con, EXAMPLE)["ok"])
        listed = registry.list_specs(con)
        self.assertEqual(listed[0]["summary"]["lucid_id"], "S123")
        self.assertTrue(registry.set_status(con, "two-leg-mnq", "1.0.0", "backtest")["ok"])


PRIVATE = os.path.join(ROOT, "examples", "live.private.strategy.json")


@unittest.skipUnless(os.path.exists(PRIVATE), "private example not present (it is gitignored)")
class PrivateExampleTests(unittest.TestCase):
    def test_private_example_valid(self):
        with open(PRIVATE, encoding="utf-8") as f:
            self.assertEqual(strategy_spec.validate(json.load(f)), [])


if __name__ == "__main__":
    unittest.main()
