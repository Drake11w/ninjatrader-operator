# ninjatrader-operator

Read-only MCP server that lets Claude monitor NinjaTrader 8 (accounts, P&L, positions). No order tools.

- `NtStatusAddOn.cs` - NinjaScript add-on. Copy to `Documents\NinjaTrader 8\bin\Custom\AddOns\`, open the NinjaScript Editor and press F5. Serves JSON (GET only, localhost only) at `/status`, `/orders`, `/executions?limit=N`, `/strategies` and `/health`.
- `server.py` - Python MCP server (`pip install mcp`, tested with mcp 2.x) exposing `get_accounts`, `get_positions`, `get_orders`, `get_executions`, `get_strategies` and `get_health`, plus `sync_journal` / `get_journal` (local SQLite trade journal at `~/.ninjatrader-operator/journal.sqlite`, override with `NT_JOURNAL_DB`; `journal.py` holds the logic), `get_session_info` (RTH/Globex state, time to open/close, front-month roll; `sessions.py`), `get_report` (daily/weekly markdown report; `report.py`), `get_rule_status` (prop-firm distance-to-breach; needs a rules file, see `rules.example.json`, placeholder values), and `get_trades` / `get_performance` (round-trip trades and metrics from the journal; `trades.py`; P&L uses a built-in point-value table, override with `point_values`). Every reply carries `age_seconds` and a `warning` if the data is stale. Set `NT_ADDON_URL` to point at a different add-on address. Tests: `python -m unittest discover tests` (fake add-on, no NinjaTrader needed).

Register in Claude Desktop's MCP config:

```json
{"mcpServers":{"ninjatrader":{"command":"python","args":["C:/path/to/server.py"]}}}
```

Note: the new add-on endpoints (orders, executions, strategies, health) have not been compiled in NT8 yet; `Account.Strategies` in particular needs confirming. Planned: regime status; pause/resume only with a daily-loss cap and kill switch enforced server-side; order entry last, if ever.
