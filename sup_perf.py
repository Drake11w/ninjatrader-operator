"""Strategy Supervisor: performance and behavior drift vs the backtest (I/O side). Warning level only.

Uses the Operator's existing verdict engine read-only for its statistics. It never calls disable_strategy, never
changes a registry status and never records a verdict. The only write is the same local journal sync that the
dashboard and sync_journal already do (fills copied from the add-on's GET /executions into the local SQLite journal).
"""
import json
from datetime import datetime, timezone

import backtest
import journal
import registry
import sup_behavior
import sup_fetch
import verdict

ACTIVE = ("sim", "live-small", "live")


def _finding(key, account, strategy, trigger, evidence):
    return {"key": key, "severity": 1, "account": account, "strategy": strategy, "trigger": trigger,
            "evidence": evidence, "caused_by": ""}


def _sync(jcon, rcon, executions):
    tagger = journal.make_tagger(jcon, registry.order_name_map(rcon))
    ex = sorted(executions, key=lambda e: e.get("time") or "")
    return journal.record(jcon, ex, "", "", tagger)["new"]


def _ref_durations(rcon, sid, version):
    runs = backtest.list_runs(rcon, sid, version)
    if not runs:
        return []
    full = backtest.get_run(rcon, runs[0]["run_id"])
    if not full or not full.get("trades_json"):
        return []
    return [t.get("duration_sec") for t in json.loads(full["trades_json"])]


def collect(profiles: list, now: datetime = None, base: str = "", rcon=None, jcon=None) -> dict:
    """Return {"findings": [...]} or {"findings": None, "skipped": reason} when nothing could be judged this round
    (the caller then keeps its previous findings rather than treating silence as health)."""
    now = now or datetime.now(timezone.utc)
    ex = sup_fetch.fetch("/executions?limit=500", base)
    if not ex["ok"]:
        return {"findings": None, "skipped": f"/executions unavailable: {ex['error']}"}
    own_r, own_j = rcon is None, jcon is None
    rcon = rcon or registry.connect()
    jcon = jcon or journal.connect()
    try:
        synced = _sync(jcon, rcon, ex["data"].get("executions", []))
        findings, notes = [], []
        seen = set()
        for p in profiles:
            key = (p["strategy_id"], p["version"], p["account"])
            if not p.get("monitored", True) or key in seen:
                continue
            seen.add(key)
            sid, ver, acct = key
            row = registry.get_spec(rcon, sid, ver)
            if "error" in row:
                notes.append(f"{sid} {ver}: not in registry")
                continue
            if row["status"] not in ACTIVE:
                notes.append(f"{sid} {ver}: status {row['status']}, not judged")
                continue
            res = verdict.evaluate_registered(rcon, jcon, sid, ver, acct)
            if "error" in res:
                notes.append(f"{sid} {ver}: {res['error']}")
                continue
            skipped = [w for w in res.get("warnings", []) if "skipped execution" in w]
            if skipped:
                findings.append(_finding(
                    "fills_unpairable", acct, sid,
                    f"{len(skipped)} fills have no usable action, so trades cannot be rebuilt; "
                    "performance and behavior checks are unreliable",
                    {"examples": skipped[:3]}))
            if res.get("edge_decayed") or res.get("verdict") == "reject":
                findings.append(_finding(
                    "performance_drift", acct, sid,
                    "sim results diverge from the backtest: " + "; ".join(res["reasons"])[:300],
                    {"verdict": res["verdict"], "edge_decayed": res.get("edge_decayed"), "reasons": res["reasons"],
                     "sample": res["sample"], "recommendation_only": res.get("next_step")}))
            tpy = res.get("expected", {}).get("trades_per_year")
            if tpy and res.get("since") and not skipped:
                since = datetime.fromisoformat(res["since"].replace("Z", "+00:00"))
                pf = sup_behavior.pace_finding(len(res["trades"]), sup_behavior.trading_days(since, now),
                                               tpy["value"] / 252.0)
                if pf:
                    findings.append(_finding("trade_pace", acct, sid, pf["note"], pf))
            if not skipped:
                hf = sup_behavior.holding_finding([t.get("duration_sec") for t in res.get("trades", [])],
                                                  _ref_durations(rcon, sid, ver))
                if hf:
                    findings.append(_finding("holding_time_shift", acct, sid, hf["note"], hf))
        return {"findings": findings, "notes": notes, "journal_new_fills": synced}
    finally:
        if own_j:
            jcon.close()
        if own_r:
            rcon.close()
