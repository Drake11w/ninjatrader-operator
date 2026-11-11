"""Scheduled feedback export: sync new fills into the journal (GET only), then write a feedback snapshot for every
registered version in sim / live-small / live that has a lucid_id. Idempotent: unchanged results are a no-op.

    python scripts/export_feedback.py            # sync + export all
    python scripts/export_feedback.py --no-sync  # export from the journal as it is

Read-only toward NinjaTrader (one GET /executions) and toward the registry. Run by scripts/install_export_task.ps1.
"""
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import feedback  # noqa: E402
import journal  # noqa: E402
import registry  # noqa: E402
import sup_profile  # noqa: E402

STAGES = ("sim", "live-small", "live")


def sync(jcon, rcon, base: str) -> dict:
    try:
        with urllib.request.urlopen(base + "/executions?limit=500", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"synced": False, "error": f"add-on unreachable: {e}"}
    tagger = journal.make_tagger(jcon, registry.order_name_map(rcon, sup_profile.bindings()))
    execs = sorted(data.get("executions", []), key=lambda e: e.get("time") or "")
    return {"synced": True, **journal.record(jcon, execs, "", "", tagger)}


def export_all(rcon, jcon, **kw) -> list:
    out = []
    marks = ",".join("?" * len(STAGES))
    for s in rcon.execute(f"SELECT id, version, status FROM specs WHERE status IN ({marks}) ORDER BY id, version",
                          STAGES).fetchall():
        spec = registry.get_spec(rcon, s["id"], s["version"])["spec"]
        if not spec.get("lucid_id"):
            out.append({"id": s["id"], "version": s["version"], "skipped": "no lucid_id"})
            continue
        res = feedback.export(rcon, jcon, s["id"], s["version"], **kw)
        out.append({"id": s["id"], "version": s["version"], "ok": res.get("ok"), "status": res.get("status"),
                    "error": res.get("error")})
    return out


def main(argv) -> int:
    rcon, jcon = registry.connect(), journal.connect()
    try:
        result = {}
        if "--no-sync" not in argv:
            result["sync"] = sync(jcon, rcon, os.environ.get("NT_ADDON_URL", "http://127.0.0.1:8765"))
        result["exports"] = export_all(rcon, jcon)
        print(json.dumps(result, indent=2))
        return 0 if all(e.get("ok") or e.get("skipped") for e in result["exports"]) else 1
    finally:
        rcon.close()
        jcon.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
