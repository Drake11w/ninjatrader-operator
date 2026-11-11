# Per-trade field gaps: derivable now vs add-on change vs nullable column

Research for GitHub issue #43 (parent #39). Question: for each per-trade field the closed-loop spec needs
(slippage, MAE, exit reason, regime, Central date), is it derivable from the current journal, trades and backtest
data, does it need a change to `NtStatusAddOn.cs`, or is it only suitable as a nullable column in v1? This reports
facts only. The v1 scope decision is a later grilling step.

Scope and privacy: public repo. No account identifiers, strategy parameters or private Lucid details. Sources are
operator `main` at commit `0fceecb` plus the closed ticket #34 findings (branch `research/operator-lucid-inventory`),
which listed these fields as the per-trade gaps. Anything inferred rather than read is marked "not verified".

## 1. Summary

| Field | Derivable now? | Add-on change? | Nullable column in v1? |
|---|---|---|---|
| Central date | Yes, from UTC fill times in Python. One decision needed on date convention (calendar date vs futures trading day). | No | Not needed |
| Exit reason | Partly: the exit order name is already carried; stop and target are recognisable by name, script-named exits need a name map. | No for the common case | Yes for unmapped names ("unknown") |
| Slippage | No. The only slippage on record is the assumed `slippage_ticks` cost input. Realized slippage needs an intended price. | Yes (intended/stop/limit price per fill) | Yes until then |
| MAE | Not from the journal. Approximately derivable by joining trades to 1-minute bars from `/history`. Exact value needs tick data or NinjaTrader's own figure. | Optional | Yes |
| Regime | No. No regime state exists in the operator. | Not an add-on matter: needs the Lucid classification as an input | Yes |

## 2. What a journaled fill and a built trade contain today

- Journal row: `account, execution_id, instrument, time, action, order_name, quantity, price, commission,
  is_entry, is_exit` plus tags `strategy, strategy_version, leg`, and `recorded_at` (`journal.py:10-30`). Dedup key is
  `(account, execution_id)` (`journal.py:27`).
- The add-on supplies exactly those fields per fill: `BuildExecutions` writes `account, execution_id, instrument,
  time, action, action_source, order_name, quantity, price, commission, is_entry, is_exit`
  (`NtStatusAddOn.cs:1034-1062`). `time` is UTC ISO 8601 (`Ts`, `NtStatusAddOn.cs:971-974`).
- Built trade (`build_trades`, `trades.py:27-83`): `account, instrument, side, quantity, entry_time, exit_time,
  entry_price, exit_price, entry_name, exit_name, strategy, strategy_version, leg, duration_sec, points, gross_pnl,
  commission, net_pnl` (`trades.py:56-66`). Trades are derived on demand, not stored.
- The module header states the gap directly: MAE/MFE and slippage vs signal "need bar/signal data the add-on does not
  expose yet, so they are not computed here" (`trades.py:4-5`). ROADMAP repeats it (`ROADMAP.md:25`, `ROADMAP.md:39`).
- Backtest trades come from two paths. The add-on `/backtest/run?trades=1` returns per trade `entry_time, exit_time,
  side, qty, entry_price, exit_price, entry_name, exit_name, profit, commission` (`NtStatusAddOn.cs:733-740`);
  `backtest_runner.to_trades_csv` rewrites those into a NinjaTrader Trades-grid CSV (`backtest_runner.py:111-127`);
  `backtest.parse` reads it (`backtest.py:91-136`) and adds `local_exit_date` (`backtest.py:110`). No slippage, MAE or
  regime column exists on either path.

## 3. Field by field

### 3.1 Central date

Verdict: derivable now, no add-on change.

- Journal and trade times are UTC (`NtStatusAddOn.cs:971-974`; the backtest import also converts to UTC,
  `backtest.py:107-110`), so a Central date is a conversion, not new data.
- The spec default timezone is `America/Chicago` (`STRATEGY_SPEC.md:10`, `STRATEGY_SPEC.md:19`;
  `strategy_spec.py:161`, `strategy_spec.py:264`). The v1 session validator only accepts `America/New_York`
  (`strategy_spec.py:90-91`), so the two spec versions disagree.
- The operator's own default for reports and performance is `America/New_York` (`trades.py:122`, `server.py:273`).
  `performance` buckets daily P&L by the exit date in that timezone (`trades.py:142`).
- Two date conventions already exist: calendar date in a timezone (`trades.py:131-142`) and futures trading day,
  where fills at or after `session_start_hour` (default 18) roll to the next date (`rules.py:26-31`,
  `report.py:34-36`). The backtest path stores the exit date as exported, in the export timezone
  (`backtest.py:110`, `backtest.py:134`, `backtest.py:136`).
- `/history` bar times and backtest trade times are NinjaTrader local time, not UTC (`NtStatusAddOn.cs:914`,
  `NtStatusAddOn.cs:733`; `history.py:6`). Which timezone that is depends on the machine setting (not verified).
- Open convention question for grilling: Central calendar date of entry, of exit, or Central futures trading day
  (`rules.trading_day` with `America/Chicago`, and which start hour)? Not verified which one the closed-loop spec
  means.

### 3.2 Exit reason

Verdict: partly derivable now; no add-on change needed for the common case.

- The exit fill's order name is journaled (`journal.py:17`, `NtStatusAddOn.cs:1058`) and carried into the trade as
  `exit_name` (`trades.py:60`). Backtest rows carry it too (`NtStatusAddOn.cs:736`, `backtest.py:112`).
- NinjaTrader names stop and target fills "Stop loss" and "Profit target" (`STRATEGY_SPEC.md:26`,
  `journal.py:50-51`), so stop vs target is recognisable by name today. Other exits (flat-by time, etc.) carry whatever
  name the script passes. The spec lets a script declare its names via `ninjascript.order_names`
  (`strategy_spec.py:243-246`, `STRATEGY_SPEC.md:26`), but that list exists to tag fills to legs; it is not a typed
  exit-reason mapping.
- `order_name` is empty when the Order object is gone, e.g. fills from before an NT restart
  (`NtStatusAddOn.cs:1058`: `e.Order != null ? e.Order.Name : ""`). Those exits would have no reason.
- Manual or flatten closes arrive as ordinary fills (`/flatten` is a separate action, `NtStatusAddOn.cs:797-811`).
  Whether their fill name is distinguishable is not verified.
- A normalized enum (stop, target, time/flat-by, manual, unknown) is a Python mapping layer plus an optional
  per-strategy name map. `unknown` or null covers the rest.

### 3.3 Slippage

Verdict: not derivable from the journal; realized slippage needs add-on data.

- The only slippage in the system today is a cost assumption: `costs.slippage_ticks` in the spec
  (`STRATEGY_SPEC.md:22`, `STRATEGY_SPEC.md:49`; validated at `strategy_spec.py:131-134`, `strategy_spec.py:287-290`),
  passed to NinjaTrader's backtest (`NtStatusAddOn.cs:588`, `NtStatusAddOn.cs:689`; `backtest_runner.py:11`,
  `backtest_runner.py:94`). It is an input, not a measured per-trade value, and backtest trade rows do not report it.
- Realized slippage needs an intended price per fill. A journaled fill has only the executed `price`
  (`NtStatusAddOn.cs:1059`). `BuildOrders` exposes `limit_price` and `stop_price`, but only for working orders, which
  drop out once filled (`NtStatusAddOn.cs:980-1007`), so a stop's price is gone at the moment it matters.
- `Execution.Order` is reachable in the add-on (`NtStatusAddOn.cs:1039-1043`). Whether the order's limit/stop price
  is still readable after the fill, and whether a script's signal price is available at all, is not verified. Signal
  price from inside a strategy would likely have to come from the strategy itself, outside the add-on.
- The roadmap and Phase 0 doc already name this as needing add-on data: `ROADMAP.md:39`,
  `docs/supervisor/SUPERVISOR_PHASE0.md:37`, `docs/supervisor/SUPERVISOR_PHASE0.md:103`.
- Partial shortcut: for stop and target exits an intended price could be computed from the spec's stop/target ticks
  and the entry fill if a leg uses fixed-tick levels. Structure-based stops cannot be reconstructed that way (spec
  `exit.stop` row, `STRATEGY_SPEC.md:21`). Not implemented anywhere today.

### 3.4 MAE

Verdict: not in the journal; approximately derivable from bar data the operator can already fetch; an exact value
needs more.

- `history.py` downloads Minute, Tick, Second or Day bars to CSV through the add-on `/history`
  (`history.py:1-9`, `history.py:22`, `NtStatusAddOn.cs:913-965`). Bar times are NinjaTrader local time at bar close
  (`NtStatusAddOn.cs:914`).
- With trade entry/exit times and side, MAE is the worst high/low against the entry over the spanning bars, converted
  with `POINT_VALUE` (`trades.py:12-13`). That is a join, not new add-on code. Accuracy is limited by bar resolution
  (1-minute bars hide intrabar order; bar-close stamps blur the first and last bar). Tick data is capped at 250000
  bars per call (`NtStatusAddOn.cs:914`) and chunked per day (`history.py:23`).
- For backtests the Analyzer trade object is iterated at `NtStatusAddOn.cs:730-740` (`NinjaTrader.Cbi.Trade`) but
  only the listed fields are emitted. Whether that object exposes MAE/MFE is not verified from this repo.
- No bar-to-trade join exists in the operator; MAE/MFE appear only in comments (`trades.py:4`, `ROADMAP.md:25`).
- Optional add-on change: emit NinjaTrader's own MAE/MFE for backtest trades (if available) and bars alongside live
  trades.

### 3.5 Regime

Verdict: not derivable; not an add-on gap.

- No regime state exists. ROADMAP lists "Regime state" as open, naming the Lucid labels BULL_TREND / BEAR_TREND /
  HIGH_VOL_RANGE / LOW_VOL_RANGE while noting its research mostly gates on VIX, event days and prior-day range
  (`ROADMAP.md:27`, `ROADMAP.md:30`, `ROADMAP.md:49`).
- Both spec versions state regime filters are not supported; gating is expressed as `gates[]` (day filters with
  `type, description, params`) (`STRATEGY_SPEC.md:26`, `STRATEGY_SPEC.md:52`).
- The inputs a regime needs (VIX, event calendar, prior-day range) are not in the journal. Prior-day range could be
  computed from `/history` day bars; VIX and events are not NinjaTrader data the add-on reads.
- The label has to come from the Lucid side (a per-date lookup, keyed by the Central date from 3.1) or stay null.

## 4. Where each field would live

- Central date and exit reason: computed keys on the trade dict in `trades.py` (`trades.py:56-66`). No journal
  migration.
- Slippage, MAE, regime: stored or joined values. The journal has no spare columns, so either an `ALTER TABLE` in the
  style of the `leg` migration (`journal.py:40-43`) or a side table keyed by `(account, execution_id)`.
- Backtest side: a new field must be added at both ends, the add-on JSON (`NtStatusAddOn.cs:733-740`) and the CSV
  bridge (`backtest_runner.py:111-127` header, `backtest.py:91-136` parser), or it is lost.

## 5. Facts that bear on the v1 decision

- Two of five fields (Central date, exit reason in common cases) need Python changes only.
- MAE has an approximate path with existing data; slippage does not.
- Regime depends on an external input, independent of the add-on.
- Simulator fills may not reproduce live slippage, so any slippage value would need a source label (sim/live). Not
  verified.
- An add-on change means recompiling in NinjaTrader. The add-on's strategy-control paths are noted as fragile to
  NinjaTrader updates (`NtStatusAddOn.cs:268`); `/executions` is the simple read path
  (`NtStatusAddOn.cs:1015-1068`).

## 6. Not verified

- Which local timezone NinjaTrader bar and backtest times use on a given machine.
- Whether `Execution.Order` retains stop/limit price after fill, and whether the Analyzer trade object exposes
  MAE/MFE.
- The fill names for flatten and kill-switch exits.
- Which date convention the closed-loop spec intends.
