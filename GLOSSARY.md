# Glossary

**Nudge**: A suggestion shown to the owner that a Sim strategy version meets the readiness criteria for `live-small`. It exists only while the criteria hold; it is not a stored decision.

**Approval**: The owner's recorded, reasoned decision to move one strategy version to `live-small` on one account alias. It changes the strategy's stage; it does not by itself start anything.

**Scheduled approval**: An approval the owner grants now that takes effect no earlier than a chosen time, and only if the strategy still meets the readiness criteria when that time arrives.

**Promotion**: Moving a strategy version from a research handoff through backtest to Sim, as one command. Its ceiling is Sim; it never moves a strategy to `live-small` or `live`.

**Bundle sign-off**: The approval flag a Lucid handoff bundle carries in its manifest, recorded once when the bundle is registered. It says the research side cleared the bundle for handoff; it is not the owner's Approval and never moves a strategy to `live-small`.

**Feedback bundle**: The immutable snapshot of one strategy version's results (trades, daily P&L, summary, verdict with readiness, drift flags, backtest check) the operator writes as plain files for the research side to read. A newer snapshot never edits an older one.

**Readiness**: Whether a Sim strategy version meets every criterion for a Nudge, reported as pass, fail or pending per criterion. It is computed fresh each time; it never changes a stage.
