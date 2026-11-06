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

import backtest
import control
import journal
import registry
import report
import rules
import sessions
import strategy_spec
import trades

BASE = os.environ.get("NT_ADDON_URL", "http://127.0.0.1:8765")
STALE_SECONDS = 30
mcp = MCPServer("ninjatrader-readonly")


def _fetch(path: str = "/status") -> dict:
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return _stamp(json.loads(r.read().decode("utf-8")))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"error": f"NinjaTrader add-on unreachable or bad reply: {e}. Is NT8 running with NtStatusAddOn compiled?"}


def _post(path: str, params: dict) -> dict:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(f"{BASE}{path}?{query}", data=b"", method="POST")
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
                tagger = journal.make_tagger(con, registry.order_name_map(rcon))
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
    it is the front month. Computed locally; exchange holidays are not modelled."""
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
