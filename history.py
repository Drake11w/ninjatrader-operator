"""Download historical bars from NinjaTrader (add-on GET /history, which uses NinjaTrader's BarsRequest) to CSV.

Files go to ~/Desktop/Market Data (override with NT_HISTORY_DIR), one per instrument/period, for example
`MNQ 12-26_Minute1.csv` with columns time,open,high,low,close,volume. Times are NinjaTrader local time (bar close).
Re-running merges into the existing file (same timestamp = replaced), so it doubles as an incremental update.
The requests are chunked by date so one reply never gets huge. Read-only toward NinjaTrader.

    python history.py "MNQ 12-26" --start 2024-01-01 --end 2026-10-16 [--type Minute] [--value 1] [--out DIR]
"""
import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

ADDON = os.environ.get("NT_ADDON_URL", "http://127.0.0.1:8765")
TYPES = ("Tick", "Second", "Minute", "Day")
CHUNK_DAYS = {"Tick": 1, "Second": 7, "Minute": 30, "Day": 1825}
HEADER = ["time", "open", "high", "low", "close", "volume"]


def history_dir() -> str:
    return os.environ.get("NT_HISTORY_DIR") or os.path.join(os.path.expanduser("~"), "Desktop", "Market Data")


def file_path(instrument: str, type_: str, value: int, out: str = "") -> str:
    safe = re.sub(r"[^A-Za-z0-9 _.\-]", "_", instrument)
    return os.path.join(out or history_dir(), f"{safe}_{type_}{value}.csv")


def chunks(start: date, end: date, days: int) -> list:
    out, a = [], start
    while a <= end:
        b = min(a + timedelta(days=days - 1), end)
        out.append((a, b))
        a = b + timedelta(days=1)
    return out


def _call(params: dict) -> dict:
    url = f"{ADDON}/history?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except ValueError:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"ok": False, "error": f"NinjaTrader add-on unreachable or bad reply: {e}"}


def _load(path: str) -> dict:
    rows = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.reader(f):
                if r and r[0] != "time":
                    rows[r[0]] = r
    return rows


def download(instrument: str, start: date, end: date, type_: str = "Minute", value: int = 1, out: str = "",
             call=_call, log=lambda *_: None) -> dict:
    """Fetch [start, end] and merge into the CSV. Returns {ok, path, bars_fetched, total_rows, first, last} or {ok: False, error}."""
    if type_ not in TYPES:
        return {"ok": False, "error": f"type must be one of {', '.join(TYPES)}"}
    if value < 1:
        return {"ok": False, "error": "value must be >= 1"}
    if end < start:
        return {"ok": False, "error": "end is before start"}
    path = file_path(instrument, type_, value, out)
    rows, fetched = _load(path), 0
    for a, b in chunks(start, end, CHUNK_DAYS[type_]):
        r = call({"instrument": instrument, "type": type_, "value": value, "from": a.isoformat(), "to": b.isoformat()})
        if not r.get("ok"):
            _save(path, rows)  # keep what we already have
            return {"ok": False, "error": f"{a}..{b}: {r.get('error', r)}", "path": path, "bars_fetched": fetched}
        if r.get("truncated"):
            _save(path, rows)
            return {"ok": False, "error": f"{a}..{b}: reply truncated at the add-on's bar cap; use a smaller range or coarser bars",
                    "path": path, "bars_fetched": fetched}
        for t, o, h, l, c, v in r["bars"]:
            rows[t] = [t, o, h, l, c, v]
        fetched += len(r["bars"])
        log(f"{a}..{b}: {len(r['bars'])} bars")
    _save(path, rows)
    keys = sorted(rows)
    return {"ok": True, "path": path, "bars_fetched": fetched, "total_rows": len(rows),
            "first": keys[0] if keys else None, "last": keys[-1] if keys else None}


def _save(path: str, rows: dict) -> None:
    if not rows:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for k in sorted(rows):
            w.writerow(rows[k])
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("instrument", help='for example "MNQ 12-26" (front month merges earlier contracts)')
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--type", default="Minute", choices=TYPES)
    ap.add_argument("--value", type=int, default=1)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    res = download(a.instrument, date.fromisoformat(a.start), date.fromisoformat(a.end), a.type, a.value, a.out, log=print)
    print(json.dumps(res, indent=1))
    sys.exit(0 if res["ok"] else 2)


if __name__ == "__main__":
    main()
