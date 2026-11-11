"""Deterministic synthetic data for the feedback export: registry + journal in memory, fixed clock, fake ids.

Scenarios: ready, not_ready (one failing criterion: an untagged fill), nullable_fields (few trades, odd exit names,
slippage/regime null), empty_period. Everything is made up (strat-a, S900, acct names, round numbers).

Regenerate the committed golden bundles (examples/feedback/<scenario>/) after a deliberate schema or logic change:
    python tests/fixtures/feedback_fixture.py --update-goldens
A schema change must also bump SCHEMA_VERSION and feedback.schema.json in the same commit.
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
import backtest  # noqa: E402
import feedback  # noqa: E402
import journal  # noqa: E402
import registry  # noqa: E402

ACCOUNT = "Sim101"
AS_OF = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
SIM_START = "2026-08-31T12:00:00Z"
SCENARIOS = ("ready", "not_ready", "nullable_fields", "empty_period")
GOLDEN_DIR = os.path.join(ROOT, "examples", "feedback")

SPEC = {"id": "strat-a", "version": "1.0.0", "lucid_id": "S900", "schema_version": 2, "timezone": "America/Chicago",
        "instrument": {"root": "MNQ"},
        "expected_metrics": {"win_rate": 0.5, "expectancy": 40.0, "trades_per_year": 252},
        "legs": [{"name": "main", "ninjascript": {"class": "StratA", "order_names": ["StratA long", "StratA time exit"]}}]}
TAG = ("strat-a", "1.0.0", "main")


def _weekdays(start: datetime, n: int):
    d, out = start, []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _executions(pnls, exit_names=None):
    """Entry + exit fill per trade, one trade per weekday from 2026-09-01. Net pnl = points*2 - 1.24 commission."""
    out = []
    for i, (day, net) in enumerate(zip(_weekdays(datetime(2026, 9, 1, tzinfo=timezone.utc), len(pnls)), pnls)):
        points = round((net + 1.24) / 2, 4)
        t_in, t_out = day.replace(hour=14, minute=30), day.replace(hour=15)
        name = (exit_names or {}).get(i, "Profit target" if net > 0 else "Stop loss")
        out.append({"account": ACCOUNT, "execution_id": f"e{i}-in", "instrument": "MNQ 12-26",
                    "time": t_in.strftime("%Y-%m-%dT%H:%M:%SZ"), "action": "Buy", "order_name": "StratA long",
                    "quantity": 1, "price": 20000.0, "commission": 0.62, "is_entry": True, "is_exit": False})
        out.append({"account": ACCOUNT, "execution_id": f"e{i}-out", "instrument": "MNQ 12-26",
                    "time": t_out.strftime("%Y-%m-%dT%H:%M:%SZ"), "action": "Sell", "order_name": name,
                    "quantity": 1, "price": 20000.0 + points, "commission": 0.62, "is_entry": False, "is_exit": True})
    return out


def build(scenario: str):
    """Returns (registry connection, journal connection). Raises on an unknown scenario."""
    if scenario not in SCENARIOS:
        raise ValueError(scenario)
    rcon, jcon = registry.connect(":memory:"), journal.connect(":memory:")
    rcon.execute("INSERT INTO specs (id, version, status, spec_json, submitted_at) VALUES (?,?,?,?,?)",
                 ("strat-a", "1.0.0", "sim", json.dumps(SPEC, sort_keys=True), "2026-08-01T00:00:00Z"))
    for frm, to, at in ((None, "candidate", "2026-08-01T00:00:00Z"), ("candidate", "backtest", "2026-08-10T00:00:00Z"),
                        ("backtest", "sim", SIM_START)):
        rcon.execute("INSERT INTO status_history (id, version, from_status, to_status, note, at) VALUES (?,?,?,?,?,?)",
                     ("strat-a", "1.0.0", frm, to, "fixture", at))
    backtest.ensure(rcon)
    metrics = {"start": "2020-01-02", "end": "2026-07-31", "years": 6.58, "trades": 1658, "net_pnl": 66000.0,
               "net_per_year": 10030.0, "max_drawdown": 1500.0, "win_rate": 0.5, "expectancy": 40.0,
               "profit_factor": 1.5, "avg_win": 60.0, "avg_loss": -20.0, "trades_per_year": 252.0}
    ref = [{"net_pnl": p, "duration_sec": 1800} for p in ([60, 60, -20, -20, 60, 60, -20] * 3)]
    rcon.execute("INSERT INTO backtest_runs (spec_id, version, label, kind, source_name, export_timezone,"
                     " imported_at, start, end, metrics_json, daily_json, trades_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("strat-a", "1.0.0", "fixture-baseline", "trades", "fixture.csv", "America/Chicago",
                      "2026-08-10T00:00:00Z", "2020-01-02", "2026-07-31", json.dumps(metrics), "{}", json.dumps(ref)))
    rcon.commit()

    tagger = lambda e: TAG if not e["execution_id"].startswith("untagged") else None  # noqa: E731
    if scenario in ("ready", "not_ready"):
        execs = _executions(([60, 60, -20] * 12)[:36])
        if scenario == "not_ready":
            execs.append({"account": ACCOUNT, "execution_id": "untagged-1", "instrument": "MNQ 12-26",
                          "time": "2026-10-05T16:00:00Z", "action": "Buy", "order_name": "Manual", "quantity": 1,
                          "price": 20010.0, "commission": 0.62, "is_entry": True, "is_exit": False})
        journal.record(jcon, execs, tagger=tagger)
    elif scenario == "nullable_fields":
        journal.record(jcon, _executions([60, -20, 60, 60, -20, 60, -20, 60, 60, -20, 60, 60],
                                         {2: "", 4: "Manual close", 6: "StratA time exit"}), tagger=tagger)
    return rcon, jcon


def export_scenario(scenario: str, out_dir: str, alias_file: str, handoff_dir: str = ""):
    rcon, jcon = build(scenario)
    try:
        return feedback.export(rcon, jcon, "strat-a", "1.0.0", out_dir=out_dir, alias_file=alias_file,
                               handoff_dir=handoff_dir, as_of=AS_OF, operator_commit=None)
    finally:
        rcon.close()
        jcon.close()


def make_handoff(root: str) -> str:
    """A synthetic handoff bundle manifest (only MANIFEST.json is read by the export)."""
    d = os.path.join(root, "handoff", "strat-a", "1.0.0")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump({"bundle_version": 1, "lucid_repo_commit": "0123456789abcdef0123456789abcdef01234567",
                   "lucid_id": "S900", "files": {"strategies/StratA.cs": {"sha256": "ab" * 32}}}, f)
    return os.path.join(root, "handoff")


def update_goldens() -> None:
    os.environ["NT_AUDIT_FILE"] = os.path.join(tempfile.gettempdir(), "feedback_fixture_audit.jsonl")
    for sc in SCENARIOS:
        with tempfile.TemporaryDirectory() as d:
            res = export_scenario(sc, os.path.join(d, "out"), os.path.join(d, "aliases.json"), make_handoff(d))
            assert res["ok"] and res["status"] == "written", res
            dest = os.path.join(GOLDEN_DIR, sc)
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(res["path"], dest)
            print("updated", dest)


if __name__ == "__main__":
    if "--update-goldens" in sys.argv:
        update_goldens()
    else:
        print(__doc__)
