"""Feedback bundle: results the operator returns to the Lucid research side as plain files. Read-only.

Layout (one writer, never committed): <NT_FEEDBACK_DIR>/<lucid_id>/<version>/<as_of>/ holds an immutable snapshot
(MANIFEST.json, trades.csv, daily_pnl.csv, summary.json, verdict.json, drift.json, backtest_check.json) and
<lucid_id>/<version>/latest.json points at the newest. Column and key definitions: docs/feedback/SCHEMA.md and
feedback.schema.json. Raw account names never leave this machine: they become stable aliases (acct-1, ...) from a
local mapping file, and every file is scrubbed for the raw names before it is hashed or written.

The builders are pure (data in, text out). `export()` is the thin I/O shell. Nothing here talks to NinjaTrader.
"""
import csv
import hashlib
import io
import json
import os
import re
import subprocess
from datetime import datetime, timezone

import backtest
import comparison
import control
import journal
import readiness
import registry
import trades as trades_mod
import verdict

SCHEMA_VERSION = "1.0.0"
TRADE_COLUMNS = ["trade_id", "source", "account", "instrument", "side", "quantity", "leg", "entry_time", "exit_time",
                 "entry_price", "exit_price", "entry_name", "exit_name", "exit_reason", "duration_sec", "points",
                 "gross_pnl", "commission", "net_pnl", "trading_date_ct", "slippage_ticks", "mae_points", "regime"]
DAILY_COLUMNS = ["date_ct", "trades", "net_pnl", "cumulative_net_pnl"]
DRAWDOWN_DEFINITION = ("largest peak-to-trough fall of cumulative closed-trade net P&L in USD, trades taken in exit "
                       "order, starting from zero at the first exported trade")
SAFE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def feedback_dir() -> str:
    return os.environ.get("NT_FEEDBACK_DIR") or os.path.join(os.path.expanduser("~"), ".ninjatrader-operator",
                                                             "feedback")


def aliases_path() -> str:
    return os.environ.get("NT_FEEDBACK_ALIASES") or os.path.join(os.path.expanduser("~"), ".ninjatrader-operator",
                                                                 "account_aliases.json")


# ---------------------------------------------------------------- pure builders

def exit_reason(name, order_names) -> str:
    """Normalized exit reason from the exit order's name. NinjaTrader names protective fills 'Stop loss' and
    'Profit target'; names the spec lists are strategy exits; an empty name (e.g. after an NT restart) is unknown."""
    n = (name or "").strip()
    if not n:
        return "unknown"
    low = n.lower()
    if low == "stop loss":
        return "stop"
    if low == "profit target":
        return "target"
    if n in (order_names or []):
        return "strategy_exit"
    return "other"


def central_date(ts: str, tz_name: str) -> str:
    """Calendar date of a UTC timestamp in the spec's time zone (the same bucketing the daily performance uses)."""
    d = trades_mod._parse(ts)
    try:
        from zoneinfo import ZoneInfo
        d = d.astimezone(ZoneInfo(tz_name))
    except Exception:
        pass  # tzdata missing: UTC date; summary.json carries the performance() warning
    return d.date().isoformat()


def _source(account: str) -> str:
    return "sim" if (account or "").lower().startswith("sim") else "live"


def trade_rows(trade_list: list, aliases: dict, order_names: list, tz_name: str) -> list:
    rows, seen = [], {}
    for t in trade_list:
        alias = aliases.get(t["account"], "acct-?")
        key = "|".join(str(x) for x in (alias, t["instrument"], t["entry_time"], t["exit_time"], t["entry_price"],
                                        t["exit_price"], t["quantity"]))
        seen[key] = seen.get(key, 0) + 1
        tid = hashlib.sha256(f"{key}|{seen[key]}".encode()).hexdigest()[:12]
        rows.append({
            "trade_id": tid, "source": _source(t["account"]), "account": alias, "instrument": t["instrument"],
            "side": t["side"], "quantity": t["quantity"], "leg": t.get("leg") or "",
            "entry_time": t["entry_time"], "exit_time": t["exit_time"], "entry_price": t["entry_price"],
            "exit_price": t["exit_price"], "entry_name": t.get("entry_name") or "", "exit_name": t.get("exit_name") or "",
            "exit_reason": exit_reason(t.get("exit_name"), order_names), "duration_sec": t["duration_sec"],
            "points": t["points"], "gross_pnl": t["gross_pnl"], "commission": t["commission"],
            "net_pnl": t["net_pnl"], "trading_date_ct": central_date(t["exit_time"], tz_name),
            "slippage_ticks": None, "mae_points": None, "regime": None})
    return rows


def daily_rows(rows: list) -> list:
    by, cum, out = {}, 0.0, []
    for r in rows:
        d = by.setdefault(r["trading_date_ct"], [0, 0.0])
        d[0] += 1
        d[1] += r["net_pnl"]
    for date in sorted(by):
        cum += by[date][1]
        out.append({"date_ct": date, "trades": by[date][0], "net_pnl": round(by[date][1], 2),
                    "cumulative_net_pnl": round(cum, 2)})
    return out


def to_csv(columns: list, rows: list) -> str:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow(["" if r[c] is None else r[c] for c in columns])
    return buf.getvalue()


def to_json(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n"


def summary_doc(perf: dict, rows: list, tz_name: str) -> dict:
    keys = ("trades", "net_pnl", "win_rate", "avg_win", "avg_loss", "expectancy", "profit_factor", "max_drawdown",
            "best_day", "worst_day", "sharpe", "sortino", "trading_days", "avg_duration_sec")
    out = {k: perf.get(k) for k in keys}
    out["trades"] = perf.get("trades", 0)
    out.update(timezone=perf.get("timezone", tz_name), drawdown_definition=DRAWDOWN_DEFINITION,
               first_exit=rows[0]["exit_time"] if rows else None, last_exit=rows[-1]["exit_time"] if rows else None,
               by_source={s: sum(1 for r in rows if r["source"] == s) for s in ("sim", "live")},
               warnings=perf.get("warnings", []))
    return out


def drift_doc(compare: dict, verdict_result: dict, untagged: int) -> dict:
    """Strategy-level drift flags only; operational supervisor incidents are not part of the bundle."""
    flags = []
    for r in compare.get("rows", []):
        if r["flag"] in ("worse", "shift"):
            flags.append({"key": f"compare.{r['metric']}", "severity": "warning", "detail":
                          f"{r['label']}: backtest {r['backtest']}, sim {r['sim']} ({r['flag']})"})
    if verdict_result.get("edge_decayed"):
        flags.append({"key": "verdict.edge_decay", "severity": "warning", "detail": "; ".join(
            c["check"] for c in verdict_result.get("checks", []) if c["check"].startswith("edge_decay") and
            c["status"] == "fail")})
    if untagged:
        flags.append({"key": "tagging.untagged_fills", "severity": "warning",
                      "detail": f"{untagged} fill(s) in the period carry no strategy tag"})
    return {"summary": compare.get("summary"), "sim_trades": compare.get("sim_trades"), "flags": flags,
            "rows": compare.get("rows", []), "notes": compare.get("notes", [])}


def backtest_doc(run, spec: dict) -> dict:
    if not run:
        return {"present": False, "run_id": None, "label": None, "kind": None, "period": None, "claims_check": None,
                "metrics": None}
    m = json.loads(run["metrics_json"])
    return {"present": True, "run_id": run["run_id"], "label": run["label"], "kind": run["kind"],
            "imported_at": run["imported_at"], "period": {"start": run["start"], "end": run["end"]},
            "claims_check": backtest.claims_check(m, spec),
            "metrics": {k: m.get(k) for k in ("trades", "net_pnl", "net_per_year", "max_drawdown", "win_rate",
                                              "expectancy", "profit_factor", "trades_per_year")}}


def scrub(text: str, aliases: dict) -> str:
    """Replace every raw account name with its alias; the last line of defence for the privacy rule."""
    for raw, alias in sorted(aliases.items(), key=lambda kv: -len(kv[0])):
        if raw:
            text = text.replace(raw, alias)
    return text


def content_hash(files: dict) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(f"{name}:{hashlib.sha256(files[name].encode('utf-8')).hexdigest()}\n".encode())
    return h.hexdigest()


def build_files(spec: dict, status: str, since: str, now: str, res: dict, run, aliases: dict,
                untagged: int, compare: dict) -> dict:
    """All snapshot files except MANIFEST.json, as {name: text}. Pure."""
    tz_name = spec.get("timezone", "America/Chicago")
    order_names = [n for leg in spec.get("legs", []) for n in (leg.get("ninjascript") or {}).get("order_names", [])]
    rows = trade_rows(res.get("trades", []), aliases, order_names, tz_name)
    perf = trades_mod.performance(res.get("trades", []), tz_name) if rows else {"trades": 0, "warnings": []}
    bt = backtest_doc(run, spec)
    ready = readiness.evaluate(
        res, status, since, now,
        {"present": bt["present"], "claims": (bt["claims_check"] or {}).get("overall")} if bt["present"] else None,
        untagged, spec.get("readiness_overrides"))
    vdoc = {k: res.get(k) for k in ("verdict", "edge_decayed", "reasons", "sample", "performance", "expected",
                                    "checks", "criteria", "warnings")}
    if "eta" in res:
        vdoc["eta"] = res["eta"]
    vdoc["readiness"] = ready
    files = {"trades.csv": to_csv(TRADE_COLUMNS, rows), "daily_pnl.csv": to_csv(DAILY_COLUMNS, daily_rows(rows)),
             "summary.json": to_json(summary_doc(perf, rows, tz_name)), "verdict.json": to_json(vdoc),
             "drift.json": to_json(drift_doc(compare, res, untagged)), "backtest_check.json": to_json(bt)}
    return {k: scrub(v, aliases) for k, v in files.items()}


def build_manifest(spec: dict, status: str, since: str, as_of: str, files: dict, handoff_info, operator_commit,
                   supersedes) -> dict:
    return {"schema_version": SCHEMA_VERSION, "as_of": as_of, "lucid_id": spec.get("lucid_id"),
            "spec_id": spec["id"], "spec_version": spec["version"], "status": status, "since": since or None,
            "handoff": handoff_info, "operator_commit": operator_commit, "supersedes": supersedes,
            "content_sha256": content_hash(files),
            "files": {n: {"sha256": hashlib.sha256(t.encode("utf-8")).hexdigest(), "bytes": len(t.encode("utf-8"))}
                      for n, t in sorted(files.items())}}


# ---------------------------------------------------------------- I/O shell

def resolve_aliases(accounts, path: str = "") -> dict:
    """{raw account: alias} for `accounts`, assigning new aliases (acct-N) and persisting them. Local file only."""
    path = path or aliases_path()
    mapping = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            mapping = json.load(f)
    changed = False
    for a in sorted(x for x in accounts if x):
        if a not in mapping:
            used = [int(v.split("-")[1]) for v in mapping.values() if re.fullmatch(r"acct-\d+", v)]
            mapping[a] = f"acct-{max(used, default=0) + 1}"
            changed = True
    if changed:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(mapping, f, indent=2, sort_keys=True)
    return mapping


def _handoff_info(rcon, spec: dict, handoff_dir: str):
    """Echo the handoff manifest (bundle version, Lucid commit, .cs hashes) if the bundle can be found, else None."""
    cands = []
    appr = registry.get_approval(rcon, spec["id"], spec["version"])
    if appr and appr.get("source"):
        cands.append(appr["source"])
    base = handoff_dir or os.environ.get("NT_HANDOFF_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lucid-trading-system", "handoff")
    cands.append(os.path.join(base, spec["id"], spec["version"]))
    for d in cands:
        p = os.path.join(d, "MANIFEST.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            return {"bundle_version": m.get("bundle_version"), "lucid_repo_commit": m.get("lucid_repo_commit"),
                    "cs_sha256": {k: v.get("sha256") for k, v in sorted((m.get("files") or {}).items())
                                  if k.endswith(".cs") and isinstance(v, dict)}}
    return None


def _operator_commit():
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=os.path.dirname(os.path.abspath(__file__)),
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def untagged_count(jcon, res: dict, spec: dict, since: str) -> int:
    """Fills in the period, on the accounts the version traded, in its instrument, that carry no strategy tag."""
    accounts = {t["account"] for t in res["trades"]}
    root = (spec.get("instrument") or {}).get("root", "").upper()
    if not accounts:
        return 0
    return sum(1 for r in journal.query(jcon, "", "", since, 5000)
               if r["account"] in accounts and not r.get("strategy") and trades_mod.root(r.get("instrument")) == root)


def readiness_for(rcon, jcon, sid: str, version: str, account: str = "", as_of: datetime = None) -> dict:
    """Fresh readiness block for one registered version (the same inputs the export uses). Read-only; never stored."""
    row = registry.get_spec(rcon, sid, version)
    if "error" in row:
        return {"error": row["error"]}
    spec = row["spec"]
    res = verdict.evaluate_registered(rcon, jcon, sid, version, account)
    if "error" in res:
        return {"error": res["error"]}
    now_iso = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = backtest.list_runs(rcon, sid, version)
    run = backtest.get_run(rcon, runs[0]["run_id"]) if runs else None
    bt = backtest_doc(run, spec)
    return readiness.evaluate(
        res, row["status"], res.get("since", ""), now_iso,
        {"present": bt["present"], "claims": (bt["claims_check"] or {}).get("overall")} if bt["present"] else None,
        untagged_count(jcon, res, spec, res.get("since", "")), spec.get("readiness_overrides"))


def export(rcon, jcon, sid: str, version: str, account: str = "", out_dir: str = "", alias_file: str = "",
           handoff_dir: str = "", as_of: datetime = None, correction: bool = False,
           operator_commit="auto") -> dict:
    """Write a feedback snapshot for one registered spec version. Idempotent: identical content is a no-op.
    A changed result is a new snapshot; `correction=True` makes it name the snapshot it supersedes."""
    row = registry.get_spec(rcon, sid, version)
    if "error" in row:
        return {"ok": False, "error": row["error"]}
    spec = row["spec"]
    lucid_id = spec.get("lucid_id")
    if not lucid_id:
        return {"ok": False, "error": f"{sid} {version} has no lucid_id, so it has no feedback folder; "
                                      f"set lucid_id in the spec"}
    if not (SAFE_PART.match(lucid_id) and SAFE_PART.match(version)):
        return {"ok": False, "error": "lucid_id and version must be plain path-safe names"}
    as_of = (as_of or datetime.now(timezone.utc)).replace(microsecond=0)
    now_iso = as_of.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    res = verdict.evaluate_registered(rcon, jcon, sid, version, account)
    if "error" in res:
        return {"ok": False, "error": res["error"]}
    since = res.get("since", "")
    accounts = sorted({t["account"] for t in res["trades"]})
    untagged = untagged_count(jcon, res, spec, since)
    aliases = resolve_aliases(accounts + ([account] if account else []), alias_file)

    runs = backtest.list_runs(rcon, sid, version)
    run = backtest.get_run(rcon, runs[0]["run_id"]) if runs else None
    ref_durations = [t.get("duration_sec") for t in json.loads(run["trades_json"])] if run and run.get(
        "trades_json") else []
    perf = trades_mod.performance(res["trades"], spec.get("timezone", "America/Chicago")) if res["trades"] \
        else {"trades": 0}
    compare = comparison.compare(perf, res.get("backtest") or {}, res["trades"], ref_durations)

    files = build_files(spec, row["status"], since, now_iso, res, run, aliases, untagged, compare)
    base = os.path.join(out_dir or feedback_dir(), lucid_id, version)
    latest_path = os.path.join(base, "latest.json")
    latest = None
    if os.path.isfile(latest_path):
        with open(latest_path, encoding="utf-8") as f:
            latest = json.load(f)
    new_hash = content_hash(files)
    snap_name = _stamp(as_of)
    snap = os.path.join(base, snap_name)

    def done(status, path, **extra):
        result = {"ok": True, "status": status, "path": path, "as_of": extra.pop("as_of", now_iso), **extra}
        control.audit("export_feedback", {"id": sid, "version": version, "account": aliases.get(account, "")},
                      result)
        return result

    if latest and latest.get("content_sha256") == new_hash:
        return done("unchanged", os.path.join(base, latest["snapshot"]), as_of=latest["as_of"])
    if os.path.isdir(snap):
        return {"ok": False, "error": f"snapshot {snap_name} already exists with different content; snapshots are "
                                      f"immutable, export again with a later as_of"}
    commit = _operator_commit() if operator_commit == "auto" else operator_commit
    supersedes = latest["as_of"] if (correction and latest) else None
    manifest = build_manifest(spec, row["status"], since, now_iso, files, _handoff_info(rcon, spec, handoff_dir),
                              commit, supersedes)
    files = dict(files, **{"MANIFEST.json": scrub(to_json(manifest), aliases)})
    os.makedirs(base, exist_ok=True)
    tmp = os.path.join(base, ".tmp-" + snap_name)
    os.makedirs(tmp, exist_ok=True)
    try:
        for name, text in files.items():
            with open(os.path.join(tmp, name), "w", encoding="utf-8", newline="") as f:
                f.write(text)
        os.rename(tmp, snap)
    finally:
        if os.path.isdir(tmp):
            for n in os.listdir(tmp):
                os.remove(os.path.join(tmp, n))
            os.rmdir(tmp)
    _write_atomic(latest_path, to_json({"as_of": now_iso, "snapshot": snap_name, "content_sha256": new_hash,
                                        "schema_version": SCHEMA_VERSION}))
    return done("written", snap, supersedes=supersedes, readiness=json.loads(files["verdict.json"])["readiness"]
                ["summary"])
