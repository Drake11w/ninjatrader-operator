# Strategy spec v2 (current) and v1 (still accepted)

The contract between the Lucid system (which proposes strategies) and this operator (which evaluates them against
NinjaTrader reality). One JSON document per strategy **version**. Validate with the `validate_strategy_spec` tool
or `strategy_spec.validate()`. `schema_version` picks the format. Worked example: `examples/two-leg-mnq.strategy.json`.

## v2 (reconciled with `lucid-trading-system`)

A spec is one deployable strategy made of one or more **legs**; each leg is its own NinjaScript (e.g. a Monday
long leg + a Tue-Fri ORB long leg on one MNQ chart). Times are **Central** (`America/Chicago`) by default, as the
Lucid system uses; `America/New_York` is also allowed.

| Section | Required | Notes |
|---|---|---|
| `schema_version` | yes | `2`. |
| `id`, `version` | yes | `id`: lowercase slug. `version`: `MAJOR.MINOR.PATCH`. Any rule/input change = new version. |
| `lucid_id` | no | The Lucid system's S-number, e.g. `S123`. |
| `hypothesis` | yes | Why the edge should exist, and caveats (e.g. found by search, post-2019 only). |
| `timezone` | no | Default `America/Chicago`. |
| `instrument` | yes | `root`, `contract_policy` `front` (default) or `fixed` (+ `contract`), optional `bar`. |
| `legs[]` | yes | Unique `name`; optional `days` (`mon`..`sun`); `side`; `session` (`entry_start` <= `entry_end` <= `flat_by`, equal start/end = enter at that moment); optional `gates[]` (day filters: `type`, `description`, `params`); `entry.rules[]`; `exit` (**`stop` mandatory**; `target` omitted = no target; `structure` stop value = ticks beyond the level); `sizing` (`contracts`, optional `max_contracts`); **`ninjascript`** (`class`, `file`, flat `inputs`). |
| `costs` | yes | `commission_per_side`, `slippage_ticks`. Results are judged net of these. |
| `backtest` | no | `start`, `end` (`YYYY-MM-DD`) the expected metrics were measured over. |
| `expected_metrics` | no | `min_trades`, `trades_per_year`, `net_per_year`, `win_rate`\*, `expectancy`, `profit_factor`, `max_drawdown`, `profit_to_drawdown`, `avg_trades_per_day`, `eval_pass`\*, `eval_fail`\*, `eval_sessions_to_pass`, `chain_odds`\*. (\* = fraction 0-1, so 0.82 = 82%.) These are the **backtest's claims**, used later for drift checks, never live results. |

`ninjascript.class` is what NinjaTrader reports as the strategy name, so the journal can tag executions to the right
leg. Unknown extra keys are ignored. Not in v2: regime filters (the Lucid system mostly gates on VIX / event days /
prior-day range, expressed here as `gates`), per-leg expected metrics, funded-phase rules.

---

# v1 (kept for specs already registered)

**This is a draft built without seeing the Lucid system.** Rules carry a free-text `description` so nothing is lost
if the Lucid system's own rule format differs. Reconcile the structured parts once its format is known.

| Section | Required | Notes |
|---|---|---|
| `schema_version` | yes | Must be `1`. Bump on breaking changes. |
| `id`, `version` | yes | `id`: lowercase slug. `version`: `MAJOR.MINOR.PATCH`. Any parameter/rule change = new version. |
| `status` | no | `candidate` (default) -> `backtest` -> `sim` -> `live-small` -> `live`; or `rejected` / `retired`. |
| `hypothesis` | yes | Why the edge should exist. Required so results can be judged against an expectation. |
| `source` | no | `{system, ref}`: where it came from (e.g. Lucid run id). |
| `instrument` | yes | `root` (e.g. `MNQ`), `contract_policy` `front` (default) or `fixed` (+ `contract`), optional `bar`. |
| `session` | yes | `entry_start` < `entry_end` <= `flat_by`, `HH:MM` Eastern. |
| `entry` | yes | `side` long/short/both, `rules[]` (`type`, `description`, optional `params`), optional `max_trades_per_day`. |
| `exit` | yes | **`stop` is mandatory** (`type`, positive `value`); optional `target`, `rules[]`. |
| `sizing` | yes | `contracts` <= `max_contracts`. |
| `parameters` | no | Flat name -> value map of tunables (for sensitivity/walk-forward later). |
| `costs` | yes | `commission_per_side`, `slippage_ticks`: results are judged net of these. |
| `expected_metrics` | no | `min_trades`, `win_rate`, `expectancy`, `profit_factor`, `max_drawdown`, `avg_trades_per_day`: what the backtest claims, used for drift checks. |

Unknown extra keys are ignored. Not in v1: regime filters (waiting on the Lucid regime labels), multi-leg or
multi-instrument strategies, news-day rules.
