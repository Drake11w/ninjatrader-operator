import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import journal
import registry
import strategy_spec
import trades

with open(os.path.join(ROOT, "examples", "two-leg-mnq.strategy.json"), encoding="utf-8") as f:
    EXAMPLE = json.load(f)


def fill(i, t, name, entry=False, exit_=False, price=100.0, action="Buy"):
    return {"account": "Sim101", "execution_id": f"e{i}", "time": f"2026-11-0{t[0]}T{t[1]}Z", "instrument": "MNQ 12-26",
            "action": action, "order_name": name, "quantity": 1, "price": price, "commission": 0.62,
            "is_entry": entry, "is_exit": exit_}


def live_registry(spec=EXAMPLE, to="sim"):
    rcon = registry.connect(":memory:")
    registry.submit(rcon, spec)
    for st in ("backtest", "sim", "live-small", "live"):
        registry.set_status(rcon, spec["id"], spec["version"], st)
        if st == to:
            break
    return rcon


class TaggingTests(unittest.TestCase):
    def test_order_names_validated(self):
        bad = copy.deepcopy(EXAMPLE)
        bad["legs"][0]["ninjascript"]["order_names"] = ["ok", "ok"]
        self.assertTrue(strategy_spec.validate(bad))
        bad["legs"][0]["ninjascript"]["order_names"] = "ORB long"
        self.assertTrue(strategy_spec.validate(bad))

    def test_map_only_includes_running_specs(self):
        rcon = registry.connect(":memory:")
        registry.submit(rcon, EXAMPLE)  # candidate: not trading yet
        self.assertEqual(registry.order_name_map(rcon), {})
        registry.set_status(rcon, "two-leg-mnq", "1.0.0", "backtest")
        self.assertEqual(registry.order_name_map(rcon), {})
        registry.set_status(rcon, "two-leg-mnq", "1.0.0", "sim")
        m = registry.order_name_map(rcon)
        self.assertEqual(m["Leg B long"], ("two-leg-mnq", "1.0.0", "leg-b-range-breakout"))

    def test_more_advanced_version_wins_name_clash(self):
        rcon = live_registry(to="sim")
        v2 = copy.deepcopy(EXAMPLE)
        v2["version"] = "1.0.1"
        registry.submit(rcon, v2)
        for st in ("backtest", "sim", "live-small"):
            registry.set_status(rcon, "two-leg-mnq", "1.0.1", st)
        self.assertEqual(registry.order_name_map(rcon)["Leg A long"][1], "1.0.1")

    def test_tagging_entries_exits_and_stop_fills(self):
        jcon = journal.connect(":memory:")
        names = registry.order_name_map(live_registry())
        execs = [fill(1, (3, "14:30:00"), "Leg A long", entry=True),
                 fill(2, (3, "20:40:00"), "Leg A time exit", exit_=True, price=101, action="Sell"),
                 fill(3, (4, "15:00:00"), "Leg B long", entry=True),
                 fill(4, (4, "16:00:00"), "Stop loss", exit_=True, price=99, action="Sell"),
                 fill(5, (4, "17:00:00"), "Manual", entry=True)]  # unknown entry stays untagged
        journal.record(jcon, execs, tagger=journal.make_tagger(jcon, names))
        rows = {r["execution_id"]: r for r in journal.query(jcon)}
        self.assertEqual(rows["e2"]["leg"], "leg-a-open-long")
        self.assertEqual((rows["e3"]["strategy"], rows["e3"]["leg"]), ("two-leg-mnq", "leg-b-range-breakout"))
        self.assertEqual(rows["e4"]["leg"], "leg-b-range-breakout")  # stop fill follows its entry
        self.assertEqual((rows["e5"]["strategy"], rows["e5"]["leg"]), ("", ""))
        built = trades.build_trades(journal.query(jcon))["trades"]
        self.assertEqual({x["leg"] for x in built}, {"leg-a-open-long", "leg-b-range-breakout"})

    def test_stop_fill_in_later_sync_uses_journal(self):
        jcon = journal.connect(":memory:")
        names = registry.order_name_map(live_registry())
        journal.record(jcon, [fill(1, (4, "15:00:00"), "Leg B long", entry=True)],
                       tagger=journal.make_tagger(jcon, names))
        journal.record(jcon, [fill(2, (4, "16:00:00"), "Stop loss", exit_=True, action="Sell")],
                       tagger=journal.make_tagger(jcon, names))  # fresh tagger, entry only in the DB
        self.assertEqual({r["execution_id"]: r["leg"] for r in journal.query(jcon)}["e2"], "leg-b-range-breakout")

    def test_explicit_strategy_still_works_and_old_journal_migrates(self):
        jcon = journal.connect(":memory:")
        journal.record(jcon, [fill(1, (4, "15:00:00"), "x", entry=True)], "manual", "9.9.9")
        r = journal.query(jcon)[0]
        self.assertEqual((r["strategy"], r["strategy_version"], r["leg"]), ("manual", "9.9.9", ""))
        import sqlite3
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "old.sqlite")
        old = sqlite3.connect(path)
        old.executescript(journal.SCHEMA.replace("    leg TEXT NOT NULL DEFAULT '',\n", ""))
        old.close()
        con = journal.connect(path)
        self.assertIn("leg", {c[1] for c in con.execute("PRAGMA table_info(executions)")})
        con.close()


if __name__ == "__main__":
    unittest.main()
