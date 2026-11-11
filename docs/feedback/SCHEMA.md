# Feedback bundle schema (v1.0.0)

What `export_feedback` (`feedback.py`) writes for the research side (Lucid). Machine-readable form:
[`feedback.schema.json`](../../feedback.schema.json). Golden examples (synthetic): [`examples/feedback/`](../../examples/feedback/)
(`ready`, `not_ready`, `nullable_fields`, `empty_period`). All ids, accounts and numbers in this repo are made up.

## Layout

```
<NT_FEEDBACK_DIR>/<lucid_id>/<version>/<as_of>/   one immutable snapshot
    MANIFEST.json  trades.csv  daily_pnl.csv  summary.json  verdict.json  drift.json  backtest_check.json
<NT_FEEDBACK_DIR>/<lucid_id>/<version>/latest.json  {as_of, snapshot, content_sha256, schema_version}
```

- `NT_FEEDBACK_DIR` defaults to `~/.ninjatrader-operator/feedback/`, outside every git repo. The operator is the only
  writer; Lucid only reads. Neither `handoff/` nor `feedback/` is ever committed to this repo.
- The snapshot folder is the compact UTC `as_of` (`20261030T120000Z`); `MANIFEST.as_of` is ISO (`2026-10-30T12:00:00Z`).
- `lucid_id` is the spec's Lucid S-number. A spec without one cannot be exported.

## Updates

- Append-only. A run whose files are byte-identical to `latest.json`'s `content_sha256` is a **no-op** (`status: unchanged`),
  whatever the clock says.
- Changed results write a **new** snapshot and move `latest.json`; old snapshots are never edited.
- A **correction** (`correction=true`) is a new snapshot whose `MANIFEST.supersedes` names the previous `as_of`.
- Exporting a different result under an `as_of` that already exists is refused (snapshots are immutable).
- Files are written to a temp folder and renamed, and `latest.json` is replaced atomically, so a reader never sees half a snapshot.

## Versioning

One semver `schema_version` in `MANIFEST.json` (and `latest.json`). A **minor** bump is additive: readers ignore unknown
fields and columns. A **major** bump is breaking: readers must refuse a major they do not know. A schema change bumps
this number, `feedback.schema.json` and the golden bundles in the same commit.

## MANIFEST.json

`schema_version`, `as_of`, `lucid_id`, `spec_id`, `spec_version`, `status` (registry status), `since` (when the version
entered Sim: the boundary of the sample), `handoff` (`bundle_version`, `lucid_repo_commit`, `cs_sha256` per `.cs` file,
echoed from the handoff manifest; `null` if the bundle is not found), `operator_commit`, `supersedes`,
`content_sha256` (hash over the other files, independent of `as_of`), `files` (`sha256` and `bytes` per file).

## trades.csv

One row per closed round trip, in exit order. Partial exits and scale-ins are split into one row per matched quantity.

| column | meaning |
|---|---|
| `trade_id` | stable 12-hex id from account alias, instrument, times, prices and quantity |
| `source` | `sim` (account name starts with `Sim`) or `live` |
| `account` | alias `acct-N` from a local mapping file; never the real name |
| `instrument`, `side`, `quantity`, `leg` | contract (e.g. `MNQ 12-26`), `long`/`short`, contracts matched, spec leg name |
| `entry_time`, `exit_time` | UTC, ISO 8601 with `Z` |
| `entry_price`, `exit_price` | price in instrument points |
| `entry_name`, `exit_name` | NinjaTrader order names as filled (may be empty) |
| `exit_reason` | `stop` (`Stop loss`), `target` (`Profit target`), `strategy_exit` (a name the spec lists), `other`, `unknown` (empty name, e.g. after an NT restart) |
| `duration_sec` | seconds between entry and exit |
| `points` | price difference in the trade's direction, times quantity |
| `gross_pnl`, `commission`, `net_pnl` | USD; point values from the operator's built-in table; commission is the sum of both fills' share |
| `trading_date_ct` | **calendar** date of `exit_time` in the spec's time zone (default `America/Chicago`). It is the same bucketing the daily performance uses. It is not the CME trading day (which starts 17:00 CT the evening before). |
| `slippage_ticks`, `mae_points`, `regime` | **always empty in v1.** Slippage needs intended prices from the add-on; MAE needs bar data; regime needs Lucid's labels. Reserved so adding them is a minor bump. |

Empty cells are nulls. A period with no trades is the header row only.

## daily_pnl.csv

`date_ct, trades, net_pnl, cumulative_net_pnl`: net P&L per `trading_date_ct`, USD, ascending. This is the input to
Lucid's own eval simulator; the odds are computed there, not here.

## summary.json

Realized metrics over the trades in `trades.csv`: `trades`, `net_pnl`, `win_rate`, `avg_win`, `avg_loss`, `expectancy`,
`profit_factor`, `max_drawdown`, `best_day`, `worst_day`, `sharpe`, `sortino`, `trading_days`, `avg_duration_sec`,
`first_exit`, `last_exit`, `by_source`, `warnings`. `max_drawdown` is defined in the file itself (`drawdown_definition`): the
largest peak-to-trough fall of cumulative closed-trade net P&L in USD, trades in exit order, starting from zero. Sharpe and
Sortino are annualised (252) from daily P&L. Fields are `null` when there is nothing to compute.

## verdict.json

The verdict engine's result (`verdict`: `promote`, `keep_testing` or `reject`; `reasons`; every `check` with its `status`,
`actual` and `threshold`; the `criteria` used; `sample`; `edge_decayed`), plus the **readiness block**.

### readiness

`state` is `ready`, `not_ready` or `rejected`. `summary` is one line (`not ready: sample_trades 18/30, ...`). `criteria` is
a list of `{key, status, actual, threshold, progress, note}` with `status` `pass`, `fail` or `pending`:

| key | passes when |
|---|---|
| `in_sim` | the version's status is `sim` |
| `baseline` | its backtest is imported and reproduces the spec's `expected_metrics` (missing is `fail`, "missing baseline") |
| `sample_trades`, `sample_days`, `sample_weeks` | at least 30 closed Sim trades, 20 trading days, 4 whole weeks since Sim start |
| `verdict` | the verdict engine says `promote` |
| `no_edge_decay` | no edge-decay flag over all Sim trades or the last 20 |
| `expectancy_vs_claim` | Sim expectancy is at least 40% of the backtest's |
| `trade_frequency` | Sim trades per year (trades / days x 252) is 0.5x to 1.5x the claimed pace |
| `untagged_fills` | zero fills in the period on the version's accounts, in its instrument, without a strategy tag |

A spec's `readiness_overrides` may only make floors stricter (higher floors, a narrower frequency band); looser values are
ignored and reported in `notes`. `suggested_action` is set only when the verdict is `reject`; it is a suggestion, and the
operator never changes a status or sends a nudge because of it. `insufficient data` shows as `not_ready` with pending
criteria, never as a nudge.

## drift.json

Strategy-level drift against the backtest only (operational supervisor incidents are excluded). `rows` are the
backtest-vs-sim comparison (`metric`, `backtest`, `sim`, `difference`, `flag`: `ok`, `better`, `worse`, `shift`,
`low_sample`, `n/a`). `flags` lists the ones that need attention, each with a stable `key`: `compare.<metric>`,
`verdict.edge_decay`, `tagging.untagged_fills`.

## backtest_check.json

The baseline: `present`, `run_id`, `label`, `kind`, `period`, headline `metrics`, and `claims_check` (the backtest vs the
spec's `expected_metrics`, with the 15% tolerance, simulator-fidelity feedback). Absent baseline: `present: false`.

## Privacy

Accounts appear only as aliases. Every file is scrubbed of the raw account names in the local mapping file
(`NT_FEEDBACK_ALIASES`, default `~/.ninjatrader-operator/account_aliases.json`, never exported) before it is hashed or written, and
the export's audit line carries the alias, not the name. Bundles reference spec id, version and manifest hashes; they never
inline strategy parameters.
