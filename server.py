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
import rules
import trades

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
def get_trades(account: str = "", strategy: str = "", since: str = "", point_values: str = "") -> str:
    """Round-trip trades built from the local journal (FIFO pairing of entry and exit fills), with P&L net of
    commission. `point_values` is optional JSON like {"MNQ": 2.0}; defaults cover common futures."""
    return _trade_report(account, strategy, since, point_values, summary_only=False)


@mcp.tool()
def get_performance(account: str = "", strategy: str = "", since: str = "", point_values: str = "") -> str:
    """Performance stats from journaled trades: net P&L, win rate, expectancy, profit factor, avg win/loss, max
    drawdown, Sharpe/Sortino (daily), and breakdowns by weekday, entry hour (ET) and month. Warns on small samples."""
    return _trade_report(account, strategy, since, point_values, summary_only=True)


def _trade_report(account, strategy, since, point_values, summary_only):
    try:
        pv = json.loads(point_values) if point_values else None
    except ValueError:
        return json.dumps({"error": 'point_values must be valid JSON, e.g. {"MNQ": 2.0}'})
    con = journal.connect()
    try:
        rows = journal.query(con, account, strategy, since, 5000)
    finally:
        con.close()
    built = trades.build_trades(rows, pv)
    if summary_only:
        out = {"performance": trades.performance(built["trades"]), "open_positions": built["open"]}
    else:
        out = {"count": len(built["trades"]), "trades": built["trades"], "open_positions": built["open"]}
    out["warnings"] = built["warnings"]
    return json.dumps(out, indent=2)


@mcp.tool()
def get_rule_status(account: str = "", strategy: str = "", include_unrealized: bool = False) -> str:
    """Prop-firm rule tracker: distance in dollars to the daily loss limit, trailing drawdown floor, profit target
    and consistency rule, with ok/warning/breach status. Uses closed trades from the journal (run sync_journal
    first); set include_unrealized to add current open P&L from NinjaTrader. Rule values come from the rules file
    (NT_RULES_FILE or ~/.ninjatrader-operator/rules.json; see rules.example.json)."""
    try:
        cfg = rules.load_rules()
    except (OSError, ValueError) as e:
        return json.dumps({"error": str(e)})
    unrealized = 0.0
    if include_unrealized:
        data = _fetch("/status")
        if "error" in data:
            return json.dumps(data)
        unrealized = sum(a.get("unrealized_pnl") or 0 for a in data.get("accounts", [])
                         if not account or a.get("name") == account)
    con = journal.connect()
    try:
        rows = journal.query(con, account, strategy, "", 5000)
    finally:
        con.close()
    built = trades.build_trades(rows)
    out = rules.evaluate(built["trades"], cfg, unrealized=unrealized)
    out["warnings"] += built["warnings"]
    return json.dumps(out, indent=2)


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
