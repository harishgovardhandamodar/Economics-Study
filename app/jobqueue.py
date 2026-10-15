"""A persisted, lease-based job queue.

Long work (a collection run, an explanation, a transmission analysis) must not
live in a request thread. A queue gives three things a thread cannot:

* **Restart survival.** A job is a row, not a stack frame.
* **Leases, not locks.** A worker claims a job by writing its own id and a
  deadline. A worker that dies stops renewing; the lease expires and the job is
  retried. Nothing has to notice that a process crashed.
* **Idempotent retries.** ``attempts`` is bounded by ``max_attempts`` and the
  payload is the same each time, so a retry does the same work.

A single worker thread is deliberate. This app runs on one machine for one
operator; the concurrency that matters is *within* a job (the search fan-out),
not between jobs.
"""
import json
import os
import threading
import traceback
from datetime import UTC, datetime, timedelta

from .database import SessionLocal
from .models import Job

LEASE_SECONDS = int(os.environ.get("ECON_JOB_LEASE_S", "900"))
WORKER_NAME = f"w-{threading.get_ident():x}"
# Off by default under test, so the test drains the queue itself. Two loops
# claiming jobs is harmless in production (the lease makes them safe) but makes
# a test nondeterministic, and a test that only sometimes sees the work it
# queued is worse than no test.
WORKER_ENABLED = os.environ.get("ECON_JOB_WORKER", "1") != "0"
_started = False
_lock = threading.Lock()


def _now():
    return datetime.now(UTC)


def enqueue(kind: str, payload: dict | None = None, *,
            investigation_id: int | None = None, priority: int = 0,
            max_attempts: int = 3, delay_s: int = 0) -> dict:
    db = SessionLocal()
    try:
        job = Job(kind=kind, payload_json=json.dumps(payload or {}),
                  investigation_id=investigation_id, priority=priority,
                  max_attempts=max_attempts, status="queued")
        if delay_s:
            # Backoff is expressed as a delayed lease rather than a status, so
            # the queue has exactly one way to decide "is this runnable".
            job.lease_until = _now() + timedelta(seconds=delay_s)
        db.add(job)
        db.commit()
        return {"job_id": job.id, "kind": kind, "status": job.status}
    finally:
        db.close()


def claim(kind: str | None = None) -> dict | None:
    """Claim one runnable job, or return None.

    Returns a plain dict, not an ORM row: the claim is handed to a different
    thread than the one that opened the session, so the row would be detached
    by the time it was read. The queue's contract crosses a thread boundary, so
    the queue's return value has to as well.

    Runs in a single transaction with ``with_for_update`` semantics emulated by
    the lease write, which is safe here because there is exactly one worker.
    """
    db = SessionLocal()
    try:
        now = _now()
        q = (db.query(Job)
             .filter(Job.status.in_(("queued", "running")))
             .filter((Job.lease_until.is_(None)) | (Job.lease_until <= now))
             .order_by(Job.priority.desc(), Job.id))
        if kind:
            q = q.filter(Job.kind == kind)
        job = q.first()
        if job is None:
            return None
        if job.status == "running" and job.attempts >= job.max_attempts:
            # An expired lease on a job that has used up its retries is a real
            # failure, not a job to hand out again.
            job.status = "error"
            job.error = "lease expired and retries exhausted"
            job.finished_at = now
            db.commit()
            return None
        job.status = "running"
        job.lease_owner = WORKER_NAME
        job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        job.attempts = (job.attempts or 0) + 1
        job.started_at = job.started_at or now
        # Snapshot everything the worker needs before the session closes.
        claimed = {"id": job.id, "kind": job.kind,
                   "payload": job.payload_json,
                   "attempts": job.attempts,
                   "investigation_id": job.investigation_id}
        db.commit()
        return claimed
    finally:
        db.close()


def renew(job_id: int) -> bool:
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job is None:
            return False
        job.lease_until = _now() + timedelta(seconds=LEASE_SECONDS)
        db.commit()
        return True
    except Exception:
        return False
    finally:
        db.close()


def finish(job_id: int, result: dict | None = None) -> None:
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job is None:
            return
        job.status = "done"
        job.result_json = json.dumps(result or {}) if result else None
        job.finished_at = _now()
        job.lease_until = None
        job.lease_owner = None
        db.commit()
    finally:
        db.close()


def fail(job_id: int, error: str) -> None:
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job is None:
            return
        job.error = f"{error}\n{traceback.format_exc()[-1500:]}"
        job.lease_until = None
        job.lease_owner = None
        if (job.attempts or 0) >= (job.max_attempts or 3):
            job.status = "error"
            job.finished_at = _now()
        else:
            job.status = "queued"
            # Exponential backoff, so a job that is failing because a dependency
            # is down does not spin and burn the rest of the queue's budget.
            job.lease_until = _now() + timedelta(
                seconds=min(300, 15 * (2 ** (job.attempts or 0))))
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
HANDLERS: dict = {}


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


@handler("collect")
def _run_collect(payload: dict) -> dict:
    from .agent import run_investigation_agent
    run_investigation_agent(
        payload["investigation_id"],
        max_items=int(payload.get("max_items", 25)),
        max_rounds=int(payload.get("max_rounds", 2)),
        trigger=payload.get("trigger", "queued"))
    return {"ok": True}


@handler("explain")
def _run_explain(payload: dict) -> dict:
    from .explainer import launch_explanation
    exp_id = launch_explanation(
        payload["investigation_id"], payload["question"],
        mode=payload.get("mode", "explain"),
        depth=payload.get("depth", "balanced"),
        audience=payload.get("audience", "intermediate"),
        parent_id=payload.get("parent_id"))
    from .database import SessionLocal
    from .models import Explanation
    db = SessionLocal()
    try:
        status = (db.query(Explanation.status)
                  .filter(Explanation.id == exp_id).scalar())
    finally:
        db.close()
    return {"explanation_id": exp_id, "status": status}


@handler("gaps")
def _run_gaps(payload: dict) -> dict:
    from .research_gaps import persist_gaps
    return persist_gaps(payload["investigation_id"])


@handler("ask")
def _run_ask(payload: dict) -> dict:
    from .ask import ask
    exp_id = ask(
        payload["question"], scope=payload.get("scope"),
        mode=payload.get("mode", "explain"),
        depth=payload.get("depth", "balanced"),
        audience=payload.get("audience", "intermediate"),
        parent_id=payload.get("parent_id"),
        include_web=payload.get("include_web", True))
    from .database import SessionLocal
    from .models import Explanation
    db = SessionLocal()
    try:
        status = (db.query(Explanation.status)
                  .filter(Explanation.id == exp_id).scalar())
    finally:
        db.close()
    return {"explanation_id": exp_id, "status": status}


@handler("assess")
def _run_assess(payload: dict) -> dict:
    import json as _json

    from .assessment import build_assessment
    from .database import SessionLocal
    from .models import EconomicAssessment
    db = SessionLocal()
    try:
        out = build_assessment(
            db, payload["investigation_id"],
            driver=payload.get("driver"), shock=payload.get("shock"),
            requested_by=payload.get("requested_by"))
        rec = EconomicAssessment(
            investigation_id=payload["investigation_id"],
            driver=out.get("driver"), shock=out.get("shock"),
            requested_by=payload.get("requested_by"),
            overall_pct=out["overall_pct"],
            coverage_pct=out["coverage_pct"],
            confidence_pct=out["confidence_pct"],
            exposure_pct=out["exposure_pct"],
            scoring_json=_json.dumps(out["scoring"]),
            hypotheses_json=_json.dumps(out["hypotheses"]),
            scenarios_json=_json.dumps(out["scenarios"]),
            channels_json=_json.dumps(out["channels"]),
            signals_json=_json.dumps(out["signals"]),
            markdown=out["markdown"],
            diagrams_json=_json.dumps(out["diagrams"]),
            taxonomy_version=out["taxonomy_version"],
            taxonomy_fingerprint=out["taxonomy_fingerprint"])
        db.add(rec)
        db.commit()
        try:
            from . import search_index as _si
            _si.index_assessment(db, rec)
        except Exception:
            pass
        return {"assessment_id": rec.id, "overall_pct": out["overall_pct"]}
    finally:
        db.close()


def run_once() -> bool:
    """Claim and run one job. Returns True if work was done."""
    job = claim()
    if job is None:
        return False
    fn = HANDLERS.get(job["kind"])
    if fn is None:
        fail(job["id"], f"no handler for kind '{job['kind']}'")
        return True
    try:
        payload = json.loads(job["payload"] or "{}")
        result = fn(payload)
        finish(job["id"], result if isinstance(result, dict)
                 else {"result": result})
    except Exception as e:
        fail(job["id"], str(e))
    return True


def _loop() -> None:
    import time
    while True:
        try:
            worked = run_once()
        except Exception:
            worked = False
        time.sleep(0.5 if worked else 3.0)


def start_worker() -> None:
    global _started
    if not WORKER_ENABLED:
        return
    with _lock:
        if _started:
            return
        _started = True
    t = threading.Thread(target=_loop, name="job-worker", daemon=True)
    t.start()


def recover_stale() -> int:
    """Re-queue jobs whose lease expired. Called at startup."""
    db = SessionLocal()
    try:
        now = _now()
        jobs = (db.query(Job)
                .filter(Job.status == "running",
                        Job.lease_until.isnot(None),
                        Job.lease_until <= now).all())
        for j in jobs:
            if (j.attempts or 0) >= (j.max_attempts or 3):
                j.status = "error"
                j.finished_at = now
            else:
                j.status = "queued"
                j.lease_owner = None
                j.lease_until = None
        db.commit()
        return len(jobs)
    finally:
        db.close()


def queue_state() -> dict:
    db = SessionLocal()
    try:
        rows = db.query(Job).order_by(Job.id.desc()).limit(50).all()
        counts: dict[str, int] = {}
        for j in db.query(Job).all():
            counts[j.status] = counts.get(j.status, 0) + 1
        return {
            "counts": counts,
            "worker": WORKER_NAME,
            "lease_seconds": LEASE_SECONDS,
            "jobs": [{"id": j.id, "kind": j.kind, "status": j.status,
                      "priority": j.priority, "attempts": j.attempts,
                      "investigation_id": j.investigation_id,
                      "error": (j.error or "")[:300] or None,
                      "created_at": j.created_at, "finished_at": j.finished_at}
                     for j in rows],
        }
    finally:
        db.close()
