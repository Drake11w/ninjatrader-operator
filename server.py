"""NinjaTrader MCP server.

Talks to NtStatusAddOn (http://127.0.0.1:8765/status). Mostly read-only. The only write tools are the
risk-reducing controls (flatten, cancel_all_orders, arm/disarm_kill_switch): Sim-only by default, audited,
no order entry.
"""
import json
import os
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer

import approvals
import backtest
import backtest_runner
import comparison
import control
import feedback
import handoff
import journal
import nudges
import promote
import registry
import sup_profile
import report
import rules
import sessions
import strategy_spec
import trades
import verdict

BASE = os.environ.get("NT_ADDON_URL", "http://127.0.0.1:8765")
STALE_SECONDS = 30
mcp = MCPServer("ninjatrader-readonly")


def _fetch(path: str = "/status") -> dict:
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return _stamp(json.loads(r.read().decode("utf-8")))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"error": f"NinjaTrader add-on unreachable or bad reply: {e}. Is NT8 running with NtStatusAddOn compiled?"}


def _post(path: str, params: dict, body: str = "") -> dict:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(f"{BASE}{path}?{query}", data=body.encode("utf-8"), method="POST")
    token = os.environ.get("NT_OPERATOR_TOKEN")
    if token:
        req.add_header("X-NT-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except ValueError:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"ok": False, "error": f"NinjaTrader add-on unreachable or bad reply: {e}"}


def _control(action: str, path: str, params: dict, reason: str = "") -> str:
    """Guard, call the add-on, audit. Refusals are audited too."""
    refusal = control.check_account(params.get("account", ""))
    if not refusal and "max_loss" in params:
        refusal = control.check_max_loss(params["max_loss"])
    result = {"ok": False, "error": refusal} if refusal else _post(path, params)
    control.audit(action, params, result, reason)
    return json.dumps(result, indent=2)


def _stamp(data: dict) -> dict:
    """Add age_seconds and a stale warning when the add-on timestamp is old."""
    ts = data.get("timestamp")
    if ts:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()
            data["age_seconds"] = round(age, 1)
            if age > STALE_SECONDS:
                data["warning"] = f"STALE: data is {age:.0f}s old, do not rely on it"
        except ValueError:
            pass
    return data


@mcp.tool()
def get_accounts() -> str:
    """List NinjaTrader accounts with cash value, realized and unrealized P&L."""
    data = _fetch()
    if "error" in data:
        return json.dumps(data)
    out = [{k: a[k] for k in ("name", "connection", "cash_value", "realized_pnl", "unrealized_pnl")}
           for a in data["accounts"]]
    return json.dumps({**_meta(data), "accounts": out}, indent=2)


def _meta(data: dict) -> dict:
    return {k: data[k] for k in ("timestamp", "age_seconds", "warning") if k in data}


@mcp.tool()
def get_positions(account: str = "") -> str:
    """List open positions. Optionally filter by account name (e.g. 'Sim101')."""
    data = _fetch()
    if "error" in data:
        return json.dumps(data)
    out = [{"account": a["name"], "positions": a["positions"]}
           for a in data["accounts"] if not account or a["name"] == account]
    return json.dumps({**_meta(data), "accounts": out}, indent=2)


@mcp.tool()
def get_orders(account: str = "") -> str:
    """List working (not filled/cancelled/rejected) orders with type, prices and OCO group."""
    data = _fetch("/orders")
    if "error" in data:
        return json.dumps(data)
    rows = [o for o in data["orders"] if not account or o["account"] == account]
    return json.dumps({**_meta(data), "orders": rows}, indent=2)


@mcp.tool()
def get_executions(account: str = "", limit: int = 50) -> str:
    """Most recent fills (newest last): time, price, quantity, entry/exit flag, order name (e.g. 'ORB long')."""
    limit = max(1, min(limit, 500))
    data = _fetch(f"/executions?limit={limit}")
    if "error" in data:
        return json.dumps(data)
    rows = [e for e in data["executions"] if not account or e["account"] == account]
    return json.dumps({**_meta(data), "executions": rows[-limit:]}, indent=2)


@mcp.tool()
def get_strategies(account: str = "") -> str:
    """Strategies running in NinjaTrader: name, instrument, state, current position."""
    data = _fetch("/strategies")
    if "error" in data:
        return json.dumps(data)
    rows = [x for x in data["strategies"] if not account or x["account"] == account]
    return json.dumps({**_meta(data), "strategies": rows}, indent=2)


@mcp.tool()
def download_history(instrument: str, start: str, end: str = "", bar_type: str = "Minute", bar_value: int = 1) -> str:
    """Download historical bars from NinjaTrader to a CSV (read-only; merges into any existing file, so re-run to update).
    instrument e.g. 'MNQ 12-26' (the front month merges earlier contracts). start/end are YYYY-MM-DD (end defaults to
    today). bar_type is Tick, Second, Minute or Day. Times in the CSV are NinjaTrader local time (bar close)."""
    import history
    from datetime import date
    try:
        s = date.fromisoformat(start)
        e = date.fromisoformat(end) if end else date.today()
    except ValueError:
        return json.dumps({"ok": False, "error": "start and end must be YYYY-MM-DD"})
    history.ADDON = BASE
    return json.dumps(history.download(instrument, s, e, bar_type, bar_value), indent=2)


@mcp.tool()
def sync_journal(strategy: str = "", version: str = "", limit: int = 500) -> str:
    """Save NinjaTrader's recent fills into the local SQLite journal (safe to repeat; duplicates are skipped).
    New rows are tagged with the strategy and leg of any registered spec in sim/live-small/live whose
    ninjascript.order_names match the fill (stop/target fills follow the latest entry); or pass strategy/version to
    tag everything yourself. Writes only to the local journal, never to NinjaTrader."""
    data = _fetch(f"/executions?limit={max(1, min(limit, 500))}")
    if "error" in data:
        return json.dumps(data)
    con = journal.connect()
    try:
        tagger = None
        if not strategy:
            rcon = registry.connect()
            try:
                tagger = journal.make_tagger(con, registry.order_name_map(rcon, sup_profile.bindings()))
            finally:
                rcon.close()
        execs = sorted(data["executions"], key=lambda e: e.get("time") or "")
        counts = journal.record(con, execs, strategy, version, tagger)
    finally:
        con.close()
    return json.dumps({**_meta(data), **counts, "fetched": len(data["executions"])}, indent=2)


@mcp.tool()
def get_journal(account: str = "", strategy: str = "", since: str = "", limit: int = 200) -> str:
    """Read persisted fills from the local journal (survives NT restarts). `since` is an ISO timestamp."""
    con = journal.connect()
    try:
        rows = journal.query(con, account, strategy, since, limit)
    finally:
        con.close()
    return json.dumps({"count": len(rows), "executions": rows}, indent=2)


@mcp.tool()
def get_trades(account: str = "", strategy: str = "", since: str = "", point_values: str = "") -> str:
    """Round-trip trades built from the local journal (FIFO pairing of entry and exit fills), with P&L net of
    commission. `point_values` is optional JSON like {"MNQ": 2.0}; defaults cover common futures."""
    return _trade_report(account, strategy, since, point_values, summary_only=False)


@mcp.tool()
def get_performance(account: str = "", strategy: str = "", since: str = "", point_values: str = "") -> str:
    """Performance stats from journaled trades: net P&L, win rate, expectancy, profit factor, avg win/loss, max
    drawdown, Sharpe/Sortino (daily), and breakdowns by weekday, entry hour (ET) and month. Warns on small samples."""
    return _trade_report(account, strategy, since, point_values, summary_only=True)


def _trade_report(account, strategy, since, point_values, summary_only):
    try:
        pv = json.loads(point_values) if point_values else None
    except ValueError:
        return json.dumps({"error": 'point_values must be valid JSON, e.g. {"MNQ": 2.0}'})
    con = journal.connect()
    try:
        rows = journal.query(con, account, strategy, since, 5000)
    finally:
        con.close()
    built = trades.build_trades(rows, pv)
    if summary_only:
        out = {"performance": trades.performance(built["trades"]), "open_positions": built["open"]}
    else:
        out = {"count": len(built["trades"]), "trades": built["trades"], "open_positions": built["open"]}
    out["warnings"] = built["warnings"]
    return json.dumps(out, indent=2)


@mcp.tool()
def get_rule_status(account: str = "", strategy: str = "", include_unrealized: bool = False) -> str:
    """Prop-firm rule tracker: distance in dollars to the daily loss limit, trailing drawdown floor, profit target
    and consistency rule, with ok/warning/breach status. Uses closed trades from the journal (run sync_journal
    first); set include_unrealized to add current open P&L from NinjaTrader. Rule values come from the rules file
    (NT_RULES_FILE or ~/.ninjatrader-operator/rules.json; see rules.example.json)."""
    try:
        cfg = rules.load_rules()
    except (OSError, ValueError) as e:
        return json.dumps({"error": str(e)})
    unrealized = 0.0
    if include_unrealized:
        data = _fetch("/status")
        if "error" in data:
            return json.dumps(data)
        unrealized = sum(a.get("unrealized_pnl") or 0 for a in data.get("accounts", [])
                         if not account or a.get("name") == account)
    con = journal.connect()
    try:
        rows = journal.query(con, account, strategy, "", 5000)
    finally:
        con.close()
    built = trades.build_trades(rows)
    out = rules.evaluate(built["trades"], cfg, unrealized=unrealized)
    out["warnings"] += built["warnings"]
    return json.dumps(out, indent=2)


@mcp.tool()
def get_report(period: str = "day", date: str = "", account: str = "", strategy: str = "") -> str:
    """Markdown trading report for a day or week (period "day" or "week"; `date` is a trading day like 2026-11-05,
    default today). Includes summary, trades, open positions and, if a rules file exists, prop-rule status."""
    if period not in ("day", "week"):
        return json.dumps({"error": 'period must be "day" or "week"'})
    try:
        cfg = rules.load_rules()
    except (OSError, ValueError):
        cfg = None
    con = journal.connect()
    try:
        rows = journal.query(con, account, strategy, "", 5000)
    finally:
        con.close()
    built = trades.build_trades(rows)
    try:
        return report.build_report(built["trades"], built["open"], period, date, cfg,
                                   tz_name=(cfg or {}).get("timezone", "America/New_York"),
                                   start_hour=(cfg or {}).get("session_start_hour", 18))
    except ValueError:
        return json.dumps({"error": "date must look like 2026-11-05"})


@mcp.tool()
def get_session_info(instrument: str = "") -> str:
    """Current market session (RTH, Globex overnight, maintenance break, weekend), time to the next RTH open or
    close, and the front-month futures contract with its roll date. Pass an instrument like "MNQ 12-26" to check
    it is the front month. Computed locally; `special_session` names a holiday, halt day or early close today (from
    date rules, so check the CME calendar), and the session times are not adjusted for it."""
    try:
        return json.dumps(sessions.session_info(instrument=instrument), indent=2)
    except Exception as e:  # missing tz database etc.
        return json.dumps({"error": f"{type(e).__name__}: {e} (on Windows, pip install tzdata)"})


@mcp.tool()
def validate_strategy_spec(spec: str) -> str:
    """Check a strategy spec (JSON text, schema v2 or legacy v1) against the shared Lucid/operator format. Returns every problem
    found at once, or a short summary if valid. Does not save or run anything. See STRATEGY_SPEC.md."""
    try:
        parsed = json.loads(spec)
    except ValueError as e:
        return json.dumps({"valid": False, "errors": [f"not valid JSON: {e}"]})
    errs = strategy_spec.validate(parsed)
    if errs:
        return json.dumps({"valid": False, "errors": errs}, indent=2)
    return json.dumps({"valid": True, "summary": strategy_spec.summarize(parsed)}, indent=2)


@mcp.tool()
def submit_strategy(spec: str, note: str = "") -> str:
    """Register a candidate strategy spec (JSON text, schema v2 or legacy v1). Validates first; always starts as 'candidate'.
    An (id, version) pair is immutable: changes must be submitted as a new version. Records only, runs nothing."""
    try:
        parsed = json.loads(spec)
    except ValueError as e:
        return json.dumps({"ok": False, "errors": [f"not valid JSON: {e}"]})
    return json.dumps(registry.submit(registry.connect(), parsed, note), indent=2)


@mcp.tool()
def set_strategy_status(id: str, version: str, status: str, note: str = "") -> str:
    """Advance a registered strategy version one stage (candidate -> backtest -> sim -> live-small -> live), or mark it
    rejected/retired (note required, terminal). Bookkeeping only: it does not enable anything in NinjaTrader."""
    return json.dumps(registry.set_status(registry.connect(), id, version, status, note), indent=2)


@mcp.tool()
def list_submitted_strategies(id: str = "", status: str = "") -> str:
    """List registered strategy spec versions with status and a short summary. Optionally filter by id or status."""
    return json.dumps(registry.list_specs(registry.connect(), id, status), indent=2)


@mcp.tool()
def get_submitted_strategy(id: str, version: str) -> str:
    """Full stored spec for one registered version, plus its status history."""
    return json.dumps(registry.get_spec(registry.connect(), id, version), indent=2)


def _load_spec(rcon, id: str, version: str):
    row = registry.get_spec(rcon, id, version)
    return None if "error" in row else row["spec"]


@mcp.tool()
def import_backtest(id: str, version: str, path: str = "", csv_text: str = "", label: str = "",
                    export_timezone: str = "") -> str:
    """Import a NinjaTrader Strategy Analyzer CSV export (Trades grid, or Periods grid with Period = Day) for a
    registered strategy version. Give a file `path` on this machine or the `csv_text`. Timestamps are read in
    `export_timezone` (default: the spec's). Stores the run and returns metrics, a check against the spec's
    expected_metrics, and warnings about what was assumed. Local only; never touches NinjaTrader."""
    rcon = registry.connect()
    try:
        spec = _load_spec(rcon, id, version)
        if spec is None:
            return json.dumps({"ok": False, "error": f"{id} {version} is not registered (use submit_strategy first)"})
        if path:
            try:
                with open(path, "rb") as f:
                    raw = f.read()
            except OSError as e:
                return json.dumps({"ok": False, "error": f"cannot read {path}: {e}"})
            try:
                csv_text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                csv_text = raw.decode("cp1252")
        if not csv_text.strip():
            return json.dumps({"ok": False, "error": "give a file path or the CSV text"})
        return json.dumps(backtest.import_run(rcon, spec, csv_text, label, os.path.basename(path), export_timezone),
                          indent=2)
    finally:
        rcon.close()


@mcp.tool()
def list_backtests(id: str = "", version: str = "") -> str:
    """List imported backtest runs (newest first) with a short metric summary."""
    rcon = registry.connect()
    try:
        return json.dumps(backtest.list_runs(rcon, id, version), indent=2)
    finally:
        rcon.close()


@mcp.tool()
def check_backtest(run_id: int) -> str:
    """Full metrics of an imported run, checked against its spec's expected_metrics (within 15% = ok)."""
    rcon = registry.connect()
    try:
        run = backtest.get_run(rcon, run_id)
        if not run:
            return json.dumps({"error": f"no backtest run {run_id}"})
        spec = _load_spec(rcon, run["spec_id"], run["version"])
        m = json.loads(run["metrics_json"])
        return json.dumps({"run_id": run_id, "spec": f"{run['spec_id']} {run['version']}", "label": run["label"],
                           "metrics": m, "claims_check": backtest.claims_check(m, spec or {})}, indent=2)
    finally:
        rcon.close()


@mcp.tool()
def compare_backtest_to_sim(id: str, version: str, account: str = "") -> str:
    """Backtest vs sim on identical metrics (expectancy, win rate, profit factor, average win/loss, trades per year,
    median holding time) for a registered strategy version, with an ok/worse/better/shift flag per row. Flags are
    suppressed below 15 sim trades. Read-only; it does not record a verdict or change a stage."""
    rcon, jcon = registry.connect(), journal.connect()
    try:
        return json.dumps(comparison.for_strategy(rcon, jcon, id, version, account), indent=2, default=str)
    finally:
        rcon.close()
        jcon.close()


@mcp.tool()
def compare_backtest_to_journal(run_id: int, account: str = "", leg: str = "") -> str:
    """Day-by-day comparison of an imported backtest against the journaled sim/live trades of the same strategy:
    days only one side traded, same-direction rate, net difference, biggest daily gaps. The backtest must cover the
    live period (re-run the Analyzer over it). `leg` limits both sides to one leg (needs a Trades-grid import)."""
    rcon = registry.connect()
    try:
        run = backtest.get_run(rcon, run_id)
        if not run:
            return json.dumps({"error": f"no backtest run {run_id}"})
        spec = _load_spec(rcon, run["spec_id"], run["version"]) or {}
    finally:
        rcon.close()
    jcon = journal.connect()
    try:
        rows = journal.query(jcon, account, run["spec_id"], "", 5000)
    finally:
        jcon.close()
    if leg:
        rows = [r for r in rows if r.get("leg") == leg]
    built = trades.build_trades(rows)
    out = backtest.compare_to_journal(run, spec, built["trades"], leg)
    return json.dumps(out, indent=2)


@mcp.tool()
def evaluate_strategy(id: str, version: str, account: str = "", criteria: str = "", record: bool = False,
                      since: str = "") -> str:
    """Judge a registered strategy version on its journaled Sim results: verdict promote / keep_testing / reject,
    with every check, threshold and actual value (see verdict.py). Run sync_journal first. Trades are limited to this
    spec version since it entered 'sim' (override with `since`, ISO time). Expected win rate/expectancy come from the
    latest imported backtest, else the spec's expected_metrics. `criteria` is optional JSON overriding defaults, e.g.
    {"min_trades": 40, "drawdown_limit": 2000}. record=true stores the verdict in the experiment log
    (list_verdicts). Recommends only: it never disables or promotes anything."""
    try:
        crit = json.loads(criteria) if criteria else None
    except ValueError:
        return json.dumps({"error": "criteria must be valid JSON"})
    rcon, jcon = registry.connect(), journal.connect()
    try:
        out = verdict.evaluate_registered(rcon, jcon, id, version, account, crit, since)
        if "error" in out:
            return json.dumps(out)
        out.pop("trades", None)
        out.pop("backtest", None)
        if record:
            out["verdict_id"] = verdict.record(rcon, id, version, out["status"], out)
        return json.dumps(out, indent=2)
    finally:
        rcon.close()
        jcon.close()


@mcp.tool()
def export_feedback(id: str, version: str, account: str = "", as_of: str = "", correction: bool = False) -> str:
    """Write a feedback snapshot (trades, daily P&L, summary, verdict with the live-small readiness block, drift
    flags, backtest check) for one registered strategy version into NT_FEEDBACK_DIR/<lucid_id>/<version>/<as_of>/
    for the Lucid research side to read (layout: docs/feedback/SCHEMA.md). Run sync_journal first. Read-only toward
    NinjaTrader and the registry: it changes no status. Account names are exported as stable aliases. Re-running with
    unchanged results is a no-op; changed results make a new snapshot, and correction=true names the one it supersedes.
    as_of is an optional ISO UTC time (default now)."""
    try:
        when = datetime.fromisoformat(as_of.replace("Z", "+00:00")) if as_of else None
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except ValueError:
        return json.dumps({"ok": False, "error": "as_of must be an ISO time such as 2026-11-10T15:00:00Z"})
    rcon, jcon = registry.connect(), journal.connect()
    try:
        return json.dumps(feedback.export(rcon, jcon, id, version, account=account, as_of=when,
                                          correction=correction), indent=2)
    finally:
        rcon.close()
        jcon.close()


def _when(as_of: str):
    when = datetime.fromisoformat(as_of.replace("Z", "+00:00")) if as_of else None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


@mcp.tool()
def approve_live_small(id: str, version: str, account_alias: str, reason: str, approval_secret: str,
                       acknowledge_not_ready: bool = False) -> str:
    """OWNER ONLY. Move a 'sim' version to 'live-small' (registry status only; starts nothing, and every NT_OPERATOR_*
    gate still applies to the launcher / deploy_strategy_live_small). Needs the owner's approval_secret (checked against
    NT_OPERATOR_APPROVAL_SECRET_SHA256 in this server's environment; never logged) and NT_OPERATOR_TOKEN. Refused unless the
    version meets the readiness criteria, or acknowledge_not_ready=true (recorded). A non-empty reason is required.
    account_alias is a stable local alias such as acct-1. Supersedes any pending schedule for the version. Audited."""
    rcon, jcon = registry.connect(), journal.connect()
    try:
        return json.dumps(approvals.approve(rcon, jcon, id, version, account_alias, reason, approval_secret,
                                            acknowledge_not_ready=acknowledge_not_ready), indent=2)
    finally:
        rcon.close()
        jcon.close()


@mcp.tool()
def schedule_live_small_approval(id: str, version: str, account_alias: str, reason: str, approval_secret: str,
                                 not_before: str, expires_at: str = "") -> str:
    """OWNER ONLY. Store an approval that the launcher tick executes at the first tick at/after not_before (ISO UTC;
    30-minute granularity, inside the enable window) if the version is still 'sim' and still meets readiness; else it is
    voided. expires_at defaults to 24 h after not_before (max 7 days). One pending schedule per version: a new one
    supersedes the old. Same secret/token rules as approve_live_small. Audited."""
    try:
        _when(not_before)
    except ValueError:
        return json.dumps({"ok": False, "error": "not_before must be an ISO UTC time such as 2026-11-12T13:30:00Z"})
    rcon, jcon = registry.connect(), journal.connect()
    try:
        return json.dumps(approvals.schedule(rcon, jcon, id, version, account_alias, reason, approval_secret,
                                             not_before, expires_at), indent=2)
    finally:
        rcon.close()
        jcon.close()


@mcp.tool()
def cancel_scheduled_approval(schedule_id: int, approval_secret: str, reason: str = "") -> str:
    """OWNER ONLY. Cancel a pending scheduled approval (needs the same secret as approving). Audited."""
    rcon = registry.connect()
    try:
        return json.dumps(approvals.cancel(rcon, schedule_id, approval_secret, reason), indent=2)
    finally:
        rcon.close()


@mcp.tool()
def list_scheduled_approvals(state: str = "") -> str:
    """List scheduled approvals (pending, executed, voided, cancelled, superseded), newest first. Read-only; no secret."""
    rcon = registry.connect()
    try:
        return json.dumps(approvals.list_schedules(rcon, state), indent=2)
    finally:
        rcon.close()


@mcp.tool()
def list_nudges(account: str = "", as_of: str = "") -> str:
    """Nudge list: Sim strategy versions that meet every live-small readiness criterion right now. Read-only and needs
    no secret: it computes readiness fresh (same rules as the feedback export), stores nothing, changes no status and
    makes no NinjaTrader call. A nudge vanishes as soon as readiness lapses. Run sync_journal first. A nudge is a
    suggestion only; moving a version to live-small is a separate owner Approval. as_of is an optional ISO UTC time."""
    try:
        when = datetime.fromisoformat(as_of.replace("Z", "+00:00")) if as_of else None
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except ValueError:
        return json.dumps({"ok": False, "error": "as_of must be an ISO time such as 2026-11-10T15:00:00Z"})
    rcon, jcon = registry.connect(), journal.connect()
    try:
        return json.dumps(nudges.list_nudges(rcon, jcon, account, when), indent=2)
    finally:
        rcon.close()
        jcon.close()


@mcp.tool()
def list_verdicts(id: str = "", version: str = "", limit: int = 50) -> str:
    """Experiment log: recorded verdicts (newest first) for a strategy, to see how a strategy's results evolved."""
    rcon = registry.connect()
    try:
        return json.dumps(verdict.history(rcon, id, version, limit), indent=2)
    finally:
        rcon.close()


@mcp.tool()
def evaluate_all_strategies(account: str = "", record: bool = False) -> str:
    """One-line verdict for every registered strategy in sim/live-small/live. Same rules as evaluate_strategy."""
    rcon = registry.connect()
    try:
        keys = [(s["id"], s["version"]) for st in ("sim", "live-small", "live")
                for s in registry.list_specs(rcon, status=st)]
    finally:
        rcon.close()
    rows = []
    for sid, ver in keys:
        r = json.loads(evaluate_strategy(sid, ver, account, "", record))
        rows.append({"id": sid, "version": ver, "verdict": r.get("verdict"), "trades": r.get("sample", {}).get("trades"),
                     "net_pnl": r.get("performance", {}).get("net_pnl"), "reasons": r.get("reasons"),
                     "error": r.get("error")})
    return json.dumps(rows, indent=2)


@mcp.tool()
def disable_decayed_strategies(account: str, reason: str = "", dry_run: bool = True) -> str:
    """Find strategies in sim whose edge has decayed against their backtest (evaluate_strategy: edge_decayed) and
    disable every leg on `account`. dry_run=true (default) only lists what would be disabled. With dry_run=false it
    calls disable_strategy per leg (Sim-only, audited), then marks the spec 'retired' with the evidence as the note.
    It does not flatten: check get_positions afterwards and use flatten if a leg holds a position."""
    rcon = registry.connect()
    try:
        specs = registry.list_specs(rcon, status="sim")
    finally:
        rcon.close()
    rows = []
    for s in specs:
        r = json.loads(evaluate_strategy(s["id"], s["version"], account, "", False))
        if not r.get("edge_decayed"):
            continue
        rcon = registry.connect()
        try:
            spec = registry.get_spec(rcon, s["id"], s["version"])["spec"]
        finally:
            rcon.close()
        names = [f"{s['id']} {s['version']} {leg['name']}" for leg in spec.get("legs", [])]
        row = {"id": s["id"], "version": s["version"], "legs": names, "reasons": r["reasons"], "disabled": []}
        if not dry_run:
            note = "edge decayed vs backtest: " + "; ".join(r["reasons"])
            for nm in names:
                res = json.loads(disable_strategy(account, nm, reason or note))
                row["disabled"].append({"name": nm, "ok": bool(res.get("ok")), "error": res.get("error")})
            if all(d["ok"] for d in row["disabled"]):
                rcon = registry.connect()
                try:
                    row["status"] = registry.set_status(rcon, s["id"], s["version"], "retired", note)
                    verdict.record(rcon, s["id"], s["version"], "sim", r)
                finally:
                    rcon.close()
        rows.append(row)
    return json.dumps({"dry_run": dry_run, "decayed": rows}, indent=2)


@mcp.tool()
def install_handoff(path: str, apply: bool = False) -> str:
    """Check a Lucid handoff bundle folder (handoff/<id>/<version>): spec schema, approval flag, .cs sha256 against
    MANIFEST, inputs and order names against the C# source. With apply=false (default) it only reports the plan.
    With apply=true it backs up and copies the .cs files into NinjaTrader's Custom/Strategies, registers the spec as
    a candidate and writes per-leg strategy templates when a fresh skeleton exists. You still compile in NinjaTrader (F5)."""
    return json.dumps(handoff.install_bundle(path, apply), indent=2)


@mcp.tool()
def promote_strategy(id: str, version: str, account: str, bundle_path: str = "") -> str:
    """One-command promotion of a Lucid handoff bundle to Sim: export check -> install (candidate) -> Strategy Analyzer
    backtest + import (status backtest) -> spec check against expected_metrics -> enable on a Sim* account (status sim).
    Stops at the first failed stage and leaves status at the last passed one, with no override; re-running skips
    completed stages and resumes. Ceiling is Sim: never touches live-small or live. Needs NinjaTrader running with the
    add-on, ONE idle Strategy Analyzer window, and NT_OPERATOR_ALLOW_STRATEGY_START=1 (for the Sim start). Single-flight:
    a second call reports busy. Writes promotion_report.json under NT_FEEDBACK_DIR/<lucid_id>/<version>/.
    bundle_path defaults to NT_HANDOFF_DIR/<id>/<version>. Every stage action is audited."""
    import tempfile

    def _install(path):
        return handoff.install_bundle(path, apply=True)

    def _backtest(spec, path):
        r = handoff.inspect_bundle(path)
        if not r["ok"]:
            return {"ok": False, "error": "bundle: " + "; ".join(r["errors"])}
        with tempfile.TemporaryDirectory() as scratch:
            return backtest_runner.collect_csv(spec, r["legs"], scratch, log=lambda *_: None)

    def _enable(spec, acct):
        if os.environ.get("NT_OPERATOR_ALLOW_STRATEGY_START") != "1":
            return {"ok": False, "error": "strategy start is locked: set NT_OPERATOR_ALLOW_STRATEGY_START=1"}
        res = handoff.enable_spec(spec, acct, lambda p, q, b: _post(p, q, b), _fetch, skip_defaults=True)
        control.audit("enable_strategy", {"id": spec["id"], "version": spec["version"], "account": acct}, res,
                      "promote_strategy")
        return res

    rcon = registry.connect()
    try:
        return json.dumps(promote.promote(rcon, id, version, account, bundle_path,
                                          {"install": _install, "backtest": _backtest, "enable": _enable}),
                          indent=2, default=str)
    finally:
        rcon.close()


@mcp.tool()
def enable_strategy(id: str, version: str, account: str, reason: str = "", skip_defaults: bool = True) -> str:
    """EXPERIMENTAL. Start every leg of a registered strategy version on a Sim account only. Requires registry status
    backtest or sim, NT_OPERATOR_ALLOW_STRATEGY_START=1 in NinjaTrader's environment, compiled scripts, and no conflicts.
    All legs or none. On success a 'backtest' spec moves to 'sim'. Logged to the audit log."""
    rcon = registry.connect()
    row = registry.get_spec(rcon, id, version)
    params = {"id": id, "version": version, "account": account}
    refusal = None if account.lower().startswith("sim") else "enable_strategy is Sim-only; use deploy_strategy_live_small for an approved live-small deployment"
    if not refusal and "error" in row:
        refusal = row["error"]
    if not refusal and row.get("status") not in ("backtest", "sim"):
        refusal = (f"status is '{row.get('status')}': move it to 'backtest' (after a Strategy Analyzer run) with "
                   f"set_strategy_status before enabling")
    if refusal:
        result = {"ok": False, "error": refusal}
    else:
        result = handoff.enable_spec(row["spec"], account, lambda p, q, b: _post(p, q, b), _fetch, skip_defaults=skip_defaults)
        if result["ok"] and row["status"] == "backtest":
            registry.set_status(rcon, id, version, "sim", f"enabled on {account} via add-on")
    control.audit("enable_strategy", params, result, reason)
    return json.dumps(result, indent=2)


def _check_live_small_deployment(account: str, status: str):
    """Dedicated two-sided live-small gate; broad live unlocks never satisfy it."""
    if status != "live-small":
        return f"strategy status must be exactly 'live-small' (currently '{status}')"
    if account and account.lower().startswith("sim"):
        return "deploy_strategy_live_small is for real accounts; use enable_strategy for Sim accounts"
    if os.environ.get("NT_OPERATOR_ALLOW_LIVE_SMALL") != "1":
        return "live-small deployment locked: set NT_OPERATOR_ALLOW_LIVE_SMALL=1 in the MCP server environment"
    allowed = [a.strip() for a in os.environ.get("NT_OPERATOR_LIVE_SMALL_ACCOUNTS", "").split(",") if a.strip()]
    if not account or account not in allowed:
        return f"account '{account}' is not in the exact NT_OPERATOR_LIVE_SMALL_ACCOUNTS allowlist"
    if os.environ.get("NT_OPERATOR_ALLOW_STRATEGY_START") != "1":
        return "strategy start is locked: set NT_OPERATOR_ALLOW_STRATEGY_START=1 in the MCP server environment"
    if not os.environ.get("NT_OPERATOR_TOKEN"):
        return "live-small deployment requires NT_OPERATOR_TOKEN to be set in both the MCP server and NinjaTrader environments"
    return None


@mcp.tool()
def deploy_strategy_live_small(id: str, version: str, account: str, reason: str = "") -> str:
    """EXPERIMENTAL LIVE ACTION. Start a registered live-small strategy on one exact allow-listed real account.
    Requires NT_OPERATOR_ALLOW_LIVE_SMALL=1, NT_OPERATOR_ALLOW_STRATEGY_START=1, and exact account membership in
    NT_OPERATOR_LIVE_SMALL_ACCOUNTS. The NinjaTrader add-on independently enforces the same live-small gate.
    All legs or none; no automatic stage promotion. Strategy sizing is not a broker-side loss limit."""
    rcon = registry.connect()
    try:
        row = registry.get_spec(rcon, id, version)
        refusal = row.get("error") if "error" in row else _check_live_small_deployment(account, row.get("status"))
        if not refusal and not reason.strip():
            refusal = "a non-empty deployment reason is required and will be written to the audit log"
        params = {"id": id, "version": version, "account": account}
        if refusal:
            result = {"ok": False, "error": refusal}
        else:
            result = handoff.enable_spec(row["spec"], account, lambda p, q, b: _post(p, q, b), _fetch, skip_defaults=True)
        control.audit("deploy_strategy_live_small", params, result, reason)
        return json.dumps(result, indent=2)
    finally:
        rcon.close()


@mcp.tool()
def disable_strategy(account: str, name: str, reason: str = "") -> str:
    """Disable and stop one strategy instance on Sim or an allow-listed live-small account (names look like '<id> <version> <leg>').
    Does not flatten: use flatten if it holds a position. Logged."""
    return _control("disable_strategy", "/strategy/disable", {"account": account, "name": name}, reason)


@mcp.tool()
def list_strategy_rows() -> str:
    """List every row in NinjaTrader's Strategies tab(s), including stopped ones left behind by earlier enables
    (get_strategies cannot see those). Read-only. Needs the add-on's /strategy/rows endpoint (experimental)."""
    return json.dumps(_fetch("/strategy/rows"), indent=2)


@mcp.tool()
def purge_strategy_rows(account: str, confirm: bool = False, name: str = "", stale: bool = False,
                        reason: str = "") -> str:
    """Remove stale rows from the Strategies tab for one account: only rows that are NOT enabled, not running
    (Historical/Transition/Realtime) and flat. Same account guards as disable_strategy. confirm=false (default) is a
    dry run that lists what it would remove; confirm=true removes them. `name` limits it to one exact row name;
    with stale=true (needs `name`) a stopped Configure row of that exact name is removable too. Audited. Experimental: the add-on endpoint is uncompiled until you press F5."""
    params = {"account": account, "name": name, "confirm": "1" if confirm else "", "stale": "1" if stale and name else ""}
    return _control("purge_strategy_rows", "/strategy/purge", params, reason)


@mcp.tool()
def flatten(account: str, instrument: str = "", reason: str = "") -> str:
    """Flatten open positions on a Sim account (market-closes them and cancels their working orders). Optional
    instrument (e.g. 'MNQ 12-26') limits it to one. Risk-reducing only; refuses non-Sim accounts. Does NOT stop a
    strategy from re-entering: disable it in NinjaTrader too. Logged to the audit log."""
    return _control("flatten", "/flatten", {"account": account, "instrument": instrument}, reason)


@mcp.tool()
def cancel_all_orders(account: str, instrument: str = "", reason: str = "") -> str:
    """Cancel all working orders on a Sim account (optionally one instrument). Warning: this also cancels protective
    stops, so only use it when flat or together with flatten. Refuses non-Sim accounts. Logged."""
    return _control("cancel_all", "/cancel_all", {"account": account, "instrument": instrument}, reason)


@mcp.tool()
def arm_kill_switch(account: str, max_loss: float, reason: str = "") -> str:
    """Arm a daily-loss kill switch inside the NinjaTrader add-on (works even if Claude is closed). When realized +
    unrealized P&L since arming reaches -max_loss dollars it flattens and cancels every 2 seconds until disarmed
    (latched). Arm it at the start of the session. It lives in add-on memory, so restarting NinjaTrader disarms it.
    Sim accounts only. Logged."""
    return _control("arm_kill_switch", "/killswitch/arm", {"account": account, "max_loss": max_loss}, reason)


@mcp.tool()
def disarm_kill_switch(reason: str = "") -> str:
    """Disarm the kill switch and clear a tripped latch. Logged."""
    result = _post("/killswitch/disarm", {})
    control.audit("disarm_kill_switch", {}, result, reason)
    return json.dumps(result, indent=2)


@mcp.tool()
def get_kill_switch() -> str:
    """Kill switch state: armed, tripped, P&L since arming, flatten cycles."""
    return json.dumps(_fetch("/killswitch"), indent=2)


@mcp.tool()
def get_audit_log(limit: int = 50) -> str:
    """Recent control actions (flatten, cancel, kill switch), including refused attempts."""
    return json.dumps(control.read_audit(max(1, min(limit, 500))), indent=2)


@mcp.tool()
def get_recovery_status() -> str:
    """Show the add-on's opt-in reconnect recovery watchdog, pending strategies, and any blocked/failed restart."""
    return json.dumps(_fetch("/recovery"), indent=2)


@mcp.tool()
def get_health() -> str:
    """Add-on heartbeat and connection / data-feed status. Call this first if other data looks wrong."""
    data = _fetch("/health")
    if "error" in data:
        return json.dumps(data)
    problems = [c for c in data.get("connections", [])
                if c.get("status") != "Connected" or c.get("price_status") != "Connected"]
    if problems:
        data["warning"] = "Connection or data feed not Connected: " + ", ".join(c["account"] for c in problems)
    return json.dumps(data, indent=2)


if __name__ == "__main__":
    mcp.run()
