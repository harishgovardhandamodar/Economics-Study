"""Drift detection: has the world moved out from under this map?

Two kinds of drift, and the distinction matters more than it sounds:

* **Artifact drift** — a source has changed since it was collected. A central
  bank corrects a rate decision, a company restates a filing. The stored
  artifact is now a record of something that was true when it was read.
* **Explanation drift** — the answer to a watched question has changed. That is
  the interesting one: the *same question* asked again returns a different
  answer, which means something in the underlying evidence moved.

Artifact drift is flagged, never deleted. A corrected figure is still evidence
about what was believed when, and a map that silently drops those rows cannot
show that a revision happened.
"""
import json
from datetime import UTC, datetime

from . import search as providers
from .database import SessionLocal
from .models import Artifact, Explanation

# A watched explanation is re-checked when the underlying sources have moved by
# more than this. Below it, re-running the question would return the same
# answer and the re-run would be noise.
MIN_NEW_ARTIFACTS = 3


def detect_artifact_drift(investigation_id: int, limit: int = 25,
                          recent_only: bool = True) -> dict:
    """Re-fetch the newest kept artifacts and flag the ones that changed."""
    db = SessionLocal()
    try:
        q = (db.query(Artifact)
             .filter(Artifact.investigation_id == investigation_id,
                     Artifact.review != "rejected",
                     Artifact.url.isnot(None)))
        if recent_only:
            q = q.order_by(Artifact.date_published.desc().nullslast())
        rows = q.limit(limit).all()

        checked, changed = 0, []
        for a in rows:
            page = providers.fetch_page_summary(a.url)
            checked += 1
            if not page.get("ok"):
                continue
            new_title = (page.get("title") or "").strip()
            if not new_title or not a.title:
                continue
            if _moved(a.title, new_title):
                a.drift = 1
                changed.append({"artifact_id": a.id, "title": a.title,
                                "now": new_title})
        db.commit()
        return {"ok": True, "checked": checked, "changed": len(changed),
                "details": changed}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def _moved(old: str, new: str) -> bool:
    """Did the headline change, or just the rendering?

    A re-fetch that only differs in whitespace, a trailing " | Site Name", or a
    tracking suffix is not drift. Comparing normalized prefixes rather than
    full equality is what keeps the flag meaningful — a flag that fires on
    every page whose <title> carries a site name is a flag people learn to
    ignore.
    """
    def norm(s):
        s = (s or "").lower().strip()
        for sep in (" | ", " - ", " — ", " :: "):
            if sep in s:
                s = s.split(sep)[0]
        return " ".join(s.split())[:80]

    a, b = norm(old), norm(new)
    if not a or not b:
        return False
    if a == b:
        return False
    # One is a prefix of the other: the headline gained or lost a suffix, which
    # is a render change rather than a content change.
    shorter, longer = sorted((a, b), key=len)
    if longer.startswith(shorter) and len(shorter) >= 20:
        return False
    return True


def clear_drift(artifact_id: int) -> dict:
    db = SessionLocal()
    try:
        a = db.query(Artifact).filter(Artifact.id == artifact_id).first()
        if a is None:
            return {"ok": False, "reason": "no such artifact"}
        a.drift = 0
        db.commit()
        return {"ok": True, "artifact_id": artifact_id, "drift": 0}
    finally:
        db.close()


def watched_explanations(investigation_id: int) -> list[dict]:
    """Watched questions whose evidence has moved since they were answered."""
    db = SessionLocal()
    try:
        rows = (db.query(Explanation)
                .filter(Explanation.investigation_id == investigation_id,
                        Explanation.watched.is_(True)).all())
        out = []
        for e in rows:
            trace = json.loads(e.trace or "{}")
            used = set(trace.get("artifact_ids") or [])
            new = (db.query(Artifact)
                   .filter(Artifact.investigation_id == investigation_id,
                           Artifact.review != "rejected",
                           Artifact.drift == 1).all())
            new_ids = {a.id for a in new} - used
            out.append({
                "explanation_id": e.id, "question": e.question,
                "answered_at": e.created_at,
                "sources": len(used),
                "new_since": len(new_ids),
                "ready_to_recheck": len(new_ids) >= MIN_NEW_ARTIFACTS,
            })
        return out
    finally:
        db.close()


def mark_watched(explanation_id: int, watched: bool) -> dict:
    db = SessionLocal()
    try:
        e = db.query(Explanation).filter(
            Explanation.id == explanation_id).first()
        if e is None:
            return {"ok": False, "reason": "no such explanation"}
        e.watched = bool(watched)
        db.commit()
        return {"ok": True, "explanation_id": explanation_id,
                "watched": bool(watched)}
    finally:
        db.close()


def drift_summary(investigation_id: int) -> dict:
    db = SessionLocal()
    try:
        flagged = (db.query(Artifact)
                   .filter(Artifact.investigation_id == investigation_id,
                           Artifact.drift == 1).all())
        watched = watched_explanations(investigation_id)
        return {
            "investigation_id": investigation_id,
            "flagged_artifacts": len(flagged),
            "flagged": [{"id": a.id, "title": a.title, "url": a.url,
                         "published": a.date_published, "review": a.review}
                        for a in flagged],
            "watched": watched,
            "ready_to_recheck": [w for w in watched if w["ready_to_recheck"]],
            "detected_at": datetime.now(UTC).isoformat(),
        }
    finally:
        db.close()
