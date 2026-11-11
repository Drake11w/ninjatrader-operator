"""'Ready for live-small' readiness block. Pure: no I/O, no clock, no database, no network.

One implementation shared by the feedback export and (later) the nudge. Readiness is the verdict engine's clean
`promote` plus a few extra conditions; there is no second criteria set. Each criterion is reported pass / fail /
pending with progress, so an unready version always says why. This module never changes a status and never nudges.
"""
from datetime import datetime

# Global floors. A spec may only make these stricter (see thresholds()).
DEFAULTS = {"min_trades": 30, "min_days": 20, "min_weeks": 4, "freq_low": 0.5, "freq_high": 1.5}
STRICTER = {"min_trades": max, "min_days": max, "min_weeks": max, "freq_low": max, "freq_high": min}


def thresholds(overrides: dict = None) -> tuple:
    """Defaults tightened by `overrides`; a looser value is ignored and reported. Returns (thresholds, notes)."""
    t, notes = dict(DEFAULTS), []
    for k, v in (overrides or {}).items():
        if k not in DEFAULTS or not isinstance(v, (int, float)) or isinstance(v, bool):
            notes.append(f"override {k!r} ignored: not a readiness threshold")
            continue
        picked = STRICTER[k](t[k], v)
        if picked != v:
            notes.append(f"override {k}={v} ignored: criteria can only be made stricter than {t[k]}")
        t[k] = picked
    return t, notes


def _c(key, status, actual=None, threshold=None, progress=None, note=""):
    return {"key": key, "status": status, "actual": actual, "threshold": threshold, "progress": progress,
            "note": note}


def _floor(key, actual, floor):
    return _c(key, "pass" if actual >= floor else "pending", actual, floor, f"{actual}/{floor}")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def evaluate(verdict_result: dict, status: str, since: str, now: str, backtest: dict = None,
             untagged_fills: int = 0, overrides: dict = None) -> dict:
    """Build the readiness block for one spec version.

    verdict_result: verdict.evaluate() output (sample, checks, verdict, edge_decayed, expected, reasons, warnings).
    status:         registry status of the version.
    since / now:    ISO times; the Sim-start boundary and the evaluation time (injected, never read from a clock).
    backtest:       {"present": bool, "claims": "matches" | "differs" | "no comparable metrics" | None} or None.
    untagged_fills: fills in the period on the version's accounts that carry no strategy tag.
    """
    th, notes = thresholds(overrides)
    sample = verdict_result.get("sample", {})
    n, days = sample.get("trades", 0), sample.get("trading_days", 0)
    v = verdict_result.get("verdict")
    checks = {c["check"]: c for c in verdict_result.get("checks", [])}
    out = []

    out.append(_c("in_sim", "pass" if status == "sim" else "fail", status, "sim",
                  note="" if status == "sim" else f"version is '{status}', not in sim"))

    bt = backtest or {}
    if not bt.get("present"):
        out.append(_c("baseline", "fail", "missing", "imported and checked", note="not ready: missing baseline"))
    elif bt.get("claims") == "differs":
        out.append(_c("baseline", "fail", "differs", "matches spec claims",
                      note="the backtest does not reproduce the spec's expected_metrics"))
    else:
        out.append(_c("baseline", "pass", bt.get("claims") or "present", "imported and checked"))

    out.append(_floor("sample_trades", n, th["min_trades"]))
    out.append(_floor("sample_days", days, th["min_days"]))
    weeks = 0
    if since:
        weeks = max(0, (_parse(now) - _parse(since)).days // 7)
    out.append(_floor("sample_weeks", weeks, th["min_weeks"]))

    hard_fail = any(c["status"] == "fail" and not c["check"].startswith(("sample_",)) for c in checks.values())
    if v == "promote":
        out.append(_c("verdict", "pass", v, "promote"))
    elif v == "reject":
        out.append(_c("verdict", "fail", v, "promote", note="; ".join(verdict_result.get("reasons", []))))
    else:
        out.append(_c("verdict", "fail" if hard_fail else "pending", v, "promote",
                      note="; ".join(verdict_result.get("reasons", []))))

    decay = [c for k, c in checks.items() if k.startswith("edge_decay")]
    if verdict_result.get("edge_decayed") or any(c["status"] == "fail" for c in decay):
        out.append(_c("no_edge_decay", "fail", "decayed", "no decay flag"))
    elif decay and all(c["status"] == "pass" for c in decay):
        out.append(_c("no_edge_decay", "pass", "none", "no decay flag"))
    else:
        out.append(_c("no_edge_decay", "pending", None, "no decay flag",
                      note="too few Sim trades, or no measured backtest, to test decay"))

    ec = checks.get("expectancy_vs_claim")
    if ec and ec["status"] in ("pass", "fail"):
        out.append(_c("expectancy_vs_claim", ec["status"], ec["actual"], ec["threshold"]))
    else:
        out.append(_c("expectancy_vs_claim", "pending", None, None, note="no sim trades or no claimed expectancy"))

    claimed = (verdict_result.get("expected", {}).get("trades_per_year") or {}).get("value")
    if not claimed:
        out.append(_c("trade_frequency", "pending", None, [th["freq_low"], th["freq_high"]],
                      note="no claimed trades per year to compare"))
    elif days < 5:
        out.append(_c("trade_frequency", "pending", None, [th["freq_low"], th["freq_high"]],
                      progress=f"{days}/5 days", note="need 5 trading days to estimate a pace"))
    else:
        ratio = round(n / days * 252 / claimed, 2)
        ok = th["freq_low"] <= ratio <= th["freq_high"]
        out.append(_c("trade_frequency", "pass" if ok else "fail", ratio, [th["freq_low"], th["freq_high"]],
                      note=f"x the claimed {claimed:g} trades/year"))

    out.append(_c("untagged_fills", "pass" if untagged_fills == 0 else "fail", untagged_fills, 0))

    statuses = [c["status"] for c in out]
    if v == "reject":
        state, ready = "rejected", False
    elif all(s == "pass" for s in statuses) and v == "promote":
        state, ready = "ready", True
    else:
        state, ready = "not_ready", False
    waiting = [f"{c['key']} {c['progress']}" if c["progress"] else c["key"] for c in out if c["status"] != "pass"]
    summary = "ready" if ready else ("rejected: suggest retiring" if state == "rejected"
                                      else "not ready: " + ", ".join(waiting))
    return {"state": state, "ready": ready, "summary": summary, "criteria": out, "notes": notes,
            "suggested_action": ("suggest rejecting or retiring this version (suggestion only; status is unchanged)"
                                 if state == "rejected" else None)}
