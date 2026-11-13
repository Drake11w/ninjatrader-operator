"""Drive NinjaTrader's Strategy Analyzer from a handoff bundle and collect the trades.

Default: ONE run per leg over the whole date range on the current front-month contract (for example `MNQ 12-26`).
NinjaTrader merges the earlier contracts into it through its merge policy, so the history reaches back to 2019. Checked
2026-11-06: the same 2026-06-03..2026-10-22 run on `MNQ 12-26` and on `MNQ ##-##` both gave 26 trades, net -151.
The continuous `MNQ ##-##` instrument itself returned empty results through the add-on in most runs, so it is not used.
`--stitch` runs one front-month contract at a time instead (prior-day filters then lack history at the start of every
window, so it is only a fallback).

Needs: NinjaTrader running with the add-on built from this repo (POST /backtest/run, GET /backtest/job) and ONE Strategy
Analyzer window open and idle. Costs: slippage is applied by NinjaTrader; commission is applied here afterwards
(commission_per_side x 2 x quantity) because no matching commission template exists in NinjaTrader.

    python backtest_runner.py <bundle dir> [--start 2020-01-02 --end 2026-10-16] [--out runs] [--stitch] [--import]
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import sessions

ADDON = os.environ.get("NT_ADDON_URL", "http://127.0.0.1:8765")
POLL_SECONDS = 5
WINDOW_TIMEOUT = 25 * 60


def windows(start: date, end: date, root: str = "MNQ") -> list:
    """Consecutive calendar-day windows [start, end] in which one contract is the front month (sessions.front_contract)."""
    out, d = [], start
    while d <= end:
        c = sessions.front_contract(d)["contract"]
        if out and out[-1]["contract"] == c:
            out[-1]["end"] = d
        else:
            out.append({"contract": c, "instrument": f"{root} {c}", "start": d, "end": d})
        d += timedelta(days=1)
    return out


def _call(path: str, params: dict = None, body: str = None) -> dict:
    url = ADDON + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, data=body.encode() if body is not None else None,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:  # add-on down or NinjaTrader closed mid-run
        return {"ok": False, "error": f"NinjaTrader add-on unreachable or bad reply: {e}"}


def _body(inputs: dict) -> str:
    return "\n".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in inputs.items())


def run_window(cls: str, inputs: dict, w: dict, slippage, bar: int = 1, call=_call, sleep=time.sleep) -> dict:
    """One Analyzer run. Returns {ok, trades, ...} or {ok: False, error}."""
    q = {"class": cls, "instrument": w["instrument"], "start": w["start"].isoformat(), "end": w["end"].isoformat(),
         "slippage": slippage, "bar": bar}
    r = call("/backtest/run", q, _body(inputs))
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", str(r))}
    job, t0 = r["job"], time.time()
    while True:
        sleep(POLL_SECONDS)
        j = call("/backtest/job", {"id": job, "trades": 1})
        if j.get("state") == "done":
            return {"ok": True, "trades": j.get("trades", []), "seconds": j.get("seconds"), "info": j.get("info", "")}
        if j.get("state") == "failed" or not j.get("ok", True):
            return {"ok": False, "error": j.get("error") or str(j)}
        if time.time() - t0 > WINDOW_TIMEOUT:
            return {"ok": False, "error": f"window run exceeded {WINDOW_TIMEOUT}s: stop and check the Analyzer window"}


def in_window(trade: dict, w: dict) -> bool:
    d = trade["entry_time"][:10]
    return w["start"].isoformat() <= d <= w["end"].isoformat()


def run_leg(spec: dict, leg: dict, wins: list, cache_dir: str, run=run_window, log=print) -> dict:
    """All windows for one leg; each finished window is cached on disk so an interrupted run resumes."""
    os.makedirs(cache_dir, exist_ok=True)
    trades, empty, failed = [], [], None
    for w in wins:
        key = os.path.join(cache_dir, f"{spec['id']}__{leg['leg']}__{w['contract']}__{w['start']}__{w['end']}.json")
        if os.path.isfile(key):
            with open(key, encoding="utf-8") as f:
                res = json.load(f)
        else:
            res = run(leg["class"], leg["inputs"], w, spec["costs"]["slippage_ticks"])
            if not res["ok"]:
                failed = {"window": w["instrument"], "start": str(w["start"]), "end": str(w["end"]), "error": res["error"]}
                log(f"  {leg['leg']} {w['instrument']} FAILED: {res['error']}")
                break
            with open(key, "w", encoding="utf-8") as f:
                json.dump(res, f)
        got = [t for t in res["trades"] if in_window(t, w)]
        for t in got:
            t["leg"] = leg["leg"]
            t["instrument"] = w["instrument"]
        if not got:
            empty.append(w["instrument"] + " " + str(w["start"]))
        trades += got
        log(f"  {leg['leg']} {w['instrument']} {w['start']}..{w['end']}: {len(got)} trades")
    return {"trades": trades, "empty_windows": empty, "failed": failed}


def to_trades_csv(trades: list, commission_per_side: float) -> str:
    """NinjaTrader Trades-grid shaped CSV (what backtest.parse reads). Profit is net of the commission applied here."""
    import io
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["Trade #", "Instrument", "Strategy", "Market pos.", "Qty", "Entry price", "Exit price", "Entry time", "Exit time",
                 "Entry name", "Exit name", "Profit", "Cum. net profit", "Commission"])
    cum = 0.0
    for i, t in enumerate(sorted(trades, key=lambda x: x["entry_time"]), 1):
        comm = round(commission_per_side * 2 * t["qty"], 2)
        net = round(t["profit"] - comm, 2)
        cum = round(cum + net, 2)
        f = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S").strftime("%m/%d/%Y %I:%M:%S %p")
        wr.writerow([i, t.get("instrument", ""), t.get("leg", ""), "Long" if t["side"] == "Long" else "Short", t["qty"],
                     t["entry_price"], t["exit_price"], f(t["entry_time"]), f(t["exit_time"]),
                     t["entry_name"], t["exit_name"], net, cum, comm])
    return buf.getvalue()


def collect_csv(spec: dict, legs: list, out_dir: str, log=print, run=run_window) -> dict:
    """One merged run per leg over the spec's backtest range (the default `main` path), as {ok, csv, summary, error}.
    Used by promote_strategy. `out_dir` is a scratch folder; partial window results from an earlier interrupted
    run are discarded first so an interrupted backtest restarts from its start."""
    import shutil
    start, end = date.fromisoformat(spec["backtest"]["start"]), date.fromisoformat(spec["backtest"]["end"])
    last = windows(start, end, spec["instrument"]["root"])[-1]
    wins = [{"contract": last["contract"], "instrument": last["instrument"], "start": start, "end": end}]
    cache = os.path.join(out_dir, "cache")
    shutil.rmtree(cache, ignore_errors=True)
    trades, summary = [], {}
    for leg in legs:
        res = run_leg(spec, leg, wins, cache, run=run, log=log)
        summary[leg["leg"]] = {"trades": len(res["trades"]), "empty_windows": res["empty_windows"],
                               "failed": res["failed"]}
        if res["failed"]:
            return {"ok": False, "error": f"leg {leg['leg']}: {res['failed']['error']}", "summary": summary}
        trades += res["trades"]
    return {"ok": True, "csv": to_csv_for(trades, spec), "summary": summary}


def to_csv_for(trades: list, spec: dict) -> str:
    return to_trades_csv(trades, spec["costs"]["commission_per_side"])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--out", default="runs")
    ap.add_argument("--stitch", action="store_true", help="one front-month contract per window instead of one merged run")
    ap.add_argument("--import", dest="do_import", action="store_true")
    a = ap.parse_args(argv)
    import handoff
    r = handoff.inspect_bundle(a.bundle)
    if not r["ok"]:
        sys.exit("bundle: " + "; ".join(r["errors"]))
    spec = r["spec"]
    start = date.fromisoformat(a.start or spec["backtest"]["start"])
    end = date.fromisoformat(a.end or spec["backtest"]["end"])
    root = spec["instrument"]["root"]
    wins = windows(start, end, root)
    if not a.stitch:   # one merged run on the contract that is front month at the end of the range
        last = wins[-1]
        wins = [{"contract": last["contract"], "instrument": last["instrument"], "start": start, "end": end}]
    print(f"{spec['id']} {spec['version']}: {len(wins)} run(s) per leg x {len(r['legs'])} legs on " + ", ".join(w["instrument"] for w in wins[:3]))
    cache = os.path.join(a.out, "cache")
    all_trades, summary = [], {"spec": spec["id"], "version": spec["version"], "start": str(start), "end": str(end), "legs": {}}
    for leg in r["legs"]:
        res = run_leg(spec, leg, wins, cache)
        summary["legs"][leg["leg"]] = {"trades": len(res["trades"]), "empty_windows": res["empty_windows"], "failed": res["failed"]}
        if res["failed"]:
            print(json.dumps(summary, indent=1))
            sys.exit(2)
        all_trades += res["trades"]
    text = to_trades_csv(all_trades, spec["costs"]["commission_per_side"])
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f"{spec['id']}__{spec['version']}__trades.csv")
    open(path, "w", encoding="utf-8", newline="").write(text)
    summary["csv"] = path
    if a.stitch:
        summary["warmup_missing"] = [w["instrument"] + " " + str(w["start"]) for w in wins]
        summary["note"] = "stitched from per-contract runs; prior-day filters lack history at the start of each window"
    else:
        summary["note"] = f"one merged run on {wins[0]['instrument']} (NinjaTrader merge policy); commission applied after the run"
    if a.do_import:
        import backtest
        import registry
        con = registry.connect()
        try:
            label = "NinjaTrader Analyzer, " + ("per-contract stitched" if a.stitch else f"one merged run on {wins[0]['instrument']}")
            res = backtest.import_run(con, spec, text, label=label, source_name=path)
        finally:
            con.close()
        summary["import"] = {k: res.get(k) for k in ("ok", "run_id", "error", "warnings")}
        summary["claims_check"] = res.get("claims_check")
        summary["metrics"] = res.get("metrics")
    print(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
