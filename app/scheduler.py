"""Cron scheduling for recurring collection.

APScheduler with a jobstore that lives in *our* tables rather than its own: a
scheduled run must survive a restart, and a scheduler whose state is in memory
silently stops collecting the first time the process dies.
"""
import os
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .database import SessionLocal
from .models import Investigation

SCHEDULER_ENABLED = os.environ.get("ECON_SCHEDULER_ENABLED", "1") != "0"

_scheduler: BackgroundScheduler | None = None
_known: dict[int, str] = {}


def _db():
    return SessionLocal()


def cron_for(investigation_id: int) -> str | None:
    db = _db()
    try:
        inv = (db.query(Investigation)
               .filter(Investigation.id == investigation_id).first())
        return inv.schedule_cron if inv else None
    finally:
        db.close()


def set_schedule(investigation_id: int, enabled: bool, cron: str | None,
                 max_items: int = 25, max_rounds: int = 2) -> dict:
    """Replace an investigation's timetable.

    Validates the cron expression before writing it: a timetable that fails to
    parse must be reported at the point the operator set it, not discovered
    three weeks later when nothing has been collected.
    """
    db = _db()
    try:
        inv = (db.query(Investigation)
               .filter(Investigation.id == investigation_id).first())
        if inv is None:
            return {"ok": False, "reason": "no such investigation"}

        if enabled:
            if not cron:
                return {"ok": False, "reason": "a cron expression is required"}
            try:
                CronTrigger.from_crontab(cron)
            except Exception as e:
                return {"ok": False, "reason": f"invalid cron expression: {e}"}

        inv.schedule_enabled = bool(enabled)
        inv.schedule_cron = cron
        inv.schedule_max_items = int(max_items)
        inv.schedule_max_rounds = int(max_rounds)
        db.commit()
        _sync_job(inv.id)
        return {"ok": True, "investigation_id": investigation_id,
                "enabled": inv.schedule_enabled, "cron": cron,
                "next_run_at": _next_fire(cron) if enabled and cron else None}
    finally:
        db.close()


def _next_fire(cron: str | None) -> str | None:
    if not cron:
        return None
    try:
        return CronTrigger.from_crontab(cron).get_next_fire_time(
            None, datetime.now(UTC)).isoformat()
    except Exception:
        return None


def _fire(investigation_id: int) -> None:
    """Runs *through the queue*, never inline.

    A scheduled collection is the longest job in the app. If the scheduler
    thread ran it directly, one slow investigation would delay every other
    timetable behind it, and a crash would take the scheduler with it.
    """
    from . import jobqueue
    from .models import AgentRun, Investigation
    db = _db()
    try:
        inv = (db.query(Investigation)
               .filter(Investigation.id == investigation_id).first())
        if inv is None or not inv.schedule_enabled:
            return
        inv.last_scheduled_at = datetime.now(UTC)
        db.commit()
        jobqueue.enqueue("collect", {
            "investigation_id": investigation_id,
            "max_items": inv.schedule_max_items or 25,
            "max_rounds": inv.schedule_max_rounds or 2,
            "trigger": "schedule",
        }, investigation_id=investigation_id)
        _ = AgentRun  # imported so a future direct-run path stays importable
    finally:
        db.close()


def _job_id(investigation_id: int) -> str:
    return f"inv-{investigation_id}"


def _sync_job(investigation_id: int) -> None:
    """Bring one investigation's scheduler entry in line with its row."""
    if _scheduler is None or not SCHEDULER_ENABLED:
        return
    jid = _job_id(investigation_id)
    try:
        existing = _scheduler.get_job(jid)
        if existing:
            existing.remove()
        _known.pop(investigation_id, None)
    except Exception:
        pass
    cron = cron_for(investigation_id)
    if not cron:
        return
    db = _db()
    try:
        inv = (db.query(Investigation)
               .filter(Investigation.id == investigation_id).first())
        if inv is None or not inv.schedule_enabled:
            return
        _scheduler.add_job(_fire, CronTrigger.from_crontab(cron), args=[inv.id],
                           id=jid, replace_existing=True, max_instances=1,
                           coalesce=True,  # a backlog collapses to one run
                           misfire_grace_time=3600)
        _known[inv.id] = cron
    except Exception:
        pass
    finally:
        db.close()


def start() -> None:
    global _scheduler
    if not SCHEDULER_ENABLED or _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="UTC",
                                     job_defaults={"coalesce": True,
                                                   "max_instances": 1})
    _scheduler.start()
    sync_all()


def sync_all() -> int:
    db = _db()
    try:
        ids = [i.id for i in db.query(Investigation)
               .filter(Investigation.schedule_enabled.is_(True)).all()]
    finally:
        db.close()
    for i in ids:
        _sync_job(i)
    return len(ids)


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def state() -> dict:
    db = _db()
    try:
        rows = (db.query(Investigation)
                .filter(Investigation.schedule_enabled.is_(True)).all())
        return {
            "enabled": SCHEDULER_ENABLED,
            "running": _scheduler is not None and _scheduler.running,
            "scheduled": [
                {"investigation_id": r.id, "title": r.title, "cron": r.schedule_cron,
                 "last_scheduled_at": r.last_scheduled_at,
                 "next_run_at": _next_fire(r.schedule_cron)}
                for r in rows],
        }
    finally:
        db.close()
