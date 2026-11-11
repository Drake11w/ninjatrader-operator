# Lucid loop rollout

Everything here is manual verification. Nothing in CI touches NinjaTrader. Use made-up or local alias names in anything
you paste into a public issue (no account numbers, no strategy parameters).

## Setup once

- `NT_FEEDBACK_DIR`, `NT_HANDOFF_DIR` (outside git; neither is ever committed).
- Choose an approval secret and keep it **only in your own shell**, never in the MCP server environment. Put its hash in
  the MCP server environment: `python -c "import approvals,sys; print(approvals.secret_hash(sys.argv[1]))" <secret>`
  -> `NT_OPERATOR_APPROVAL_SECRET_SHA256`. `NT_OPERATOR_TOKEN` must also be set. A Claude session without the secret
  cannot approve, schedule or cancel.
- Optional daily export: `powershell -File scripts\install_export_task.ps1` (read-only; `-WhatIf` first).

## Sim101 verification checklist

1. **Export (S1).** `sync_journal`, then `export_feedback` for a Sim version. The snapshot validates against
   `feedback.schema.json`; a second run is `unchanged`; a changed result writes a new snapshot; `correction=true` names
   the superseded one. No raw account name appears in any file (aliases only).
2. **Nudge (S3a).** `list_nudges` lists a version only while every readiness criterion holds; add an untagged fill and
   it disappears. The dashboard panel agrees.
3. **Promote (S2).** Open ONE idle Strategy Analyzer window, set `NT_OPERATOR_ALLOW_STRATEGY_START=1`, then
   `promote_strategy <id> <version> Sim101` with a synthetic bundle. Expect: candidate -> backtest -> spec check -> sim, a
   `promotion_report.json` next to the snapshots. Force a failure (close the Analyzer): the run stops at `backtest`, status
   stays `candidate`, no override exists. Fix it and re-run: completed stages show `skipped`. Start a second run
   mid-flight: it reports `busy`.
4. **Approve (S3b).** With the wrong or no secret `approve_live_small` is refused and the status is unchanged. With the
   secret it moves a ready Sim101 version to `live-small` (registry only). Confirm in `get_audit_log` that no secret
   appears and that no NinjaTrader call was made. `deploy_strategy_live_small` must still refuse until every
   `NT_OPERATOR_*` gate is set.
5. **Schedule (S3b).** `schedule_live_small_approval` with `not_before` a little ahead; `python launcher.py` (plan mode)
   prints "would execute" once due and writes nothing. `python launcher.py --apply` executes it inside the enable window
   when still ready. Repeat with an untagged fill added: the tick voids it. Cancel a pending one with
   `cancel_scheduled_approval`. Schedules run at 30-minute granularity and only inside the enable window.
6. `python -m unittest discover -s tests` is green, including `tests/test_no_new_post_path.py`.

## Real-account trial plan (separate from the Sim101 check; do it only after the above)

Do not start until the Sim101 checklist passes. This plan changes real-account behaviour; you run it, not an automated session.

1. Pick one version that is `ready` in the feedback bundle on Sim, and one account alias (`acct-N`).
2. Confirm the existing gates by hand, unchanged: `NT_OPERATOR_ALLOW_LIVE_SMALL=1`, the exact
   `NT_OPERATOR_LIVE_SMALL_ACCOUNTS`, `NT_OPERATOR_ALLOW_STRATEGY_START=1`, `NT_OPERATOR_TOKEN` (server and NinjaTrader).
   Approval satisfies none of these.
3. Approve with a written reason (immediate, or scheduled for the next enable window). Expect `live-small` in the
   registry and nothing running yet.
4. Let the launcher tick (or run `deploy_strategy_live_small`) start it once. Watch the first session: position size,
   the broker/prop-firm loss limit (the add-on kill switch is Sim-only), the supervisor incidents.
5. Export feedback after each session for the first week; compare live trades with Sim in `drift.json`.
6. Rollback: `disable_strategy`, `flatten` if needed, then `set_strategy_status` to `retired` with a reason. There is no
   "unapprove".
