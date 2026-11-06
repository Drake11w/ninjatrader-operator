# ninjatrader-operator

Read-only MCP server that lets Claude monitor NinjaTrader 8 (accounts, P&L, positions). No order tools.

- `NtStatusAddOn.cs` - NinjaScript add-on. Copy to `Documents\NinjaTrader 8\bin\Custom\AddOns\`, open the NinjaScript Editor and press F5. Serves JSON (GET only, localhost only) at `/status`, `/orders`, `/executions?limit=N`, `/strategies` and `/health`.
- `server.py` - Python MCP server (`pip install mcp`, tested with mcp 2.x) exposing `get_accounts`, `get_positions`, `get_orders`, `get_executions`, `get_strategies` and `get_health`. Every reply carries `age_seconds` and a `warning` if the data is stale. Set `NT_ADDON_URL` to point at a different add-on address. Tests: `python -m unittest discover tests` (fake add-on, no NinjaTrader needed).

Register in Claude Desktop's MCP config:

```json
{"mcpServers":{"ninjatrader":{"command":"python","args":["C:/path/to/server.py"]}}}
```

Note: the new add-on endpoints (orders, executions, strategies, health) have not been compiled in NT8 yet; `Account.Strategies` in particular needs confirming. Planned: regime status; pause/resume only with a daily-loss cap and kill switch enforced server-side; order entry last, if ever.
