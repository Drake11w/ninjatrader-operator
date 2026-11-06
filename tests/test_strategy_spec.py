import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import strategy_spec

with open(os.path.join(ROOT, "examples", "orb-mnq.strategy.json"), encoding="utf-8") as f:
    EXAMPLE = json.load(f)


def broken(mutate):
    spec = copy.deepcopy(EXAMPLE)
    mutate(spec)
    return strategy_spec.validate(spec)


class SpecTests(unittest.TestCase):
    def test_example_is_valid(self):
        self.assertEqual(strategy_spec.validate(EXAMPLE), [])
        self.assertEqual(strategy_spec.summarize(EXAMPLE)["window"], "09:45-11:00 ET, flat by 15:55")

    def test_not_an_object(self):
        self.assertEqual(strategy_spec.validate([1]), ["spec must be a JSON object"])

    def test_missing_sections_reported_together(self):
        errs = strategy_spec.validate({"schema_version": 1})
        for part in ("spec.id", "spec.version", "spec.instrument", "spec.session", "spec.entry", "spec.exit",
                     "spec.sizing", "hypothesis", "spec.costs"):
            self.assertTrue(any(part in e for e in errs), part)

    def test_requires_stop_hypothesis_and_costs(self):
        self.assertTrue(any("exit.stop" in e for e in broken(lambda s: s["exit"].pop("stop"))))
        self.assertTrue(any("hypothesis" in e for e in broken(lambda s: s.update(hypothesis="  "))))
        self.assertTrue(any("costs" in e for e in broken(lambda s: s.pop("costs"))))

    def test_bad_values(self):
        self.assertTrue(broken(lambda s: s.update(id="Bad Id")))
        self.assertTrue(broken(lambda s: s.update(version="1.0")))
        self.assertTrue(broken(lambda s: s.update(status="yolo")))
        self.assertTrue(broken(lambda s: s["exit"]["stop"].update(value=0)))
        self.assertTrue(broken(lambda s: s["exit"]["stop"].update(type="vibes")))
        self.assertTrue(broken(lambda s: s["entry"].update(side="up")))
        self.assertTrue(broken(lambda s: s["entry"]["rules"][0].pop("description")))
        self.assertTrue(broken(lambda s: s["expected_metrics"].update(sharpe_ratio_of_dreams=9)))
        self.assertTrue(broken(lambda s: s["parameters"].update(nested={"a": 1})))

    def test_session_order_and_format(self):
        self.assertTrue(broken(lambda s: s["session"].update(entry_end="09:00")))
        self.assertTrue(broken(lambda s: s["session"].update(flat_by="25:00")))
        self.assertTrue(broken(lambda s: s["session"].update(entry_end="16:30")))  # after flat_by

    def test_sizing(self):
        self.assertTrue(any("exceed" in e for e in broken(lambda s: s["sizing"].update(contracts=3))))
        self.assertTrue(broken(lambda s: s["sizing"].update(contracts=True)))

    def test_fixed_contract_needs_contract(self):
        self.assertTrue(broken(lambda s: s["instrument"].update(contract_policy="fixed")))
        self.assertEqual(broken(lambda s: s["instrument"].update(contract_policy="fixed", contract="12-26")), [])


if __name__ == "__main__":
    unittest.main()
