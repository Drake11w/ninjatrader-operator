"""Strategy Supervisor: pure operational-health checks (Phase 1). No I/O, no NinjaTrader calls.

evaluate(obs, profiles, state, now, recent_disables) -> (findings, new_state)

obs is {"health": Observation, "strategies": Observation} where an Observation is
{"ok": bool, "fetched_at": datetime, "data": dict | None, "error": str | None}.
A failed fetch is an observation, never an assumption of health or failure. `state` is a plain JSON-able dict kept
between cycles by the daemon. Stale data is labelled and suppresses every check that would depend on it.

Finding: {"key", "severity" (1-3), "account", "strategy", "trigger", "evidence", "caused_by"}.
`caused_by` is the key of an infrastructure finding that explains this one.
"""
from datetime import datetime, timezone

STALE_SECONDS = 30
UNREACHABLE_AFTER = 3
CONNECTED = "connected"


def _finding(key, severity, account, strategy, trigger, evidence=None, caused_by=""):
    return {"key": key, "severity": severity, "account": account, "strategy": strategy,
            "trigger": trigger, "evidence": evidence or {}, "caused_by": caused_by}


def _parse(ts):
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _age(o, now):
    """Age in seconds of the add-on's own timestamp; None if missing or unparseable."""
    t = _parse((o.get("data") or {}).get("timestamp"))
    return None if t is None else (now - t).total_seconds()


def _iso(t):
    return t.astimezone(timezone.utc).isoformat()


def _pkey(p, name):
    return p["account"] + "|" + name


def evaluate(obs, profiles, state, now, recent_disables=()):
    state = {"fail_counts": dict(state.get("fail_counts", {})), "running": dict(state.get("running", {})),
             "not_running": dict(state.get("not_running", {})), "missing": dict(state.get("missing", {}))}
    findings = []
    stale_sources = []

    # --- reachability and staleness, per source ---------------------------------------------------------------
    usable = {}
    for src in ("health", "strategies"):
        o = obs[src]
        if not o["ok"]:
            state["fail_counts"][src] = state["fail_counts"].get(src, 0) + 1
            if state["fail_counts"][src] >= UNREACHABLE_AFTER:
                findings.append(_finding("addon_unreachable", 2, "", "",
                                         f"/{src} failed {state['fail_counts'][src]} cycles in a row",
                                         {"source": src, "error": o.get("error")}))
            stale_sources.append(src)
            continue
        state["fail_counts"][src] = 0
        age = _age(o, now)
        if age is None or age > STALE_SECONDS:
            findings.append(_finding("data_stale", 2, "", "",
                                     f"/{src} data is {'of unknown age' if age is None else f'{age:.0f}s old'}",
                                     {"source": src, "age_seconds": age}))
            stale_sources.append(src)
            continue
        usable[src] = o["data"]

    infra = next((f["key"] for f in findings if f["key"] in ("addon_unreachable", "data_stale")), "")
    monitored = sorted({p["account"] for p in profiles if p.get("monitored", True)})

    # --- connections ----------------------------------------------------------------------------------------
    broken_accounts = {}
    if "health" in usable:
        conns = {}
        for c in usable["health"].get("connections", []):
            conns.setdefault(c.get("account"), []).append(c)
        for acct in monitored:
            entries = conns.get(acct)
            if not entries:
                findings.append(_finding("account_disconnected", 2, acct, "", "no connection entry for account",
                                         {"account": acct}))
                broken_accounts[acct] = "account_disconnected"
                continue
            if any(str(c.get("status", "")).lower() != CONNECTED for c in entries):
                findings.append(_finding("account_disconnected", 2, acct, "", "account connection is not Connected",
                                         {"connections": entries}))
                broken_accounts[acct] = "account_disconnected"
            elif any(str(c.get("price_status", "")).lower() != CONNECTED for c in entries):
                findings.append(_finding("feed_disconnected", 2, acct, "", "price feed is not Connected",
                                         {"connections": entries}))
                broken_accounts[acct] = "feed_disconnected"

    # --- strategies -----------------------------------------------------------------------------------------
    if "strategies" in usable and not (infra and "strategies" in stale_sources):
        rows = usable["strategies"].get("strategies", [])
        known = {n: p for p in profiles for n in p["instance_names"]}
        for r in rows:
            p = known.get(r.get("name"))
            if p is None:
                if r.get("account") in monitored:
                    findings.append(_finding("unknown_strategy", 1, r.get("account", ""), r.get("name", ""),
                                             "strategy instance matches no profile", {"instance": r}))
            elif r.get("account") not in p["authorized_accounts"]:
                findings.append(_finding("wrong_account", 3, r.get("account", ""), r.get("name", ""),
                                         "strategy instance is on an account its profile does not authorize",
                                         {"instance": r, "authorized_accounts": p["authorized_accounts"]}))
        for p in profiles:
            if not p.get("monitored", True):
                continue
            cause = broken_accounts.get(p["account"], "")
            for name in p["instance_names"]:
                k = _pkey(p, name)
                row = next((r for r in rows if r.get("name") == name and r.get("account") == p["account"]), None)
                if cause:  # infrastructure explains it; do not blame the strategy, and keep timers from running on
                    state["not_running"].pop(k, None)
                    state["missing"].pop(k, None)
                    continue
                if row is None:
                    since = _parse(state["missing"].setdefault(k, _iso(now)))
                    if (now - since).total_seconds() >= p["grace_seconds"]:
                        findings.append(_finding("strategy_missing", 2, p["account"], name,
                                                 "profile expects this instance but the add-on lists none",
                                                 {"missing_since": _iso(since), "was_running": k in state["running"]}))
                    continue
                state["missing"].pop(k, None)
                if row.get("state") == p["expected_state"]:
                    state["running"][k] = _iso(now)
                    state["not_running"].pop(k, None)
                    continue
                if k in state["running"]:
                    if name in recent_disables:
                        state["running"].pop(k, None)  # the operator stopped it on purpose (audit log)
                    else:
                        findings.append(_finding("strategy_unexpected_stop", 2, p["account"], name,
                                                 f"was {p['expected_state']}, now {row.get('state')}, "
                                                 "no operator disable recorded",
                                                 {"instance": row, "last_running": state["running"][k]}))
                    continue
                since = _parse(state["not_running"].setdefault(k, _iso(now)))
                if (now - since).total_seconds() >= p["grace_seconds"]:
                    findings.append(_finding("strategy_not_running", 2, p["account"], name,
                                             f"expected {p['expected_state']} but state is {row.get('state')}",
                                             {"instance": row, "since": _iso(since)}))
    else:
        # strategy data is unusable this cycle: keep timers and last-running marks untouched, report nothing new
        pass

    for f in findings:
        if f["key"] in ("addon_unreachable", "data_stale"):
            continue
        if not f["caused_by"] and infra and f["severity"] < 3:
            f["caused_by"] = infra
    return findings, state


def data_current(obs, now) -> bool:
    """True only if every source was fetched OK and is fresh; incidents may be resolved only then."""
    for src in ("health", "strategies"):
        o = obs[src]
        if not o["ok"]:
            return False
        a = _age(o, now)
        if a is None or a > STALE_SECONDS:
            return False
    return True
