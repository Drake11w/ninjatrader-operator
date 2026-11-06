import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import backtest
import registry
import trades

with open(os.path.join(ROOT, "examples", "two-leg-mnq.strategy.json"), encoding="utf-8") as f:
    SPEC = json.load(f)

TRADES_CSV = """Trade #,Instrument,Account,Strategy,Market pos.,Qty,Entry price,Exit price,Entry time,Exit time,Entry name,Exit name,Profit,Cum. net profit,Commission,MAE,MFE,ETD,Bars
1,MNQ 12-26,Backtest,X,Long,1,"20,000.00","20,010.00",11/2/2026 8:30:00 AM,11/2/2026 3:40:00 PM,Leg A long,Leg A time exit,"$20.00","$20.00","$1.24",$5.00,$30.00,$10.00,100
2,MNQ 12-26,Backtest,X,Long,1,"20,000.00","19,990.00",11/3/2026 9:05:00 AM,11/3/2026 10:00:00 AM,Leg B long,Stop loss,"($20.00)","$0.00","$1.24",$25.00,$2.00,$5.00,12
3,MNQ 12-26,Backtest,X,Long,1,"20,000.00","20,030.00",11/4/2026 9:05:00 AM,11/4/2026 2:55:00 PM,Leg B long,Leg B time exit,"$60.00","$60.00","$1.24",$2.00,$70.00,$10.00,80
"""
# Same trades, but Profit is gross (cum. net profit = profit - commission)
GROSS_CSV = TRADES_CSV.replace('"$20.00","$20.00"', '"$21.24","$20.00"').replace('"($20.00)","$0.00"', '"($18.76)","$0.00"').replace('"$60.00","$60.00"', '"$61.24","$60.00"')
PERIODS_CSV = "Period,Net profit,Trades\n11/2/2026,$20.00,1\n11/3/2026,($20.00),1\n11/4/2026,$60.00,1\n"


class ParseTests(unittest.TestCase):
    def test_numbers_and_dates(self):
        self.assertEqual(backtest._num("($1,234.50)"), -1234.5)
        self.assertEqual(backtest._num("$12"), 12.0)
        self.assertIsNone(backtest._num(""))
        self.assertIsNone(backtest._num("abc"))
        self.assertEqual(backtest._dt("11/2/2026 3:40:00 PM").hour, 15)
        self.assertEqual(backtest._dt("2026-11-02").day, 2)
        self.assertIsNone(backtest._dt("not a date"))

    def test_trades_grid(self):
        p = backtest.parse(TRADES_CSV, "America/Chicago")
        self.assertEqual(p["kind"], "trades")
        self.assertEqual([t["net_pnl"] for t in p["trades"]], [20.0, -20.0, 60.0])
        self.assertEqual(p["trades"][0]["entry_time"], "2026-11-02T14:30:00Z")  # 08:30 CST = 14:30Z
        self.assertEqual(p["daily"], {"2026-11-02": 20.0, "2026-11-03": -20.0, "2026-11-04": 60.0})
        self.assertTrue(any("already net" in w for w in p["warnings"]))

    def test_gross_profit_detected_via_cumulative(self):
        p = backtest.parse(GROSS_CSV, "America/Chicago")
        self.assertEqual([t["net_pnl"] for t in p["trades"]], [20.0, -20.0, 60.0])
        self.assertTrue(any("subtracted" in w for w in p["warnings"]))

    def test_periods_grid(self):
        p = backtest.parse(PERIODS_CSV, "America/Chicago")
        self.assertEqual((p["kind"], p["trades"], len(p["daily"])), ("periods", [], 3))

    def test_semicolon_bom_and_junk(self):
        p = backtest.parse("﻿" + PERIODS_CSV.replace(",", ";"), "America/Chicago")
        self.assertEqual(p["kind"], "periods")
        self.assertIn("error", backtest.parse("a,b\n1,2\n", "America/Chicago"))
        self.assertIn("error", backtest.parse("", "America/Chicago"))
        monthly = "Period,Net profit\nNov 2026,$5\n"
        self.assertIn("error", backtest.parse(monthly, "America/Chicago"))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.con = registry.connect(":memory:")
        registry.submit(self.con, SPEC)

    def test_metrics(self):
        m = backtest.metrics(backtest.parse(TRADES_CSV, "America/Chicago"))
        self.assertEqual((m["trades"], m["net_pnl"], m["max_drawdown"]), (3, 60.0, 20.0))
        self.assertEqual((m["start"], m["end"], m["win_rate"]), ("2026-11-02", "2026-11-04", 0.6667))
        self.assertEqual(m["profit_to_drawdown"], 3.0)

    def test_import_store_and_list(self):
        r = backtest.import_run(self.con, SPEC, TRADES_CSV, label="first", source_name="x.csv")
        self.assertTrue(r["ok"])
        self.assertEqual(backtest.list_runs(self.con, "two-leg-mnq")[0]["summary"]["trades"], 3)
        self.assertEqual(backtest.get_run(self.con, r["run_id"])["label"], "first")
        self.assertFalse(backtest.import_run(self.con, SPEC, "junk\n1\n")["ok"])
        self.assertFalse(backtest.import_run(self.con, SPEC, TRADES_CSV, export_tz="Mars/Base")["ok"])

    def test_claims_check(self):
        m = {"trades": 1000, "net_per_year": 3100, "max_drawdown": 3500, "win_rate": 0.5}
        spec = {"expected_metrics": {"min_trades": 932, "net_per_year": 3000, "max_drawdown": 2000, "eval_pass": 0.8}}
        c = backtest.claims_check(m, spec)
        self.assertEqual(c["metrics"]["min_trades"]["status"], "ok")
        self.assertEqual(c["metrics"]["net_per_year"]["status"], "ok")
        self.assertEqual(c["metrics"]["max_drawdown"]["status"], "differs")
        self.assertEqual(c["overall"], "differs")
        self.assertEqual(c["not_comparable"], ["eval_pass"])  # needs a simulation, not a CSV

    def journal_trades(self, rows):
        return [{"exit_time": t, "net_pnl": p} for t, p in rows]

    def test_compare_to_journal(self):
        r = backtest.import_run(self.con, SPEC, TRADES_CSV)
        run = backtest.get_run(self.con, r["run_id"])
        live = self.journal_trades([("2026-11-02T21:40:00Z", 15.0), ("2026-11-03T16:00:00Z", 10.0)])  # Nov 4 missed
        out = backtest.compare_to_journal(run, SPEC, live)
        self.assertEqual(out["overlap"], {"start": "2026-11-02", "end": "2026-11-03", "days_compared": 2})
        self.assertEqual((out["days_both_traded"], out["same_direction_rate"]), (2, 0.5))
        self.assertEqual((out["backtest_net"], out["live_net"]), (0.0, 25.0))

    def test_compare_flags_backtest_not_covering_live(self):
        r = backtest.import_run(self.con, SPEC, TRADES_CSV)
        run = backtest.get_run(self.con, r["run_id"])
        live = self.journal_trades([("2026-11-03T16:00:00Z", 10.0), ("2026-11-20T16:00:00Z", 10.0)])
        out = backtest.compare_to_journal(run, SPEC, live)
        self.assertTrue(any("ends 2026-11-04" in w for w in out["warnings"]))
        self.assertEqual(out["n_only_live"], 0)  # days past the backtest are outside the compared window
        far = self.journal_trades([("2027-01-05T16:00:00Z", 10.0)])
        self.assertIn("error", backtest.compare_to_journal(run, SPEC, far))
        self.assertIn("error", backtest.compare_to_journal(run, SPEC, []))

    def test_leg_filter(self):
        r = backtest.import_run(self.con, SPEC, TRADES_CSV)
        run = backtest.get_run(self.con, r["run_id"])
        live = self.journal_trades([("2026-11-03T16:00:00Z", -18.0)])
        out = backtest.compare_to_journal(run, SPEC, live, leg="leg-b-range-breakout")
        self.assertEqual((out["backtest_net"], out["live_net"]), (-20.0, -18.0))
        self.assertIn("error", backtest.compare_to_journal(run, SPEC, live, leg="nope"))
        p = backtest.import_run(self.con, SPEC, PERIODS_CSV)
        prun = backtest.get_run(self.con, p["run_id"])
        self.assertIn("error", backtest.compare_to_journal(prun, SPEC, live, leg="leg-b-range-breakout"))


class ToolTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        d = tempfile.mkdtemp()
        os.environ["NT_REGISTRY_DB"] = os.path.join(d, "r.sqlite")
        os.environ["NT_JOURNAL_DB"] = os.path.join(d, "j.sqlite")
        self.csv_path = os.path.join(d, "export.csv")
        with open(self.csv_path, "w", encoding="utf-8-sig") as f:
            f.write(TRADES_CSV)
        import server
        self.server = server

    def test_end_to_end(self):
        sv = self.server
        self.assertFalse(json.loads(sv.import_backtest("two-leg-mnq", "1.0.0", path=self.csv_path))["ok"])  # unregistered
        self.assertTrue(json.loads(sv.submit_strategy(json.dumps(SPEC)))["ok"])
        r = json.loads(sv.import_backtest("two-leg-mnq", "1.0.0", path=self.csv_path, label="t"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["metrics"]["trades"], 3)
        self.assertFalse(json.loads(sv.import_backtest("two-leg-mnq", "1.0.0", path=self.csv_path + ".nope"))["ok"])
        self.assertFalse(json.loads(sv.import_backtest("two-leg-mnq", "1.0.0"))["ok"])
        self.assertEqual(len(json.loads(sv.list_backtests("two-leg-mnq"))), 1)
        c = json.loads(sv.check_backtest(r["run_id"]))
        self.assertEqual(c["claims_check"]["overall"], "differs")  # example claims are placeholders
        self.assertIn("error", json.loads(sv.check_backtest(999)))
        self.assertIn("error", json.loads(sv.compare_backtest_to_journal(r["run_id"])))  # empty journal


if __name__ == "__main__":
    unittest.main()
