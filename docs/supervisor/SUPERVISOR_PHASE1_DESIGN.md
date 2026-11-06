# Strategy Supervisor: Phase 1 design (operational health monitoring)

Design only. No code written. Builds on `SUPERVISOR_PHASE0.md`. Scope is Phase 1 of the handoff: health profiles, connection and strategy-state monitoring, incident recording, basic alerts, with no AI dependency.

**Caveat from Phase 0:** the add-on endpoints this reads (`/health`, `/strategies`, `/orders`, `/status`) have not been compiled or checked against the NT UI (Gate 0). Treat every field below as an assumption until Gate 0 passes. Nothing here acts on NinjaTrader; Phase 1 is observe, record, notify.

## 1. Decisions

| Decision | Choice | Why |
|---|---|---|
| Process | Separate Python daemon, `supervisor.py`, run on its own (no MCP, no AI session) | Safety monitoring must work with the assistant disconnected |
| Repo placement | New top-level modules in this repo, prefixed `sup_` | Reuses `registry.py`, `journal.py`; keeps the separate-project boundary visible; easy to split out later |
| Data source | Add-on HTTP directly, SQLite files opened read-only | Avoids depending on MCP formatting; no new add-on code needed for Phase 1 |
| Storage | Own `supervisor.sqlite` (override `NT_SUPERVISOR_DB`) in `~/.ninjatrader-operator/` | Never writes to journal or registry |
| Actions | None in Phase 1 (alerts only) | Handoff: observation and diagnosis before action |
| Polling | Every 10 s, one fetch per endpoint per cycle | Add-on timestamp staleness is 30 s today; 10 s gives three samples per window |

## 2. Modules

```
sup_daemon.py     loop, scheduling, clean shutdown, writes heartbeat
sup_fetch.py      add-on HTTP client; returns Observation(source, ok, fetched_at, age_s, data|error)
sup_profile.py    load/validate health profiles, derive from registry specs
sup_checks.py     pure functions: (observations, profiles, prior state) -> list[Finding]
sup_incidents.py  open/update/close incidents, dedupe, persistence
sup_alerts.py     notifier interface (log file + Windows toast first)
```

`sup_checks.py` stays pure (no I/O) so every rule is unit-testable with fixtures, matching how `rules.py` and `verdict.py` are tested.

## 3. Operational checks (Phase 1 rule set)

Each check emits a Finding with a stable `key` so repeats update one incident instead of creating many.

| Key | Condition | Severity |
|---|---|---|
| `addon_unreachable` | `/health` fails or times out for 3 consecutive cycles | 2 (Degraded) |
| `data_stale` | add-on timestamp older than 30 s, or supervisor's own last good fetch older than 30 s | 2; observations from stale data are flagged, never treated as current |
| `account_disconnected` | connection `status` not Connected on an account used by a registered strategy | 2 |
| `feed_disconnected` | `price_status` not Connected | 2 |
| `strategy_missing` | profile expects an instance, `/strategies` has none | 2 |
| `strategy_not_running` | expected state Realtime but state differs for more than 60 s | 2 |
| `strategy_unexpected_stop` | was Realtime in an earlier cycle, now not, and no operator disable in `audit.jsonl` | 2 |
| `unknown_strategy` | instance on a monitored account that matches no profile | 1 (Warning) |
| `wrong_account` | registered strategy instance found on an account its profile does not authorize | 3 (Critical, alert only) |
| `position_mismatch` | strategy reports a position but its profile is not allowed to hold one now (outside session) | 1 |
| `journal_lag` | newest journal fill older than newest `/executions` fill by more than 5 min | 1 |
| `supervisor_degraded` | supervisor's own cycle took longer than 3x the interval | 1 |

Notes:
- Level 3 appears in Phase 1 only for the deterministic account-authorization check. Risk limits are Phase 3.
- "Add-on up but NT disconnected" and "add-on down" are separate findings, so infrastructure failures are not reported as strategy failures: when `addon_unreachable` or a connection check is open, strategy-level findings for that account are suppressed and linked to it as `caused_by`.
- No auto-recovery decisions here. If the watchdog (PR #21) is enabled, the supervisor only records its `/recovery` events.

## 4. Health profile schema (stored as JSON, versioned)

Profiles are derived from the registry spec plus an operator-approved overrides file, so hard limits never come from learned data.

```
{
  "profile_version": 1,
  "strategy": {"id": "s742", "version": "1.0.0", "leg": "orb"},
  "instance_names": ["s742 1.0.0 orb"],
  "account": "Sim101",
  "authorized_accounts": ["Sim101"],
  "instrument": "MNQ 12-26",
  "stage": "sim",
  "operating": {"expected_state": "Realtime", "sessions_ct": [["08:30", "15:00"]], "grace_seconds": 60},
  "hard": {"max_contracts": 1},
  "ranges": {},          // Phase 2
  "baselines": {}        // Phase 2, change-logged
}
```

Rules:
- `hard` and `operating` are only editable by a human (file plus a recorded change entry); the daemon never rewrites them.
- Instance identity today is the name string. A profile lists every name it should match; `unknown_strategy` catches renames.
- Strategies in stage `candidate` or `backtest` have no live profile and are ignored.

## 5. Database (`supervisor.sqlite`)

```
profiles(strategy_id, version, leg, profile_json, updated_at)
profile_changes(seq, strategy_id, version, leg, changed_by, diff_json, at)
observations(seq, source, fetched_at, ok, age_s, payload_hash)      -- sample log, pruned after N days
incidents(
  id TEXT PRIMARY KEY,                 -- e.g. INC-20261106-0001
  key TEXT, severity INT, status TEXT, -- open | acknowledged | resolved
  account TEXT, strategy TEXT,
  opened_at, last_seen_at, resolved_at,
  trigger TEXT, evidence_json TEXT, diagnosis TEXT,
  actions_json TEXT DEFAULT '[]',      -- always empty in Phase 1
  caused_by TEXT, needs_human INT, notified_at
)
incident_events(seq, incident_id, at, kind, detail_json)   -- append-only history
daemon_state(k, v)                     -- last heartbeat, last good fetch per source, schema version
```

- `(key, account, strategy)` identifies a condition; at most one non-resolved incident per condition.
- Resolution needs 3 consecutive clear cycles to avoid flapping.
- Severity is a field on the incident, never inferred later; escalation writes an `incident_events` row.
- Persistence across restarts: the daemon rebuilds in-memory state from `incidents` and `daemon_state` on start. After a computer restart it records a `supervisor_restarted` event and treats all previous observations as stale.

## 6. Alerts (Phase 1)

- Append-only `supervisor_alerts.log` plus a Windows toast for severity 2 and 3.
- Severity 1 is logged and shown in a read-only incident view only.
- Notifier is an interface so email or push can be added without touching checks.
- Dashboard integration (read-only incident panel on the existing dashboard) is a follow-up; the dashboard must never get action buttons.

## 7. Safety properties for Phase 1

1. Read-only toward NinjaTrader: no POST calls exist in `sup_fetch.py`.
2. A failed fetch is an observation, not an assumption of health or of failure.
3. Stale data is labelled and cannot close an incident.
4. The daemon's own death is detectable: it writes a heartbeat; a tiny watchdog check (Task Scheduler or the dashboard) flags a heartbeat older than 60 s.
5. Restart behavior is defined above; no state lives only in memory.
6. Simulated testing first: fixture replay, then Sim101.

## 8. Test plan

- Unit tests for every check with fixture observations (up, down, stale, mismatched account, missing instance, flapping).
- Incident lifecycle tests: dedupe, escalation, 3-cycle resolution, restart rebuild.
- Fake add-on (same pattern as `tests/test_server.py`) driving the daemon loop with scripted failures.
- Gate 1 on Sim101 once Gate 0 passes: kill NT, drop the connection, stop a strategy by hand, confirm the right incident appears within one minute and nothing is falsely reported as healthy.

## 9. Decisions recorded (2026-11-06) and what is still open

Decided by the user:
1. **Accounts:** Sim101 and the Lucid sim account. Both must be listed in each profile's `authorized_accounts`; anything else is `wrong_account`.
2. **Alerts:** log file plus Windows toast. No external services.
3. **Repo:** REVERSED (2026-11-06, later the same day): build inside ninjatrader-operator after all, as `sup_*` modules (section 1 applies again). The `Drake11w/desk-maanger` repo exists with an earlier copy of these docs and is not used for code. The supervisor may import Operator modules such as `control` and `registry`, but still only reads NinjaTrader.
4. **Startup:** hybrid. Task Scheduler starts the daemon at logon and restarts it on failure. Separately, the daemon writes an "attention flag" file (for example `~/.ninjatrader-operator/supervisor_attention.json`, listing open severity 2 and 3 incidents) in addition to the toast, so an agent session can read it on start and report outstanding incidents. The flag is read-only information for the agent: reading it grants no action permission. A stale daemon heartbeat also raises the flag.

5. **Hard limits:** account authorization only in Phase 1. No max-contracts or loss limits until the user supplies values (Phase 3). `hard.max_contracts` stays unset in profiles and no check reads it yet.

Still open:
6. Name and location of the new repo, and whether it should be public or private (the Operator repo is public; profiles may contain account names, so private is safer).

## 10. Build order once approved

1. `sup_checks.py` + tests (pure logic, no NT needed).
2. `sup_incidents.py` + schema + tests.
3. `sup_fetch.py` + fake add-on tests.
4. `sup_daemon.py`, alerts, heartbeat.
5. Gate 0 then Gate 1 on Sim101.

## 11. Added after the first build: performance and behavior drift (warning level only)

`sup_perf.py` + `sup_behavior.py`, run by the daemon every 5 minutes (cached in between, previous findings kept if a refresh cannot judge anything). All findings are severity 1 and never trigger an action.

| Key | Condition |
|---|---|
| `performance_drift` | the Operator's verdict engine reports `edge_decayed` or `reject` for the spec (recommendation text kept as evidence only; nothing is disabled or recorded) |
| `trade_pace` | trades since activation are far below or above the backtest rate (two-sided Poisson test, alpha 0.01, needs at least 5 expected trades) |
| `holding_time_shift` | holding times fall outside the backtest's 1st-99th percentile band more often than chance (binomial, p < 0.001, needs 15 sim and 30 backtest trades) |
| `fills_unpairable` | fills in the journal have no action, so trades cannot be rebuilt; the other checks are suppressed for that spec |

The sync writes new fills from GET /executions into the local journal (same as the dashboard). Known limit: the add-on emits an empty `action` for executions whose `Order` object is gone (for example fills from before an NT restart), which `trades.py` cannot pair. A fix in the add-on (derive the action from `Execution.MarketPosition` plus entry/exit) is not made or compiled.
