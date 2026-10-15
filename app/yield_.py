"""What each *shape* of query has returned, kept across runs.

The planner works from a blank slate every run, so without this a shape that
returned nothing last time is proposed first again — the app would spend its
whole budget re-asking the question that already has an answer of "nothing".

The unit is the **shape**, not the query text: a shape is the normalized query
plus its source mix, so "central bank rate decision" asked against RSS and
asked against the web are two different shapes with two different histories.
"""
import re
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .models import Investigation, QueryShapeYield

_WORD = re.compile(r"[^a-z0-9]+")


def shape_of(query: dict) -> str:
    """Normalized identity of a query for yield accounting.

    Sorted, de-duplicated, lowercased content words. Sorting is deliberate: a
    shape is a *question*, and word order is not part of what makes it the same
    question.
    """
    text = (query.get("text") or "").lower()
    words = [w for w in _WORD.split(text) if w and len(w) > 2]
    return " ".join(sorted(set(words)))


def shape_key(query: dict) -> str:
    srcs = sorted(set(query.get("sources") or []))
    return f"{shape_of(query)}|{'+'.join(srcs)}"


def _row_for(db: Session, inv_id: int, key: str) -> QueryShapeYield | None:
    return db.query(QueryShapeYield).filter(
        QueryShapeYield.investigation_id == inv_id,
        QueryShapeYield.shape == key).first()


def record_yield(db: Session, inv_id: int, queries: list, *, found: int = 0,
                 kept: int = 0, llm_calls: int = 0) -> None:
    """Credit a round to the shapes that were asked.

    Called *before* any early exit below, so a round that analyzed candidates
    and kept none — the single most useful thing to remember — still leaves a
    trace.
    """
    if not queries:
        return
    found_share = found / max(1, len(queries))
    kept_share = kept / max(1, len(queries))
    call_share = llm_calls / max(1, len(queries))
    now = datetime.now(UTC)
    for q in queries:
        key = shape_key(q)
        row = _row_for(db, inv_id, key)
        if row is None:
            row = QueryShapeYield(
                investigation_id=inv_id, shape=key,
                raw_queries=(q.get("text") or "")[:600],
                sources=",".join(sorted(set(q.get("sources") or [])))[:120],
                created_at=now)
            db.add(row)
        row.times_used = (row.times_used or 0) + 1
        row.found = (row.found or 0) + found_share
        row.kept = (row.kept or 0) + kept_share
        row.llm_calls = (row.llm_calls or 0) + call_share
        row.last_used_at = now
    db.commit()


def rank_queries(db: Session, inv_id: int, queries: list) -> list:
    """Re-order proposals by what has historically worked. Order only —
    nothing is dropped: the planner's judgement about what to ask is not this
    function's to override."""
    scored = []
    for i, q in enumerate(queries):
        row = _row_for(db, inv_id, shape_key(q))
        if row is None:
            # Untested shapes keep planner order but sit after proven ones, so
            # a proven shape is spent before a coin-flip is.
            score = 0.0
        else:
            uses = max(1, row.times_used or 1)
            keep_rate = (row.kept or 0) / uses
            # Cost per kept item: a shape that burns four model calls for one
            # artifact is worse than one that gets three for the same spend.
            yield_rate = (row.kept or 0) / max(1.0, row.llm_calls or 1)
            # A shape that has been tried and returned nothing is demoted hard,
            # not merely scored lower — otherwise it still outranks a shape with
            # a genuinely unknown yield.
            score = (keep_rate * 2.0 + yield_rate * 3.0
                     - (1.0 if keep_rate == 0 and (row.times_used or 0) >= 2 else 0.0))
        scored.append((-score, i, q))
    scored.sort()
    return [q for _, _, q in scored]


def shape_stats(db: Session, inv_id: int) -> list[dict]:
    rows = (db.query(QueryShapeYield)
            .filter(QueryShapeYield.investigation_id == inv_id)
            .order_by(QueryShapeYield.kept.desc()).all())
    out = []
    for r in rows:
        uses = max(1, r.times_used or 1)
        kept = r.kept or 0
        out.append({
            "shape": r.shape,
            "query": r.raw_queries,
            "sources": r.sources,
            "times_used": r.times_used,
            "llm_calls": round(r.llm_calls or 0, 2),
            "found": round(r.found or 0, 1),
            "kept": round(kept, 1),
            # Named "per use" rather than "rate" because it is not a rate and
            # routinely exceeds 1.0: one well-shaped query can yield six
            # artifacts. Calling that a 600% keep rate reads as a bug.
            "kept_per_use": round(kept / uses, 3),
            # The complementary question: did this shape ever keep anything?
            "hit": kept > 0,
            "last_used_at": r.last_used_at,
            "created_at": r.created_at,
        })
    return out


def useless_shapes(db: Session, inv_id: int, min_uses: int = 2) -> list[str]:
    """Shapes tried at least twice that have never kept anything."""
    rows = (db.query(QueryShapeYield)
            .filter(QueryShapeYield.investigation_id == inv_id).all())
    return [r.shape for r in rows
            if (r.times_used or 0) >= min_uses and (r.kept or 0) == 0]


def investigation_yield(db: Session, inv_id: int) -> dict:
    inv = db.query(Investigation).filter(Investigation.id == inv_id).first()
    shapes = shape_stats(db, inv_id)
    return {
        "investigation_id": inv_id,
        "title": inv.title if inv else None,
        "shapes": shapes,
        "useless": useless_shapes(db, inv_id),
        "total_kept": round(sum(s["kept"] for s in shapes), 1),
        "best": shapes[0] if shapes else None,
    }
