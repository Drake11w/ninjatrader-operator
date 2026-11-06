"""Prop-firm rule tracker: distance to breach for daily loss, trailing drawdown, profit target and consistency.

Works on CLOSED trades from the journal only. Open-position P&L is not included (pass `unrealized` to add it),
so an intraday breach caused by a floating loss will not show until the trade closes. Rule values come from a
config file; nothing is assumed. See rules.example.json (placeholder numbers, replace with your firm's rules).
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

DEFAULT_RULES = os.path.join(os.path.expanduser("~"), ".ninjatrader-operator", "rules.json")


def load_rules(path: str = "") -> dict:
    path = path or os.environ.get("NT_RULES_FILE", DEFAULT_RULES)
    if not os.path.exists(path):
        raise FileNotFoundError(f"no rules file at {path}; copy rules.example.json there and fill in your firm's values")
    with open(path, encoding="utf-8") as f:
        rules = json.load(f)
    if "starting_balance" not in rules:
        raise ValueError("rules file needs at least starting_balance")
    return rules


def trading_day(ts: str, tz, session_start_hour: int) -> str:
    """Futures day label: fills at/after session_start_hour local time belong to the next calendar date."""
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if tz:
        d = d.astimezone(tz)
    return (d + timedelta(hours=(24 - session_start_hour) % 24)).date().isoformat()


def _status(used: float, limit: float, warn_at: float) -> str:
    if limit <= 0:
        return "n/a"
    return "breach" if used >= limit else "warning" if used >= warn_at * limit else "ok"


def evaluate(trades: list, rules: dict, now: datetime = None, unrealized: float = 0.0) -> dict:
    tz, tz_name, warnings = None, "UTC", []
    tz_name_cfg = rules.get("timezone", "America/New_York")
    try:
        from zoneinfo import ZoneInfo
        tz, tz_name = ZoneInfo(tz_name_cfg), tz_name_cfg
    except Exception:
        warnings.append(f"timezone {tz_name_cfg!r} unavailable (pip install tzdata); using UTC day boundaries")
    start_hr = rules.get("session_start_hour", 18)
    warn_at = rules.get("warn_at", 0.8)
    start = float(rules["starting_balance"])
    now = now or datetime.now(timezone.utc)
    today = trading_day(now.isoformat(), tz, start_hr)

    by_day = defaultdict(float)
    ordered = sorted(trades, key=lambda t: t["exit_time"])
    for t in ordered:
        by_day[trading_day(t["exit_time"], tz, start_hr)] += t["net_pnl"]
    total = sum(by_day.values())
    balance = start + total
    out = {"as_of": now.isoformat(), "trading_day": today, "timezone": tz_name, "balance_closed": round(balance, 2),
           "total_pnl": round(total, 2), "pnl_today": round(by_day.get(today, 0.0), 2), "rules": {}}
    if unrealized:
        out["unrealized_included"] = unrealized

    limit = rules.get("daily_loss_limit")
    if limit:
        loss = max(0.0, -(by_day.get(today, 0.0) + unrealized))
        out["rules"]["daily_loss_limit"] = {
            "limit": limit, "loss_today": round(loss, 2), "distance": round(limit - loss, 2),
            "status": _status(loss, limit, warn_at)}

    dd = rules.get("trailing_drawdown")
    if dd:
        amount, mode = dd["amount"], dd.get("mode", "eod")
        peak, running = start, start
        if mode == "intraday":
            for t in ordered:
                running += t["net_pnl"]
                peak = max(peak, running)
        else:  # end-of-day high-water mark; today's open day does not move the floor yet
            for day in sorted(d for d in by_day if d < today):
                running += by_day[day]
                peak = max(peak, running)
        floor = peak - amount
        if dd.get("lock_at_start", True):
            floor = min(floor, start)
        current = balance + unrealized
        out["rules"]["trailing_drawdown"] = {
            "mode": mode, "amount": amount, "peak_balance": round(peak, 2), "floor": round(floor, 2),
            "balance": round(current, 2), "distance": round(current - floor, 2),
            "status": _status(max(0.0, peak - current), peak - floor, warn_at)}

    target = rules.get("profit_target")
    if target:
        out["rules"]["profit_target"] = {"target": target, "progress": round(total, 2),
                                         "remaining": round(max(0.0, target - total), 2), "reached": total >= target}

    pct = rules.get("consistency_pct")
    if pct:
        best_day, best = (max(by_day.items(), key=lambda kv: kv[1]) if by_day else (None, 0.0))
        ratio = best / total if total > 0 else None
        c = {"max_share_of_profit": pct, "best_day": best_day, "best_day_pnl": round(best, 2),
             "share": round(ratio, 4) if ratio is not None else None}
        if total <= 0 or best <= 0:
            c["status"] = "n/a"
        else:
            c["profit_needed_to_comply"] = round(max(0.0, best / pct - total), 2)
            c["status"] = "breach" if ratio > pct else "warning" if ratio >= warn_at * pct else "ok"
        out["rules"]["consistency"] = c

    statuses = [r["status"] for r in out["rules"].values() if "status" in r]
    out["overall"] = "breach" if "breach" in statuses else "warning" if "warning" in statuses else "ok"
    if not trades:
        warnings.append("no closed trades in the journal; run sync_journal first")
    out["warnings"] = warnings
    return out
