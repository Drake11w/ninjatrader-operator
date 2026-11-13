"""Backtest vs sim comparison on identical metrics. Read-only; recommends nothing and changes nothing.

`compare()` is pure. `for_strategy()` loads the registered spec's latest backtest and its sim trades and calls it.
Each row: metric, backtest value, sim value, difference, flag. Flags:
  ok            within tolerance
  worse/better  outside tolerance in the bad / good direction
  shift         outside tolerance on a metric with no good direction (holding time)
  low_sample    too few sim trades to judge (the numbers are still shown)
  n/a           one side is missing
Tolerances are deliberately loose (this is a first look, not a statistical test; the supervisor's pace and holding
checks and the verdict engine do the tests). Not covered: slippage and fill quality, which need signal prices the
add-on does not expose.
"""
import json
import statistics
from datetime import datetime, timezone

import backtest
import registry
import sessions
import trades as trades_mod
import verdict

MIN_SIM_TRADES = 15

# metric: (label, higher_is_better or None, kind, tolerance). kind "rel" = relative to the backtest value,
# "abs" = absolute difference.
METRICS = {
    "expectancy": ("Expectancy / trade ($)", True, "rel", 0.30),
    "win_rate": ("Win rate", True, "abs", 0.10),
    "profit_factor": ("Profit factor", True, "rel", 0.30),
    "avg_win": ("Average win ($)", True, "rel", 0.30),
    "avg_loss": ("Average loss ($, negative)", True, "rel", 0.30),  # less negative is better
    "trades_per_year": ("Trades / year", None, "rel", 0.40),
    "avg_duration_min": ("Median holding time (min)", None, "rel", 0.40),
}


def _flag(key, bt, sim, enough):
    if bt is None or sim is None:
        return "n/a"
    if not enough:
        return "low_sample"
    _, higher, kind, tol = METRICS[key]
    diff = sim - bt
    big = abs(diff) > tol if kind == "abs" else (abs(diff) > tol * abs(bt) if bt else abs(diff) > 0)
    if not big:
        return "ok"
    if higher is None:
        return "shift"
    return "better" if (diff > 0) == higher else "worse"


def sessions_since(since: str, now: datetime = None) -> int:
    """Finished regular sessions since the ISO time `since` (0 when it never entered Sim)."""
    if not since:
        return 0
    return sessions.elapsed_sessions(trades_mod._parse(since), now or datetime.now(timezone.utc))


def compare(sim_perf: dict, bt: dict, sim_trades: list, ref_durations: list = None, sessions_elapsed: int = 0) -> dict:
    """sim_perf: trades.performance() of the sim trades since activation; bt: the backtest's metrics dict;
    sim_trades: those trades; ref_durations: the backtest's per-trade holding times in seconds (or None);
    sessions_elapsed: finished sessions since activation (sessions_since), traded or not: the pace denominator.
    Without it (or under 5) the pace row is n/a, never a guess from the days that happened to have a trade."""
    bt = bt or {}
    n = sim_perf.get("trades") or 0
    enough = n >= MIN_SIM_TRADES
    sim = {k: sim_perf.get(k) for k in ("expectancy", "win_rate", "profit_factor", "avg_win", "avg_loss")}
    sim["trades_per_year"] = round(n / sessions_elapsed * 252, 1) if sessions_elapsed >= 5 else None
    durs = [t["duration_sec"] for t in sim_trades if t.get("duration_sec") is not None]
    sim["avg_duration_min"] = round(statistics.median(durs) / 60, 1) if durs else None
    ref = [d for d in (ref_durations or []) if d is not None]
    ref_vals = dict(bt)
    ref_vals["avg_duration_min"] = round(statistics.median(ref) / 60, 1) if ref else None
    rows = []
    for key, (label, *_rest) in METRICS.items():
        b, s = ref_vals.get(key), sim.get(key)
        rows.append({"metric": key, "label": label, "backtest": b, "sim": s,
                     "difference": round(s - b, 4) if b is not None and s is not None else None,
                     "flag": _flag(key, b, s, enough)})
    notes = []
    if not bt:
        notes.append("no backtest imported for this version; nothing to compare against")
    if not enough:
        notes.append(f"only {n} sim trades (need {MIN_SIM_TRADES}); flags are suppressed, numbers are shown for reference")
    flags = [r["flag"] for r in rows]
    summary = ("no_backtest" if not bt else "low_sample" if not enough else
               "diverging" if any(f in ("worse", "shift") for f in flags) else "consistent")
    return {"summary": summary, "sim_trades": n, "rows": rows, "notes": notes}


def for_strategy(rcon, jcon, sid: str, version: str, account: str = "") -> dict:
    res = verdict.evaluate_registered(rcon, jcon, sid, version, account)
    if "error" in res:
        return res
    ts = res["trades"]
    perf = trades_mod.performance(ts, "America/Chicago") if ts else {"trades": 0}
    ref = []
    runs = backtest.list_runs(rcon, sid, version)
    if runs:
        full = backtest.get_run(rcon, runs[0]["run_id"])
        if full and full.get("trades_json"):
            ref = [t.get("duration_sec") for t in json.loads(full["trades_json"])]
    out = compare(perf, res.get("backtest") or {}, ts, ref, sessions_since(res.get("since")))
    out.update(id=sid, version=version, status=res.get("status"), since=res.get("since"),
               backtest_run=(res.get("backtest") or {}).get("run_id"))
    return out
