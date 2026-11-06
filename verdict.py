"""Verdict engine: written promotion / rejection criteria applied to a strategy's journaled Sim results.

Verdicts:
  reject        a hard stop fired (drawdown, loss, or statistically significant underperformance): disable it
  promote       every promotion check passed on enough data: it may move to the next stage
  keep_testing  nothing failed yet, but the sample is too small or a check is not met: keep it running

Design rules:
  * Small samples cannot promote. Only the drawdown stop can reject before `min_trades` (a risk limit, not a statistic).
  * The verdict only recommends. It never disables a strategy or changes status itself; the caller does that.
  * Every check is reported with its threshold and actual value so a human can see why.
  * Results are judged on trades since the spec entered 'sim', for that spec version only.
"""
import json
import math
from datetime import datetime, timezone

import trades as trades_mod

DEFAULT_CRITERIA = {
    "min_trades": 30,                  # no promotion below this many closed Sim trades
    "min_days": 20,                    # ... or this many distinct trading days
    "min_expectancy": 0.0,             # $/trade, after commission, must exceed this to promote
    "min_profit_factor": 1.2,
    "drawdown_limit": 2000.0,          # firm trailing drawdown ($); LucidFlex 50K
    "max_drawdown_pct": 0.5,           # promote only if max drawdown stays under this share of the limit
    "reject_drawdown_pct": 0.9,        # reject at any sample size when drawdown reaches this share of the limit
    "max_consecutive_losses": 8,       # warn (not reject) beyond this
    "reject_min_trades": 20,           # statistical rejects need at least this many trades
    "reject_profit_factor": 0.8,       # ... and profit factor below this with negative expectancy
    "min_win_rate_z": -2.5,            # reject if win rate is this many standard errors below the claim
    "min_expectancy_ratio": 0.4,       # promote only if sim expectancy >= this share of the claimed expectancy
    "decay_min_trades": 20,            # edge-decay test needs this many Sim trades
    "decay_window": 20,                # also test the most recent N trades on their own
    "decay_z": -2.0,                   # decayed if mean P&L/trade is this many standard errors below the backtest
    "decay_ratio": 0.5,                # ... and below this share of the backtest expectancy
    "consistency_pct": 0.5,            # best day must be under this share of total profit (prop rule)
}


def _consecutive_losses(pnls: list) -> int:
    worst = cur = 0
    for p in pnls:
        cur = cur + 1 if p < 0 else 0
        worst = max(worst, cur)
    return worst


def _expected(spec: dict, backtest_metrics: dict) -> dict:
    """Claimed win_rate / expectancy / trades_per_year: a measured backtest wins over the spec's own claims."""
    claims = spec.get("expected_metrics", {}) or {}
    out = {}
    bm = backtest_metrics
    if bm and bm.get("expectancy") is None and bm.get("trades") and bm.get("net_pnl") is not None:
        bm = {**bm, "expectancy": round(bm["net_pnl"] / bm["trades"], 2)}  # summary-only exports
    backtest_metrics = bm
    for k in ("win_rate", "expectancy", "profit_factor", "trades_per_year"):
        v = (backtest_metrics or {}).get(k)
        src = "backtest"
        if v is None:
            v, src = claims.get(k), "spec"
        if isinstance(v, (int, float)):
            out[k] = {"value": v, "source": src}
    return out


def _check(name, status, actual, threshold, note=""):
    return {"check": name, "status": status, "actual": actual, "threshold": threshold, "note": note}


def _decay(pnls: list, ref_exp: float, ref_pnls: list, c: dict):
    """Is mean P&L/trade significantly below the backtest's? Returns (decayed, z, mean). The spread comes from the
    backtest's own trades when available, else from these trades."""
    n = len(pnls)
    mean = sum(pnls) / n
    pool = ref_pnls if ref_pnls and len(ref_pnls) >= 10 else pnls
    m = sum(pool) / len(pool)
    sd = math.sqrt(sum((x - m) ** 2 for x in pool) / (len(pool) - 1)) if len(pool) > 1 else 0.0
    if sd == 0:
        return False, None, mean
    z = (mean - ref_exp) / (sd / math.sqrt(n))
    return (z < c["decay_z"] and mean < c["decay_ratio"] * ref_exp), round(z, 2), mean


def evaluate(spec: dict, all_trades: list, backtest_metrics: dict = None, criteria: dict = None,
             since: str = "", tz_name: str = "America/Chicago", reference_pnls: list = None) -> dict:
    """Judge one spec version. `all_trades` are round-trip trades (trades.build_trades) already limited to this
    spec version; `since` (ISO time) drops trades that closed earlier (e.g. before it entered Sim)."""
    c = {**DEFAULT_CRITERIA, **(criteria or {})}
    cut = trades_mod._parse(since) if since else None
    mine = [t for t in all_trades if cut is None or trades_mod._parse(t["exit_time"]) >= cut]
    perf = trades_mod.performance(mine, tz_name) if mine else {"trades": 0}
    n = perf["trades"]
    pnls = [t["net_pnl"] for t in mine]
    expected = _expected(spec, backtest_metrics)
    checks, rejects, blockers = [], [], []

    # ---- hard stops
    dd = perf.get("max_drawdown", 0.0) or 0.0
    dd_share = dd / c["drawdown_limit"] if c["drawdown_limit"] else 0.0
    if dd_share >= c["reject_drawdown_pct"]:
        rejects.append(f"max drawdown ${dd:,.0f} is {dd_share:.0%} of the ${c['drawdown_limit']:,.0f} limit")
        checks.append(_check("drawdown_stop", "fail", round(dd_share, 3), c["reject_drawdown_pct"]))
    else:
        checks.append(_check("drawdown_stop", "pass", round(dd_share, 3), c["reject_drawdown_pct"]))

    if n >= c["reject_min_trades"]:
        pf = perf.get("profit_factor")
        if perf["expectancy"] < 0 and pf is not None and pf < c["reject_profit_factor"]:
            rejects.append(f"losing after costs: expectancy ${perf['expectancy']:,.2f}/trade, profit factor {pf} "
                           f"over {n} trades")
        wr = expected.get("win_rate")
        if wr and 0 < wr["value"] < 1:
            se = math.sqrt(wr["value"] * (1 - wr["value"]) / n)
            z = (perf["win_rate"] - wr["value"]) / se
            bad = z < c["min_win_rate_z"]
            checks.append(_check("win_rate_vs_claim", "fail" if bad else "pass", round(perf["win_rate"], 3),
                                 f"claim {wr['value']:.2f} ({wr['source']}), z>={c['min_win_rate_z']}",
                                 f"z={z:.2f}"))
            if bad:
                rejects.append(f"win rate {perf['win_rate']:.0%} is significantly below the claimed "
                               f"{wr['value']:.0%} (z={z:.1f}, n={n})")

    # ---- edge decay: Sim results vs the backtest reference (only a measured backtest counts as the reference)
    decayed = False
    ref = expected.get("expectancy")
    if ref and ref["source"] == "backtest" and ref["value"] > 0:
        if n >= c["decay_min_trades"]:
            for label, sample in (("all Sim trades", pnls),
                                  (f"last {c['decay_window']} trades", pnls[-c["decay_window"]:])):
                if len(sample) < c["decay_min_trades"] and label != "all Sim trades":
                    continue
                bad, z, mean = _decay(sample, ref["value"], reference_pnls, c)
                checks.append(_check(f"edge_decay ({label})", "fail" if bad else "pass", round(mean, 2),
                                     f"backtest ${ref['value']:,.2f}/trade; decayed if z<{c['decay_z']} and "
                                     f"<{c['decay_ratio']:.0%} of it", f"z={z}"))
                if bad:
                    decayed = True
                    rejects.append(f"edge decay: {label} average ${mean:,.2f}/trade vs ${ref['value']:,.2f} in the "
                                   f"backtest (z={z}, n={len(sample)})")
        else:
            checks.append(_check("edge_decay", "pending", n, c["decay_min_trades"], "too few Sim trades to test"))

    # ---- promotion checks (all must pass, and the sample must be big enough)
    days = perf.get("trading_days", 0)
    for name, ok, actual, thr in (
            ("sample_trades", n >= c["min_trades"], n, c["min_trades"]),
            ("sample_days", days >= c["min_days"], days, c["min_days"])):
        checks.append(_check(name, "pass" if ok else "pending", actual, thr))
        if not ok:
            blockers.append(f"{name}: {actual}/{thr}")
    if n:
        ok = perf["expectancy"] > c["min_expectancy"]
        checks.append(_check("expectancy", "pass" if ok else "fail", perf["expectancy"], f">{c['min_expectancy']}"))
        if not ok:
            blockers.append(f"expectancy ${perf['expectancy']:,.2f} not above ${c['min_expectancy']}")
        pf = perf.get("profit_factor")
        ok = pf is None and perf["net_pnl"] > 0 or (pf is not None and pf >= c["min_profit_factor"])
        checks.append(_check("profit_factor", "pass" if ok else "fail", pf, f">={c['min_profit_factor']}"))
        if not ok:
            blockers.append(f"profit factor {pf} below {c['min_profit_factor']}")
        ok = dd_share <= c["max_drawdown_pct"]
        checks.append(_check("drawdown", "pass" if ok else "fail", round(dd_share, 3), f"<={c['max_drawdown_pct']}"))
        if not ok:
            blockers.append(f"drawdown {dd_share:.0%} of limit exceeds {c['max_drawdown_pct']:.0%}")
        exp = expected.get("expectancy")
        if exp and exp["value"] > 0:
            ratio = perf["expectancy"] / exp["value"]
            ok = ratio >= c["min_expectancy_ratio"]
            checks.append(_check("expectancy_vs_claim", "pass" if ok else "fail", round(ratio, 2),
                                 f">={c['min_expectancy_ratio']} of ${exp['value']:,.2f} ({exp['source']})"))
            if not ok:
                blockers.append(f"expectancy is only {ratio:.0%} of the claimed ${exp['value']:,.2f}")
        else:
            checks.append(_check("expectancy_vs_claim", "skipped", None, None, "no claimed expectancy to compare"))
        if perf["net_pnl"] > 0 and perf.get("best_day"):
            share = perf["best_day"] / perf["net_pnl"]
            ok = share < c["consistency_pct"]
            checks.append(_check("consistency", "pass" if ok else "warn", round(share, 3),
                                 f"<{c['consistency_pct']}", "best day share of net profit"))
        streak = _consecutive_losses(pnls)
        checks.append(_check("loss_streak", "pass" if streak <= c["max_consecutive_losses"] else "warn",
                             streak, f"<={c['max_consecutive_losses']}"))

    if rejects:
        verdict, reasons = "reject", rejects
    elif blockers:
        verdict, reasons = "keep_testing", blockers
    else:
        verdict, reasons = "promote", ["all promotion checks passed"]

    out = {"verdict": verdict, "edge_decayed": decayed, "reasons": reasons, "sample": {"trades": n, "trading_days": days},
           "performance": {k: perf.get(k) for k in ("net_pnl", "win_rate", "expectancy", "profit_factor",
                                                    "avg_win", "avg_loss", "max_drawdown", "best_day", "worst_day",
                                                    "sharpe")} if n else {},
           "expected": expected, "checks": checks, "criteria": c, "warnings": perf.get("warnings", [])}
    tpy = expected.get("trades_per_year")
    if verdict == "keep_testing" and tpy and n < c["min_trades"]:
        weeks = (c["min_trades"] - n) / tpy["value"] * 52
        out["eta"] = f"about {weeks:.0f} more weeks to reach {c['min_trades']} trades at the claimed {tpy['value']:.0f}/year"
    return out


def action_for(verdict: str, status: str) -> str:
    """Plain-language next step. The operator carries it out; this module never does."""
    nxt = {"sim": "live-small", "live-small": "live"}.get(status)
    if verdict == "reject":
        return ("disable_strategy for each leg (and flatten if in a position), then "
                "set_strategy_status rejected with the reasons as the note")
    if verdict == "promote":
        return (f"set_strategy_status {nxt}: only after a human reviews the checks" if nxt
                else "already at the final stage: keep monitoring")
    return "leave it running and re-evaluate after more trades"


SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_id TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    verdict TEXT NOT NULL,
    trades INTEGER NOT NULL,
    net_pnl REAL,
    result_json TEXT NOT NULL,
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""


def record(con, spec_id: str, version: str, status: str, result: dict) -> int:
    con.executescript(SCHEMA)
    cur = con.execute("INSERT INTO verdicts (spec_id, version, status, verdict, trades, net_pnl, result_json)"
                      " VALUES (?,?,?,?,?,?,?)",
                      (spec_id, version, status, result["verdict"], result["sample"]["trades"],
                       result["performance"].get("net_pnl"), json.dumps(result)))
    con.commit()
    return cur.lastrowid


def history(con, spec_id: str = "", version: str = "", limit: int = 50) -> list:
    con.executescript(SCHEMA)
    q, args = "SELECT seq, spec_id, version, status, verdict, trades, net_pnl, at FROM verdicts WHERE 1=1", []
    for col, val in (("spec_id", spec_id), ("version", version)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    return [dict(r) for r in con.execute(q + " ORDER BY seq DESC LIMIT ?", args + [max(1, min(limit, 500))])]


def sim_start(con, spec_id: str, version: str) -> str:
    """When the spec most recently entered 'sim' (ISO UTC), or ''."""
    r = con.execute("SELECT at FROM status_history WHERE id=? AND version=? AND to_status='sim'"
                    " ORDER BY seq DESC LIMIT 1", (spec_id, version)).fetchone()
    return r["at"] if r else ""
