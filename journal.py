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
    return con


def record(con: sqlite3.Connection, executions: list, strategy: str = "", version: str = "") -> dict:
    """Insert executions; returns counts of new and already-known rows."""
    new = 0
    for e in executions:
        if not e.get("execution_id"):
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO executions (account, execution_id, instrument, time, action, order_name,"
            " quantity, price, commission, is_entry, is_exit, strategy, strategy_version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e.get("account", ""), e["execution_id"], e.get("instrument"), e.get("time"), e.get("action"),
             e.get("order_name"), e.get("quantity"), e.get("price"), e.get("commission"),
             int(bool(e.get("is_entry"))), int(bool(e.get("is_exit"))), strategy, version))
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
