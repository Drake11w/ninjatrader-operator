# Strategy spec v1 (draft)

The contract between the Lucid system (which proposes strategies) and this operator (which evaluates them against
NinjaTrader reality). One JSON document per strategy **version**. Validate with the `validate_strategy_spec` tool
or `strategy_spec.validate()`; see `examples/orb-mnq.strategy.json` (placeholder numbers).

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
