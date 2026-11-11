# Strategy Supervisor: Phase 0 foundation assessment

Read-only assessment of ninjatrader-operator. No code was changed. Evidence: repo at local `main` (1250139), `origin/main` (9f238ca), the `origin/feature/reconnect-strategy-recovery` branch, source reading, and `python -m unittest discover tests` (130 run, OK, 1 skipped). NinjaTrader was not running, so nothing below marked "unverified" was exercised live.

## 1. Headline findings

1. **The reconnect recovery watchdog is not on `main`.** The handoff assumes it exists. It lives only on `origin/feature/reconnect-strategy-recovery` (264 lines in `NtStatusAddOn.cs`, plus `/recovery` and `get_recovery_status`). Its own README says it has never been compiled or verified in NinjaTrader. PR #19's merge commit (f32ba26) is not an ancestor of `origin/main`; check what it was merged into.
2. **PR #20 removed the handoff-approval check from `deploy_strategy_live_small`** (it is in `main`). That is a safety gate the supervisor's design should not rely on.
3. **The add-on's new endpoints are mostly unverified in NT.** The README states orders/executions/strategies/health and the controls "have not been compiled in NT8 yet". Verified on NT 8.1.8.4 / Sim101: strategy enable (S742 legs reached Realtime) and the Strategy Analyzer backtest run. Live-small has not been verified on a real account.
4. **There is no incident store, no continuous monitor, and no persistence of runtime state.** Everything is request/response. The kill switch and recovery memory are in-process and lost on NT restart.
5. **An autonomous action path already exists**: `disable_decayed_strategies` (dry-run by default) disables legs and retires the spec based on a statistical verdict. This conflicts with safety requirement 2 ("no automatic strategy changes based solely on statistical anomalies") if ever automated.

## 2. Capability map (what exists, how verified)

| Need | Existing source | Status |
|---|---|---|
| Add-on liveness | `/health` (`uptime_seconds`, `timestamp`), `age_seconds` + STALE warning after 30 s in `server.py` | Code only; add-on compile unverified |
| Account connection / data feed | `/health` per account: `status`, `price_status` | Same |
| Strategy state per instance | `/strategies`: account, name, instrument, `state` (e.g. Realtime), position, quantity | Same; README flags `Account.Strategies` as needing confirmation |
| Positions, orders, fills | `/status`, `/orders`, `/executions?limit=N` | Same |
| Durable trade record | `journal.py` SQLite, dedupe on (account, execution_id), tagged strategy/version/leg | Unit-tested; sync is manual (`sync_journal`, dashboard refresh every 15 s) |
| Round trips and metrics | `trades.py` (FIFO), `get_performance` | Unit-tested; point-value table built in; no MAE/MFE or slippage |
| Strategy registry and lifecycle | `registry.py` SQLite, immutable (id, version), stage history | Unit-tested; bookkeeping only |
| Prop-rule distance | `rules.py`, `rules.example.json` | Unit-tested; **placeholder values**, closed-trade basis, no alerts |
| Backtest vs sim vs live | `backtest.py`, `compare_backtest_to_journal`, `verdict.py` (`DEFAULT_CRITERIA`) | S742 verified end to end; other bundles open |
| Session state | `sessions.py` | Holidays and early closes by date rule only (`special_session`); times not adjusted |
| Controls | flatten, cancel_all, kill switch, enable/disable strategy, live-small deploy | Sim-gated; live-small unverified; audited in `audit.jsonl` |
| Reconnect recovery | feature branch only | Unverified, opt-in, in-memory |

## 3. Gaps against the supervisor's needs

- **No continuous evaluation loop.** Nothing polls on a schedule outside the dashboard. The supervisor needs its own local daemon (Python) that does not depend on an AI session.
- **No incident model or storage.** Needs a new SQLite table (ID, severity, evidence, actions, status, approval flag).
- **No health profile.** The spec has `expected_metrics` and legs but no hard-constraint / operating-range / baseline split, no permitted sessions per strategy beyond leg `session`, and no per-strategy daily-loss or exposure limits.
- **Strategy instance identity is the name string** (`"{id} {version} {leg}"`). Stability across NT restarts and duplicate instances is unresolved.
- **Account-level risk is not authoritative.** `get_rule_status` uses closed trades from the journal with example rule values. There is no aggregate exposure across strategies and no broker-side limit; the README itself says `max_contracts` is not a loss limit.
- **Behavior baselines are missing:** no slippage-vs-signal, no per-session trade count baseline, no holding-time baseline stored (holding time is derivable from `trades.py`).
- **Stale-data semantics are shallow.** One 30 s threshold on add-on timestamp; no distinction between "add-on up but NT disconnected" and "add-on down" in a persisted way, and journal freshness is not tracked.
- **Add-on only runs while NT runs.** The "critical safety works with the AI disconnected" requirement is met only by the add-on kill switch (flatten only, in-memory). Nothing flags NT process death.
- **Token is optional for non-live-small reads/controls**; supervisor-issued actions should require it.

## 4. Proposed data contract (supervisor consumes, read-only)

All reads via the existing add-on HTTP endpoints (not the MCP layer), each stamped with `fetched_at` and `age_seconds`:

- `GET /health` -> add-on up, uptime, per-account `status`, `price_status`.
- `GET /strategies` -> instance state, position, quantity (add: stable instance ID, enabled flag, if obtainable).
- `GET /orders`, `GET /status`, `GET /executions` -> exposure, working orders, P&L.
- Journal and registry SQLite files, opened read-only.
- Recovery status (`/recovery`) once merged and verified.

Needed additions before Phase 1: (a) a supervisor-owned store (`supervisor.sqlite`: profiles, incidents, baseline snapshots, observations); (b) a profile schema (see section 5); (c) a heartbeat sample log so outages can be reconstructed after the fact.

## 5. Health profile shape (proposal)

```
identity:     spec id, version, leg, account, instrument, instance name(s)
hard:         authorized accounts/instruments, max contracts, permitted sessions, daily loss $, drawdown $   (deterministic, Level 3)
ranges:       expected state, trades/session band, holding-time band, slippage band                        (Level 1-2)
baselines:    backtest/sim expectancy, PF, win rate, drawdown distribution                                 (versioned, changes recorded)
```

Hard limits come from the registry spec and an explicit operator-approved file, never from learned baselines.

## 6. Module plan

| Module | Phase | Inputs | Depends on |
|---|---|---|---|
| Supervisor daemon + scheduler | 1 | add-on HTTP, own DB | Add-on verified in NT |
| Operational Health Monitor | 1 | `/health`, `/strategies`, process check | Daemon |
| Incident store + basic alerts | 1, 4 | all monitors | Daemon |
| Behavior Monitor | 2 | journal, `trades.py` | Journal auto-sync |
| Risk Monitor | 3 | `/status`, `/orders`, `rules.py`, profiles | Real rule values |
| Policy engine + approval workflow | 4-5 | incidents, permissions matrix | Incident store |
| Performance Monitor | 6 | `verdict.py`, backtest data | Baselines |

## 7. Permissions and autonomy matrix (proposed starting point)

| Capability | Phase | Autonomous? | Notes |
|---|---|---|---|
| Observe, record incidents, report | 1-4 | Yes | Read-only |
| Notify user | 1 | Yes | |
| Reconnect recovery (existing watchdog) | after verification | Yes, only for allowlisted account + strategy | Already refuses with open position/working order; Sim101 first |
| Flatten / cancel / kill switch | 5 | Only a preapproved Level 3 procedure | Reduces risk only |
| Disable strategy | 5 | Only for deterministic hard-limit breach | Never for statistical decay alone |
| `disable_decayed_strategies` | n/a | **No**; human approval | Existing tool, dry-run default |
| Enable / deploy live-small | n/a | **No** | Human only |
| Order entry | n/a | **Never** | |

## 8. Test and acceptance plan

- Gate 0 (before any supervisor code): add-on compiled in NT; `/health`, `/strategies`, `/orders`, `/executions` return correct data on Sim101 compared with the NT UI.
- Gate 1: kill NT, drop the connection, stop a strategy by hand; supervisor records each as the correct incident within a stated time bound; stale data never shown as current.
- Gate 2: replay recorded journal data through the behavior monitor; known-abnormal day flagged, normal days not (false-positive rate recorded).
- Gate 3: forced limit breach on Sim101 produces the expected Level 3 incident with evidence.
- Gate 5: every action verified by re-reading state, failed actions reported, audit entry present.
- All of it on Sim101 before any real account.

## 9. Answers to the handoff's open questions (so far)

- Reliably observable: connection and price status, strategy state, positions, orders, fills (once compile verified).
- Already in journal: fills, commissions, strategy/version/leg tags; trades and P&L derivable.
- Must be collected: slippage vs intended price, instance identity, NT process state, heartbeat history.
- Authoritative account risk source: undecided; today only the journal-derived rule tracker, which is not authoritative.
- Persistence: incidents and profiles go in SQLite; add-on state (kill switch, recovery memory) remains in memory and needs an explicit restart policy.

## 10. Recommended next steps (in order)

1. Pull `origin/main`; decide whether PR #20's removal of the approval gate is intended.
2. Decide the fate of `feature/reconnect-strategy-recovery` (merge, or keep as the base for supervisor recovery), and run it on Sim101.
3. Compile the current add-on in NT and run Gate 0 against the NT UI.
4. Get real prop-rule values and decide the authoritative source for account risk.
5. Then start Phase 1 (health profiles, daemon, incident store).
