# Inventory: what the operator computes vs what Lucid needs

Research for GitHub issue #34 (parent map #31). Question: what does the operator already compute (journal, trades,
performance, comparison, verdict, supervisor), what do the Lucid system's reporting, eval-odds and handoff export
expect, and what is missing for a feedback bundle (per-trade rows, summary metrics, verdict, drift flags) returned
from operator to Lucid?

Scope and privacy: this repo is public. Lucid is described by field names and concepts only. No strategy
parameters, account identifiers, credentials or Lucid source are reproduced. Sources are the code and docs of both
repos as of operator `main` at commit `0fceecb`; file names below are the primary sources. Where something was
inferred rather than read, it is marked "not verified".

## 1. Summary

The operator already computes almost everything a feedback bundle needs for the **per-trade rows**, **summary
metrics** and **verdict**, but only as in-memory JSON returned by MCP tools or rows in local SQLite files. There is
**no export, no file output and no schema for it** (ROADMAP Phase 4 `get_feedback` is unbuilt). Lucid has a clear
import side for the other direction (handoff bundles, `export_handoff`) and **no ingest side** for results. The gaps
are therefore mostly about (a) packaging and a versioned schema, (b) a few fields that Lucid's own trade and eval
tooling use and the operator does not produce (slippage, MAE, stop/target levels, normalized exit reason, regime
tag, eval odds from realized results), and (c) vocabulary mapping between the operator verdict and Lucid's evidence
statuses.

## 2. What the operator computes today

| Area | Source | Output | Persisted? |
|---|---|---|---|
| Journal | `journal.py` | One row per NinjaTrader execution: account, execution_id, instrument, time (UTC ISO), action, order_name, quantity, price, commission, is_entry/is_exit, plus tags strategy, strategy_version, leg. Deduped on (account, execution_id). | SQLite `executions` |
| Trades | `trades.py` `build_trades` | FIFO round trips: account, instrument, side, quantity, entry/exit time and price, entry/exit order name, strategy, version, leg, duration_sec, points, gross_pnl, commission, net_pnl. Also open lots and warnings. | No (derived on demand) |
| Performance | `trades.py` `performance` | trades, net_pnl, win_rate (fraction), avg_win, avg_loss, expectancy, profit_factor, max_drawdown (closed-trade equity peak to trough), avg_duration_sec, trading_days, best_day, worst_day, daily sharpe and sortino, breakdowns by weekday, entry hour, month (in a chosen timezone), warnings. | No |
| Rule tracker | `rules.py`, `get_rule_status` | Prop-rule state on realized P&L: target progress, trailing drawdown (EOD or intraday, lock), consistency, distance to breach, ok/warning/breach. | No |
| Backtest ingest | `backtest.py` | Strategy Analyzer Trades, Periods (Day) or Backtests-list exports parsed to trades, daily P&L and metrics (start, end, years, net_pnl, net_per_year, max_drawdown on the daily series, win_day_rate, best/worst day, plus trade stats). `check_backtest` compares them with the spec's claimed `expected_metrics` with a tolerance and reports ok/differs per metric. | SQLite `backtest_runs` (metrics, daily, trades JSON) |
| Comparison | `comparison.py` | Backtest vs Sim on 7 metrics (expectancy, win rate, profit factor, avg win, avg loss, trades/year, median holding time): backtest value, sim value, difference, flag ok/worse/better/shift/low_sample/n/a, and a summary consistent/diverging/low_sample/no_backtest. Needs 15 Sim trades before flags show. | No |
| Verdict | `verdict.py` | `reject` / `promote` / `keep_testing` with every check listed (name, status, actual, threshold, note), the criteria used, sample size, performance block, expected values and their source (backtest or spec), `edge_decayed`, warnings, an ETA to the minimum sample, and a recommended next step (recommendation only). Judged on the spec version's trades since it entered `sim`. | SQLite `verdicts` (`result_json`; history via `list_verdicts`) |
| Supervisor | `sup_perf.py`, `sup_behavior.py`, `sup_checks.py`, `sup_incidents.py` | Warning-level findings per strategy instance: `performance_drift` (verdict reject or edge decay), `trade_pace` (Poisson test vs the backtest rate), `holding_time_shift` (holding time outside the backtest band), `fills_unpairable`; plus operational findings (add-on unreachable, strategy not running, wrong account, and so on). Findings become incidents with severity, acknowledge and notify state. | SQLite incidents; alert log and attention flag file |
| Handoff in | `handoff.py`, `strategy_spec.py` | Validates a Lucid bundle (spec v2, `.cs` sha256 vs manifest, inputs and order names vs the C# source) and registers it as `candidate`. | Registry SQLite |
| Reports | `report.py` | Daily or weekly markdown summary from trades (and rules). Human-facing, not machine-readable. | No |

Notes that matter for the loop:
- Identity is solid: every trade carries spec id, version and leg (order-name tagging via `registry.order_name_map`),
  and the spec carries Lucid's own id (`lucid_id`). This is the natural join key for a feedback bundle.
- The verdict already separates "claim" from "measured": it prefers a measured Strategy Analyzer backtest over the
  spec's `expected_metrics` and reports which one it used.
- The backtest-vs-claim check (`check_backtest`) tells Lucid whether NinjaTrader reproduces Lucid's simulator
  numbers. That is simulator-fidelity information Lucid does not currently receive.
- Limits stated in the code: no MAE/MFE and no slippage vs signal price (the add-on does not expose bar or signal
  data); no regime tagging (ROADMAP open item); verdict queries read at most 5000 journal rows; the supervisor's
  trading-day count has no holiday calendar.

## 3. What Lucid produces and expects (field and concept level)

1. **Handoff bundle** (`export_handoff`, `handoff/<id>/<version>/`): `spec.json` (schema v2), `MANIFEST.json`
   (bundle version, creation time, Lucid commit, Lucid S-number, spec id and version, approval block, export
   timezone, per-file sha256 and compiled flag, pending Strategy Analyzer runs), `NOTES.md`, the `.cs` files. Bundles
   are immutable per (id, version) and carry an explicit approval gate and an `evidence_status` that is never
   upgraded. Spec `expected_metrics` keys: `trades_per_year`, `net_per_year`, `win_rate`, `expectancy`,
   `profit_factor`, `max_drawdown`, `profit_to_drawdown`, `avg_trades_per_day`, `eval_pass`, `eval_fail`,
   `eval_sessions_to_pass`, `chain_odds`. These are claims for later drift checks; regime filters, per-leg expected
   metrics and funded-phase rules are not in v2.
2. **Eval-odds simulator**: Lucid's primary metric. It takes a per-session dollar P&L series and a per-session
   intraday worst-adverse-excursion series at a given size (1 to 4 micros) and replays 63-session windows of a prop
   evaluation (profit target, EOD trailing drawdown with a lock, consistency rule, intraday excursion check).
   Outputs: pass share, fail share, sessions to pass, an eval edge (pass minus fail), an "eval-ready" flag,
   bootstrap percentiles, funded-phase survival and a combined "chain odds". Efficiency (profit over max drawdown)
   is kept as information only.
3. **Per-trade backtest record** (its backtest engine CSV): entry and exit time (naive local), direction, contracts,
   entry and exit price, stop and target levels, gross P&L, per-side commission, slippage paid, net P&L, exit
   reason, holding period in bars, same-bar and gap flags, cumulative P&L, exit group.
4. **Summary metrics block** (its backtest summary JSON): net profit, return, CAGR, daily Sharpe and Sortino, max
   drawdown in dollars and percent with timing and recovery, best and worst day, calmar and trade-validation
   statistics. Sharpe and drawdown there are computed on bar equity with Lucid's own risk-free and annualisation
   choices.
5. **Regime labels**: a detector with four labels (bull trend, bear trend, high-vol range, low-vol range) computed
   from price bars. Lucid's research mostly gates on VIX, event days and prior-day range rather than these labels.
6. **Statuses and rules of the research loop**: eval-ready, robust, "needs fresh-quarter / Sim confirmation", a frozen
   Sim period that tuning and verdicts must never use, edge-decay and bootstrap diagnostics, and an owner-approval
   rule for hand-off (the exporter never marks anything approved for live).
7. **Forward-test summary** in its execution bridge: account id, active position, order count, risk status, last few
   logs. This is not a results schema, and nothing in Lucid ingests operator output (not verified beyond a search of
   the repo for the operator name, which only finds the export side and the operator-path validator).

## 4. Field-level mapping for per-trade rows

| Need (Lucid trade record) | Operator today | Gap |
|---|---|---|
| Entry/exit time | UTC ISO strings | Timezone and format conversion: Lucid works in Central, naive. The bundle should state the timezone and also give the Central trading day. |
| Direction | `side` long/short | Rename only. |
| Contracts | `quantity` | Lucid streams are normalized to one micro; the bundle needs the quantity and either normalizes or records the contract multiple. |
| Entry/exit price | present | None. |
| Stop and target levels | absent | Not in executions. Derivable from the spec inputs, not from fills. |
| Gross P&L, commission, net P&L | present (commission per round trip) | Per-side split not kept; fine to drop. |
| Slippage paid | absent | Needs signal price (add-on does not expose it). Biggest fidelity gap for the slippage cost assumption. |
| Exit reason | `exit_name` (order name: stop, target, strategy exit names) | Normalize NinjaTrader names ("Stop loss", "Profit target", strategy-specific exit names) to Lucid's exit reasons. |
| Holding period | `duration_sec` | Convert seconds to minutes or bars. |
| Cumulative P&L | absent per trade | Trivial to add. |
| Per-session MAE | absent | The eval simulator needs it. It could be computed offline from bar history (`history.py` downloads NinjaTrader bars) plus trade times, but that is new work and a decision about who does it. |
| Regime / event-day tag | absent | Not yet specified (map #31 "Not yet specified"). Cheapest design: Lucid joins its own labels using trade timestamps and the trading day, so the operator only has to supply a reliable Central trading date. |
| Leg, spec id, version, Lucid id | leg, id, version present as tags | Include `lucid_id` (in the spec, not on trade rows). |
| Source (backtest / sim / live), account | account name present, source absent | Add a source field; replace or hash the account in anything published. |

## 5. Summary-metric mapping

| Lucid claim key (`expected_metrics`) | Operator realized value | Gap |
|---|---|---|
| `win_rate`, `expectancy`, `profit_factor` | computed | None (fractions and dollars match). |
| `trades_per_year` | computed in `comparison` (n / days * 252) | Only appears there; add it to the summary. |
| `net_per_year` | backtest only | Add for Sim. |
| `max_drawdown` | computed on closed trades | Definition differs from the backtest side (daily EOD in `backtest.metrics`; trailing and intraday in Lucid's evaluation). The bundle must name the definition. |
| `profit_to_drawdown` | not computed | Derivable (net over drawdown). |
| `avg_trades_per_day` | not computed | Derivable from trades and trading days. |
| `eval_pass`, `eval_fail`, `eval_sessions_to_pass`, `chain_odds` | not computed | The central gap. The operator has a rule tracker on one realized path, not window-replay odds. Options: ship the daily P&L (and MAE if available) and let Lucid run its own simulator, or port the simulator. Keeping the logic in Lucid avoids two diverging simulators. |
| Sharpe / sortino | daily, 252 annualisation, no risk-free | Not comparable to Lucid's metric block; Lucid ranks by eval odds, so treat as informational. |
| Sample size and window | `sample` and `since` in the verdict | Include since date, trade count, trading days, and a note when below the minimum sample. |

Also useful to Lucid and already computed: the daily P&L series (the input of the eval simulator), the
by-weekday / entry-hour / month breakdowns (a first cut at "where it lost" before real attribution exists), and the
backtest-reproduction check described in section 2.

## 6. Verdict

Operator: a three-state verdict with transparent checks and criteria echoed back, a hard drawdown stop, statistical
rejects (only with enough trades), an edge-decay z-test on all Sim trades and on the latest window, promotion that
needs a minimum of trades and days, an ETA, and a recommended next status. History is stored. It only recommends;
the existing live gates are unchanged.

Gaps for the bundle:
- **Vocabulary**: nothing maps `reject / promote / keep_testing` to Lucid's evidence statuses and tuning rules. An
  agreed mapping is needed, plus a statement that a promote verdict is a nudge for the owner, not an approval.
- **Provenance**: the bundle should carry the criteria (defaults are hard-coded in `DEFAULT_CRITERIA`, including the
  prop-account drawdown limit) so Lucid can tell when thresholds change.
- **Sample realism**: Lucid's own notes say a quarter of Sim cannot prove an edge for a strategy that trades a few
  times a week. The verdict's `keep_testing` plus ETA is the right shape, but the bundle should say clearly when a
  verdict is sample-limited.
- **Frozen-period alignment**: the verdict is measured from when the version entered `sim`; Lucid has a frozen Sim
  start that must stay out of its tuning. Both sides need to agree the bundle's `since` is that boundary.
- **Per-leg**: verdicts are per spec version and expected metrics are aggregate (v2 has no per-leg claims). Per-leg
  rows can be supplied from the trades, but there is nothing to compare them to.

## 7. Drift flags

Operator has: per-metric comparison flags; `performance_drift` (reject or edge decay); `trade_pace` (too few or too
many trades vs the claimed rate); `holding_time_shift`; `fills_unpairable`; backtest-vs-claim `differs`.
Supervisor incidents have severity, state and history.

Gaps:
- No **fill-quality / slippage drift** (needs signal prices).
- No **regime or calendar drift** (depends on regime tagging).
- No **artifact drift** at runtime: the `.cs` hash is checked at install, and nothing found in this review re-checks
  it against what is on the chart later (not verified; worth confirming in a later ticket).
- Supervisor findings mix strategy drift with operational incidents (launcher, add-on, login window). A bundle
  should include only strategy-level flags, with an explicit list and a stable `key`, and exclude operational ones.
- Findings are produced per supervisor cycle and only incidents persist. A bundle needs an "as of" time and a
  snapshot of the flags active at that time.

## 8. Packaging and transport gaps

- No output folder, file naming, schema version or idempotency rule for feedback (ROADMAP Phase 4 lists
  `get_feedback` and versioned feedback files as open).
- Lucid has no ingest module or documented place to read results from.
- No privacy filter: the journal holds account names. A public repo must never receive raw journals; any bundle that
  is committed publicly needs account fields removed or hashed, and Lucid-private parameters must not be copied in
  from the spec (feedback should reference spec id, version and manifest hash instead of inlining inputs).
- Join keys to echo back: `lucid_id`, spec id, spec version, manifest `.cs` sha256 values, the Lucid commit that
  built the bundle, the bundle version, plus the operator commit and a feedback schema version.
- Journal reads are capped (5000 rows per verdict evaluation); a long-running strategy needs a window rule.

## 9. Gap list for the SPEC (input to ticket #36)

1. Define the feedback bundle file set and schema (trades, daily P&L, summary, verdict, drift flags, provenance), with
   a schema version and an "as of" time.
2. Decide the eval-odds approach: send daily P&L (and optional MAE) and have Lucid compute odds, versus computing
   them in the operator. Suggested starting point: Lucid computes, so there is one simulator.
3. Decide whether MAE is in scope for the first version and, if so, who computes it from bar data.
4. Specify the trade-row field mapping in section 4 (names, units, timezone, exit-reason vocabulary, quantity
   normalization, source field).
5. Specify the summary-metric set and name the drawdown definition used.
6. Map operator verdicts to Lucid statuses and carry the criteria with them.
7. Choose the strategy-level drift flags to export, drop operational ones, and add missing ones later (slippage,
   artifact hash, regime).
8. Add the Central trading date to every row and leave regime tagging to Lucid.
9. Include the backtest-reproduction check as simulator-fidelity feedback.
10. Define transport and privacy rules: folder layout, file naming, anonymized accounts, no inlined private
    parameters, immutability and idempotent re-send.
