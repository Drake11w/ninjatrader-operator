"""Local web dashboard: every registered strategy, how long it has run, approval and verdict status, P&L since activation.

    python dashboard.py            # http://127.0.0.1:8766 (NT_DASHBOARD_PORT to change)

Read-only. It reads the registry, journal and verdict log, asks the NinjaTrader add-on for live state when it is
reachable, and syncs new fills into the local journal on each refresh (same as sync_journal). It never sends a
control command. Binds to 127.0.0.1 only.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import journal
import registry
import rules as rules_mod
import trades as trades_mod
import verdict

BASE = os.environ.get("NT_ADDON_URL", "http://127.0.0.1:8765")
LIVE_STAGES = ("sim", "live-small", "live")
HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


def fetch(path: str) -> dict:
    try:
        with urllib.request.urlopen(BASE + path, timeout=4) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"error": str(e)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def sync_journal(rcon, jcon, executions: list) -> int:
    tagger = journal.make_tagger(jcon, registry.order_name_map(rcon))
    ex = sorted(executions, key=lambda e: e.get("time") or "")
    return journal.record(jcon, ex, "", "", tagger)["new"]


def _equity(ts: list, tz_name: str) -> list:
    """[[day, cumulative net P&L]] by exit day since activation."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    daily = {}
    for t in ts:
        d = _parse(t["exit_time"]).astimezone(tz).date().isoformat()
        daily[d] = daily.get(d, 0.0) + t["net_pnl"]
    cum, out = 0.0, []
    for d in sorted(daily):
        cum += daily[d]
        out.append([d, round(cum, 2), round(daily[d], 2)])
    return out


def _strategy(rcon, jcon, spec_row: dict, nt: dict, account: str, now: datetime) -> dict:
    sid, ver = spec_row["id"], spec_row["version"]
    full = registry.get_spec(rcon, sid, ver)
    spec, status = full["spec"], full["status"]
    tz_name = spec.get("timezone", "America/Chicago")
    res = verdict.evaluate_registered(rcon, jcon, sid, ver, account)
    ts = res["trades"]
    activated = verdict.sim_start(rcon, sid, ver)
    running = status in LIVE_STAGES and bool(activated)
    perf = trades_mod.performance(ts, tz_name) if ts else {"trades": 0}
    eq = _equity(ts, tz_name)
    try:
        from zoneinfo import ZoneInfo
        today = now.astimezone(ZoneInfo(tz_name)).date().isoformat()
    except Exception:
        today = now.date().isoformat()
    pnl_today = sum(d[2] for d in eq if d[0] == today)

    legs = []
    for leg in spec.get("legs", []):
        inst = f"{sid} {ver} {leg['name']}"
        live = next((s for s in nt.get("strategies", []) if s.get("name") == inst), None)
        lt = [t for t in ts if t.get("leg") == leg["name"]]
        legs.append({
            "name": leg["name"], "instance": inst, "side": leg.get("side"), "days": leg.get("days"),
            "session": leg.get("session"), "contracts": (leg.get("sizing") or {}).get("contracts"),
            "nt": ({"state": live.get("state"), "position": live.get("position"), "quantity": live.get("quantity"),
                    "account": live.get("account")} if live else None),
            "trades": len(lt), "net_pnl": round(sum(t["net_pnl"] for t in lt), 2),
            "wins": sum(1 for t in lt if t["net_pnl"] > 0)})

    hist = full["history"]
    cur = hist[-1] if hist else {}
    last_trade = ts[-1]["exit_time"] if ts else None
    appr = registry.get_approval(rcon, sid, ver)
    recorded = verdict.history(rcon, sid, ver, 10)
    bt = res.get("backtest") or {}
    return {
        "id": sid, "version": ver, "name": spec.get("name") or sid, "lucid_id": spec.get("lucid_id"),
        "hypothesis": spec.get("hypothesis"), "status": status, "status_since": cur.get("at"),
        "stage_history": hist, "approval": appr,
        "activated_at": activated or None,
        "running_seconds": int((now - _parse(activated)).total_seconds()) if running else None,
        "running": running,
        "nt_running": any(l["nt"] and l["nt"]["state"] == "Realtime" for l in legs) if legs else False,
        "legs": legs, "instrument": spec["instrument"]["root"] if "instrument" in spec else None,
        "performance": {k: perf.get(k) for k in ("trades", "net_pnl", "win_rate", "expectancy", "profit_factor",
                                                 "avg_win", "avg_loss", "max_drawdown", "best_day", "worst_day",
                                                 "trading_days", "avg_duration_sec", "sharpe")},
        "pnl_today": round(pnl_today, 2), "last_trade": last_trade, "equity": eq,
        "backtest": {k: bt.get(k) for k in ("run_id", "label", "start", "end", "net_per_year", "trades_per_year",
                                           "expectancy", "win_rate", "max_drawdown", "net_pnl", "trades")} if bt else None,
        "expected_metrics": spec.get("expected_metrics"),
        "verdict": {k: res.get(k) for k in ("verdict", "edge_decayed", "reasons", "checks", "sample", "eta",
                                            "next_step", "expected", "warnings", "criteria")},
        "recorded_verdicts": recorded,
    }


def collect(account: str = "", sync: bool = True, fetch_fn=None, now: datetime = None) -> dict:
    fetch_fn = fetch_fn or fetch
    now = now or _now()
    status = fetch_fn("/status")
    health = fetch_fn("/health")
    nt = {"reachable": "error" not in status, "error": status.get("error"), "accounts": [], "strategies": [],
          "connections": health.get("connections", []) if "error" not in health else []}
    synced = None
    rcon, jcon = registry.connect(), journal.connect()
    try:
        if nt["reachable"]:
            nt["accounts"] = [{k: a.get(k) for k in ("name", "connection", "cash_value", "realized_pnl", "unrealized_pnl")}
                              | {"positions": a.get("positions", [])} for a in status.get("accounts", [])]
            st = fetch_fn("/strategies")
            nt["strategies"] = st.get("strategies", []) if "error" not in st else []
            if sync:
                ex = fetch_fn("/executions?limit=500")
                if "error" not in ex:
                    synced = sync_journal(rcon, jcon, ex.get("executions", []))
        specs = registry.list_specs(rcon)
        items = [_strategy(rcon, jcon, s, nt, account, now) for s in specs]
        rules_out = None
        try:
            cfg = rules_mod.load_rules()
            rows = journal.query(jcon, account, "", "", 5000)
            rules_out = rules_mod.evaluate(trades_mod.build_trades(rows)["trades"], cfg, now=now)
        except (OSError, ValueError, KeyError):
            pass
    finally:
        rcon.close()
        jcon.close()
    active = [i for i in items if i["status"] in LIVE_STAGES]
    summary = {
        "total": len(items), "active": len(active),
        "by_stage": {st: sum(1 for i in items if i["status"] == st)
                     for st in registry.STAGES + registry.TERMINAL},
        "running_in_nt": sum(1 for i in active if i["nt_running"]),
        "net_pnl_since_activation": round(sum(i["performance"]["net_pnl"] or 0 for i in active), 2),
        "pnl_today": round(sum(i["pnl_today"] for i in active), 2),
        "trades": sum(i["performance"]["trades"] or 0 for i in active),
        "verdicts": {v: sum(1 for i in active if i["verdict"]["verdict"] == v)
                     for v in ("promote", "keep_testing", "reject")},
        "needs_attention": [f"{i['id']} {i['version']}: {i['verdict']['verdict']}" for i in active
                            if i["verdict"]["verdict"] in ("promote", "reject")],
    }
    return {"generated_at": now.isoformat(), "nt": nt, "summary": summary, "strategies": items,
            "rules": rules_out, "journal_synced_new_fills": synced}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/api/data":
            account = ""
            for kv in query.split("&"):
                if kv.startswith("account="):
                    account = urllib.parse.unquote(kv[8:])
            try:
                body = json.dumps(collect(account), default=str).encode()
                self._send(200, body, "application/json")
            except Exception as e:  # keep the page alive; show the error in the UI
                self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")
        elif path in ("/", "/index.html"):
            with open(HTML, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


def main():
    port = int(os.environ.get("NT_DASHBOARD_PORT", "8766"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard on http://127.0.0.1:{port}  (Ctrl+C to stop)", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
