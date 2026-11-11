"""Owner Approval of live-small, scheduled approvals, and the launcher-tick executor (S3b).

The only code that moves a strategy to 'live-small'. It touches the registry and audit.jsonl only: no NinjaTrader
call, no POST path. Starting the strategy stays with the launcher / deploy_strategy_live_small behind the unchanged
NT_OPERATOR_* gates; an Approval never satisfies or bypasses one.

Owner-only: the caller passes the approval secret per call. The server environment holds only its SHA-256
(NT_OPERATOR_APPROVAL_SECRET_SHA256), so a Claude session that can read its own environment still cannot approve.
NT_OPERATOR_TOKEN must also be set. The secret is compared by hash, never logged, never stored.

Tables (registry.py SCHEMA) are the source of truth; audit.jsonl gets a copy written after the database commit.
Pure-ish: `now` and the readiness function are injected, so fixtures test it without a clock or NinjaTrader.
"""
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone

import control
import feedback
import registry

SECRET_HASH_ENV = "NT_OPERATOR_APPROVAL_SECRET_SHA256"
DEFAULT_EXPIRY = timedelta(hours=24)   # the launcher enables 07:30-15:00 CT only: a day always spans one window
MAX_EXPIRY = timedelta(days=7)
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")


def secret_hash(secret: str) -> str:
    """What to put in NT_OPERATOR_APPROVAL_SECRET_SHA256 (run once by the owner; the secret itself stays with them)."""
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def check_owner(secret: str, env=None):
    """Error string if the caller is not the owner, else None."""
    env = os.environ if env is None else env
    if not env.get("NT_OPERATOR_TOKEN"):
        return "owner actions need NT_OPERATOR_TOKEN set in the server environment"
    want = (env.get(SECRET_HASH_ENV) or "").strip().lower()
    if not want:
        return f"approval is not configured: set {SECRET_HASH_ENV} (sha256 of the owner's secret) in the server environment"
    if not secret or not hmac.compare_digest(secret_hash(secret), want):
        return "approval secret missing or wrong"
    return None


def _utc(dt: datetime) -> datetime:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)


def _stamp(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return _utc(datetime.fromisoformat(ts.replace("Z", "+00:00")))


def _snapshot(r: dict, now: datetime) -> dict:
    verdict = next((c["actual"] for c in r.get("criteria", []) if c["key"] == "verdict"), None)
    return {"as_of": _stamp(now), "state": r.get("state"), "ready": r.get("ready"), "summary": r.get("summary"),
            "verdict": verdict}


def _audit(action: str, params: dict, result: dict, reason: str) -> None:
    control.audit(action, params, result, reason)


def _validate(rcon, sid, version, alias, reason):
    if not (alias and ALIAS_RE.match(alias)):
        return "account alias is required (a stable local alias such as acct-1, never a raw account name)"
    if not (reason or "").strip():
        return "a non-empty reason is required"
    row = registry.get_spec(rcon, sid, version)
    if "error" in row:
        return row["error"]
    if row["status"] != "sim":
        return f"{sid} {version} is '{row['status']}'; only a 'sim' version can be approved for live-small"
    return None


def _supersede(rcon, sid, version, why, now):
    n = rcon.execute("UPDATE scheduled_approvals SET state='superseded', state_reason=?, updated_at=? "
                     "WHERE id=? AND version=? AND state='pending'", (why, _stamp(now), sid, version)).rowcount
    return n


def _insert_approval(rcon, sid, version, alias, reason, snap, source, schedule_id, now):
    cur = rcon.execute(
        "INSERT INTO live_small_approvals (id, version, account_alias, reason, snapshot_json, source, schedule_id,"
        " approved_at) VALUES (?,?,?,?,?,?,?,?)",
        (sid, version, alias, reason, json.dumps(snap, sort_keys=True), source, schedule_id, _stamp(now)))
    return cur.lastrowid


def approve(rcon, jcon, sid: str, version: str, alias: str, reason: str, secret: str, now: datetime = None,
            acknowledge_not_ready: bool = False, env=None, readiness_fn=None) -> dict:
    """Owner Approval now: sim -> live-small through registry.set_status, committed with the approval row."""
    now = _utc(now or datetime.now(timezone.utc))
    params = {"id": sid, "version": version, "account_alias": alias, "acknowledge_not_ready": acknowledge_not_ready}
    err = check_owner(secret, env) or _validate(rcon, sid, version, alias, reason)
    if err:
        res = {"ok": False, "error": err}
        _audit("approve_live_small", params, res, reason or "")
        return res
    r = (readiness_fn or feedback.readiness_for)(rcon, jcon, sid, version, "", now)
    if "error" in r:
        res = {"ok": False, "error": r["error"]}
        _audit("approve_live_small", params, res, reason)
        return res
    if not r["ready"] and not acknowledge_not_ready:
        res = {"ok": False, "error": f"readiness not met ({r['summary']}); the owner may approve anyway with "
                                     f"acknowledge_not_ready=true, which is recorded",
               "readiness": r["summary"]}
        _audit("approve_live_small", params, res, reason)
        return res
    snap = _snapshot(r, now)
    snap["acknowledged_not_ready"] = bool(acknowledge_not_ready and not r["ready"])
    try:
        superseded = _supersede(rcon, sid, version, "superseded by an immediate approval", now)
        st = registry.set_status(rcon, sid, version, "live-small", f"owner approval ({alias}): {reason}",
                                 commit=False)
        if not st["ok"]:
            rcon.rollback()
            res = {"ok": False, "error": st["error"]}
            _audit("approve_live_small", params, res, reason)
            return res
        aid = _insert_approval(rcon, sid, version, alias, reason, snap, "immediate", None, now)
        rcon.commit()
    except Exception:
        rcon.rollback()
        raise
    res = {"ok": True, "approval_id": aid, "id": sid, "version": version, "status": "live-small",
           "account_alias": alias, "superseded_schedules": superseded, "readiness": snap,
           "note": "Status changed only. Nothing was started: the launcher or deploy_strategy_live_small still "
                   "needs every NT_OPERATOR_* gate."}
    _audit("approve_live_small", params, res, reason)
    return res


def schedule(rcon, jcon, sid: str, version: str, alias: str, reason: str, secret: str, not_before: str,
             expires_at: str = "", now: datetime = None, env=None, readiness_fn=None) -> dict:
    """Store a pending approval for the launcher tick: runs at the first tick at/after not_before while readiness
    still holds and the version is still 'sim'; otherwise it is voided. One pending schedule per version."""
    now = _utc(now or datetime.now(timezone.utc))
    params = {"id": sid, "version": version, "account_alias": alias, "not_before": not_before,
              "expires_at": expires_at}
    err = check_owner(secret, env) or _validate(rcon, sid, version, alias, reason)
    nb = exp = None
    if not err:
        try:
            nb = _parse(not_before)
            exp = _parse(expires_at) if expires_at else nb + DEFAULT_EXPIRY
        except ValueError:
            err = "not_before / expires_at must be ISO UTC times such as 2026-11-12T13:30:00Z"
    if not err and exp <= nb:
        err = "expires_at must be after not_before"
    if not err and exp > nb + MAX_EXPIRY:
        err = f"expires_at may be at most {MAX_EXPIRY.days} days after not_before"
    if not err and exp <= now:
        err = "the schedule would already be expired"
    if err:
        res = {"ok": False, "error": err}
        _audit("schedule_live_small_approval", params, res, reason or "")
        return res
    r = (readiness_fn or feedback.readiness_for)(rcon, jcon, sid, version, "", now)
    snap = _snapshot(r, now) if "error" not in r else {"as_of": _stamp(now), "error": r["error"]}
    try:
        superseded = _supersede(rcon, sid, version, "superseded by a newer schedule", now)
        cur = rcon.execute(
            "INSERT INTO scheduled_approvals (id, version, account_alias, not_before, expires_at, reason,"
            " snapshot_json, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,'pending',?,?)",
            (sid, version, alias, _stamp(nb), _stamp(exp), reason, json.dumps(snap, sort_keys=True),
             _stamp(now), _stamp(now)))
        rcon.commit()
    except Exception:
        rcon.rollback()
        raise
    res = {"ok": True, "schedule_id": cur.lastrowid, "id": sid, "version": version, "account_alias": alias,
           "not_before": _stamp(nb), "expires_at": _stamp(exp), "superseded_schedules": superseded,
           "readiness_now": snap.get("summary"),
           "note": "Runs at the first launcher tick at or after not_before (30-minute granularity, enable window "
                   "only); voided if readiness lapsed, the version left 'sim', or it expired."}
    _audit("schedule_live_small_approval", params, res, reason)
    return res


def cancel(rcon, schedule_id: int, secret: str, reason: str = "", now: datetime = None, env=None) -> dict:
    now = _utc(now or datetime.now(timezone.utc))
    params = {"schedule_id": schedule_id}
    err = check_owner(secret, env)
    row = None
    if not err:
        row = rcon.execute("SELECT * FROM scheduled_approvals WHERE schedule_id=?", (schedule_id,)).fetchone()
        if not row:
            err = f"schedule {schedule_id} not found"
        elif row["state"] != "pending":
            err = f"schedule {schedule_id} is already {row['state']}"
    if err:
        res = {"ok": False, "error": err}
        _audit("cancel_scheduled_approval", params, res, reason)
        return res
    rcon.execute("UPDATE scheduled_approvals SET state='cancelled', state_reason=?, updated_at=? WHERE schedule_id=?",
                 (reason or "cancelled by owner", _stamp(now), schedule_id))
    rcon.commit()
    res = {"ok": True, "schedule_id": schedule_id, "id": row["id"], "version": row["version"], "state": "cancelled"}
    _audit("cancel_scheduled_approval", dict(params, id=row["id"], version=row["version"]), res, reason)
    return res


def list_schedules(rcon, state: str = "") -> list:
    q, args = "SELECT * FROM scheduled_approvals", ()
    if state:
        q, args = q + " WHERE state=?", (state,)
    return [dict(r) for r in rcon.execute(q + " ORDER BY schedule_id DESC", args)]


def has_pending(rcon, now: datetime) -> bool:
    return bool(rcon.execute("SELECT 1 FROM scheduled_approvals WHERE state='pending' AND not_before<=?",
                             (_stamp(_utc(now)),)).fetchone())


def run_due(rcon, jcon, now: datetime, apply: bool = True, readiness_fn=None) -> list:
    """Launcher-tick step. For each pending schedule due now: execute it (set_status sim -> live-small, approval row
    source 'scheduled') if still ready, in 'sim' and unexpired, else void it with the reason. apply=False only
    reports. Needs no secret: authorisation was checked when the schedule was created. Returns event dicts."""
    now = _utc(now)
    rows = rcon.execute("SELECT * FROM scheduled_approvals WHERE state='pending' AND not_before<=? "
                        "ORDER BY schedule_id", (_stamp(now),)).fetchall()
    events = []
    for row in rows:
        sid, ver, n = row["id"], row["version"], row["schedule_id"]
        base = {"schedule_id": n, "id": sid, "version": ver}
        void = None
        spec = registry.get_spec(rcon, sid, ver)
        if _parse(row["expires_at"]) <= now:
            void = f"expired at {row['expires_at']}"
        elif "error" in spec:
            void = spec["error"]
        elif spec["status"] != "sim":
            void = f"version is '{spec['status']}', not 'sim'"
        else:
            r = (readiness_fn or feedback.readiness_for)(rcon, jcon, sid, ver, "", now)
            if "error" in r:
                void = r["error"]
            elif not r["ready"]:
                void = f"readiness lapsed: {r['summary']}"
        if void:
            events.append(dict(base, action="voided" if apply else "would_void", reason=void))
            if apply:
                rcon.execute("UPDATE scheduled_approvals SET state='voided', state_reason=?, updated_at=? "
                             "WHERE schedule_id=?", (void, _stamp(now), n))
                rcon.commit()
                _audit("scheduled_approval_voided", base, {"ok": True, "reason": void}, void)
            continue
        if not apply:
            events.append(dict(base, action="would_execute", reason="ready"))
            continue
        snap = _snapshot(r, now)
        try:
            st = registry.set_status(rcon, sid, ver, "live-small",
                                     f"scheduled owner approval #{n} ({row['account_alias']}): {row['reason']}",
                                     commit=False)
            if not st["ok"]:
                rcon.rollback()
                rcon.execute("UPDATE scheduled_approvals SET state='voided', state_reason=?, updated_at=? "
                             "WHERE schedule_id=?", (st["error"], _stamp(now), n))
                rcon.commit()
                events.append(dict(base, action="voided", reason=st["error"]))
                _audit("scheduled_approval_voided", base, {"ok": True, "reason": st["error"]}, st["error"])
                continue
            aid = _insert_approval(rcon, sid, ver, row["account_alias"], row["reason"], snap, "scheduled", n, now)
            rcon.execute("UPDATE scheduled_approvals SET state='executed', state_reason='ready at execution', "
                         "updated_at=? WHERE schedule_id=?", (_stamp(now), n))
            rcon.commit()
        except Exception:
            rcon.rollback()
            raise
        events.append(dict(base, action="executed", reason="ready", approval_id=aid))
        _audit("scheduled_approval_executed", dict(base, approval_id=aid, account_alias=row["account_alias"]),
               {"ok": True, "status": "live-small"}, row["reason"])
    return events
