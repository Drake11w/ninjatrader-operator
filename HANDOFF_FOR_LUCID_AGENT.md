# Handoff: build the "approved strategy -> NinjaTrader operator" exporter

You are working in the **lucid-trading-system** repo (private: `Drake11w/lucid-trading-system`). Your job is to build
a small, reliable exporter that packages an *approved* strategy into a **handoff bundle** that the separate
**ninjatrader-operator** project (public: `Drake11w/ninjatrader-operator`) can ingest, validate, track and later
compare against live/sim fills.

Do not change the operator. If the contract below is missing something you need, write it down under "Open
questions" in your final report instead of inventing fields.

## 1. Who receives this and what they do with it

The operator is an MCP server that sits next to NinjaTrader 8. It cannot research or backtest anything. It:
1. validates the spec (`strategy_spec.validate`, schema v2),
2. stores it immutably per (id, version) in a registry with the lifecycle `candidate -> backtest -> sim -> live-small -> live`,
3. ingests the backtest results (NinjaTrader Strategy Analyzer CSV export) and checks them against the spec's `expected_metrics`,
4. tags real NinjaTrader fills with strategy / version / leg by matching **order names**,
5. compares sim/live fills to the backtest day by day, to detect drift.

It does **not** deploy anything: a human compiles the `.cs` file and puts it on a chart. So the bundle must make that
unambiguous.

Read these in the operator repo first (clone it or use `gh api`; it is small):
- `STRATEGY_SPEC.md`: the v2 contract (authoritative).
- `examples/two-leg-mnq.strategy.json`: worked example (placeholder values, real structure).
- `backtest.py` docstring: exactly which CSV shapes are parsed.
- `ROADMAP.md`: what exists and what is planned.

## 2. Deliverable: one folder per strategy version

```
handoff/<id>/<version>/
  spec.json                 # schema_version 2, validates with strategy_spec.validate
  strategies/<Class>.cs     # the exact NinjaScript file(s) the backtest ran, one per leg/class
  backtests/<leg-or-class>__trades.csv     # NinjaTrader Strategy Analyzer export (see 4)
  backtests/<leg-or-class>__summary.csv    # optional: Backtests-list or Periods export
  MANIFEST.json             # provenance + checksums (see 5)
  NOTES.md                  # human caveats (see 6)
```

A single command (for example `python -m export_handoff S663 --out handoff/`) should produce it from the repo's
own data, and a second one should re-validate an existing bundle. Do not hand-edit bundles.

## 3. spec.json rules (schema v2)

- `id` = lowercase slug, `version` = `MAJOR.MINOR.PATCH`; **any change to a rule, input, cost or date range is a new
  version**. `lucid_id` = the S-number (e.g. `S663`).
- One **leg per NinjaScript class** (a "pair" strategy = two legs, each its own `.cs`). Each leg needs `session`
  (Central time, `HH:MM`), a **mandatory `exit.stop`**, `sizing`, and `ninjascript` with `class`, `file`, `inputs`,
  `order_names`.
- **`ninjascript.inputs` must be the exact property values the backtest ran with**, not the file's defaults. A file
  such as `OrbEvalMNQ.cs` has one class and many presets (S563, S584, S663...) whose defaults match none of them, so
  the preset must be expanded into explicit property values. This is the single most important requirement.
  Property names must match the C# `[NinjaScriptProperty]` names exactly.
- **`ninjascript.order_names` must list every entry/exit name the script passes to NinjaTrader** (for example
  `"ORB long"`, `"ORB time exit"`; `"Calendar long"`, `"Calendar time exit"`). Pull them from the `.cs` source rather
  than typing them. Fills are tagged by these; a missing name means untagged fills. Stop/target fills named
  "Stop loss" / "Profit target" are handled by the operator.
- `gates[]` for day filters (skip event days, skip Mondays, skip FOMC week, skip wide prior day), with params.
- `costs`: `commission_per_side` and `slippage_ticks` exactly as the backtest used (house numbers: 0.62 / 2).
- `backtest.start` / `backtest.end`: the window the metrics were measured over.
- `expected_metrics`: only numbers the repo actually computed. Fractions for rates (0.82, not 82). Use the keys
  listed in `STRATEGY_SPEC.md`. Do not round up, do not fill in estimates, omit what you do not have.
- `hypothesis`: why the edge should exist **plus the caveats** (found by search, number of tests on the same
  history, post-2019 only, regime dependence). The repo already writes these; carry them over honestly.

### Additions the operator does not understand yet (put them in `spec.json`; unknown keys are ignored, so this is safe)
- `holds_overnight: true|false` per leg (the calendar strategy buys at 17:05 CT and holds to 15:40 CT next day).
- `conflicts_with: ["<id or class>", ...]` (the calendar strategy must not run with `MondayLongMNQ` /
  `MondayRthLongMNQ`; pair legs must not overlap).
- `evidence_status`: copy the strategy's own status line verbatim (for example "NEEDS FRESH-QUARTER / SIM
  CONFIRMATION, not for live"). Never upgrade it.
- `compiled_in_nt8: true|false|"unknown"`: several repo files say "not compiled here". Report that truthfully.

## 4. Backtest exports

The operator parses **NinjaTrader Strategy Analyzer** CSV exports, not the Lucid repo's own result format. So the
numbers in `expected_metrics` come from the Lucid backtest, but the CSVs must come from actually running the `.cs`
file in NinjaTrader's Strategy Analyzer with the **same inputs, costs and date range**. Prefer, in order:
1. **Trades grid** (best: gives drawdown and day-by-day comparison with journaled fills). Needs columns `Entry time`,
   `Exit time`, `Profit`; also useful: `Market pos.`, `Qty`, entry/exit prices, `Entry name`/`Exit name`, `Commission`.
2. **Periods grid with Period = Day** (needs `Period`, `Net profit`).
3. **Backtests list grid** (one summary row per run: net profit and trade count only).

The operator can run the backtests itself from a bundle (`python backtest_runner.py <bundle dir> --import`), on the
current front-month contract (`MNQ 12-26`; NinjaTrader merges earlier contracts, history back to 2019) with the spec's
`ninjascript.inputs`, slippage and commission. A bundle therefore only needs the exact inputs; you do not have to attach CSVs.
Do not use the continuous `MNQ ##-##` instrument in the Strategy Analyzer: it returned empty results.

If you cannot run NinjaTrader from the repo, do not fabricate CSVs. Instead produce the spec and `NOTES.md` and
list in the final report "Strategy Analyzer run needed: <class>, inputs, date range, costs". Record the export
time zone (`export_timezone`, the PC/chart time zone; the house assumption is Central) in `MANIFEST.json`.

If the repo's existing `research/compare_nt_results.py` already defines how Lucid reconciles NT results, reuse its
conventions and mention any difference.

## 5. MANIFEST.json

```json
{
  "bundle_version": 1,
  "created_at": "<UTC ISO8601>",
  "lucid_repo_commit": "<git sha>",
  "lucid_id": "S663",
  "spec_id": "<id>", "spec_version": "<x.y.z>",
  "files": {"strategies/OrbEvalMNQ.cs": {"sha256": "...", "compiled_in_nt8": "unknown"}},
  "backtests": {"backtests/orb__trades.csv": {"kind": "trades", "leg": "<leg name>", "export_timezone": "America/Chicago",
                 "inputs": {"...": "..."}, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD",
                 "costs": {"commission_per_side": 0.62, "slippage_ticks": 2}}}
}
```
The sha256 of each `.cs` lets the operator detect that the file on the chart differs from the one that was tested.

## 6. NOTES.md (short, plain)

What the strategy is in two sentences; which leg is which; the preset/inputs; evidence quality and known caveats
(multiple-testing, regime sensitivity, sample size, the "skipped wide ranges" effect where relevant); overnight
holding; conflicts; the Lucid eval-odds numbers and how they were computed (LucidFlex 50K: $3,000 target, $2,000
EOD trailing DD locking at $50,100, 50% consistency, flat by 15:45 CT).

## 7. Constraints and gotchas

- All times Central. Bar times in these scripts are bar **close** times (`ToTime(Time[0])`).
- Only export strategies explicitly marked **approved** in the repo. Refuse otherwise, with a clear message.
- Never include account numbers, credentials or API keys. Do not copy the 150 MB `data/` directory or raw price data.
- Never edit the `.cs` files during export. Copy them byte for byte (check the hash).
- The exporter must fail loudly (non-zero exit) when: an input in `spec.json` is not a property in the `.cs`; an
  `order_names` entry does not appear in the `.cs`; the spec fails operator validation; a CSV is missing for a leg.
- Mark honestly what is verified vs unverified. A bundle with unverified items is fine; a bundle that hides them is not.

## 8. Acceptance test

1. Export the two strategies below as separate bundles.
2. Clone `Drake11w/ninjatrader-operator`, then for each bundle run `strategy_spec.validate(json.load(open("spec.json")))`
   and report the result (errors must be zero).
3. Confirm the inputs in each `spec.json` reproduce the repo's stated preset/metrics, and say which numbers you
   could not reproduce.

Start with these two files (currently in `C:\Users\drake\Documents\NinjaTrader 8\bin\Custom\Strategies\`; they should
also be under `execution/` in the repo):
- `OrbEvalMNQ.cs`: pick the approved preset (S663 pairs it with `MondayRthAtrLongMNQ.cs`, so that is a two-leg bundle).
- `CalendarLongMNQ.cs`: single leg, holds overnight, conflicts with the Monday long strategies.

## 9. Final report format

Short: bundles produced (paths), validation output, anything unverified, what Strategy Analyzer runs are still needed,
and open questions about the contract (fields you needed that the operator does not have).
