"""Promotion (S2): one explicit command that takes a Lucid handoff bundle through install, backtest, spec check and
Sim. Its ceiling is Sim: it never touches 'live-small' or 'live'.

Stages (each maps to a registry status, so progress and resume come from state, not logs):
  export      the bundle exists in handoff/ (Lucid's stage; the operator only checks it)      -> candidate exists
  install     preflight + install + register as candidate                                     -> candidate
  backtest    Analyzer run, import, status 'backtest'                                          -> backtest
  spec_check  the imported backtest reproduces the spec's expected_metrics                     -> (stays backtest)
  sim         enable on a Sim* account, status 'sim'                                           -> sim

Stop at the first failed stage and leave the status at the last passed one; there is no skip or override flag. A failed
spec check leaves the version at 'backtest' and never starts Sim (the fix is a new version from Lucid). Re-running is
idempotent: stages whose state already holds are reported 'done' and skipped. A single-flight lock refuses a second run.

`plan_stages` is pure. `promote` is the thin shell: every side effect (install, Analyzer run, enable) is an injected
callable, so this module imports no network code and has no path to NinjaTrader of its own; server.py supplies the
existing, already-gated callables. `promotion_report.json` is written per version next to the feedback snapshots.
"""
import hashlib
import json
import os
import time
from datetime import datetime, timezone

import backtest
import control
import feedback
import handoff
import registry

STAGES = ("export", "install", "backtest", "spec_check", "sim")
RANK = {"candidate": 0, "backtest": 1, "sim": 2, "live-small": 3, "live": 4}
LOCK_STALE_SECONDS = 12 * 3600


def handoff_base() -> str:
    return os.environ.get("NT_HANDOFF_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lucid-trading-system", "handoff")


# ---------------------------------------------------------------- pure

def plan_stages(status, bundle_ok, backtest_state: dict, account: str) -> list:
    """Stage plan from loaded state. status: registry status or None (not registered). bundle_ok: None unknown/not
    needed, True/False. backtest_state: {"present": bool, "claims": str|None}. Returns [{stage, state, note}] with
    state 'done' or 'todo'; a terminal or beyond-Sim status makes every stage 'done' (nothing to do) or raises via
    the caller's checks."""
    rank = RANK.get(status, -1) if status else -1
    out = []

    def add(stage, done, note=""):
        out.append({"stage": stage, "state": "done" if done else "todo", "note": note})

    add("export", status is not None or bool(bundle_ok), "registered" if status else "")
    add("install", status is not None, "registered as candidate" if status else "")
    add("backtest", rank >= 1, f"status {status}" if rank >= 1 else "")
    add("spec_check", rank >= 2, "")
    add("sim", rank >= 2, f"status {status}" if rank >= 2 else "")
    return out


def spec_check(backtest_state: dict) -> tuple:
    """(passed, reason) from the stored backtest's claims check, using the readiness 'baseline' rule."""
    if not backtest_state.get("present"):
        return False, "no imported backtest to check against the spec's expected_metrics"
    if backtest_state.get("claims") == "differs":
        return False, "the backtest does not reproduce the spec's expected_metrics"
    return True, backtest_state.get("claims") or "present"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(text) -> str:
    return hashlib.sha256(text if isinstance(text, bytes) else text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- lock

def acquire_lock(path: str):
    """Single-flight local lock. Returns an error string if busy, else None. A lock older than 12 h is stale."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) > LOCK_STALE_SECONDS:
            os.remove(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return "busy: another promotion is running (the Analyzer takes one job at a time); try again when it ends"
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return None


def release_lock(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- shell

def _latest_backtest(rcon, spec: dict, sid: str, version: str) -> dict:
    runs = backtest.list_runs(rcon, sid, version)
    run = backtest.get_run(rcon, runs[0]["run_id"]) if runs else None
    if not run:
        return {"present": False, "claims": None, "run_id": None}
    m = json.loads(run["metrics_json"])
    return {"present": True, "claims": (backtest.claims_check(m, spec) or {}).get("overall"), "run_id": run["run_id"]}


def report_path(out_dir: str, folder: str, version: str) -> str:
    return os.path.join(out_dir or feedback.feedback_dir(), folder, version, "promotion_report.json")


def promote(rcon, sid: str, version: str, account: str, bundle_dir: str = "", io: dict = None, out_dir: str = "",
            alias_file: str = "", lock_path: str = "") -> dict:
    """Run the stages. io (all injected): inspect(bundle_dir)->handoff.inspect_bundle-shaped dict, install(bundle_dir)->
    {ok, ...}, backtest(spec, bundle_dir)->{ok, csv|error}, enable(spec, account)->{ok, ...}."""
    io = io or {}
    inspect = io.get("inspect", handoff.inspect_bundle)
    install, run_bt, enable = io.get("install"), io.get("backtest"), io.get("enable")
    bundle_dir = bundle_dir or os.path.join(handoff_base(), sid, version)
    params = {"id": sid, "version": version}

    if not (account or "").lower().startswith("sim"):
        return {"ok": False, "error": "promotion ends in Sim: account must be a Sim* account"}
    row = registry.get_spec(rcon, sid, version)
    status = None if "error" in row else row["status"]
    if status in registry.TERMINAL:
        return {"ok": False, "error": f"{sid} {version} is {status}; promote a new version instead"}

    lock_path = lock_path or os.path.join(out_dir or feedback.feedback_dir(), "promote.lock")
    busy = acquire_lock(lock_path)
    if busy:
        return {"ok": False, "busy": True, "error": busy}
    try:
        return _run(rcon, sid, version, account, bundle_dir, status, inspect, install, run_bt, enable, out_dir,
                    alias_file, params)
    finally:
        release_lock(lock_path)


def _run(rcon, sid, version, account, bundle_dir, status, inspect, install, run_bt, enable, out_dir, alias_file,
         params) -> dict:
    results, hashes, failed = [], {}, None
    bundle = None
    spec = None if status is None else registry.get_spec(rcon, sid, version)["spec"]

    def record(stage, result, reason="", **extra):
        results.append(dict({"stage": stage, "result": result, "reason": reason, "at": _now()}, **extra))
        control.audit("promote_stage", dict(params, stage=stage), {"ok": result != "fail", "result": result},
                      reason or result)

    stages = plan_stages(status, None, {}, account)
    for st in stages:
        name = st["stage"]
        if failed:
            results.append({"stage": name, "result": "pending", "reason": f"not run: {failed} failed", "at": None})
            continue
        if st["state"] == "done" and name != "export":
            results.append({"stage": name, "result": "pass", "reason": "already done: " + (st["note"] or "state holds"),
                            "at": None, "skipped": True})
            continue

        if name == "export":
            if status is not None:
                results.append({"stage": name, "result": "pass", "reason": "already registered", "at": None,
                                "skipped": True})
                continue
            bundle = inspect(bundle_dir)
            if not bundle.get("ok"):
                record(name, "fail", "; ".join(bundle.get("errors", ["bundle not found"])))
                failed = name
                continue
            if bundle["spec"].get("id") != sid or bundle["spec"].get("version") != version:
                record(name, "fail", f"bundle holds {bundle['spec'].get('id')} {bundle['spec'].get('version')}, "
                                     f"not {sid} {version}")
                failed = name
                continue
            hashes["spec_json_sha256"] = _sha(json.dumps(bundle["spec"], sort_keys=True))
            hashes["cs_sha256"] = {l["class"]: l.get("sha256") for l in bundle.get("legs", [])}
            record(name, "pass", "bundle present and checked", sha256=hashes["spec_json_sha256"])

        elif name == "install":
            res = install(bundle_dir) if install else {"ok": False, "error": "no installer wired"}
            row = registry.get_spec(rcon, sid, version)
            if not res.get("ok") or "error" in row:
                record(name, "fail", res.get("error") or "; ".join(res.get("errors", [])) or "not registered")
                failed = name
                continue
            spec, status = row["spec"], row["status"]
            record(name, "pass", "installed and registered as candidate")

        elif name == "backtest":
            res = run_bt(spec, bundle_dir) if run_bt else {"ok": False, "error": "no Analyzer runner wired"}
            if not res.get("ok"):
                record(name, "fail", res.get("error", "backtest failed"))
                failed = name
                continue
            imp = backtest.import_run(rcon, spec, res["csv"], "promote", "promote_strategy")
            if not imp.get("ok"):
                record(name, "fail", "import failed: " + str(imp.get("error")))
                failed = name
                continue
            sres = registry.set_status(rcon, sid, version, "backtest", "promotion: backtest imported")
            if not sres["ok"]:
                record(name, "fail", sres["error"])
                failed = name
                continue
            status = "backtest"
            hashes["backtest_csv_sha256"] = _sha(res["csv"])
            record(name, "pass", f"imported run {imp['run_id']}", sha256=hashes["backtest_csv_sha256"])

        elif name == "spec_check":
            ok, why = spec_check(_latest_backtest(rcon, spec, sid, version))
            if not ok:
                record(name, "fail", why)
                failed = name
                continue
            record(name, "pass", why)

        elif name == "sim":
            res = enable(spec, account) if enable else {"ok": False, "error": "no enabler wired"}
            if not res.get("ok"):
                record(name, "fail", res.get("error", "enable failed"))
                failed = name
                continue
            sres = registry.set_status(rcon, sid, version, "sim", f"promotion: enabled on {account} via add-on")
            if not sres["ok"]:
                record(name, "fail", sres["error"])
                failed = name
                continue
            status = "sim"
            record(name, "pass", "enabled on a Sim account; status sim")

    out = {"ok": failed is None, "id": sid, "version": version, "status": status, "failed_stage": failed,
           "stages": results, "hashes": hashes, "account_class": "sim"}
    out["next"] = ("Sim running; readiness is judged by the feedback export. Moving to live-small is the owner's "
                   "Approval, never part of promotion." if not failed else
                   f"stopped at '{failed}'; status left at '{status}'. Fix the cause and run promote_strategy again "
                   f"(completed stages are skipped). There is no override.")
    _write_report(out, spec, out_dir, alias_file, account)
    return out


def _write_report(out: dict, spec, out_dir: str, alias_file: str, account: str) -> None:
    folder = (spec or {}).get("lucid_id") or out["id"]
    if not (feedback.SAFE_PART.match(folder) and feedback.SAFE_PART.match(out["version"])):
        return
    path = report_path(out_dir, folder, out["version"])
    prev = None
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                prev = json.load(f)
        except (OSError, ValueError):
            prev = None
    doc = dict(out, schema_version="1.0.0", written_at=_now(), attempts=(prev or {}).get("attempts", 0) + 1,
               first_attempt_at=(prev or {}).get("first_attempt_at") or _now())
    aliases = feedback.resolve_aliases([account], alias_file)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    feedback._write_atomic(path, feedback.scrub(feedback.to_json(doc), aliases))
    out["report"] = path
