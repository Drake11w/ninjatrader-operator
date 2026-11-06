"""Strategy Supervisor: read-only add-on client. GET only; there is deliberately no POST path in this module."""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("NT_ADDON_URL", "http://127.0.0.1:8765")
TIMEOUT = 5


def fetch(path: str, base: str = "", timeout: float = TIMEOUT) -> dict:
    """Return an Observation. Failure is data: {"ok": False, "error": ...}, never an exception."""
    url = (base or BASE) + path
    at = datetime.now(timezone.utc)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        if not isinstance(data, dict):
            return {"ok": False, "fetched_at": at, "data": None, "error": "reply is not a JSON object"}
        return {"ok": True, "fetched_at": at, "data": data, "error": None}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        return {"ok": False, "fetched_at": at, "data": None, "error": f"{type(e).__name__}: {e}"}


def observe(base: str = "") -> dict:
    return {"health": fetch("/health", base), "strategies": fetch("/strategies", base)}
