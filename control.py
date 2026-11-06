"""Guards and audit log for the risk-reducing control tools (flatten, cancel, kill switch).

Controls never enter or modify orders. They are limited to accounts named Sim*, accounts listed in NT_OPERATOR_ALLOWED_ACCOUNTS (comma-separated, exact names), or everything if NT_OPERATOR_ALLOW_LIVE=1 is set (the add-on
enforces the same rule, so both sides must be unlocked). Every attempt, allowed or refused, is appended to a
JSON-lines audit log (NT_AUDIT_FILE, default ~/.ninjatrader-operator/audit.jsonl).
"""
import json
import os
from datetime import datetime, timezone

MAX_KILL_LOSS = 10000.0


def audit_path() -> str:
    return os.environ.get("NT_AUDIT_FILE") or os.path.join(os.path.expanduser("~"), ".ninjatrader-operator", "audit.jsonl")


def allow_live() -> bool:
    return os.environ.get("NT_OPERATOR_ALLOW_LIVE") == "1"


def allowed_accounts() -> list:
    return [a.strip() for a in os.environ.get("NT_OPERATOR_ALLOWED_ACCOUNTS", "").split(",") if a.strip()]


def check_account(account: str):
    """Return an error string if control of this account is not allowed, else None."""
    if not account or not account.strip():
        return "account is required (no default: name the account explicitly)"
    if not (account.lower().startswith("sim") or account in allowed_accounts() or allow_live()):
        return (f"refused: '{account}' is not a Sim* account or in NT_OPERATOR_ALLOWED_ACCOUNTS, "
                "and live control is locked (NT_OPERATOR_ALLOW_LIVE)")
    return None


def check_max_loss(max_loss: float):
    if not (0 < max_loss <= MAX_KILL_LOSS):
        return f"max_loss must be between 0 and {MAX_KILL_LOSS:g} dollars"
    return None


def audit(action: str, params: dict, result: dict, reason: str = "") -> None:
    row = {"time": datetime.now(timezone.utc).isoformat(), "action": action, "params": params,
           "reason": reason, "ok": bool(result.get("ok")), "result": result}
    path = audit_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def read_audit(limit: int = 50) -> list:
    path = audit_path()
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    return [json.loads(x) for x in lines[-limit:]]
