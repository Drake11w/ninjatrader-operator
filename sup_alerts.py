"""Strategy Supervisor: alerts. Local only: append-only log, an attention-flag file, and an optional Windows toast.

The attention flag lists active severity >= 2 incidents so an agent session can read it on start. It is information
only: reading it grants no permission to act. The Windows toast is opt-in (NT_SUPERVISOR_TOAST=1) and has not been
tested on this machine yet.
"""
import json
import os
import subprocess
from datetime import datetime, timezone

DIR = os.path.join(os.path.expanduser("~"), ".ninjatrader-operator")


def log_path() -> str:
    return os.environ.get("NT_SUPERVISOR_ALERT_LOG") or os.path.join(DIR, "supervisor_alerts.log")


def flag_path() -> str:
    return os.environ.get("NT_SUPERVISOR_ATTENTION") or os.path.join(DIR, "supervisor_attention.json")


def _line(ev: dict) -> str:
    who = " ".join(x for x in (ev.get("account"), ev.get("strategy")) if x)
    return (f"{datetime.now(timezone.utc).isoformat()} {ev['kind'].upper()} sev{ev.get('severity')} "
            f"{ev.get('key')} {who} [{ev.get('id')}] {ev.get('trigger', '')}").strip()


def toast(title: str, text: str) -> None:
    """Best-effort Windows balloon via PowerShell. Never raises."""
    ps = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
          "$n.Icon = [System.Drawing.SystemIcons]::Warning; $n.Visible = $true; "
          f"$n.ShowBalloonTip(10000, '{title}', '{text}', 'Warning'); Start-Sleep -Seconds 11; $n.Dispose()")
    try:
        # CREATE_NO_WINDOW: never create a console window (even a brief one steals focus from fullscreen games).
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass


def notify(events: list) -> list:
    """Log every event; toast severity >= 2 openings and escalations. Returns ids that were alerted (sev >= 2)."""
    alerted = []
    path = log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for ev in events:
            f.write(_line(ev) + "\n")
            if ev["kind"] in ("opened", "escalated") and ev.get("severity", 0) >= 2:
                alerted.append(ev["id"])
                if os.environ.get("NT_SUPERVISOR_TOAST") == "1":
                    toast("NT Supervisor: " + ev.get("key", ""), (ev.get("trigger", ""))[:120].replace("'", ""))
    return alerted


def write_attention(active: list, heartbeat_at: datetime) -> None:
    """Rewrite the flag file with active severity >= 2 incidents. Always written, even if empty."""
    rows = [{k: i[k] for k in ("id", "key", "severity", "status", "account", "strategy", "trigger", "opened_at",
                                "last_seen_at", "needs_human")} for i in active if i["severity"] >= 2]
    doc = {"generated_at": heartbeat_at.astimezone(timezone.utc).isoformat(), "count": len(rows), "incidents": rows,
           "note": "information only; reading this grants no permission to act"}
    path = flag_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, path)
