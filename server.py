"""Read-only NinjaTrader MCP server.

Talks to NtStatusAddOn (http://127.0.0.1:8765/status). Exposes NO order tools.
"""
import json
import urllib.request
import urllib.error

from mcp.server.mcpserver import MCPServer

BASE = "http://127.0.0.1:8765"
mcp = MCPServer("ninjatrader-readonly")


def _fetch() -> dict:
    try:
        with urllib.request.urlopen(BASE + "/status", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as e:
        return {"error": f"NinjaTrader add-on unreachable: {e}. Is NT8 running with NtStatusAddOn compiled?"}


@mcp.tool()
def get_accounts() -> str:
    """List NinjaTrader accounts with cash value, realized and unrealized P&L."""
    data = _fetch()
    if "error" in data:
        return json.dumps(data)
    out = [{k: a[k] for k in ("name", "connection", "cash_value", "realized_pnl", "unrealized_pnl")}
           for a in data["accounts"]]
    return json.dumps({"timestamp": data["timestamp"], "accounts": out}, indent=2)


@mcp.tool()
def get_positions(account: str = "") -> str:
    """List open positions. Optionally filter by account name (e.g. 'Sim101')."""
    data = _fetch()
    if "error" in data:
        return json.dumps(data)
    out = [{"account": a["name"], "positions": a["positions"]}
           for a in data["accounts"] if not account or a["name"] == account]
    return json.dumps({"timestamp": data["timestamp"], "accounts": out}, indent=2)


if __name__ == "__main__":
    mcp.run()
