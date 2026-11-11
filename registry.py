"""Strategy registry: stores submitted strategy spec versions and their lifecycle status.

A (id, version) pair is immutable once submitted; any change must be a new version. Status moves one stage at a
time along candidate -> backtest -> sim -> live-small -> live, or to rejected/retired from any non-terminal stage.
This only records status. It never touches NinjaTrader.
"""
import json
import os
import sqlite3

import strategy_spec

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".ninjatrader-operator", "registry.sqlite")
STAGES = ("candidate", "backtest", "sim", "live-small", "live")
TERMINAL = ("rejected", "retired")

SCHEMA = """
CREATE TABLE IF NOT EXISTS specs (
    id TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    submitted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT NOT NULL,
    version TEXT NOT NULL,
    approved INTEGER NOT NULL,
    limits TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS status_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL,
    version TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS live_small_approvals (
    approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL,
    version TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    reason TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    source TEXT NOT NULL,
    schedule_id INTEGER,
    approved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled_approvals (
    schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL,
    version TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    not_before TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    state_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def connect(path: str = "") -> sqlite3.Connection:
    path = path or os.environ.get("NT_REGISTRY_DB", DEFAULT_DB)
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def submit(con: sqlite3.Connection, spec, note: str = "") -> dict:
    """Validate and register a spec as a new candidate. Returns {ok, ...} or {ok: False, errors: [...]}."""
    errs = strategy_spec.validate(spec)
    if errs:
        return {"ok": False, "errors": errs}
    sid, ver = spec["id"], spec["version"]
    if con.execute("SELECT 1 FROM specs WHERE id=? AND version=?", (sid, ver)).fetchone():
        return {"ok": False, "errors": [f"{sid} {ver} is already registered; versions are immutable, "
                                        f"submit changes as a new version"]}
    con.execute("INSERT INTO specs (id, version, status, spec_json) VALUES (?,?,?,?)",
                (sid, ver, "candidate", json.dumps(spec, sort_keys=True)))
    con.execute("INSERT INTO status_history (id, version, from_status, to_status, note) VALUES (?,?,?,?,?)",
                (sid, ver, None, "candidate", note or "submitted"))
    con.commit()
    out = {"ok": True, "id": sid, "version": ver, "status": "candidate"}
    if spec.get("status", "candidate") != "candidate":
        out["warning"] = f"spec.status '{spec['status']}' ignored; new submissions always start as candidate"
    return out


def set_status(con: sqlite3.Connection, sid: str, version: str, to: str, note: str = "", commit: bool = True) -> dict:
    row = con.execute("SELECT status FROM specs WHERE id=? AND version=?", (sid, version)).fetchone()
    if not row:
        return {"ok": False, "error": f"{sid} {version} not found"}
    cur = row["status"]
    if cur in TERMINAL:
        return {"ok": False, "error": f"{sid} {version} is {cur} (terminal); submit a new version instead"}
    if to not in STAGES and to not in TERMINAL:
        return {"ok": False, "error": f"status must be one of {', '.join(STAGES + TERMINAL)}"}
    if to in STAGES and STAGES.index(to) != STAGES.index(cur) + 1:
        nxt = STAGES[STAGES.index(cur) + 1] if cur != "live" else None
        return {"ok": False, "error": f"cannot move {cur} -> {to}; next stage is {nxt or 'none (use retired)'}, "
                                      f"or use rejected/retired"}
    if to in TERMINAL and not note.strip():
        return {"ok": False, "error": f"a note (reason) is required to mark a strategy {to}"}
    con.execute("UPDATE specs SET status=? WHERE id=? AND version=?", (to, sid, version))
    con.execute("INSERT INTO status_history (id, version, from_status, to_status, note) VALUES (?,?,?,?,?)",
                (sid, version, cur, to, note))
    if commit:  # approvals pass commit=False so the status change and the approval row land together
        con.commit()
    return {"ok": True, "id": sid, "version": version, "from": cur, "to": to}


def order_name_map(con: sqlite3.Connection, bindings=()) -> dict:
    """{order_name: (spec id, version, leg name)} for specs that are actually trading (sim, live-small, live).

    If two versions claim the same order name, the more advanced stage wins, then the newer submission.
    Strategies commonly share order names, so `bindings` ([(account, spec id, version)], from the supervisor
    profiles) adds {(account, order_name): tag} entries that win on that account: a fill on Sim101 belongs to the
    strategy bound to Sim101, whatever stage another version holding the same names is in."""
    rows = con.execute("SELECT id, version, status, spec_json FROM specs WHERE status IN (?,?,?)"
                       " ORDER BY submitted_at, rowid", ("sim", "live-small", "live")).fetchall()
    rows = sorted(rows, key=lambda r: STAGES.index(r["status"]))  # stable: later submissions stay later
    out = {}
    for r in rows:
        spec = json.loads(r["spec_json"])
        if spec.get("schema_version") != 2:
            continue
        for leg in spec["legs"]:
            for name in leg["ninjascript"].get("order_names", []):
                out[name] = (r["id"], r["version"], leg["name"])
    bound = {(sid, ver): acct for acct, sid, ver in bindings}
    for r in rows:
        spec = json.loads(r["spec_json"])
        acct = bound.get((r["id"], r["version"]))
        if acct and spec.get("schema_version") == 2:
            for leg in spec["legs"]:
                for name in leg["ninjascript"].get("order_names", []):
                    out[(acct, name)] = (r["id"], r["version"], leg["name"])
    return out


def list_specs(con: sqlite3.Connection, sid: str = "", status: str = "") -> list:
    q, args = "SELECT id, version, status, submitted_at, spec_json FROM specs WHERE 1=1", []
    if sid:
        q += " AND id=?"
        args.append(sid)
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY submitted_at DESC, id, version"
    return [{"id": r["id"], "version": r["version"], "status": r["status"], "submitted_at": r["submitted_at"],
             "summary": strategy_spec.summarize(json.loads(r["spec_json"]))} for r in con.execute(q, args)]


def get_spec(con: sqlite3.Connection, sid: str, version: str) -> dict:
    row = con.execute("SELECT status, spec_json FROM specs WHERE id=? AND version=?", (sid, version)).fetchone()
    if not row:
        return {"error": f"{sid} {version} not found"}
    hist = [dict(h) for h in con.execute(
        "SELECT from_status, to_status, note, at FROM status_history WHERE id=? AND version=? ORDER BY seq",
        (sid, version))]
    return {"id": sid, "version": version, "status": row["status"], "spec": json.loads(row["spec_json"]),
            "history": hist}


def record_approval(con: sqlite3.Connection, sid: str, version: str, approved: bool, limits: str = "",
                    source: str = "") -> None:
    """Remember the handoff bundle's approval (MANIFEST.approval) for a spec version; the first record is kept."""
    con.execute("INSERT OR IGNORE INTO approvals (id, version, approved, limits, source) VALUES (?,?,?,?,?)",
                (sid, version, int(bool(approved)), limits or "", source))
    con.commit()


def get_approval(con: sqlite3.Connection, sid: str, version: str):
    r = con.execute("SELECT approved, limits, source, recorded_at FROM approvals WHERE id=? AND version=?",
                    (sid, version)).fetchone()
    return {"approved": bool(r["approved"]), "limits": r["limits"], "source": r["source"],
            "recorded_at": r["recorded_at"]} if r else None
