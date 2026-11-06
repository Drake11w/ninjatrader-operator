"""Strategy Supervisor: outside watcher. Run on a schedule, independent of the daemon.

    python sup_watch.py            prints one line per NEW alert, or nothing if there is nothing new (exit 0)
    python sup_watch.py --notify   also shows a Windows toast, and POSTs to NT_WATCH_PUSH_URL if set (opt-in)

It reports (1) a stale or missing daemon heartbeat, once until the daemon recovers, and (2) each active severity >= 2
incident once. What it already reported is remembered in supervisor.sqlite, so repeated runs do not repeat alerts.
Read-only toward NinjaTrader; it does not touch it at all. Whatever consumes the output (a phone push) gets
information only.
"""
import os
import sys
import urllib.request
from datetime import datetime, timezone

import sup_alerts
import sup_daemon
import sup_incidents


def check(con, now=None) -> list:
    now = now or datetime.now(timezone.utc)
    out = []
    reported = set(sup_incidents.get_state(con, "watch_reported", []))
    st = sup_daemon.status(con, now)

    dead_reported = sup_incidents.get_state(con, "watch_dead_reported", False)
    if st["heartbeat_stale"]:
        if not dead_reported:
            age = st["heartbeat_age_seconds"]
            out.append("SUPERVISOR NOT RUNNING: " + ("never started" if age is None else f"no heartbeat for {age:.0f}s")
                       + ". Strategies are unmonitored.")
            sup_incidents.set_state(con, "watch_dead_reported", True)
    elif dead_reported:
        out.append("Supervisor is running again.")
        sup_incidents.set_state(con, "watch_dead_reported", False)

    active = [i for i in sup_incidents.list_incidents(con, "active") if i["severity"] >= 2]
    for i in active:
        tag = f"{i['id']}:{i['severity']}"  # re-alert if the severity escalates
        if tag in reported:
            continue
        who = " ".join(x for x in (i["account"], i["strategy"]) if x)
        out.append(f"SEV{i['severity']} {i['key']} {who}: {i['trigger']}".replace("  ", " "))
        reported.add(tag)
    live = {f"{i['id']}:{i['severity']}" for i in active}
    sup_incidents.set_state(con, "watch_reported", sorted(reported & live))
    return out


def push(line: str) -> None:
    """Best-effort delivery of one alert line. Always a local toast; also an HTTP POST to NT_WATCH_PUSH_URL when set
    (for example an ntfy.sh topic URL). The push is opt-in because it sends the alert text to that service. Never raises."""
    try:
        sup_alerts.toast("NT Supervisor", line[:200].replace("'", ""))
    except Exception:
        pass
    url = os.environ.get("NT_WATCH_PUSH_URL", "")
    if url.startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(url, data=line.encode("utf-8"), method="POST",
                                         headers={"Title": "NT Supervisor", "Priority": "high"})
            urllib.request.urlopen(req, timeout=10).close()
        except Exception:
            pass


def main(argv=()) -> int:
    con = sup_incidents.connect()
    for line in check(con):
        print(line)
        if "--notify" in argv:
            push(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
