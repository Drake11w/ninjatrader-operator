"""Backtest ingestion: NinjaTrader Strategy Analyzer CSV exports -> stored runs, checked against a spec's claims.

Two export shapes are understood (detected from the header, column names matched case-insensitively):
  * Trades grid  (needs "Entry time", "Exit time", "Profit"; uses Market pos., Qty, prices, names, Commission,
                  Cum. net profit when present)
  * Periods grid with Period = Day (needs "Period", "Net profit")
Numbers are read in US format ("$1,234.50", "($12.00)"). Non-US decimal commas are not supported. Timestamps are
read in `export_timezone` (the time zone of the PC / chart that produced the export), default the spec's.

This was written from the column names NinjaTrader documents and the Lucid repo's compare script, not from a real
export; the first real file may need a small adjustment. parse() reports what it assumed in `warnings`.
"""
import csv
import io
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone

import trades as trades_mod

SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_id TEXT NOT NULL,
    version TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    export_timezone TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    start TEXT, end TEXT,
    metrics_json TEXT NOT NULL,
    daily_json TEXT NOT NULL,
    trades_json TEXT
);
"""

DT_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y",
              "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y%m%d %H%M%S", "%Y%m%d")
TOLERANCE = 0.15  # claims within 15% of the measured value count as matching


def ensure(con):
    con.executescript(SCHEMA)


def _num(s):
    if s is None:
        return None
    t = str(s).strip().replace("$", "").replace(",", "").replace("%", "")
    if not t or t in ("-", "N/A", "n/a"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _dt(s):
    t = str(s or "").strip()
    for f in DT_FORMATS:
        try:
            return datetime.strptime(t, f)
        except ValueError:
            pass
    return None


def _tz(name):
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


def _read(text: str):
    text = text.lstrip("﻿")
    head = text.splitlines()[0] if text.strip() else ""
    delim = max((",", ";", "\t"), key=head.count)
    rdr = csv.DictReader(io.StringIO(text), delimiter=delim)
    rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in rdr]
    return [(k or "").strip().lower() for k in (rdr.fieldnames or [])], rows


def parse(text: str, export_tz: str) -> dict:
    """-> {kind, trades, daily:{YYYY-MM-DD: pnl}, warnings} or {error}."""
    fields, rows = _read(text)
    if not rows:
        return {"error": "no data rows found in the CSV"}
    tz = _tz(export_tz)
    warnings = []
    if {"entry time", "exit time", "profit"} <= set(fields):
        out, bad = [], 0
        for r in rows:
            en, ex, pnl = _dt(r["entry time"]), _dt(r["exit time"]), _num(r["profit"])
            if en is None or ex is None or pnl is None:
                bad += 1
                continue
            comm = _num(r.get("commission")) or 0.0
            en_u = en.replace(tzinfo=tz).astimezone(timezone.utc)
            ex_u = ex.replace(tzinfo=tz).astimezone(timezone.utc)
            pos = r.get("market pos.", r.get("market pos", "")).lower()
            out.append({"entry_time": en_u.strftime("%Y-%m-%dT%H:%M:%SZ"), "exit_time": ex_u.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "local_exit_date": ex.date().isoformat(), "side": "short" if pos.startswith("s") else "long",
                        "quantity": int(_num(r.get("qty")) or 1), "instrument": r.get("instrument", ""),
                        "entry_name": r.get("entry name", ""), "exit_name": r.get("exit name", ""),
                        "duration_sec": max(0, int((ex_u - en_u).total_seconds())),
                        "commission": comm, "profit_raw": pnl, "net_pnl": pnl})
        if bad:
            warnings.append(f"{bad} row(s) skipped: unreadable time or profit (totals rows are expected here)")
        if not out:
            return {"error": "no readable trades; check the time format (US month/day/year expected)"}
        # Is "Profit" already net of commission? Use the cumulative column to find out.
        cum = [_num(r.get("cum. net profit")) for r in rows]
        cum = [c for c in cum if c is not None]
        total_raw, total_comm = sum(t["profit_raw"] for t in out), sum(t["commission"] for t in out)
        if cum and total_comm:
            if abs(cum[-1] - total_raw) <= abs(cum[-1] - (total_raw - total_comm)):
                warnings.append("Profit column matches Cum. net profit, so it is treated as already net of commission")
            else:
                for t in out:
                    t["net_pnl"] = round(t["profit_raw"] - t["commission"], 2)
                warnings.append("Cum. net profit matches Profit minus Commission, so commission was subtracted")
        elif total_comm:
            warnings.append("no Cum. net profit column to check against: Profit is assumed to be net of commission")
        daily = defaultdict(float)
        for t in out:
            daily[t["local_exit_date"]] += t["net_pnl"]
        warnings.append(f"timestamps read as {export_tz}; daily buckets use the exit date as exported")
        return {"kind": "trades", "trades": sorted(out, key=lambda t: t["entry_time"]),
                "daily": {d: round(v, 2) for d, v in sorted(daily.items())}, "warnings": warnings}
    if {"period", "net profit"} <= set(fields):
        daily, bad = defaultdict(float), 0
        for r in rows:
            d, v = _dt(r["period"]), _num(r["net profit"])
            if d is None or v is None:
                bad += 1
                continue
            if d.hour or d.minute:
                bad += 1
                continue
            daily[d.date().isoformat()] += v
        if not daily:
            return {"error": "no readable Period rows: export the Periods grid with Period = Day"}
        if bad:
            warnings.append(f"{bad} row(s) skipped (unreadable or intraday periods)")
        warnings.append("Periods export has no individual trades: trade-level metrics and leg filters are unavailable")
        return {"kind": "periods", "trades": [], "daily": {d: round(v, 2) for d, v in sorted(daily.items())},
                "warnings": warnings}
    return {"error": "unrecognised CSV. Expected the Strategy Analyzer Trades grid (Entry time, Exit time, Profit, ...) "
                     "or the Periods grid with Period = Day (Period, Net profit). Found columns: " + ", ".join(fields)}


def _eod_drawdown(daily: dict) -> float:
    peak = eq = dd = 0.0
    for _, v in sorted(daily.items()):
        eq += v
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return round(dd, 2)


def metrics(parsed: dict) -> dict:
    daily = parsed["daily"]
    days = sorted(daily)
    start, end = days[0], days[-1]
    years = max((datetime.fromisoformat(end) - datetime.fromisoformat(start)).days + 1, 1) / 365.25
    vals = list(daily.values())
    net = round(sum(vals), 2)
    m = {"start": start, "end": end, "years": round(years, 2), "days_with_pnl": len(days), "net_pnl": net,
         "net_per_year": round(net / years, 2), "max_drawdown": _eod_drawdown(daily),
         "win_day_rate": round(sum(1 for v in vals if v > 0) / len(vals), 4),
         "best_day": round(max(vals), 2), "worst_day": round(min(vals), 2)}
    m["profit_to_drawdown"] = round(net / m["max_drawdown"], 2) if m["max_drawdown"] else None
    if len(vals) > 1:
        mean = net / len(vals)
        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))
        m["daily_sharpe"] = round(mean / sd * math.sqrt(252), 3) if sd else None
    if parsed["trades"]:
        p = trades_mod.performance(parsed["trades"], "America/Chicago")
        for k in ("trades", "win_rate", "expectancy", "profit_factor", "avg_win", "avg_loss"):
            m[k] = p.get(k)
        m["trades_per_year"] = round(p["trades"] / years, 1)
        m["trade_warnings"] = p["warnings"]
    return m


def claims_check(m: dict, spec: dict) -> dict:
    """Compare measured metrics to the spec's expected_metrics; only keys present in both are compared."""
    claims, rows = spec.get("expected_metrics", {}), {}
    for k, claimed in claims.items():
        actual = m.get("trades" if k == "min_trades" else k)
        if actual is None or not isinstance(claimed, (int, float)):
            continue
        if k == "min_trades":
            ok = actual >= claimed * (1 - TOLERANCE)
        else:
            ok = abs(actual - claimed) <= TOLERANCE * max(abs(claimed), 1e-9)
        rows[k] = {"claimed": claimed, "actual": actual, "status": "ok" if ok else "differs"}
    skipped = [k for k in claims if k not in rows]
    out = {"tolerance": TOLERANCE, "metrics": rows,
           "overall": "no comparable metrics" if not rows else
           "matches" if all(r["status"] == "ok" for r in rows.values()) else "differs"}
    if skipped:
        out["not_comparable"] = skipped  # e.g. eval odds need a simulation, not a CSV
    return out


def import_run(con, spec: dict, text: str, label: str = "", source_name: str = "", export_tz: str = "") -> dict:
    ensure(con)
    export_tz = export_tz or spec.get("timezone", "America/Chicago")
    try:
        parsed = parse(text, export_tz)
    except Exception as e:  # unknown time zone name etc.
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if "error" in parsed:
        return {"ok": False, "error": parsed["error"]}
    m = metrics(parsed)
    cur = con.execute(
        "INSERT INTO backtest_runs (spec_id, version, label, kind, source_name, export_timezone, start, end,"
        " metrics_json, daily_json, trades_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (spec["id"], spec["version"], label, parsed["kind"], source_name, export_tz, m["start"], m["end"],
         json.dumps(m), json.dumps(parsed["daily"]), json.dumps(parsed["trades"]) if parsed["trades"] else None))
    con.commit()
    return {"ok": True, "run_id": cur.lastrowid, "kind": parsed["kind"], "metrics": m,
            "claims_check": claims_check(m, spec), "warnings": parsed["warnings"]}


def get_run(con, run_id: int):
    ensure(con)
    r = con.execute("SELECT * FROM backtest_runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(r) if r else None


def list_runs(con, spec_id: str = "", version: str = "") -> list:
    ensure(con)
    q, args = "SELECT run_id, spec_id, version, label, kind, source_name, imported_at, start, end, metrics_json " \
              "FROM backtest_runs WHERE 1=1", []
    for col, val in (("spec_id", spec_id), ("version", version)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    out = []
    for r in con.execute(q + " ORDER BY run_id DESC", args):
        d = dict(r)
        m = json.loads(d.pop("metrics_json"))
        d["summary"] = {k: m.get(k) for k in ("trades", "net_pnl", "net_per_year", "max_drawdown", "win_rate")}
        out.append(d)
    return out


def compare_to_journal(run: dict, spec: dict, journal_trades: list, leg: str = "", max_days: int = 20) -> dict:
    """Day-by-day: backtest P&L vs journaled (sim/live) P&L over the days the journal covers."""
    daily = json.loads(run["daily_json"])
    bt_trades = json.loads(run["trades_json"]) if run["trades_json"] else []
    warnings = []
    if leg:
        leg_obj = next((l for l in spec.get("legs", []) if l["name"] == leg), None)
        names = (leg_obj or {}).get("ninjascript", {}).get("order_names", [])
        if not leg_obj:
            return {"error": f"spec has no leg named {leg!r}"}
        if run["kind"] != "trades" or not names:
            return {"error": "leg filtering needs a Trades-grid import and ninjascript.order_names on that leg"}
        daily = defaultdict(float)
        for t in bt_trades:
            if t["entry_name"] in names:
                daily[t["local_exit_date"]] += t["net_pnl"]
        daily = dict(daily)
    tz = _tz(run["export_timezone"])
    live = defaultdict(float)
    for t in journal_trades:
        d = trades_mod._parse(t["exit_time"]).astimezone(tz).date().isoformat()
        live[d] += t["net_pnl"]
    if not live:
        return {"error": "no journaled trades for this strategy (sync_journal first, and register/advance the spec "
                         "so fills get tagged)"}
    first, last = min(live), max(live)
    if run["end"] < last:
        warnings.append(f"the backtest ends {run['end']}, before the last live trade {last}: re-run the Analyzer "
                        f"over the live period to compare those days")
    if run["start"] > first:
        warnings.append(f"the backtest starts {run['start']}, after the first live trade {first}")
    lo, hi = max(first, run["start"]), min(last, run["end"])
    if lo > hi:
        return {"error": "the backtest and the journal do not overlap in time", "warnings": warnings}
    days = sorted({d for d in list(daily) + list(live) if lo <= d <= hi})
    both = [d for d in days if d in daily and d in live]
    only_bt = [d for d in days if d in daily and d not in live]
    only_live = [d for d in days if d in live and d not in daily]
    diffs = sorted(({"date": d, "backtest": round(daily.get(d, 0.0), 2), "live": round(live.get(d, 0.0), 2),
                     "diff": round(live.get(d, 0.0) - daily.get(d, 0.0), 2)} for d in days),
                   key=lambda r: -abs(r["diff"]))
    bt_sum, lv_sum = sum(daily.get(d, 0.0) for d in days), sum(live.get(d, 0.0) for d in days)
    same_sign = sum(1 for d in both if (daily[d] > 0) == (live[d] > 0))
    if len(days) < 20:
        warnings.append(f"only {len(days)} overlapping days: drift numbers are not reliable yet")
    return {"overlap": {"start": lo, "end": hi, "days_compared": len(days)},
            "days_both_traded": len(both), "days_only_in_backtest": only_bt[:max_days],
            "days_only_live": only_live[:max_days], "n_only_in_backtest": len(only_bt), "n_only_live": len(only_live),
            "same_direction_rate": round(same_sign / len(both), 3) if both else None,
            "backtest_net": round(bt_sum, 2), "live_net": round(lv_sum, 2), "live_minus_backtest": round(lv_sum - bt_sum, 2),
            "biggest_differences": diffs[:max_days], "warnings": warnings}
