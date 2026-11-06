# Gate 1: supervisor incident detection under real failures (Sim101)

Goal: prove the daemon reports each real failure within about a minute, never reports health it did not observe, and clears the incident after recovery. Sim101 only. Run with the supervisor daemon and the watcher scheduled (`scripts/install_supervisor_task.ps1`, `scripts/install_watch_task.ps1`), the dashboard open, and a strategy enabled on Sim101 that is listed in your supervisor profile.

Nothing here needs a POST call or any `NT_OPERATOR_*` variable. I (the agent) cannot run this gate: it needs you at the NinjaTrader console. After each step, check the Supervisor panel on the dashboard, `python sup_daemon.py --status`, and `~/.ninjatrader-operator/supervisor_alerts.log`.

Record PASS / FAIL / NOTES in the table at the bottom. Also record the exact time of the action and the time the incident appeared.

## 0. Baseline
1. `python sup_daemon.py --status` shows `heartbeat_stale: false`.
2. The dashboard shows "Daemon running" and no active incidents.

## 1. Add-on unreachable
1. Close NinjaTrader (or stop the add-on by recompiling with an error).
2. Expect: `addon_unreachable` (severity 2) within about 30 to 60 s (3 failed cycles). A phone/toast alert from the watcher within 2 minutes.
3. Expect: no strategy is reported as "running" or "stopped" while the add-on is down (those checks are suppressed on stale data).
4. Restart NinjaTrader. Expect the incident to resolve after 3 clean cycles, not instantly.

## 2. Data connection lost
1. File > Disconnect (data provider).
2. Expect: `feed_disconnected` or `account_disconnected` (severity 2) naming the provider.
3. Reconnect. Expect resolution after 3 clean cycles.

## 3. Strategy stopped by hand
1. Disable the Sim101 strategy from the Strategies tab.
2. Expect: `strategy_unexpected_stop`, `strategy_not_running` or `strategy_missing` (depending on what `/strategies` does for a disabled strategy; record which).
3. Re-enable it. Expect resolution after 3 clean cycles.
4. Record the `state` values seen in `/strategies` (Historical, Realtime, others).

## 4. Wrong account
1. Enable a strategy on an account that is not in the profile's `authorized_accounts` (use a second Sim account).
2. Expect: `wrong_account` at severity 3, needs_human set, and a toast.

## 5. Supervisor dies
1. Stop the daemon (`Stop-ScheduledTask -TaskName 'NinjaTrader Strategy Supervisor'`, then kill the python process if it keeps running).
2. Expect: within 2 minutes the watcher reports "SUPERVISOR NOT RUNNING"; the dashboard shows a red "Daemon not running" pill.
3. Start it again. Expect "Supervisor is running again." once.

## 6. Fills have an action (add-on fix)
1. After recompiling the add-on, restart NinjaTrader, then make one entry and one exit on Sim101.
2. `Invoke-RestMethod "http://127.0.0.1:8765/executions?limit=20" | ConvertTo-Json -Depth 5`
3. Expect: every fill has `action` set, with `action_source` of `order` (or `derived` for fills whose Order object is gone). For any `derived` fill, confirm by hand that the action is right (long entry Buy, long exit Sell, short entry SellShort, short exit BuyToCover). If a `derived` action is wrong, the assumption about `Execution.MarketPosition` is wrong and the C# must be changed.
4. Expect: no `fills_unpairable` incident afterwards.

## 7. Results

| Section | PASS / FAIL | Time of action | Time incident appeared | Notes |
|---|---|---|---|---|
| 1 Add-on unreachable | | | | |
| 2 Data connection lost | | | | |
| 3 Strategy stopped | | | | |
| 4 Wrong account | | | | |
| 5 Supervisor dies | | | | |
| 6 Fill action | | | | |

Any FAIL is a bug in `sup_checks.py` or the add-on to fix before the supervisor is trusted.
