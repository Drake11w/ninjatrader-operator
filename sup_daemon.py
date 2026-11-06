"""Strategy Supervisor daemon (Phase 1: observe, record, notify). Read-only toward NinjaTrader.

    python sup_daemon.py             run the loop (Ctrl+C to stop)
    python sup_daemon.py --once      one cycle, then exit
    python sup_daemon.py --status    print heartbeat age and active incidents, exit 1 if the heartbeat is stale

Works without any AI session. Needs profiles (see sup_profile.py). All state is in supervisor.sqlite, so a restart
resumes; on start it records a `supervisor_restarted` event and treats earlier observations as stale.
"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone

import control
import sup_alerts
import sup_checks
import sup_fetch
import sup_incidents
import sup_profile

INTERVAL = 10
HEARTBEAT_STALE = 60
DISABLE_WINDOW = timedelta(minutes=10)


def recent_disables(now, audit=None) -> set:
    """Instance names the Operator itself disabled recently (audit log), so a deliberate stop is not an incident."""
    rows = control.read_audit(500) if audit is None else audit
    out = set()
    for r in rows:
        if r.get("action") != "disable_strategy" or not r.get("ok"):
            continue
        try:
            t = datetime.fromisoformat(r["time"])
        except (KeyError, ValueError):
            continue
        if now - t <= DISABLE_WINDOW:
            out.add((r.get("params") or {}).get("name", ""))
    return out


def run_once(con, profiles, obs=None, now=None, audit=None) -> list:
    """One cycle. Returns the incident events it produced."""
    now = now or datetime.now(timezone.utc)
    obs = obs or sup_fetch.observe()
    state = sup_incidents.get_state(con, "check_state", {})
    findings, state = sup_checks.evaluate(obs, profiles, state, now, recent_disables(now, audit))
    events = sup_incidents.apply_findings(con, findings, now, allow_resolve=sup_checks.data_current(obs, now))
    sup_incidents.set_state(con, "check_state", state)
    for iid in sup_alerts.notify(events):
        sup_incidents.mark_notified(con, iid, now)
    sup_incidents.set_state(con, "heartbeat", now.isoformat())
    sup_alerts.write_attention(sup_incidents.list_incidents(con, "active"), now)
    return events


def start(con, now=None) -> None:
    """Restart handling: observations from before the restart are not trusted, then log the restart."""
    now = now or datetime.now(timezone.utc)
    sup_incidents.set_state(con, "check_state", {})
    sup_alerts.notify([{"kind": "supervisor_restarted", "id": "-", "key": "supervisor_restarted", "severity": 0,
                        "trigger": "daemon started; previous observations discarded"}])


def status(con, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    hb = sup_incidents.get_state(con, "heartbeat")
    age = None if hb is None else (now - datetime.fromisoformat(hb)).total_seconds()
    return {"heartbeat_age_seconds": age, "heartbeat_stale": age is None or age > HEARTBEAT_STALE,
            "active_incidents": [{k: i[k] for k in ("id", "key", "severity", "status", "account", "strategy")}
                                 for i in sup_incidents.list_incidents(con, "active")]}


def main(argv) -> int:
    con = sup_incidents.connect()
    if "--status" in argv:
        s = status(con)
        print(json.dumps(s, indent=2))
        return 1 if s["heartbeat_stale"] else 0
    try:
        profiles = sup_profile.load()
    except ValueError as e:
        print(f"refusing to start: {e}", file=sys.stderr)
        return 2
    start(con)
    while True:
        t0 = time.monotonic()
        try:
            run_once(con, profiles)
        except Exception as e:  # keep running; the heartbeat going stale is how a stuck daemon is noticed
            print(f"cycle error: {e}", file=sys.stderr)
        if "--once" in argv:
            return 0
        time.sleep(max(0.0, INTERVAL - (time.monotonic() - t0)))


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(0)
