# Handoff for the next agent

You are continuing **ninjatrader-operator**: a read-only bridge that lets Claude observe NinjaTrader 8 (NT8).
You do NOT need access to the Lucid trading system repo. Everything you need about it is in this file.
Do not ask Drake to connect it unless a task below is genuinely blocked without it.

## 1. State of this repo (Nov 6, 2026)
- `NtStatusAddOn.cs`: NT8 add-on, HTTP on `127.0.0.1:8765`, GET only. Endpoints: `/status` (accounts, cash, P&L,
  positions), `/orders` (working orders), `/executions?limit=N` (fills), `/strategies`, `/health` (heartbeat + connection / feed status).
  No order, cancel or modify calls anywhere. Keep it that way until Phase 5.
- `server.py`: Python MCP server (`pip install mcp`, tested with mcp 2.3.0). Tools: `get_accounts`, `get_positions`,
  `get_orders`, `get_executions`, `get_strategies`, `get_health`. Every reply has `age_seconds` and a `warning` if data is > 30 s old.
  `NT_ADDON_URL` overrides the add-on address.
- `tests/test_server.py`: 7 tests against a fake add-on (`python -m unittest discover tests`). No NT8 needed.
- `ROADMAP.md`: phased plan. Phase 1 (observe) is done. Drake confirmed the add-on works in NT8 ("seems like it works").
  `/strategies` uses `Account.Strategies`; confirm it really returned data on a running strategy, not just compiled.
- Branch: `claude/nice-darwin-hl0e9a` (merge to `main` is Drake's call; do not open a PR unless asked).
- The roadmap says the Lucid repo is not public; that is outdated. It is `Drake11w/lucid-trading-system` (private, Python). Details below.

## 2. Suggested next work (in order)
1. **Trade journal**: poll `/executions` and persist to SQLite (dedupe on `execution_id`), tagged with order name. Survives NT restarts.
2. **Round-trip builder**: pair entry and exit fills into trades (instrument, side, qty, entry/exit price and time, net P&L after
   commission, duration). Add MAE/MFE only if NT data allows; otherwise skip and say so.
3. **Metrics** (net P&L, win rate, expectancy, profit factor, max drawdown, by weekday / hour). Unit-test the math with fixtures.
4. **Lucid rule tracker** (rules in section 3 below; put the numbers in a config file, not code): distance to trailing-drawdown breach in
   dollars, progress to the $3,000 target, consistency-rule status, with alerts.
5. Then Phase 3 (strategy spec schema, comparison vs backtest, verdict). A spec schema can be drafted from section 4 without the Lucid repo.
Phases 4-5 (feedback files, guarded controls) stay later; Phase 5 needs server-side guards (loss cap, sim-only flag, kill switch, audit log).

## 3. What is trading, and the account rules (from the Lucid system's docs)
- **Owner:** Drake. Trades micro futures (MNQ mostly) in a **LucidFlex 50K evaluation** on NT8, currently on **Sim**
  (his account name on Sim appeared as `LFE...`; do not commit account numbers). He runs Python 3 on Windows cmd.
- **Eval rules the research models:** profit target **$3,000**; **$2,000 end-of-day trailing drawdown** that locks once the account is
  $2,000 above start (floor then fixed at $50,100); **50% consistency rule** (no single day may be more than half of total profit);
  flat by **15:45 CT**; no eval time limit. Trading day runs 17:00 CT to 15:45 CT next day, overnight trades allowed inside that window.
- **Funded phase** (after the target): same $2,000 EOD trailing drawdown, stops trailing at +$2,000 (floor then fixed).
  Treat these as defaults in config and have Drake confirm against Lucid's current published rules before relying on alerts.
- Costs assumed in research: micro commission plus slippage; Lucid "eval odds" use overlapping 63-session windows, so treat percentages as rough.

## 4. The Lucid trading system (research repo), what it is
- Python research and backtesting repo plus NinjaScript files in `execution/`. Goal: strategies that trade automatically and pass the
  Lucid eval. Judged by **eval odds** (pass vs fail % over 63-session windows at 1-4 micros), then chain odds (eval then funded).
  Efficiency (profit / max drawdown) is secondary.
- Data: 1-minute NinjaTrader exports (MNQ, MES, M2K, MYM, MCL, MGC; NQ / YM full-size 2009-2019 as out-of-sample). Time zone: raw stamps are UTC bar-end; sessions are on the 17:00 CT grid.
- **Live candidate: S742** = two legs, 1 micro each, skipping CPI / NFP / FOMC days (real calendar):
  (a) **Monday RTH long** on MNQ, open 08:30 CT to 15:40 CT, 1 ATR stop (`execution/MondayRthAtrLongMNQ.cs`);
  (b) **ORB30 long Tue-Fri** on MNQ: 30-minute opening range (08:30-09:00 CT), long break of the range high, stop at the opposite side
  (one tick beyond the low), no profit target, flat at 14:55 CT; requires a strong close of the 08:30-08:59 range (close location >= 0.5);
  skips CPI/NFP/FOMC (`execution/OrbEvalMNQ.cs`, with `Use` inputs for each part).
  Research odds: about 94% pass / 5% fail at 1 micro, ~122 sessions to pass, chain about 82%. In-sample and post-hoc: treat as hypotheses
  (it is positive only on MNQ since ~2019/2020; it loses on NQ 2009-2019). Fresh Sim sessions from Mon Oct 26 2026 are the real test.
  S663 and S674 are near-equivalent alternatives.
- Trade names visible in NT8 for the older ORB: "ORB long", "ORB time exit", "Stop loss" (use these to tag journal rows).
- Entry fills in research: stop order at level + 1 tick, plus 2 ticks slippage; exits modelled with 0.5 pt slippage on market exits.
- The Lucid repo's findings that matter for an operator: eval pass depends on daily risk size, not average profit; one big day breaks the
  consistency rule; the 2026 volatility regime matters (check how often sized rules trade now).
- **Open questions for Drake (only he knows):** exact current Lucid rule values, whether he wants verdicts advisory or gating,
  which NT8 strategies are actually enabled on Sim, and whether he wants Sim fills compared to the research simulator
  (the Lucid repo has `research/compare_nt_results.py` that does this for Strategy Analyzer exports; a CSV export of fills is enough).
- If you later need the research code, ask Drake once; the repo has a `CLAUDE.md` that is long and mostly an experiment log, so read only its top and
  `docs/FINAL_REVIEW.md`.

## 5. Working rules
- Read-only first. Never add order entry. No account numbers or credentials in commits.
- Do not create PRs unless Drake asks. Develop on `claude/nice-darwin-hl0e9a`. Run `python -m unittest discover tests` before pushing.
- I could not compile C# in the cloud. Any `.cs` change needs Drake (or a local session) to compile in NT8 (F5 in the NinjaScript Editor).
