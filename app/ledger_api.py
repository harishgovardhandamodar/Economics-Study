"""The audit surface, as its own router.

Kept separate from the product API on purpose: the audit/ledger view has a
different threat model from the feature views (it reads chains, it exports
proofs, it is what you reach for when something is wrong), so it can be
reasoned about — and locked down — independently.

The one thing this router is not allowed to do is break the product. Session
resolution and tracing are both fail-open: a ledger outage degrades to
*unaudited* work, never to a 500, because an audit outage is precisely when you
need the tool that produced the evidence to keep working.
"""
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from . import ledger as L
from .database import get_db
from .ledger_models import LedgerApproval, LedgerAuditDrop, LedgerEvent, LedgerRun

router = APIRouter(prefix="/api/ledger", tags=["ledger"])

MAX_TRACE = 48


def trace_id() -> str:
    """A short id for this request, so every event it produces can be found."""
    import hashlib as _h

    seed = f"{id(object())}:{L.text_digest(str(L.current_session()))}"
    return _h.sha256(seed.encode()).hexdigest()[:MAX_TRACE]


def open_session(request: Request, x_econ_session: str | None = Header(None)):
    """Resolve the caller's sitting.

    Installed as an app-level dependency, so it runs for every request. Fail-open
    by design: a failure here must not turn a product call into an error, and the
    only honest record of that failure is that the events were not traced. The
    trace id itself is set by the middleware, which can reset its token.
    """
    try:
        request.state.session = L.resolve_session(x_econ_session)
    except Exception:
        request.state.session = None
    return request.state.session


@router.get("/integrity-monitor")
def integrity_monitor(limit: int = 50):
    """Re-verify every recent chain, and report dropped audit writes.

    ``audit_drops`` is the number that matters operationally: a chain can verify
    perfectly while the recorder has been quietly failing to write for hours.
    """
    return L.monitor_integrity(limit=limit)


@router.get("/sessions")
def sessions(limit: int = 50):
    return {"sessions": L.list_sessions(limit=limit)}


@router.get("/sessions/{session_id}")
def session_detail(session_id: str, limit: int = 500):
    out = L.session_timeline(session_id, limit=limit)
    if not out.get("ok"):
        raise HTTPException(404, out.get("reason", "no such session"))
    out["insights"] = L.session_insights(session_id)
    return out


@router.get("/timeline")
def global_timeline(limit: int = 200):
    """Every chain interleaved into one newest-first stream."""
    return L.global_timeline(limit=limit)


@router.get("/runs/{run_id}")
def run_chain(run_id: str, limit: int = 2000):
    out = L.timeline(run_id, limit=limit)
    if not out.get("ok"):
        raise HTTPException(404, out.get("reason", "no such run"))
    return out


@router.get("/runs/{run_id}/verify")
def verify(run_id: str):
    return L.verify_chain(run_id)


@router.get("/runs/{run_id}/summary")
def run_summary(run_id: str):
    return L.summary(run_id)


@router.get("/runs/{run_id}/proofs/{seq}")
def verify_proof(run_id: str, seq: int):
    """Re-derive one event's proof from the stored row."""
    out = L.verify_proof(run_id, seq)
    if not out.get("ok") and out.get("reason") == "no such event":
        raise HTTPException(404, out["reason"])
    return out


@router.get("/runs/{run_id}/export")
def export(run_id: str):
    """A self-verifying bundle. Verifies offline with no database attached."""
    out = L.export_run(run_id)
    if not out.get("ok"):
        raise HTTPException(404, out.get("reason", "no such run"))
    return out


@router.post("/verify-export")
def verify_export(doc: dict = None):
    if not doc:
        raise HTTPException(400, "a bundle document is required")
    return L.verify_export(doc)


@router.get("/claims/{run_id}")
def claims(run_id: str, db: Session = Depends(get_db)):
    from .ledger_models import LedgerClaim
    rows = (db.query(LedgerClaim)
            .filter(LedgerClaim.run_id == run_id)
            .order_by(LedgerClaim.id).all())
    return {"claims": [
        {"claim_hash": c.claim_hash, "seq": c.seq, "actor": c.actor,
         "text": c.text, "citations": json.loads(c.citations_json or "[]"),
         "n_sources": c.n_sources, "confidence": c.confidence,
         "verdict": c.verdict, "verifier": c.verifier,
         "created_at": c.created_at} for c in rows]}


@router.get("/runs/{run_id}/contamination/{ref}")
def contamination(run_id: str, ref: str):
    """Blast radius: what consumed this hash, transitively."""
    if not L.is_hash(ref):
        raise HTTPException(400, "ref must be a sha256")
    return L.contamination(run_id, ref)


@router.get("/approvals")
def approvals(run_id: str | None = None, status: str | None = None,
              db: Session = Depends(get_db)):
    q = db.query(LedgerApproval)
    if run_id:
        q = q.filter(LedgerApproval.run_id == run_id)
    if status == "pending":
        q = q.filter(LedgerApproval.decision.is_(None))
    elif status:
        q = q.filter(LedgerApproval.decision == status)
    rows = q.order_by(LedgerApproval.id.desc()).limit(200).all()
    return {"approvals": [
        {"id": a.id, "run_id": a.run_id, "seq": a.seq, "kind": a.kind,
         "subject_hash": a.subject_hash, "summary": a.summary,
         "requested_by": a.requested_by, "requested_at": a.requested_at,
         "decision": a.decision, "decided_by": a.decided_by,
         "decided_at": a.decided_at, "note": a.note, "event_hash": a.event_hash}
        for a in rows]}


class DecisionIn(dict):
    pass


@router.post("/approvals/{approval_id}/decide")
def decide(approval_id: int, body: dict):
    decision = body.get("decision")
    if decision not in ("allow", "grant", "accept", "deny"):
        raise HTTPException(400, "decision must be allow or deny")
    out = L.decide_approval(approval_id, decision,
                            decided_by=body.get("decided_by", "operator"),
                            note=body.get("note", ""))
    if not out.get("ok"):
        raise HTTPException(404, out.get("reason"))
    return out


@router.get("/drops")
def drops(limit: int = 100, db: Session = Depends(get_db)):
    """Audit writes that failed. The chain can verify while this is non-empty."""
    rows = (db.query(LedgerAuditDrop)
            .order_by(LedgerAuditDrop.id.desc()).limit(limit).all())
    return {"drops": [
        {"id": d.id, "ts": d.ts, "run_id": d.run_id, "recorder": d.recorder,
         "actor": d.actor, "error": d.error} for d in rows],
        "count": db.query(LedgerAuditDrop).count()}


@router.get("/runs")
def list_runs(limit: int = 100, db: Session = Depends(get_db)):
    rows = (db.query(LedgerRun)
            .filter(LedgerRun.kind == "task")
            .order_by(LedgerRun.created_at.desc()).limit(limit).all())
    return {"runs": [
        {"id": r.id, "label": r.label, "session_id": r.session_id,
         "status": r.status, "created_at": r.created_at,
         "events": (r.head_seq + 1) if r.head_seq is not None else 0,
         "head_hash": r.head_hash} for r in rows]}


@router.get("/events/{event_hash}")
def event_detail(event_hash: str, db: Session = Depends(get_db)):
    row = (db.query(LedgerEvent)
           .filter(LedgerEvent.hash == event_hash).first())
    if row is None:
        raise HTTPException(404, "no such event")
    return {"run_id": row.run_id, "seq": row.seq, "ts": row.ts,
            "actor_type": row.actor_type, "actor": row.actor, "kind": row.kind,
            "intent": row.intent, "verdict": row.verdict,
            "severity": row.severity,
            "data": json.loads(row.data_json or "{}"),
            "input_refs": json.loads(row.input_refs or "[]"),
            "prev_hash": row.prev_hash, "hash": row.hash, "trace": row.trace}


@router.get("/siem-export")
def siem_export(run_id: str | None = None, since: int = 0, limit: int = 200,
                db: Session = Depends(get_db)):
    """Stream the ledger to a SIEM as JSON lines with a cursor.

    One cursor covers every chain: poll with the returned ``cursor`` and each
    event arrives exactly once. Payloads are minimised (digests, never
    prompts) — the fabric sees the decision without the data behind it.
    """
    from . import siem as _siem
    return _siem.export_siem_events(
        run_ids=[run_id] if run_id else None, since=since, limit=limit, db=db)


@router.get("/siem-export/text")
def siem_export_text(run_id: str | None = None, since: int = 0, limit: int = 200,
                     db: Session = Depends(get_db)):
    """The same export as newline-delimited JSON, for dumb pipes."""
    from fastapi.responses import PlainTextResponse

    from . import siem as _siem
    out = _siem.export_siem_events(
        run_ids=[run_id] if run_id else None, since=since, limit=limit, db=db)
    return PlainTextResponse("\n".join(out["lines"]),
                             media_type="application/x-ndjson")
