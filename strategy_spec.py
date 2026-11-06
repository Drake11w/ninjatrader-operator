"""Strategy spec v1 (DRAFT): the contract between the Lucid system and this operator.

A spec describes one candidate strategy version. The Lucid system emits it, the operator validates and (later)
registers and evaluates it. Entry/exit rules are structured enough to compare and diff, but each carries a
free-text description because the Lucid system's real rule format is not known yet. Reconcile once it is.

validate() returns a list of human-readable problems (empty list = valid). It never raises on bad input.
"""
import re

SCHEMA_VERSION = 2          # current; version 1 specs are still accepted
SUPPORTED_VERSIONS = (1, 2)
STATUSES = ("candidate", "backtest", "sim", "live-small", "live", "rejected", "retired")
SIDES = ("long", "short", "both")
STOP_TYPES = ("points", "ticks", "atr_multiple", "percent", "structure")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
METRIC_KEYS = ("min_trades", "win_rate", "expectancy", "profit_factor", "max_drawdown", "avg_trades_per_day")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _need(d: dict, key: str, kind, where: str, errs: list):
    """Require d[key] of type `kind` (tuple ok); returns the value or None."""
    if key not in d:
        errs.append(f"{where}.{key} is required")
        return None
    v = d[key]
    ok = _num(v) if kind is float else isinstance(v, kind) and not (kind is int and isinstance(v, bool))
    if not ok:
        errs.append(f"{where}.{key} has the wrong type")
        return None
    return v


def _rules(rules, where: str, errs: list):
    if not isinstance(rules, list) or not rules:
        errs.append(f"{where} must be a non-empty list of rules")
        return
    for i, r in enumerate(rules):
        w = f"{where}[{i}]"
        if not isinstance(r, dict):
            errs.append(f"{w} must be an object")
            continue
        _need(r, "type", str, w, errs)
        if not str(r.get("description", "")).strip():
            errs.append(f"{w}.description is required (plain-English rule, so a human can review it)")
        if "params" in r and not isinstance(r["params"], dict):
            errs.append(f"{w}.params must be an object")


def _validate_v1(spec: dict) -> list:
    errs = []
    sid = _need(spec, "id", str, "spec", errs)
    if sid is not None and not ID_RE.match(sid):
        errs.append("spec.id must be lowercase letters, digits, - or _ (2-64 chars)")
    ver = _need(spec, "version", str, "spec", errs)
    if ver is not None and not VERSION_RE.match(ver):
        errs.append("spec.version must look like 1.0.0")
    if not str(spec.get("hypothesis", "")).strip():
        errs.append("spec.hypothesis is required: why should this edge exist? (needed to judge results honestly)")
    status = spec.get("status", "candidate")
    if status not in STATUSES:
        errs.append(f"spec.status must be one of {', '.join(STATUSES)}")

    inst = _need(spec, "instrument", dict, "spec", errs)
    if inst is not None:
        _need(inst, "root", str, "instrument", errs)
        if inst.get("contract_policy", "front") not in ("front", "fixed"):
            errs.append("instrument.contract_policy must be 'front' or 'fixed'")
        if inst.get("contract_policy") == "fixed" and not inst.get("contract"):
            errs.append("instrument.contract is required when contract_policy is 'fixed'")
        if "bar" in inst:
            b = inst["bar"]
            if not (isinstance(b, dict) and b.get("type") and _num(b.get("value"))):
                errs.append("instrument.bar must look like {\"type\": \"minute\", \"value\": 5}")

    sess = _need(spec, "session", dict, "spec", errs)
    if sess is not None:
        for k in ("entry_start", "entry_end", "flat_by"):
            v = _need(sess, k, str, "session", errs)
            if v is not None and not TIME_RE.match(v):
                errs.append(f"session.{k} must be HH:MM (24h)")
        if all(isinstance(sess.get(k), str) and TIME_RE.match(sess[k]) for k in ("entry_start", "entry_end", "flat_by")):
            if not sess["entry_start"] < sess["entry_end"] <= sess["flat_by"]:
                errs.append("session times must satisfy entry_start < entry_end <= flat_by")
        if sess.get("timezone", "America/New_York") != "America/New_York":
            errs.append("session.timezone: only America/New_York is supported for now")

    entry = _need(spec, "entry", dict, "spec", errs)
    if entry is not None:
        if entry.get("side") not in SIDES:
            errs.append(f"entry.side must be one of {', '.join(SIDES)}")
        _rules(entry.get("rules"), "entry.rules", errs)
        if "max_trades_per_day" in entry and not (isinstance(entry["max_trades_per_day"], int) and entry["max_trades_per_day"] > 0):
            errs.append("entry.max_trades_per_day must be a positive integer")

    ex = _need(spec, "exit", dict, "spec", errs)
    if ex is not None:
        stop = _need(ex, "stop", dict, "exit", errs)  # a strategy without a stop is not acceptable
        if stop is not None:
            if stop.get("type") not in STOP_TYPES:
                errs.append(f"exit.stop.type must be one of {', '.join(STOP_TYPES)}")
            if not (_num(stop.get("value")) and stop["value"] > 0):
                errs.append("exit.stop.value must be a positive number")
        tgt = ex.get("target")
        if tgt is not None and not (isinstance(tgt, dict) and _num(tgt.get("value")) and tgt["value"] > 0):
            errs.append("exit.target, if present, needs a positive numeric value")
        if "rules" in ex:
            _rules(ex["rules"], "exit.rules", errs)

    size = _need(spec, "sizing", dict, "spec", errs)
    if size is not None:
        c, m = size.get("contracts"), size.get("max_contracts")
        if not (isinstance(c, int) and not isinstance(c, bool) and c > 0):
            errs.append("sizing.contracts must be a positive integer")
        if not (isinstance(m, int) and not isinstance(m, bool) and m > 0):
            errs.append("sizing.max_contracts must be a positive integer")
        elif isinstance(c, int) and c > m:
            errs.append("sizing.contracts cannot exceed sizing.max_contracts")

    params = spec.get("parameters", {})
    if not isinstance(params, dict) or not all(isinstance(v, (int, float, str, bool)) for v in params.values()):
        errs.append("spec.parameters must be a flat object of numbers, strings or booleans")

    costs = spec.get("costs")
    if costs is None:
        errs.append("spec.costs is required: expected results must be judged net of commission and slippage")
    elif not isinstance(costs, dict) or not all(_num(costs.get(k)) and costs[k] >= 0
                                               for k in ("commission_per_side", "slippage_ticks")):
        errs.append("spec.costs needs non-negative commission_per_side and slippage_ticks")

    exp = spec.get("expected_metrics")
    if exp is not None:
        if not isinstance(exp, dict):
            errs.append("spec.expected_metrics must be an object")
        else:
            for k, v in exp.items():
                if k not in METRIC_KEYS:
                    errs.append(f"expected_metrics.{k} is not a known metric ({', '.join(METRIC_KEYS)})")
                elif not _num(v):
                    errs.append(f"expected_metrics.{k} must be a number")
    return errs


def _summarize_v1(spec: dict) -> dict:
    return {"id": spec["id"], "version": spec["version"], "status": spec.get("status", "candidate"),
            "instrument": spec["instrument"]["root"], "side": spec["entry"]["side"],
            "window": f"{spec['session']['entry_start']}-{spec['session']['entry_end']} ET, flat by {spec['session']['flat_by']}",
            "stop": f"{spec['exit']['stop']['value']} {spec['exit']['stop']['type']}",
            "contracts": spec["sizing"]["contracts"], "hypothesis": spec["hypothesis"]}


# ---------------------------------------------------------------- schema v2 (reconciled with the Lucid system)
# One spec = one deployable strategy, which may be several legs (e.g. a Monday long leg + a Tue-Fri breakout leg) that run
# as separate NinjaScripts on one chart. Times are exchange-local Central by default, as the Lucid system uses.

TIMEZONES = ("America/Chicago", "America/New_York")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
LUCID_ID_RE = re.compile(r"^S\d{1,5}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
METRIC_KEYS_V2 = ("min_trades", "trades_per_year", "net_per_year", "win_rate", "expectancy", "profit_factor",
                  "max_drawdown", "profit_to_drawdown", "avg_trades_per_day", "eval_pass", "eval_fail",
                  "eval_sessions_to_pass", "chain_odds")
FRACTION_KEYS = ("win_rate", "eval_pass", "eval_fail", "chain_odds")


def _pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _check_leg(leg, where: str, errs: list):
    if not isinstance(leg, dict):
        errs.append(f"{where} must be an object")
        return
    if not str(leg.get("name", "")).strip():
        errs.append(f"{where}.name is required")
    days = leg.get("days")
    if days is not None and not (isinstance(days, list) and days and len(set(days)) == len(days)
                                 and all(d in DAYS for d in days)):
        errs.append(f"{where}.days must be a non-empty list of unique days from {', '.join(DAYS)}")
    if leg.get("side") not in SIDES:
        errs.append(f"{where}.side must be long, short or both")

    sess = _need(leg, "session", dict, where, errs)
    if sess is not None:
        vals = {}
        for k in ("entry_start", "entry_end", "flat_by"):
            v = _need(sess, k, str, f"{where}.session", errs)
            if v is not None:
                if TIME_RE.match(v):
                    vals[k] = v
                else:
                    errs.append(f"{where}.session.{k} must be HH:MM (24h)")
        if len(vals) == 3 and not vals["entry_start"] <= vals["entry_end"] <= vals["flat_by"]:
            errs.append(f"{where}.session times must satisfy entry_start <= entry_end <= flat_by "
                        f"(equal start and end = enter at that moment)")

    if "gates" in leg:
        _rules(leg["gates"], f"{where}.gates", errs)

    entry = _need(leg, "entry", dict, where, errs)
    if entry is not None:
        _rules(entry.get("rules"), f"{where}.entry.rules", errs)
        if "max_trades_per_day" in entry and not _pos_int(entry["max_trades_per_day"]):
            errs.append(f"{where}.entry.max_trades_per_day must be a positive integer")

    ex = _need(leg, "exit", dict, where, errs)
    if ex is not None:
        stop = _need(ex, "stop", dict, f"{where}.exit", errs)  # a leg without a stop is not acceptable
        if stop is not None:
            if stop.get("type") not in STOP_TYPES:
                errs.append(f"{where}.exit.stop.type must be one of {', '.join(STOP_TYPES)}")
            if not (_num(stop.get("value")) and stop["value"] > 0):
                errs.append(f"{where}.exit.stop.value must be a positive number "
                            f"(for 'structure': ticks beyond the structure level)")
        tgt = ex.get("target")
        if tgt is not None and not (isinstance(tgt, dict) and _num(tgt.get("value")) and tgt["value"] > 0):
            errs.append(f"{where}.exit.target, if present, needs a positive numeric value (omit it for no target)")
        if "rules" in ex:
            _rules(ex["rules"], f"{where}.exit.rules", errs)

    size = _need(leg, "sizing", dict, where, errs)
    if size is not None:
        c = size.get("contracts")
        m = size.get("max_contracts", c)
        if not _pos_int(c):
            errs.append(f"{where}.sizing.contracts must be a positive integer")
        elif not (_pos_int(m) and m >= c):
            errs.append(f"{where}.sizing.max_contracts must be an integer >= contracts")

    ns = _need(leg, "ninjascript", dict, where, errs)
    if ns is not None:
        for k in ("class", "file"):
            if not str(ns.get(k, "")).strip():
                errs.append(f"{where}.ninjascript.{k} is required (maps NinjaTrader executions back to this leg)")
        inputs = ns.get("inputs", {})
        if not isinstance(inputs, dict) or not all(isinstance(v, (int, float, str, bool)) for v in inputs.values()):
            errs.append(f"{where}.ninjascript.inputs must be a flat object of numbers, strings or booleans")


def _validate_v2(spec: dict) -> list:
    errs = []
    sid = _need(spec, "id", str, "spec", errs)
    if sid is not None and not ID_RE.match(sid):
        errs.append("spec.id must be lowercase letters, digits, - or _ (2-64 chars)")
    ver = _need(spec, "version", str, "spec", errs)
    if ver is not None and not VERSION_RE.match(ver):
        errs.append("spec.version must look like 1.0.0")
    if "lucid_id" in spec and not (isinstance(spec["lucid_id"], str) and LUCID_ID_RE.match(spec["lucid_id"])):
        errs.append("spec.lucid_id must look like S123")
    if not str(spec.get("hypothesis", "")).strip():
        errs.append("spec.hypothesis is required: why should this edge exist? (needed to judge results honestly)")
    if spec.get("status", "candidate") not in STATUSES:
        errs.append(f"spec.status must be one of {', '.join(STATUSES)}")
    if spec.get("timezone", "America/Chicago") not in TIMEZONES:
        errs.append(f"spec.timezone must be one of {', '.join(TIMEZONES)}")

    inst = _need(spec, "instrument", dict, "spec", errs)
    if inst is not None:
        _need(inst, "root", str, "instrument", errs)
        if inst.get("contract_policy", "front") not in ("front", "fixed"):
            errs.append("instrument.contract_policy must be 'front' or 'fixed'")
        if inst.get("contract_policy") == "fixed" and not inst.get("contract"):
            errs.append("instrument.contract is required when contract_policy is 'fixed'")

    legs = spec.get("legs")
    if not isinstance(legs, list) or not legs:
        errs.append("spec.legs must be a non-empty list (a single-leg strategy has one entry)")
    else:
        names = [l.get("name") for l in legs if isinstance(l, dict)]
        if len(set(names)) != len(names):
            errs.append("spec.legs names must be unique")
        for i, leg in enumerate(legs):
            _check_leg(leg, f"legs[{i}]", errs)

    costs = spec.get("costs")
    if costs is None:
        errs.append("spec.costs is required: expected results must be judged net of commission and slippage")
    elif not isinstance(costs, dict) or not all(_num(costs.get(k)) and costs[k] >= 0
                                               for k in ("commission_per_side", "slippage_ticks")):
        errs.append("spec.costs needs non-negative commission_per_side and slippage_ticks")

    bt = spec.get("backtest")
    if bt is not None and not (isinstance(bt, dict) and all(isinstance(bt.get(k), str) and DATE_RE.match(bt[k])
                                                            for k in ("start", "end")) and bt["start"] < bt["end"]):
        errs.append("spec.backtest needs start and end dates (YYYY-MM-DD), start before end")

    exp = spec.get("expected_metrics")
    if exp is not None:
        if not isinstance(exp, dict):
            errs.append("spec.expected_metrics must be an object")
        else:
            for k, v in exp.items():
                if k not in METRIC_KEYS_V2:
                    errs.append(f"expected_metrics.{k} is not a known metric ({', '.join(METRIC_KEYS_V2)})")
                elif not _num(v):
                    errs.append(f"expected_metrics.{k} must be a number")
                elif k in FRACTION_KEYS and not 0 <= v <= 1:
                    errs.append(f"expected_metrics.{k} is a fraction between 0 and 1 (0.82 = 82%)")
    return errs


def validate(spec) -> list:
    """Validate a spec of any supported schema version. Returns problems (empty list = valid)."""
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]
    v = spec.get("schema_version")
    if v == 1:
        return _validate_v1(spec)
    if v == 2:
        return _validate_v2(spec)
    return [f"schema_version must be one of {', '.join(map(str, SUPPORTED_VERSIONS))} (current is {SCHEMA_VERSION})"]


def _summarize_v2(spec: dict) -> dict:
    legs = []
    for l in spec["legs"]:
        s = l["session"]
        tgt = l["exit"].get("target")
        legs.append({"name": l["name"], "days": l.get("days", "all"), "side": l["side"],
                     "window": f"{s['entry_start']}-{s['entry_end']}, flat by {s['flat_by']}",
                     "stop": f"{l['exit']['stop']['value']} {l['exit']['stop']['type']}",
                     "target": tgt["value"] if tgt else "none",
                     "contracts": l["sizing"]["contracts"], "ninjascript": l["ninjascript"]["class"]})
    return {"id": spec["id"], "version": spec["version"], "lucid_id": spec.get("lucid_id"),
            "status": spec.get("status", "candidate"), "instrument": spec["instrument"]["root"],
            "timezone": spec.get("timezone", "America/Chicago"), "legs": legs,
            "contracts_total": sum(l["contracts"] for l in legs), "hypothesis": spec["hypothesis"]}


def summarize(spec: dict) -> dict:
    """Short human-readable view of a valid spec, for tool output."""
    return _summarize_v2(spec) if spec.get("schema_version") == 2 else _summarize_v1(spec)
