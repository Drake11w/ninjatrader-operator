"""Lucid handoff bundles -> checked, installed, registered, and turned into NinjaTrader strategy templates.

A bundle is handoff/<id>/<version>/ with spec.json, MANIFEST.json, strategies/*.cs (see HANDOFF_FOR_LUCID_AGENT.md).
`inspect_bundle` only reads. `install_bundle` (apply=True) additionally backs up and copies the .cs files into the
NinjaTrader Custom\\Strategies folder, registers the spec as a candidate, and writes one strategy template per leg
(templates\\Strategy\\<Class>\\<id> <version> <leg>.xml) holding the spec's exact inputs. It never overwrites an
existing template of the same name and never edits a .cs file.
"""
import hashlib
import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
from datetime import date, datetime

import registry
import sessions
import strategy_spec

PROP_RE = re.compile(r"\[NinjaScriptProperty\][\s\S]{0,400}?public\s+[\w<>\[\]\.?]+\s+(\w+)\s*\{\s*get;\s*set;\s*\}")


def nt_root() -> str:
    return os.environ.get("NT_HOME") or os.path.join(os.path.expanduser("~"), "Documents", "NinjaTrader 8")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def properties(cs_text: str) -> set:
    return set(PROP_RE.findall(cs_text))


def inspect_bundle(path: str) -> dict:
    """Read-only checks. Returns {ok, errors, warnings, spec, manifest, legs: [...]}."""
    errors, warnings = [], []
    try:
        with open(os.path.join(path, "spec.json"), encoding="utf-8") as f:
            spec = json.load(f)
        with open(os.path.join(path, "MANIFEST.json"), encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        return {"ok": False, "errors": [f"cannot read spec.json / MANIFEST.json in {path}: {e}"]}

    errors += [f"spec: {e}" for e in strategy_spec.validate(spec)]
    approval = manifest.get("approval") or {}
    if not approval.get("handoff_approved"):
        errors.append("MANIFEST.approval.handoff_approved is not true: refusing an unapproved bundle")
    if manifest.get("spec_id") != spec.get("id") or manifest.get("spec_version") != spec.get("version"):
        errors.append("MANIFEST spec_id/spec_version do not match spec.json")
    if approval.get("limits"):
        warnings.append("approval limits: " + approval["limits"])
    if spec.get("evidence_status"):
        warnings.append("evidence_status: " + str(spec["evidence_status"]))
    if spec.get("compiled_in_nt8") in (None, "unknown", False):
        warnings.append(f"compiled_in_nt8 = {spec.get('compiled_in_nt8')!r}: NinjaTrader must compile the files before they can run")

    legs = []
    for leg in spec.get("legs", []):
        ns = leg.get("ninjascript") or {}
        cls = ns.get("class")
        rel = ns.get("bundle_file") or f"strategies/{cls}.cs"
        cs_path = os.path.join(path, rel)
        info = {"leg": leg.get("name"), "class": cls, "file": cs_path, "inputs": ns.get("inputs") or {}}
        if not os.path.isfile(cs_path):
            errors.append(f"leg {leg.get('name')}: {rel} missing from bundle")
            legs.append(info)
            continue
        info["sha256"] = sha256(cs_path)
        want = (manifest.get("files", {}).get(rel) or {}).get("sha256")
        if not want:
            errors.append(f"{rel}: no sha256 in MANIFEST")
        elif want != info["sha256"]:
            errors.append(f"{rel}: sha256 differs from MANIFEST (file edited after export?)")
        with open(cs_path, encoding="utf-8-sig") as f:
            text = f.read()
        props = properties(text)
        if f"class {cls}" not in text:
            errors.append(f"{rel}: no 'class {cls}'")
        missing = sorted(set(info["inputs"]) - props)
        if missing:
            errors.append(f"{rel}: inputs not found as [NinjaScriptProperty]: {missing}")
        for n in ns.get("order_names") or []:
            if f'"{n}"' not in text:
                errors.append(f"{rel}: order name {n!r} not in source")
        legs.append(info)
    return {"ok": not errors, "errors": errors, "warnings": warnings, "spec": spec, "manifest": manifest, "legs": legs}


def _xml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def build_template(skeleton_path: str, cls: str, inputs: dict, instrument: str, name: str) -> str:
    """Take a real template NinjaTrader saved for this class and set InstrumentOrInstrumentList, the strategy name
    and every input. Fails if an input has no element in the skeleton (the skeleton must come from the same .cs)."""
    ET.register_namespace("xsd", "http://www.w3.org/2001/XMLSchema")
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
    tree = ET.parse(skeleton_path)
    root = tree.getroot()
    st = root.find("Strategy/" + cls)
    if st is None:
        raise ValueError(f"{skeleton_path} has no <Strategy><{cls}> element")
    missing = []
    for k, v in inputs.items():
        el = st.find(k)
        if el is None:
            missing.append(k)
        else:
            el.text = _xml_value(v)
    if missing:
        raise ValueError(f"skeleton template lacks elements for inputs {missing}: re-save a template from the installed "
                         f"{cls} (the skeleton predates this version of the script)")
    for tag, val in (("InstrumentOrInstrumentList", instrument), ("Name", name)):
        el = st.find(tag)
        if el is None:
            raise ValueError(f"skeleton template has no <{tag}>")
        el.text = val
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")


def _skeleton(cls: str):
    d = os.path.join(nt_root(), "templates", "Strategy", cls)
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".xml"):
                return os.path.join(d, fn)
    return None


def install_bundle(path: str, apply: bool = False, nt_dir: str = "", today: date = None) -> dict:
    r = inspect_bundle(path)
    if not r["ok"]:
        return {"ok": False, "applied": False, "errors": r["errors"], "warnings": r.get("warnings", [])}
    spec, root = r["spec"], nt_root()
    strat_dir = os.path.join(nt_dir or root, "bin", "Custom", "Strategies")
    contract = sessions.front_contract(today or date.today())["contract"]
    instrument = f"{spec['instrument']['root']} {contract}"
    plan = {"copy": [], "templates": [], "instrument": instrument}
    for leg in r["legs"]:
        dest = os.path.join(strat_dir, leg["class"] + ".cs")
        same = os.path.isfile(dest) and sha256(dest) == leg["sha256"]
        plan["copy"].append({"class": leg["class"], "dest": dest, "already_identical": same})
        sk = _skeleton(leg["class"])
        tname = f"{spec['id']} {spec['version']} {leg['leg']}.xml"
        tdir = os.path.join(nt_dir or root, "templates", "Strategy", leg["class"])
        plan["templates"].append({"leg": leg["leg"], "path": os.path.join(tdir, tname), "skeleton": sk})

    # Templates are a convenience for applying by hand; enable_strategy sends the inputs itself. Build every one
    # before touching anything; a stale or missing skeleton only costs that template (warning), not the install.
    built, warnings = {}, list(r["warnings"])
    for leg, t in zip(r["legs"], plan["templates"]):
        if t["skeleton"] is None:
            warnings.append(f"{leg['class']}: no saved NinjaTrader template to use as a skeleton; template for leg "
                            f"'{leg['leg']}' not written")
            continue
        try:
            built[t["path"]] = build_template(t["skeleton"], leg["class"], leg["inputs"], instrument,
                                              f"{spec['id']} {spec['version']} {leg['leg']}")
        except ValueError as e:
            warnings.append(f"template for leg '{leg['leg']}' not written: {e}")
    r["warnings"] = warnings
    if not apply:
        return {"ok": True, "applied": False, "warnings": r["warnings"], "plan": plan,
                "note": "dry run: nothing written. Call again with apply=true."}

    backup = os.path.join(root, "operator_backups", "StrategiesBackup_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    for c, leg in zip(plan["copy"], r["legs"]):
        if c["already_identical"]:
            continue
        os.makedirs(os.path.dirname(c["dest"]), exist_ok=True)
        if os.path.isfile(c["dest"]):
            os.makedirs(backup, exist_ok=True)
            shutil.copy2(c["dest"], backup)
            c["backed_up_to"] = backup
        shutil.copyfile(leg["file"], c["dest"])
    for p, text in built.items():
        if os.path.exists(p):
            continue
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            f.write(text)
    con = registry.connect()
    try:
        reg = registry.submit(con, spec, "installed from handoff bundle " + os.path.abspath(path))
        appr = r["manifest"].get("approval") or {}
        registry.record_approval(con, spec["id"], spec["version"], appr.get("handoff_approved"),
                                 appr.get("limits", ""), os.path.abspath(path))
    finally:
        con.close()
    already = (not reg["ok"]) and any("already registered" in e for e in reg.get("errors", []))
    return {"ok": reg["ok"] or already, "applied": True, "registry": reg, "warnings": r["warnings"], "plan": plan,
            "next": "In NinjaTrader press F5 in the NinjaScript Editor to compile, then enable_strategy (Sim only)."}


def _form_line(k, v) -> str:
    from urllib.parse import quote
    return f"{k}={quote(_xml_value(v), safe='')}"


def enable_requests(spec: dict, today: date = None) -> list:
    """One request per leg for the add-on: class, instrument, strategy name and a body of 'Input=urlencoded' lines."""
    contract = sessions.front_contract(today or date.today())["contract"]
    instrument = f"{spec['instrument']['root']} {contract}"
    out = []
    for leg in spec["legs"]:
        ns = leg["ninjascript"]
        out.append({"leg": leg["name"], "class": ns["class"], "instrument": instrument,
                    "name": f"{spec['id']} {spec['version']} {leg['name']}",
                    "body": "\n".join(_form_line(k, v) for k, v in ns.get("inputs", {}).items())})
    return out


def conflicts(spec: dict, running: list) -> list:
    """Names of running strategies (from the add-on /strategies reply) that the spec says it conflicts with."""
    bad = set(spec.get("conflicts_with") or [])
    return [s.get("name", "") for s in running
            if any(c and (c in s.get("name", "")) for c in bad)]


def enable_spec(spec: dict, account: str, post, fetch, today: date = None, skip_defaults: bool = False) -> dict:
    """Start every leg on `account` through the add-on. `post(path, params, body)` and `fetch(path)` are injected.
    All-or-nothing: if a leg fails, the legs already started are disabled again."""
    reqs = enable_requests(spec, today)
    listing = fetch("/strategies")
    if "error" in listing:
        return {"ok": False, "error": listing["error"]}
    running = [s for s in listing.get("strategies", []) if s.get("account") == account]
    hit = conflicts(spec, running)
    if hit:
        return {"ok": False, "error": f"conflicts_with strategies already on {account}: {hit}"}
    dup = [r["name"] for r in reqs if any(s.get("name") == r["name"] for s in running)]
    if dup:
        return {"ok": False, "error": f"already present on {account}: {dup}"}
    started, results = [], []
    for r in reqs:
        q = {"account": account, "class": r["class"], "name": r["name"], "instrument": r["instrument"]}
        if skip_defaults:
            q["skip_defaults"] = "1"
        res = post("/strategy/enable", q, r["body"])
        results.append({"leg": r["leg"], "name": r["name"], "result": res})
        if not res.get("ok"):
            for name in started:
                post("/strategy/disable", {"account": account, "name": name}, "")
            return {"ok": False, "error": f"leg '{r['leg']}' failed: {res.get('error')}",
                    "rolled_back": started, "results": results}
        started.append(r["name"])
    after = fetch("/strategies")
    seen = {s.get("name"): s for s in after.get("strategies", []) if s.get("account") == account}
    verified = {n: (seen[n].get("state") if n in seen else None) for n in started}
    return {"ok": all(v is not None for v in verified.values()), "results": results, "verified_states": verified}
