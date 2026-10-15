"""SIEM export: the ledger as syslog-style JSON lines.

Each event becomes one line: ``@timestamp``, ``event.kind``, ``run_id``,
``actor``, ``severity`` and a *minimised* payload. Full prompts and contexts
are never exported (digests only), so the detection fabric sees the decision
without the data behind it.

Pagination is cursor-based on the event id: ``since`` is the last id the
consumer saw, ``limit`` caps the batch. A consumer that polls with the
returned ``cursor`` gets every event exactly once, even while the app keeps
writing — which is the property a SIEM integration needs and a page-number
scheme cannot give it.
"""
from __future__ import annotations

import json
import time as _time

# Payload keys allowed across the boundary. Everything else stays in the
# database. The list is intentionally short: each entry is a field someone
# writes a detection rule against, and a field nobody rules on is just
# exfiltration surface.
ALLOWED_KEYS = frozenset({
    "assessment_id", "investigation_id", "driver", "decision", "status",
    "stage", "message", "model", "tokens_in", "tokens_out", "latency_ms",
    "verdict", "kind", "actor", "objective", "error", "trigger",
    "domains", "n_sources", "confidence", "overall_pct",
})


def siem_line(run_id: str, e: dict) -> str:
    data = e.get("data") or {}
    return json.dumps({
        "@timestamp": e.get("ts") or _time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                    _time.gmtime()),
        "event": {"kind": e.get("kind"), "intent": e.get("intent"),
                  "outcome": e.get("verdict"),
                  "severity": e.get("severity") or "info"},
        "run_id": str(run_id),
        "actor": {"type": e.get("actor_type"), "name": e.get("actor")},
        "seq": e.get("seq"),
        "chain": {"hash": e.get("hash"), "prev_hash": e.get("prev_hash")},
        "economics": {k: v for k, v in data.items()
                      if k in ALLOWED_KEYS and v is not None},
    }, default=str)


def export_siem_events(*, run_ids: list[str] | None = None, since: int = 0,
                       limit: int = 200, db=None) -> dict:
    """Collect SIEM lines across chains, newest chains first, cursor-paged.

    ``since`` filters on the event *row id* (monotonic across chains), so one
    cursor covers the whole export. Returns the lines plus the cursor to pass
    next time — the consumer stores one integer, not per-chain state.
    """
    from .database import SessionLocal
    from .ledger_models import LedgerEvent, LedgerRun
    own = db is None
    db = db or SessionLocal()
    try:
        q = db.query(LedgerEvent)
        if run_ids:
            q = q.filter(LedgerEvent.run_id.in_([str(r) for r in run_ids]))
        rows = (q.filter(LedgerEvent.id > since)
                .order_by(LedgerEvent.id).limit(max(1, limit)).all())
        lines = []
        for r in rows:
            try:
                data = json.loads(r.data_json or "{}")
            except Exception:
                data = {}
            lines.append(siem_line(r.run_id, {
                "ts": r.ts, "kind": r.kind, "intent": r.intent,
                "verdict": r.verdict, "severity": r.severity,
                "actor_type": r.actor_type, "actor": r.actor,
                "seq": r.seq, "hash": r.hash, "prev_hash": r.prev_hash,
                "data": data,
            }))
        cursor = rows[-1].id if rows else since
        n_chains = (db.query(LedgerRun).count() if not run_ids else len(run_ids))
        return {"lines": lines, "count": len(lines), "cursor": cursor,
                "chains": n_chains, "lines_as_json": True,
                "note": "Content is minimised: digests, not prompts."}
    finally:
        if own:
            db.close()
