"""Read-only NinjaTrader MCP server.

Talks to NtStatusAddOn (http://127.0.0.1:8765/status). Exposes NO order tools.
"""
import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer

import journal

BASE = os.environ.get("NT_ADDON_URL", "http://127.0.0.1:8765")
STALE_SECONDS = 30
mcp = MCPServer("ninjatrader-readonly")


def _fetch(path: str = "/status") -> dict:
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return _stamp(json.loads(r.read().decode("utf-8")))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"error": f"NinjaTrader add-on unreachable or bad reply: {e}. Is NT8 running with NtStatusAddOn compiled?"}


def _stamp(data: dict) -> dict:
    """Add age_seconds and a stale warning when the add-on timestamp is old."""
    ts = data.get("timestamp")
    if ts:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()
            data["age_seconds"] = round(age, 1)
            if age > STALE_SECONDS:
                data["warning"] = f"STALE: data is {age:.0f}s old, do not rely on it"
        except ValueError:
            pass
    return data


@mcp.tool()
def get_accounts() -> str:
    """List NinjaTrader accounts with cash value, realized and unrealized P&L."""
    data = _fetch()
    if "error" in data:
        return json.dumps(data)
    out = [{k: a[k] for k in ("name", "connection", "cash_value", "realized_pnl", "unrealized_pnl")}
           for a in data["accounts"]]
    return json.dumps({**_meta(data), "accounts": out}, indent=2)


def _meta(data: dict) -> dict:
    return {k: data[k] for k in ("timestamp", "age_seconds", "warning") if k in data}


@mcp.tool()
def get_positions(account: str = "") -> str:
    """List open positions. Optionally filter by account name (e.g. 'Sim101')."""
    data = _fetch()
    if "error" in data:
        return json.dumps(data)
    out = [{"account": a["name"], "positions": a["positions"]}
           for a in data["accounts"] if not account or a["name"] == account]
    return json.dumps({**_meta(data), "accounts": out}, indent=2)


@mcp.tool()
def get_orders(account: str = "") -> str:
    """List working (not filled/cancelled/rejected) orders with type, prices and OCO group."""
    data = _fetch("/orders")
    if "error" in data:
        return json.dumps(data)
    rows = [o for o in data["orders"] if not account or o["account"] == account]
    return json.dumps({**_meta(data), "orders": rows}, indent=2)


@mcp.tool()
def get_executions(account: str = "", limit: int = 50) -> str:
    """Most recent fills (newest last): time, price, quantity, entry/exit flag, order name (e.g. 'ORB long')."""
    limit = max(1, min(limit, 500))
    data = _fetch(f"/executions?limit={limit}")
    if "error" in data:
        return json.dumps(data)
    rows = [e for e in data["executions"] if not account or e["account"] == account]
    return json.dumps({**_meta(data), "executions": rows[-limit:]}, indent=2)


@mcp.tool()
def get_strategies(account: str = "") -> str:
    """Strategies running in NinjaTrader: name, instrument, state, current position."""
    data = _fetch("/strategies")
    if "error" in data:
        return json.dumps(data)
    rows = [x for x in data["strategies"] if not account or x["account"] == account]
    return json.dumps({**_meta(data), "strategies": rows}, indent=2)


@mcp.tool()
def sync_journal(strategy: str = "", version: str = "", limit: int = 500) -> str:
    """Save NinjaTrader's recent fills into the local SQLite journal (safe to repeat; duplicates are skipped).
    Optionally tag new rows with a strategy name and version. Writes only to the local journal, never to NinjaTrader."""
    data = _fetch(f"/executions?limit={max(1, min(limit, 500))}")
    if "error" in data:
        return json.dumps(data)
    con = journal.connect()
    try:
        counts = journal.record(con, data["executions"], strategy, version)
    finally:
        con.close()
    return json.dumps({**_meta(data), **counts, "fetched": len(data["executions"])}, indent=2)


@mcp.tool()
def get_journal(account: str = "", strategy: str = "", since: str = "", limit: int = 200) -> str:
    """Read persisted fills from the local journal (survives NT restarts). `since` is an ISO timestamp."""
    con = journal.connect()
    try:
        rows = journal.query(con, account, strategy, since, limit)
    finally:
        con.close()
    return json.dumps({"count": len(rows), "executions": rows}, indent=2)


@mcp.tool()
def get_health() -> str:
    """Add-on heartbeat and connection / data-feed status. Call this first if other data looks wrong."""
    data = _fetch("/health")
    if "error" in data:
        return json.dumps(data)
    problems = [c for c in data.get("connections", [])
                if c.get("status") != "Connected" or c.get("price_status") != "Connected"]
    if problems:
        data["warning"] = "Connection or data feed not Connected: " + ", ".join(c["account"] for c in problems)
    return json.dumps(data, indent=2)


if __name__ == "__main__":
    mcp.run()
