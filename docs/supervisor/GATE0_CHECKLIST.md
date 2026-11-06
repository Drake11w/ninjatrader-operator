# Gate 0: verify the Operator add-on against the NinjaTrader UI (Sim101)

Goal: confirm the add-on's read endpoints return what NinjaTrader actually shows. Nothing in the supervisor may depend on a field until it passes here. **Read-only: do not call any POST endpoint, and do not set any `NT_OPERATOR_*` variables for this gate.** Use Sim101 only.

Record each result as PASS / FAIL / NOTES in the table at the bottom.

## 0. Prerequisites
- NinjaTrader 8.1.8.4 running, connected to a data provider and with Sim101 available.
- Add-on built from the Operator `main` branch (the watchdog from PR #21 is **not** part of this gate; test it separately afterwards).
- Copy `NtStatusAddOn.cs` to `Documents\NinjaTrader 8\bin\Custom\AddOns\`, open the NinjaScript Editor, press F5.

## 1. Compile and start
1. Compile succeeds with no errors. If Windows blocks the DLL (0x800711C7), press F5 again.
2. In the NinjaTrader Output window, look for `NtStatusAddOn: listening on http://127.0.0.1:8765/status`.
3. In PowerShell: `Invoke-RestMethod http://127.0.0.1:8765/health`

## 2. Health (`/health`)
Command: `Invoke-RestMethod http://127.0.0.1:8765/health | ConvertTo-Json -Depth 5`

| Check | Expected |
|---|---|
| `ok` | true |
| `timestamp` | within a few seconds of now (UTC) |
| `uptime_seconds` | increases between two calls, resets after recompiling |
| `connections[]` | one entry per connected provider; `status` and `price_status` match the Connections indicator in the Control Center |
| Disconnect the provider (File > Disconnect) and call again | `status` / `price_status` change away from `Connected`; reconnect and confirm they return |

## 3. Accounts and positions (`/status`)
Command: `Invoke-RestMethod http://127.0.0.1:8765/status | ConvertTo-Json -Depth 6`

| Check | Expected |
|---|---|
| Sim101 present | name matches the Accounts tab exactly |
| `cash_value`, `realized_pnl`, `unrealized_pnl` | match the Accounts tab to the cent |
| Open one small Sim101 position manually, call again | position instrument, side, quantity, average price match the Positions tab; unrealized P&L moves |
| Flatten manually | position disappears |

## 4. Orders (`/orders`)
1. Place a far-from-market limit order on Sim101 by hand.
2. `Invoke-RestMethod http://127.0.0.1:8765/orders | ConvertTo-Json -Depth 5`
3. Expected: the order appears with the right instrument, action, quantity, price and a working state; cancel it manually and confirm it disappears.

## 5. Executions (`/executions?limit=20`)
1. Make one entry and one exit on Sim101.
2. `Invoke-RestMethod "http://127.0.0.1:8765/executions?limit=20" | ConvertTo-Json -Depth 5`
3. Expected: both fills present, time (UTC vs your timezone noted), price, quantity, entry/exit flags, commission and order name match the Executions tab.

## 6. Strategies (`/strategies`)
This is the endpoint the README flags as needing confirmation (`Account.Strategies`).
1. With no strategy running: response is a valid JSON with an empty list (or the call errors; record exactly what happens).
2. Enable one strategy by hand on a Sim101 chart (Strategies tab). Call `Invoke-RestMethod http://127.0.0.1:8765/strategies | ConvertTo-Json -Depth 5`
3. Expected: `account`, `name`, `instrument`, `state`, `position`, `quantity` match the Strategies tab.
4. Watch `state` go through `Historical` to `Realtime` as the strategy starts; record the values actually seen.
5. Disable the strategy manually: record whether it disappears from the list or shows another state (this matters for `strategy_unexpected_stop` and `strategy_missing`).
6. Open a position through the strategy if it trades: `position` and `quantity` update.

## 7. Staleness and failure behavior
1. Note the `timestamp` in `/health` and confirm it is generated per request, not cached. (If cached, record how old it gets.)
2. Close NinjaTrader: calls to port 8765 should fail with a connection error, not return old data.
3. Restart NinjaTrader and confirm the add-on comes back by itself after NT starts (or note that F5/compile is required).

## 8. Results

| Section | PASS / FAIL | Notes (field names that differ, wrong values, errors) |
|---|---|---|
| 1 Compile and start | PASS | user-reported 2026-11-06, no field notes |
| 2 Health | PASS | user-reported 2026-11-06, no field notes |
| 3 Accounts and positions | PASS | user-reported 2026-11-06, no field notes |
| 4 Orders | PASS | user-reported 2026-11-06, no field notes |
| 5 Executions | PASS | user-reported 2026-11-06, no field notes |
| 6 Strategies | PASS | user-reported 2026-11-06, no field notes |
| 7 Staleness and failure | PASS | user-reported 2026-11-06, no field notes |

## 9. What happens with the results
- Any FAIL or wrong value is a bug to fix in the Operator add-on before the supervisor consumes that field.
- Observed `state` values, the disabled-strategy behavior and the timezone of timestamps go into the supervisor data contract (Phase 0 doc, section 4).
- Gate 1 (incident detection under real failures) starts only after this gate passes.

**Status 2026-11-06:** all sections reported PASS by the user. Not recorded: the exact `state` values seen in `/strategies` and what a manually disabled strategy looks like. Those must be captured before the strategy checks are treated as verified.
