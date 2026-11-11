"""Nudge list: Sim versions that meet every readiness criterion right now. Read-only.

A nudge is computed fresh on each call from the shared readiness module and is never stored, so it disappears the
moment readiness lapses. This module changes no status and makes no NinjaTrader call; the owner's Approval is a
separate, later step.
"""
from datetime import datetime, timezone

import feedback
import registry


def list_nudges(rcon, jcon, account: str = "", as_of: datetime = None) -> dict:
    as_of = (as_of or datetime.now(timezone.utc)).replace(microsecond=0)
    stamp = as_of.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    specs = [dict(r) for r in rcon.execute("SELECT id, version FROM specs WHERE status = 'sim' ORDER BY id, version")]
    nudges, errors = [], []
    for s in specs:
        r = feedback.readiness_for(rcon, jcon, s["id"], s["version"], account, as_of)
        if "error" in r:
            errors.append({"id": s["id"], "version": s["version"], "error": r["error"]})
        elif r["ready"]:
            full = registry.get_spec(rcon, s["id"], s["version"])["spec"]
            nudges.append({"id": s["id"], "version": s["version"], "name": full.get("name") or s["id"],
                           "lucid_id": full.get("lucid_id"), "summary": r["summary"],
                           "criteria": [{k: c[k] for k in ("key", "status", "actual", "threshold")}
                                        for c in r["criteria"]]})
    return {"as_of": stamp, "checked": len(specs), "nudges": nudges, "errors": errors,
            "note": "Suggestions only. Computed now; nothing is stored and no status has changed."}
