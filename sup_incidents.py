"""Strategy Supervisor: incident store (SQLite, supervisor.sqlite).

One non-resolved incident per condition (key, account, strategy). Repeats update it; a higher severity escalates it;
a condition resolves only after RESOLVE_AFTER consecutive clear cycles on current (non-stale) data. Every change is
appended to incident_events. Phase 1 never records actions: actions_json stays '[]'.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

RESOLVE_AFTER = 3
DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".ninjatrader-operator", "supervisor.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL, account TEXT NOT NULL DEFAULT '', strategy TEXT NOT NULL DEFAULT '',
    severity INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    opened_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, resolved_at TEXT,
    trigger TEXT NOT NULL DEFAULT '', evidence_json TEXT NOT NULL DEFAULT '{}',
    diagnosis TEXT NOT NULL DEFAULT '', actions_json TEXT NOT NULL DEFAULT '[]',
    caused_by TEXT NOT NULL DEFAULT '', needs_human INTEGER NOT NULL DEFAULT 0,
    clear_cycles INTEGER NOT NULL DEFAULT 0, notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_inc_status ON incidents(status);
CREATE TABLE IF NOT EXISTS incident_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL, at TEXT NOT NULL,
    kind TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS daemon_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def connect(path: str = "") -> sqlite3.Connection:
    path = path or os.environ.get("NT_SUPERVISOR_DB", DEFAULT_DB)
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def _event(con, iid, at, kind, detail=None):
    con.execute("INSERT INTO incident_events(incident_id, at, kind, detail_json) VALUES (?,?,?,?)",
                (iid, _iso(at), kind, json.dumps(detail or {})))


def _new_id(con, now):
    day = now.strftime("%Y%m%d")
    n = con.execute("SELECT COUNT(*) FROM incidents WHERE id LIKE ?", (f"INC-{day}-%",)).fetchone()[0]
    return f"INC-{day}-{n + 1:04d}"


def apply_findings(con, findings: list, now: datetime, allow_resolve: bool = True) -> list:
    """Update incidents from this cycle's findings. Returns events: [{"kind": opened|escalated|resolved, ...}].

    allow_resolve=False (stale or failed fetches) freezes the clear counters: stale data can never close an incident.
    """
    events = []
    seen = set()
    for f in findings:
        ident = (f["key"], f.get("account", ""), f.get("strategy", ""))
        seen.add(ident)
        row = con.execute("SELECT * FROM incidents WHERE key=? AND account=? AND strategy=? AND status!='resolved'",
                          ident).fetchone()
        if row is None:
            iid = _new_id(con, now)
            con.execute("INSERT INTO incidents(id,key,account,strategy,severity,opened_at,last_seen_at,trigger,"
                        "evidence_json,caused_by,needs_human) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (iid, *ident, f["severity"], _iso(now), _iso(now), f["trigger"],
                         json.dumps(f.get("evidence", {})), f.get("caused_by", ""), 1 if f["severity"] >= 3 else 0))
            _event(con, iid, now, "opened", {"severity": f["severity"], "trigger": f["trigger"]})
            events.append({"kind": "opened", "id": iid, **f})
            continue
        sev = max(row["severity"], f["severity"])
        con.execute("UPDATE incidents SET last_seen_at=?, clear_cycles=0, evidence_json=?, trigger=?, severity=?, "
                    "caused_by=?, needs_human=? WHERE id=?",
                    (_iso(now), json.dumps(f.get("evidence", {})), f["trigger"], sev, f.get("caused_by", ""),
                     1 if sev >= 3 else row["needs_human"], row["id"]))
        if f["severity"] > row["severity"]:
            _event(con, row["id"], now, "escalated", {"from": row["severity"], "to": f["severity"]})
            events.append({"kind": "escalated", "id": row["id"], **f})
    if allow_resolve:
        for row in con.execute("SELECT * FROM incidents WHERE status!='resolved'").fetchall():
            if (row["key"], row["account"], row["strategy"]) in seen:
                continue
            clear = row["clear_cycles"] + 1
            if clear >= RESOLVE_AFTER:
                con.execute("UPDATE incidents SET status='resolved', resolved_at=?, clear_cycles=? WHERE id=?",
                            (_iso(now), clear, row["id"]))
                _event(con, row["id"], now, "resolved", {"clear_cycles": clear})
                events.append({"kind": "resolved", "id": row["id"], "key": row["key"], "account": row["account"],
                               "strategy": row["strategy"], "severity": row["severity"]})
            else:
                con.execute("UPDATE incidents SET clear_cycles=? WHERE id=?", (clear, row["id"]))
    con.commit()
    return events


def acknowledge(con, incident_id: str, now: datetime, note: str = "") -> bool:
    cur = con.execute("UPDATE incidents SET status='acknowledged' WHERE id=? AND status='open'", (incident_id,))
    if cur.rowcount:
        _event(con, incident_id, now, "acknowledged", {"note": note})
    con.commit()
    return bool(cur.rowcount)


def mark_notified(con, incident_id: str, now: datetime) -> None:
    con.execute("UPDATE incidents SET notified_at=? WHERE id=?", (_iso(now), incident_id))
    con.commit()


def list_incidents(con, status: str = "", min_severity: int = 0) -> list:
    q, args = "SELECT * FROM incidents WHERE severity>=?", [min_severity]
    if status == "active":
        q += " AND status!='resolved'"
    elif status:
        q += " AND status=?"
        args.append(status)
    return [dict(r) for r in con.execute(q + " ORDER BY opened_at DESC", args)]


def events_for(con, incident_id: str) -> list:
    return [dict(r) for r in con.execute("SELECT * FROM incident_events WHERE incident_id=? ORDER BY seq",
                                         (incident_id,))]


def get_state(con, k: str, default=None):
    r = con.execute("SELECT v FROM daemon_state WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def set_state(con, k: str, value) -> None:
    con.execute("INSERT INTO daemon_state(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, json.dumps(value)))
    con.commit()
