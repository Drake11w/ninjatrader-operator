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
- [x] Round-trip trade builder: FIFO pairing into trades with duration and net P&L (`get_trades`); MAE/MFE and slippage vs signal price still need bar/signal data from the add-on
- [x] Performance tools (`get_performance`): net P&L, win rate, expectancy, profit factor, avg win/loss, max drawdown, daily Sharpe/Sortino,
      by weekday, entry hour, month. Still open: by regime (needs the regime state below)
- [x] Prop-firm rule tracker (`get_rule_status`): daily loss limit, trailing drawdown (EOD or intraday, optional lock), profit target, consistency rule,
      distance-to-breach in dollars, ok/warning/breach thresholds. Closed-trade basis (optional unrealized). Needs real Lucid values in the rules file; push alerts not built
- [ ] Regime state: expose the system's regime classification (trend/range/high-vol) alongside each trade
- [x] Daily/weekly report generator (`get_report`, markdown; HTML and sending not built)

## Phase 3 - Strategy evaluation bridge (the core of the integration)
- [~] Shared strategy spec (v1 DRAFT in `STRATEGY_SPEC.md` + `validate_strategy_spec`; needs reconciling with the Lucid system's real format): one JSON/YAML schema (id, version, instrument, session, entry/exit rules,
      sizing, stops, parameters, hypothesis, expected metrics). Lucid system emits it; operator ingests it.
- [ ] `submit_strategy(spec)`: register a candidate; status moves candidate -> backtest -> sim -> live-small -> live
- [ ] Backtest ingestion: import NinjaTrader Strategy Analyzer exports (CSV/XML) and/or Lucid backtest results
- [ ] Comparison tools: backtest vs sim vs live on identical metrics; flag drift (fill quality, slippage, win-rate gap)
- [ ] Robustness checks: walk-forward splits, parameter sensitivity, Monte Carlo trade reshuffle,
      worst-case drawdown vs prop-firm limit, minimum sample size warnings
- [ ] Verdict engine: explicit, written promotion criteria (e.g. N trades, expectancy > X after costs,
      max DD < Y% of the firm limit, live/sim drift < Z). Output: promote / keep testing / reject, with reasons
- [ ] Experiment log: every spec, version, result and verdict stored so nothing is re-tested blindly

## Phase 4 - Feedback to the Lucid system
- [ ] `get_feedback(strategy_id)`: structured results (per-trade rows + summary + verdict + drift notes)
      in the same schema the Lucid system consumes
- [ ] Parameter suggestions flagged as hypotheses, not auto-applied (human or Lucid-system review gate)
- [ ] Failure attribution: where it lost (regime, time-of-day, slippage, news days) to guide the next iteration
- [ ] Versioned feedback files committed to a results folder/repo so the Lucid system can pull them

## Phase 5 - Controlled actions (only after Phases 1-4 are trusted)
All of these sit behind server-side guards, not just prompts:
- [ ] Hard daily-loss cap, max position size, max trades/day, allowed instruments/hours, kill switch
- [ ] Pause/resume a strategy (disable/enable only; never changes parameters live)
- [ ] Flatten-all emergency tool (reduces risk only)
- [ ] Sim-only enforcement flag: control tools refuse to act on non-sim accounts unless explicitly unlocked
- [ ] Audit log of every control action with who/why/result
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
