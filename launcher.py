"""Launcher: starts NinjaTrader and turns on the currently live strategy when it is meant to run.

    python launcher.py              plan only: print what a tick would do, change nothing
    python launcher.py --apply      one tick for real (this is what the scheduled task runs, every few minutes)
    python launcher.py --status     print today's state
    python launcher.py --pause      create the pause file: the launcher does nothing until --resume
    python launcher.py --resume

Deliberately separate from the read-only supervisor daemon (sup_*.py has no POST path and keeps it that way).

What "the live strategy" means: every registry spec whose status is exactly 'live-small' or 'live', plus (with
"enable_sim": true, the default) every 'sim' spec, which may only run on a Sim* account. The account comes from the
supervisor profile of that strategy, so there is one place that says where a strategy runs.

A tick is idempotent and never blocks. Order of steps:
  1. Weekend closure (Fri 16:00 CT to Sun 16:45 CT), paused, or not enabled in config: do nothing.
  2. NinjaTrader not running (any time the exchange is open, so a crash overnight is recovered): start it minimized and without taking focus (at most once per 10 minutes).
  3. Add-on unreachable, or the account's connection / data feed not Connected: wait (alert after a timeout).
  4. Inside the enable window (weekdays, default 07:30-15:00 CT): for each live (or sim) strategy: if any of its legs is already in /strategies, leave it alone. Otherwise enable it, but
     only if it passes the same gates as deploy_strategy_live_small, the account is flat and nothing else is
     running on it, and it has not already been enabled today. Right before enabling, the old stopped rows of its
     legs are removed from the Strategies tab (/strategy/purge with stale=1), so the tab keeps one row per leg.

"Once per day" is the safety valve: if you stop a strategy by hand, the launcher does not start it again until
tomorrow. Failed attempts are retried up to MAX_ATTEMPTS per day, then it alerts and stops trying.

It cannot log in for you, and NinjaTrader 8.1 asks for the password on every launch (docs/KNOWN_ISSUES.md BUG-001):
a restart stops at the login window. The launcher alerts when it sees that window, unless the supervisor daemon is
running (it reports the same thing). Every --apply run writes launcher_heartbeat.json for the watcher (sup_watch.py). Exchange holidays are not modelled (see sessions.py).
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone

import control
import handoff
import nt_local
import registry
import sessions
import sup_alerts
import sup_profile

DIR = nt_local.DIR
BASE = os.environ.get("NT_ADDON_URL", "http://127.0.0.1:8765")
NT_EXE_DEFAULT = r"C:\Program Files\NinjaTrader 8\bin\NinjaTrader.exe"
LIVE_STAGES = ("live-small", "live")
SIM_STAGE = "sim"
RELAUNCH_MIN = timedelta(minutes=10)
CONNECT_TIMEOUT = timedelta(minutes=10)
MAX_ATTEMPTS = 3
DEFAULT_CONFIG = {
    "enabled": False,               # master switch: nothing happens until you set this to true
    "launch_nt": True,
    "auto_enable": True,
    "enable_sim": True,             # also enable 'sim'-stage strategies (Sim* accounts only)
    "nt_exe": NT_EXE_DEFAULT,
    "timezone": "America/Chicago",  # the strategies' own clock (08:30 CT open)
    "days": [0, 1, 2, 3, 4],        # Monday = 0
    "weekend_close": "16:00",       # Friday, local time: exchange closes, launcher goes quiet (CME: 5pm ET = 4pm CT)
    "weekend_reopen": "16:45",      # Sunday, local time: launcher resumes (15 min before the 5pm CT Globex open)
    "enable_from": "07:30",         # enable strategies from here (on `days` only)...
    "enable_until": "15:00",        # ...to here; never later (legs flatten at 15:40)
}


config_path = nt_local.launcher_config_path
state_path = nt_local.launcher_state_path


def pause_path() -> str:
    return os.path.join(os.path.dirname(state_path()), "launcher.pause")


def load_config(path: str = "") -> dict:
    path = path or config_path()
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        unknown = set(raw) - set(DEFAULT_CONFIG)
        if unknown:
            raise ValueError(f"unknown launcher config keys: {sorted(unknown)}")
        cfg.update(raw)
    for k in ("weekend_close", "weekend_reopen", "enable_from", "enable_until"):
        _hm(cfg[k])
    return cfg


def _hm(s: str) -> time:
    try:
        h, m = s.split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        raise ValueError(f"bad time '{s}', expected HH:MM")


def load_state(path: str = "") -> dict:
    try:
        with open(path or state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: str = "") -> None:
    path = path or state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


# --- I/O (replaced in tests) -------------------------------------------------------------------------------------

nt_running = nt_local.nt_running
login_window_open = nt_local.login_window_open


def supervisor_alive() -> bool:
    """True if the supervisor daemon's heartbeat is fresh: it (and the watcher) will report a stuck NinjaTrader, so
    the launcher does not send a second alert for the same thing. Any doubt counts as not alive."""
    try:
        import sup_daemon
        import sup_incidents
        con = sup_incidents.connect()
        try:
            return not sup_daemon.status(con)["heartbeat_stale"]
        finally:
            con.close()
    except Exception:
        return False


def start_nt(exe: str) -> None:
    """Start NinjaTrader detached, minimized, not activated (SW_SHOWMINNOACTIVE), so it cannot pull a game out of
    fullscreen. NinjaTrader inherits this process's environment, which is how its NT_OPERATOR_* gates get set."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 7
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen([exe], cwd=os.path.dirname(exe), startupinfo=si, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fetch(path: str) -> dict:
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        return {"error": f"{type(e).__name__}: {e}"}


def post(path: str, params: dict, body: str = "") -> dict:
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
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        return {"ok": False, "error": f"add-on unreachable or bad reply: {e}"}


def alert(text: str) -> None:
    sup_alerts.notify([{"kind": "launcher", "id": "-", "key": "launcher", "severity": 1, "trigger": text}])
    sup_alerts.toast("NT Launcher", text[:200].replace("'", ""))


# --- decisions ---------------------------------------------------------------------------------------------------

def live_gate(account: str, status: str):
    """Same gate as server.deploy_strategy_live_small (kept in step by a test). Returns a refusal or None."""
    if status not in LIVE_STAGES:
        return f"stage '{status}' is not live"
    if account.lower().startswith("sim"):
        return None  # Sim accounts need no live gate; enable_strategy rules apply
    if status != "live-small":
        return "only 'live-small' strategies may be auto-enabled on a real account; 'live' needs a human"
    if os.environ.get("NT_OPERATOR_ALLOW_LIVE_SMALL") != "1":
        return "NT_OPERATOR_ALLOW_LIVE_SMALL is not 1 in the launcher's environment"
    allowed = [a.strip() for a in os.environ.get("NT_OPERATOR_LIVE_SMALL_ACCOUNTS", "").split(",") if a.strip()]
    if account not in allowed:
        return f"account '{account}' is not in NT_OPERATOR_LIVE_SMALL_ACCOUNTS"
    if os.environ.get("NT_OPERATOR_ALLOW_STRATEGY_START") != "1":
        return "NT_OPERATOR_ALLOW_STRATEGY_START is not 1 in the launcher's environment"
    if not os.environ.get("NT_OPERATOR_TOKEN"):
        return "NT_OPERATOR_TOKEN is not set"
    return None


def target_gate(account: str, status: str):
    """Gate for one launcher target: 'sim' strategies only on a Sim* account, live stages through live_gate."""
    if status == SIM_STAGE:
        if not account.lower().startswith("sim"):
            return f"'sim' strategies are only auto-enabled on a Sim* account, not '{account}'"
        return None  # the add-on still requires NT_OPERATOR_ALLOW_STRATEGY_START=1 in NinjaTrader's environment
    return live_gate(account, status)


def auto_stages(cfg: dict) -> tuple:
    return LIVE_STAGES + ((SIM_STAGE,) if cfg.get("enable_sim", True) else ())


def in_window(now: datetime, cfg: dict, start_key: str, end_key: str = "") -> bool:
    """Is local time (cfg timezone) inside [start, end) on a configured day? No end = until midnight."""
    from zoneinfo import ZoneInfo
    local = now.astimezone(ZoneInfo(cfg["timezone"]))
    if local.weekday() not in cfg["days"]:
        return False
    t = local.time()
    return _hm(cfg[start_key]) <= t < (_hm(cfg[end_key]) if end_key else time(23, 59, 59))


def weekend_closed(now: datetime, cfg: dict) -> bool:
    """True from Friday `weekend_close` until Sunday `weekend_reopen` (local time). The launcher does nothing then:
    it neither starts NinjaTrader nor enables anything. Holidays are not modelled."""
    return sessions.weekend_closed(now, cfg["timezone"], _hm(cfg["weekend_close"]), _hm(cfg["weekend_reopen"]))


def live_targets(rcon, profiles: list, stages: tuple = LIVE_STAGES) -> tuple:
    """([{id, version, account, spec}], [problem strings]) for every spec in one of `stages`."""
    targets, problems = [], []
    for st in stages:
        for s in registry.list_specs(rcon, status=st):
            mine = [p for p in profiles if p["strategy_id"] == s["id"] and p["version"] == s["version"]]
            accounts = sorted({p["account"] for p in mine})
            if len(accounts) != 1:
                problems.append(f"{s['id']} {s['version']} ({st}): needs exactly one account in the supervisor profiles, "
                                f"found {accounts or 'none'}; not started")
                continue
            row = registry.get_spec(rcon, s["id"], s["version"])
            targets.append({"id": s["id"], "version": s["version"], "account": accounts[0], "status": st,
                            "spec": row["spec"]})
    return targets, problems


def account_ready(health: dict, account: str):
    """None if the account's connection and data feed are Connected, else why not."""
    if "error" in health:
        return f"add-on unreachable ({health['error']})"
    for c in health.get("connections", []):
        if c.get("account") == account:
            if c.get("status") != "Connected":
                return f"{account} connection is '{c.get('status')}'"
            if c.get("price_status") != "Connected":
                return f"data feed is '{c.get('price_status')}'"
            return None
    return f"no connection found for {account}"


def _positions(status: dict, account: str) -> list:
    for a in status.get("accounts", []):
        if a.get("name") == account:
            return [p for p in a.get("positions", []) if p.get("quantity", p.get("qty", 0))]
    return []


def clear_stale_rows(account: str, names: list, _post) -> str:
    """Remove the stopped rows a leg left behind in the Strategies tab (NinjaTrader keeps a stopped strategy as a
    Configure row, and a restart restores it), so enabling again does not add a second copy. Only exact leg names, only
    flat, never a running one (the add-on checks). Best effort: a failure is reported and the enable goes ahead."""
    removed, problems = 0, []
    for name in names:
        res = _post("/strategy/purge", {"account": account, "name": name, "confirm": "1", "stale": "1"}, "")
        if res.get("ok"):
            removed += int(res.get("removed", 0))
            if res.get("errors"):
                problems.append(f"{name}: {res['errors']} row(s) could not be removed")
        else:
            problems.append(f"{name}: {res.get('error')}")
    if removed or problems:
        control.audit("launcher_clear_stale_rows", {"account": account, "names": names},
                      {"removed": removed, "problems": problems}, "clear old rows before enabling")
    parts = [f"removed {removed} old stopped row(s)"] if removed else []
    return "; ".join(parts + [f"could not clear old rows ({'; '.join(problems)})"] if problems else parts)


def tick(cfg: dict, state: dict, rcon, profiles: list, now: datetime, apply: bool, io=None) -> list:
    """One pass. Returns human-readable lines describing what it did (or would do). Mutates `state`."""
    io = io or {}
    running = io.get("nt_running", nt_running)
    start = io.get("start_nt", start_nt)
    at_login = io.get("login_window_open", login_window_open)
    _fetch, _post, _alert = io.get("fetch", fetch), io.get("post", post), io.get("alert", alert)
    sup_up = io.get("supervisor_alive", supervisor_alive)
    out = []

    def say(msg):
        out.append(msg)

    day = now.astimezone(__import__("zoneinfo").ZoneInfo(cfg["timezone"])).date().isoformat()
    if state.get("day") != day:
        # New day: forget enables and attempts, but keep what describes the running NinjaTrader. Dropping
        # launched_at at midnight meant a late-evening relaunch stuck at login was never alerted.
        keep = {k: state[k] for k in ("launched_at", "stuck_alerted") if k in state}
        state.clear()
        state.update(keep)
        state["day"] = day
    if not cfg["enabled"]:
        return ["launcher is disabled in config (\"enabled\": false); doing nothing"]
    if os.path.exists(pause_path()):
        return [f"paused ({pause_path()} exists); doing nothing"]
    if weekend_closed(now, cfg):
        return ["exchange closed for the weekend (Friday close to Sunday reopen); doing nothing"]

    # 2. NinjaTrader process
    if not running():
        if not cfg["launch_nt"]:
            return ["NinjaTrader is not running and launch_nt is false"]
        last = state.get("launched_at")
        if last and now - datetime.fromisoformat(last) < RELAUNCH_MIN:
            return ["NinjaTrader was started recently and is not running; waiting before retrying"]
        say(f"starting NinjaTrader: {cfg['nt_exe']}")
        if apply:
            if not os.path.exists(cfg["nt_exe"]):
                _alert(f"NinjaTrader not found at {cfg['nt_exe']}")
                return out + ["nt_exe does not exist"]
            start(cfg["nt_exe"])
            state["launched_at"] = now.isoformat()
            control.audit("launcher_start_nt", {"exe": cfg["nt_exe"]}, {"ok": True}, "scheduled launch")
        return out + ["waiting for the add-on to come up"]

    # NinjaTrader is up. If the add-on does not answer, it is most likely sitting at its login window (the launcher
    # cannot log in for you). Alert at once if that window is seen, whoever started NinjaTrader (a reboot, a manual
    # start), or after CONNECT_TIMEOUT if the launcher started it. Once per stuck episode, whatever the time.
    stuck = _fetch("/health")
    launched = state.get("launched_at")
    login = "error" in stuck and at_login()
    if login:
        say("NinjaTrader is at its login window; waiting for someone to log in")
    if "error" not in stuck:
        state.pop("stuck_alerted", None)
    elif apply and not state.get("stuck_alerted"):
        msg = ""
        if login:
            msg = ("NinjaTrader is waiting at its login window, so nothing can trade. Log in on the PC "
                   "(NinjaTrader does not remember the password across restarts).")
        elif launched and now - datetime.fromisoformat(launched) > CONNECT_TIMEOUT:
            msg = ("NinjaTrader is running but its add-on is not answering after 10 min. It is probably waiting at "
                   "the NinjaTrader login window: log in on the PC.")
        if msg:
            state["stuck_alerted"] = True
            if sup_up():
                say("add-on down; the supervisor is running and reports it, so no launcher alert")
            else:
                _alert(msg)
    if not cfg["auto_enable"]:
        return out + ["NinjaTrader running; auto_enable is false"]
    if not in_window(now, cfg, "enable_from", "enable_until"):
        return out + ["NinjaTrader running; outside the enable window"]

    # 3. add-on and connection
    health = _fetch("/health")
    targets, problems = live_targets(rcon, profiles, auto_stages(cfg))
    for p in problems:
        say(f"PROBLEM: {p}")
    if not targets:
        return out + ["no live strategy to start"]
    launched = state.get("launched_at")
    status = None
    for t in targets:
        label = f"{t['id']} {t['version']} on {t['account']}"
        why = account_ready(health, t["account"])
        if why:
            say(f"{label}: waiting, {why}")
            started = datetime.fromisoformat(launched) if launched else None
            if started and now - started > CONNECT_TIMEOUT and not state.get("connect_alerted") and apply:
                state["connect_alerted"] = True
                _alert(f"NinjaTrader has been up {int((now - started).total_seconds() // 60)} min but {why}. "
                       "Check the connection / login.")
            continue
        gate = target_gate(t["account"], t["status"])
        if gate:
            say(f"{label}: refused, {gate}")
            continue
        listing = _fetch("/strategies")
        if "error" in listing:
            say(f"{label}: cannot read /strategies ({listing['error']})")
            continue
        names = {r["name"] for r in handoff.enable_requests(t["spec"], now.date())}
        present = sorted(s["name"] for s in listing.get("strategies", [])
                         if s.get("account") == t["account"] and s.get("name") in names)
        if present:
            say(f"{label}: already present ({len(present)}/{len(names)} legs); leaving it alone")
            continue
        key = f"{t['id']}|{t['version']}"
        done = state.setdefault("enabled", {})
        tries = state.setdefault("attempts", {})
        if key in done:
            say(f"{label}: already enabled today at {done[key]} and not present now; not restarting "
                "(stopped by hand or by the supervisor?). Use --resume-today to allow another start.")
            continue
        if tries.get(key, 0) >= MAX_ATTEMPTS:
            say(f"{label}: {MAX_ATTEMPTS} failed attempts today; giving up until tomorrow")
            continue
        if status is None:
            status = _fetch("/status")
        if "error" in status:
            say(f"{label}: cannot read /status ({status['error']})")
            continue
        if _positions(status, t["account"]):
            say(f"{label}: refused, account has an open position; flatten or enable by hand")
            continue
        others = [s.get("name") for s in listing.get("strategies", []) if s.get("account") == t["account"]]
        if others:
            say(f"{label}: refused, other strategies are on the account: {others}")
            continue
        say(f"{label}: enabling {sorted(names)}")
        if not apply:
            continue
        tries[key] = tries.get(key, 0) + 1
        cleared = clear_stale_rows(t["account"], sorted(names), _post)
        if cleared:
            say(f"{label}: {cleared}")
        res = handoff.enable_spec(t["spec"], t["account"], lambda p, q, b: _post(p, q, b), _fetch, now.date(),
                                  skip_defaults=True)
        control.audit("launcher_enable_strategy", {"id": t["id"], "version": t["version"], "account": t["account"]},
                      res, f"scheduled auto-enable ({t['status']})")
        if res.get("ok"):
            done[key] = now.isoformat()
            say(f"{label}: enabled, states {res.get('verified_states')}")
            _alert(f"Enabled {label}: {res.get('verified_states')}")
        else:
            say(f"{label}: FAILED, {res.get('error')} (attempt {tries[key]}/{MAX_ATTEMPTS})")
            if tries[key] >= MAX_ATTEMPTS:
                _alert(f"Could not enable {label}: {res.get('error')}. Giving up for today.")
    return out


def main(argv) -> int:
    path_state = state_path()
    if "--pause" in argv or "--resume" in argv:
        os.makedirs(os.path.dirname(pause_path()), exist_ok=True)
        if "--pause" in argv:
            open(pause_path(), "w").close()
            print("paused")
        elif os.path.exists(pause_path()):
            os.remove(pause_path())
            print("resumed")
        return 0
    if "--resume-today" in argv:
        st = load_state()
        st.pop("enabled", None)
        st.pop("attempts", None)
        save_state(st)
        print("cleared today's enabled/attempt records")
        return 0
    if "--status" in argv:
        print(json.dumps({"config": load_config(), "state": load_state(), "paused": os.path.exists(pause_path())},
                         indent=2))
        return 0
    apply = "--apply" in argv
    now = datetime.now(timezone.utc)
    try:
        cfg = load_config()
        profiles = sup_profile.load()
    except ValueError as e:
        print(f"refusing to run: {e}", file=sys.stderr)
        if apply:
            nt_local.write_launcher_heartbeat(now, False, f"refusing to run: {e}")
        return 2
    state = load_state()
    try:
        rcon = registry.connect()
        try:
            lines = tick(cfg, state, rcon, profiles, now, apply)
        finally:
            rcon.close()
    except Exception as e:
        if apply:
            nt_local.write_launcher_heartbeat(now, False, f"tick failed: {type(e).__name__}: {e}")
        raise
    if apply:
        save_state(state)
        nt_local.write_launcher_heartbeat(now, True, lines[-1] if lines else "")
    for line in lines:
        print(("" if apply else "[plan] ") + line)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
