"""Strategy Supervisor: health profiles (Phase 1, operational fields only).

A profile says which strategy instances should exist, on which account, and in what state. Profiles are loaded from
a human-edited JSON file (NT_SUPERVISOR_PROFILES, default ~/.ninjatrader-operator/supervisor_profiles.json). The
daemon never rewrites it. Hard limits beyond account authorization are not enforced in Phase 1.

File shape: {"profiles": [ {profile}, ... ]}
"""
import json
import os

DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".ninjatrader-operator", "supervisor_profiles.json")
DEFAULTS = {"expected_state": "Realtime", "grace_seconds": 60, "monitored": True}


def profiles_path() -> str:
    return os.environ.get("NT_SUPERVISOR_PROFILES") or DEFAULT_PATH


def validate(p: dict) -> list:
    """Return a list of problems (empty if the profile is usable)."""
    errs = []
    if not isinstance(p, dict):
        return ["profile must be an object"]
    for k in ("strategy_id", "version", "account"):
        if not isinstance(p.get(k), str) or not p[k].strip():
            errs.append(f"{k} is required")
    names = p.get("instance_names")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n.strip() for n in names):
        errs.append("instance_names must be a non-empty list of strings")
    auth = p.get("authorized_accounts")
    if not isinstance(auth, list) or not auth or not all(isinstance(a, str) and a.strip() for a in auth):
        errs.append("authorized_accounts must be a non-empty list of strings")
    elif isinstance(p.get("account"), str) and p["account"] not in auth:
        errs.append("account must be listed in authorized_accounts")
    g = p.get("grace_seconds", DEFAULTS["grace_seconds"])
    if isinstance(g, bool) or not isinstance(g, (int, float)) or g < 0:
        errs.append("grace_seconds must be a number >= 0")
    return errs


def normalize(p: dict) -> dict:
    return {**DEFAULTS, **p}


def load(path: str = "") -> list:
    """Load and validate profiles. Raises ValueError listing every problem; never returns a partial set."""
    path = path or profiles_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"profiles file not found: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"profiles file is not valid JSON: {e}")
    items = raw.get("profiles") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise ValueError('profiles file must be {"profiles": [...]}')
    problems, out, seen = [], [], set()
    for i, p in enumerate(items):
        errs = validate(p)
        problems += [f"profile {i}: {e}" for e in errs]
        if not errs:
            for n in p["instance_names"]:
                if n in seen:
                    problems.append(f"profile {i}: instance name '{n}' is used by more than one profile")
                seen.add(n)
            out.append(normalize(p))
    if problems:
        raise ValueError("; ".join(problems))
    return out
