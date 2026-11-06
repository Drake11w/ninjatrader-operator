# ninjatrader-operator

Read-only MCP server that lets Claude monitor NinjaTrader 8 (accounts, P&L, positions). No order tools.

- `NtStatusAddOn.cs` - NinjaScript add-on. Copy to `Documents\NinjaTrader 8\bin\Custom\AddOns\`, open the NinjaScript Editor and press F5. Serves JSON at `http://127.0.0.1:8765/status` (GET only, localhost only).
- `server.py` - Python MCP server (`pip install mcp`, tested with mcp 2.x) exposing `get_accounts` and `get_positions`.

Register in Claude Desktop's MCP config:

```json
{"mcpServers":{"ninjatrader":{"command":"python","args":["C:/path/to/server.py"]}}}
```

Planned: strategy on/off and regime status; pause/resume only with a daily-loss cap and kill switch enforced server-side; order entry last, if ever.
