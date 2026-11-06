"""SQLite trade journal: persists NinjaTrader executions so they survive NT restarts.

Dedupes on (account, execution_id), so syncing repeatedly is safe.
"""
import os
import sqlite3

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".ninjatrader-operator", "journal.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    account TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    instrument TEXT,
    time TEXT,
    action TEXT,
    order_name TEXT,
    quantity INTEGER,
    price REAL,
    commission REAL,
    is_entry INTEGER,
    is_exit INTEGER,
    strategy TEXT NOT NULL DEFAULT '',
    strategy_version TEXT NOT NULL DEFAULT '',
    leg TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (account, execution_id)
);
CREATE INDEX IF NOT EXISTS idx_exec_time ON executions(time);
"""


def connect(path: str = "") -> sqlite3.Connection:
    path = path or os.environ.get("NT_JOURNAL_DB", DEFAULT_DB)
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(executions)")}
    if "leg" not in cols:  # journals created before legs existed
        con.execute("ALTER TABLE executions ADD COLUMN leg TEXT NOT NULL DEFAULT ''")
        con.commit()
    return con


def make_tagger(con: sqlite3.Connection, name_map: dict):
    """Build a tagger(e) -> (strategy, version, leg) | None from {order_name: (strategy, version, leg)}.

    Entries and named exits are matched by order name. Anonymous exits (NinjaTrader names stop and target fills
    "Stop loss" / "Profit target") inherit the leg of the latest entry on the same account and instrument. Pass
    executions oldest first.
    """
    last = {}

    def seed(key):
        r = con.execute("SELECT strategy, strategy_version, leg FROM executions WHERE account=? AND instrument=?"
                        " AND is_entry=1 AND leg!='' ORDER BY time DESC LIMIT 1", key).fetchone()
        return tuple(r) if r else None

    def tagger(e):
        key = (e.get("account", ""), e.get("instrument"))
        tag = name_map.get(e.get("order_name"))
        if tag:
            if e.get("is_entry"):
                last[key] = tag
            return tag
        if e.get("is_exit"):
            if key not in last:
                last[key] = seed(key)
            return last[key]
        return None
    return tagger


def record(con: sqlite3.Connection, executions: list, strategy: str = "", version: str = "", tagger=None) -> dict:
    """Insert executions; returns counts of new and already-known rows.

    `tagger(e)` (see make_tagger) may return (strategy, version, leg) per execution; otherwise strategy/version apply."""
    new = 0
    for e in executions:
        if not e.get("execution_id"):
            continue
        strat, ver, leg = (tagger(e) if tagger else None) or (strategy, version, "")
        cur = con.execute(
            "INSERT OR IGNORE INTO executions (account, execution_id, instrument, time, action, order_name,"
            " quantity, price, commission, is_entry, is_exit, strategy, strategy_version, leg)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e.get("account", ""), e["execution_id"], e.get("instrument"), e.get("time"), e.get("action"),
             e.get("order_name"), e.get("quantity"), e.get("price"), e.get("commission"),
             int(bool(e.get("is_entry"))), int(bool(e.get("is_exit"))), strat, ver, leg))
        new += cur.rowcount
    con.commit()
    return {"new": new, "already_recorded": len(executions) - new}


def query(con: sqlite3.Connection, account: str = "", strategy: str = "", since: str = "", limit: int = 200) -> list:
    sql, args = "SELECT * FROM executions WHERE 1=1", []
    for col, val, op in (("account", account, "="), ("strategy", strategy, "="), ("time", since, ">=")):
        if val:
            sql += f" AND {col} {op} ?"
            args.append(val)
    sql += " ORDER BY time DESC LIMIT ?"
    args.append(max(1, min(limit, 5000)))
    return [dict(r) for r in reversed(con.execute(sql, args).fetchall())]
