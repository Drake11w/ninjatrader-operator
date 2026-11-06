# ninjatrader-operator: feature roadmap

Goal: a closed loop between the Lucid trading system (strategy research) and NinjaTrader (execution reality).

    Lucid system --(candidate strategy spec)--> operator --(analysis + verdict)--> Lucid system
         ^                                          |
         +-----------(live/sim results, drift)------+

Note: the Lucid trading system repo is not among the public repos on Drake11w, so this roadmap is based on what is
visible in NinjaTrader (MNQ ORB strategy: "ORB long", "ORB time exit", "Stop loss", account LFE05079396550004 on Sim)
and needs to be reconciled with the real system's strategy format and metrics.

## Phase 1 - Observe (read-only; done / nearly done)
- [x] Accounts: cash value, realized/unrealized P&L
- [x] Open positions
- [x] Orders: working/pending orders, with strategy name and ATM/stop/target
- [x] Executions: fills with time, price, entry/exit, strategy name (source of truth for trade logs)
- [x] (needs NT8 compile check) Strategy status: enabled/disabled, which chart/instrument, current position, last signal
- [x] Connection health: connected/disconnected, data feed status, add-on heartbeat
- [x] Session/market info (`get_session_info`): current session, time to RTH open/close, front-month contract roll (holidays and early closes not modelled)

## Phase 2 - Record and analyze (still read-only)
- [x] Trade journal: persist every execution to SQLite (survives NT restarts), tagged by strategy + version (call `sync_journal` periodically; auto-sync not built)
- [x] Auto-tagging: `sync_journal` tags fills with spec id/version/leg from registered specs' `ninjascript.order_names` (only specs in sim/live-small/live; stop fills follow the latest entry; `leg` column added to the journal, old journals migrate). Fills synced before a spec was registered stay untagged
- [x] Round-trip trade builder: FIFO pairing into trades with duration and net P&L (`get_trades`); MAE/MFE and slippage vs signal price still need bar/signal data from the add-on
- [x] Performance tools (`get_performance`): net P&L, win rate, expectancy, profit factor, avg win/loss, max drawdown, daily Sharpe/Sortino,
      by weekday, entry hour, month. Still open: by regime (needs the regime state below)
- [x] Prop-firm rule tracker (`get_rule_status`): daily loss limit, trailing drawdown (EOD or intraday, optional lock), profit target, consistency rule,
      distance-to-breach in dollars, ok/warning/breach thresholds. Closed-trade basis (optional unrealized). `rules.example.json` now holds LucidFlex 50K values from the Lucid repo (target 3000, 2000 EOD trailing locking at 50100, 50% consistency; no daily loss limit found there, confirm against your account terms); push alerts not built
- [ ] Regime state (Lucid labels: BULL_TREND / BEAR_TREND / HIGH_VOL_RANGE / LOW_VOL_RANGE, but its research mostly gates on VIX, event days and prior-day range): expose the system's regime classification (trend/range/high-vol) alongside each trade
- [x] Daily/weekly report generator (`get_report`, markdown; HTML and sending not built)

## Phase 3 - Strategy evaluation bridge (the core of the integration)
- [x] Shared strategy spec v2 (`STRATEGY_SPEC.md`, `validate_strategy_spec`, example `examples/two-leg-mnq.strategy.json`): legs + NinjaScript mapping + CT times + Lucid eval-odds metrics, reconciled with `lucid-trading-system`. v1 still accepted. Still to confirm with the Lucid side: per-leg metrics, how it will emit specs (file drop vs tool call): one JSON/YAML schema (id, version, instrument, session, entry/exit rules,
      sizing, stops, parameters, hypothesis, expected metrics). Lucid system emits it; operator ingests it.
- [x] `submit_strategy(spec)` + `set_strategy_status`, `list_submitted_strategies`, `get_submitted_strategy`: immutable (id, version) registry; status moves one stage at a time candidate -> backtest -> sim -> live-small -> live (or rejected/retired with a reason); history kept. Bookkeeping only, no gating by metrics yet (that is the verdict engine)
- [x] Backtest ingestion (`import_backtest`, `list_backtests`, `check_backtest`, `compare_backtest_to_journal`; `backtest.py`): Strategy Analyzer Trades grid, Periods grid (Period = Day) or Backtests list (summary rows only: net, trade count; latest run per leg) CSV, stored per spec version, metrics checked against the spec's expected_metrics, day-by-day comparison with journaled trades. The Backtests list parser was checked on a real export; the Trades/Periods grids were written from documented columns and are unverified. XML and Lucid-format results not supported
- [x] Automated Strategy Analyzer runs (`backtest_runner.py`, add-on `POST /backtest/run`): drives an open Analyzer window per leg, collects the trade list, applies commission, imports and checks against the spec. Verified on S742 (2020-2026, all metrics within 15%). Experimental: relies on private NinjaTrader 8.1.8.4 members; needs one Analyzer window open; use a specific front-month contract (`MNQ 12-26`), not `MNQ ##-##`. Still open: the other four bundles (S584, S663, S674, S713), a commission template in NinjaTrader, and warm-up/Monte Carlo robustness checks
- [~] Comparison tools (`compare_backtest_to_sim`, dashboard table; `comparison.py`): backtest vs sim on identical metrics with flags. Still open: live column, fill quality and slippage (needs signal prices from the add-on)
- [ ] Robustness checks: walk-forward splits, parameter sensitivity, Monte Carlo trade reshuffle,
      worst-case drawdown vs prop-firm limit, minimum sample size warnings
- [x] Verdict engine (`evaluate_strategy`, `evaluate_all_strategies`; `verdict.py`): criteria in `DEFAULT_CRITERIA` (30 trades / 20 days minimum, expectancy > 0, PF >= 1.2, drawdown <= 50% of the $2,000 limit, sim expectancy >= 40% of claim). Hard rejects: drawdown >= 90% of limit at any sample size; after 20 trades, negative expectancy with PF < 0.8, or win rate z < -2.5 vs claim. Recommends only; never disables or promotes
- [~] Experiment log: verdicts stored with `evaluate_strategy(record=true)`, read with `list_verdicts`. Still open: logging backtests/parameter variants together with verdicts

## Phase 4 - Feedback to the Lucid system
- [ ] `get_feedback(strategy_id)`: structured results (per-trade rows + summary + verdict + drift notes)
      in the same schema the Lucid system consumes
- [ ] Parameter suggestions flagged as hypotheses, not auto-applied (human or Lucid-system review gate)
- [ ] Failure attribution: where it lost (regime, time-of-day, slippage, news days) to guide the next iteration
- [ ] Versioned feedback files committed to a results folder/repo so the Lucid system can pull them

## Phase 5 - Controlled actions (only after Phases 1-4 are trusted)
All of these sit behind server-side guards, not just prompts:
- [x] Daily-loss kill switch (`arm_kill_switch`, in the add-on, latched flatten+cancel; in-memory, Sim-only). Still open: max position size, max trades/day, allowed instruments/hours, persistence across NT restarts, disabling the strategy itself
- [x] Handoff bundle import (`install_handoff`; `handoff.py`): checks approval flag, spec, .cs sha256 vs MANIFEST, inputs/order names vs C#; with apply=true backs up + copies the .cs into Custom\Strategies, registers the candidate, writes strategy templates when a fresh skeleton exists. Tested with fake bundles only
- [x] Start/stop strategies (`enable_strategy` / `disable_strategy`, add-on `/strategy/enable|disable`): works on Sim101 as of 2026-11-06 (NT 8.1.8.4, both S742 legs reached Realtime) but calls NT8 private StrategiesGrid routines by reflection, so it can break on an NT update. NT's own StrategySetDefaults throws a NullReferenceException from the add-on, so `enable_strategy(skip_defaults=true)` is required and the add-on sets bars, hours, instrument and 30 days of history itself. Sim-only, needs NT_OPERATOR_ALLOW_STRATEGY_START=1, all-or-nothing legs, conflict check, audited, failed adds are removed. Input values are not read back: check them in the Strategies tab
- [ ] Pause/resume a strategy (disable/enable only; never changes parameters live)
- [x] Flatten and cancel-all tools (`flatten`, `cancel_all_orders`; reduce risk only). Unverified in NT8: Account.Flatten / Account.Cancel
- [x] Sim-only enforcement: both the add-on and server refuse non-Sim accounts unless NT_OPERATOR_ALLOW_LIVE=1
- [x] Audit log of every control action (time, params, reason, result; refusals included): `get_audit_log`
- [ ] Order entry: last, if ever; sim accounts first; human confirmation per order

## Cross-cutting
- Security: localhost-only, optional shared token, GET-only in read mode, separate write endpoint that is off by default
- Reliability: add-on heartbeat, timestamps on all data, stale-data warnings so Claude never reasons from old numbers
- Data model: one stable schema for strategy specs, trades and metrics; schema version field
- Testing: replay recorded NT data through the server; unit tests for metric math and rule tracker
- Repo hygiene: no account numbers or credentials committed; config file for Lucid rules and thresholds

## Suggested build order
1. Executions + orders + strategy status (Phase 1 finish)
2. Trade journal + round-trip builder + metrics (Phase 2)
3. Prop-rule tracker with alerts
4. Strategy spec schema agreed with the Lucid system, then ingestion + comparison + verdict (Phase 3)
5. Feedback export (Phase 4)
6. Guarded controls (Phase 5)

## Open questions for the Lucid system
- Where does it live and what language/format are strategies in? (needed to define the shared spec)
- What metrics and regime labels does it already compute?
- Should verdicts be advisory only, or gate automatic promotion between stages?
- Exact Lucid rule values (daily loss, trailing DD, targets, consistency rule, allowed trading times)
