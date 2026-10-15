"""Full-text search over everything the app holds.

Artifacts, explanations, assessments and investigations in one ranked index,
so a question like "what do we have on stablecoin reserves" searches the
evidence *and* the answers, not just the evidence. SQLite FTS5 with BM25
ranking — no service, no dependency, and fast enough that reindexing the
whole estate is a button rather than a migration.

Two properties keep this honest:

* **The index is derived, never primary.** Every row points back at its source
  row by (doc_type, doc_id). Reindexing rebuilds from stored rows, so the
  index can never disagree with the database for longer than it takes to
  rebuild it.
* **Indexing never breaks collection.** Every write path wraps its index call
  so a failure there degrades to "this row is searchable after the next
  reindex", never to a failed run. Search freshness is best-effort; collection
  correctness is not negotiable.
"""
from __future__ import annotations

from sqlalchemy import text as _text

FTS_TABLE = "search_index_fts"

# doc_type -> (label, route helper for the UI)
DOC_TYPES = ("artifact", "explanation", "assessment", "investigation")


def _exec(db, sql: str, params: dict | None = None):
    return db.execute(_text(sql), params or {})


def ensure_table(db) -> bool:
    """Create the FTS table if FTS5 exists. Returns whether search is live."""
    try:
        _exec(db,
              f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
              "doc_type, doc_id UNINDEXED, investigation_id UNINDEXED, "
              "title, body, tokenize='porter')")
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _put(db, doc_type: str, doc_id: int, investigation_id: int | None,
         title: str, body: str) -> None:
    _exec(db, f"DELETE FROM {FTS_TABLE} WHERE doc_type=:t AND doc_id=:d",
          {"t": doc_type, "d": doc_id})
    _exec(db,
          f"INSERT INTO {FTS_TABLE} "
          "(doc_type, doc_id, investigation_id, title, body) "
          "VALUES (:t, :d, :i, :title, :body)",
          {"t": doc_type, "d": doc_id, "i": investigation_id,
           "title": title or "", "body": body or ""})


def delete_doc(db, doc_type: str, doc_id: int) -> None:
    try:
        _exec(db, f"DELETE FROM {FTS_TABLE} WHERE doc_type=:t AND doc_id=:d",
              {"t": doc_type, "d": doc_id})
        db.commit()
    except Exception:
        pass


def _artifact_text(a) -> tuple[str, str]:
    # The domain label and type are indexed as body text on purpose: a query
    # for "stablecoin reserves" must match a stablecoin-domain artifact even
    # when neither word appears in its title. The domain is what the analyst
    # classified, which makes it content, not metadata.
    from . import domains as _D
    parts = [a.description or "", a.content or "", a.tags or "",
             " ".join(a.entities_json or []), a.source or "",
             a.relevance_reason or "", _D.domain_label(a.domain),
             (a.artifact_type or "").replace("_", " ")]
    return a.title or "", "\n".join(p for p in parts if p)


def index_artifact(db, a) -> None:
    """Best-effort by contract: callers must not let this raise."""
    try:
        title, body = _artifact_text(a)
        _put(db, "artifact", a.id, a.investigation_id, title, body)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def index_explanation(db, e) -> None:
    try:
        body = "\n".join(x for x in
                         [e.answer or "", e.critique or "", e.roadmap or ""]
                         if x)
        _put(db, "explanation", e.id, e.investigation_id, e.question or "",
             body)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def index_assessment(db, r) -> None:
    try:
        _put(db, "assessment", r.id, r.investigation_id,
             f"Transmission assessment #{r.id} ({r.driver or 'no driver'})",
             r.markdown or "")
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def index_investigation(db, inv) -> None:
    try:
        _put(db, "investigation", inv.id, inv.id,
             inv.title or "",
             "\n".join(x for x in [inv.keywords or "", inv.description or "",
                                   inv.shock or ""] if x))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def reindex_all(db) -> dict:
    """Full rebuild from stored rows. The catch-up path and the honesty
    mechanism in one: afterwards the index *is* the database, by construction.
    """
    from .models import Artifact, EconomicAssessment, Explanation, Investigation
    if not ensure_table(db):
        return {"ok": False, "reason": "fts5 unavailable"}
    try:
        _exec(db, f"DELETE FROM {FTS_TABLE}")
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}
    counts = {"artifact": 0, "explanation": 0, "assessment": 0,
              "investigation": 0}
    for a in db.query(Artifact).filter(Artifact.review != "rejected").all():
        title, body = _artifact_text(a)
        _put(db, "artifact", a.id, a.investigation_id, title, body)
        counts["artifact"] += 1
    for e in db.query(Explanation).filter(
            Explanation.status.in_(("done", "imported"))).all():
        body = "\n".join(x for x in
                         [e.answer or "", e.critique or "", e.roadmap or ""]
                         if x)
        _put(db, "explanation", e.id, e.investigation_id, e.question or "",
             body)
        counts["explanation"] += 1
    for r in db.query(EconomicAssessment).all():
        _put(db, "assessment", r.id, r.investigation_id,
             f"Transmission assessment #{r.id} ({r.driver or 'no driver'})",
             r.markdown or "")
        counts["assessment"] += 1
    for inv in db.query(Investigation).all():
        _put(db, "investigation", inv.id, inv.id, inv.title or "",
             "\n".join(x for x in [inv.keywords or "", inv.description or "",
                                   inv.shock or ""] if x))
        counts["investigation"] += 1
    try:
        db.commit()
    except Exception:
        pass
    counts["ok"] = True
    counts["total"] = sum(v for k, v in counts.items() if k != "ok")
    return counts


def _match_expr(query: str) -> str:
    """Quote-escaped FTS match. Double quotes are doubled so a stray quote in
    the question cannot break the expression."""
    return '"' + (query or "").replace('"', '""') + '"'


def _fallback_expr(query: str) -> str:
    import re
    toks = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]{2,}", query or "")
            if len(t) > 2][:8]
    if not toks:
        return ""
    return " AND ".join(f'"{t}"' for t in toks)


def search(db, query: str, *, doc_type: str | None = None,
           investigation_id: int | None = None, limit: int = 20) -> dict:
    """Ranked full-text search with highlight snippets.

    ``snippet()`` marks hits with <mark>, so the UI shows *why* a row matched
    rather than just that it did. An FTS syntax error falls back to AND-joined
    tokens; an empty query is a 400 at the route, never an empty lie here.
    """
    if not (query or "").strip():
        return {"ok": False, "reason": "empty query", "results": []}
    if not ensure_table(db):
        return {"ok": False, "reason": "fts5 unavailable", "results": []}
    if doc_type and doc_type not in DOC_TYPES:
        return {"ok": False, "reason": f"unknown type {doc_type}",
                "results": []}
    clauses = ["search_index_fts MATCH :q"]
    params: dict = {"q": _match_expr(query)}
    if doc_type:
        clauses.append("doc_type = :t")
        params["t"] = doc_type
    if investigation_id is not None:
        clauses.append("investigation_id = :i")
        params["i"] = investigation_id
    where = " AND ".join(clauses)
    sql = (f"SELECT doc_type, doc_id, investigation_id, title, "
           f"snippet({FTS_TABLE}, 3, '<mark>', '</mark>', '…', 28) AS snip, "
           f"bm25({FTS_TABLE}) AS rank FROM {FTS_TABLE} "
           f"WHERE {where} ORDER BY rank LIMIT :n")
    params["n"] = max(1, min(limit, 100))
    try:
        rows = _exec(db, sql, params).fetchall()
    except Exception:
        # Raw user text is not a query language: retry as AND-joined tokens.
        db.rollback()
        params["q"] = _fallback_expr(query)
        if not params["q"]:
            return {"ok": True, "results": [], "query": query,
                    "fallback": True}
        try:
            rows = _exec(db, sql, params).fetchall()
        except Exception as e:
            db.rollback()
            return {"ok": False, "reason": str(e)[:200], "results": []}
    if not rows and len((query or "").split()) > 1:
        # A phrase that matches nothing is usually two ideas, not one quoted
        # phrase: retry as AND-joined tokens and say so, rather than
        # returning an empty page for a corpus that has both words.
        params["q"] = _fallback_expr(query)
        if params["q"]:
            try:
                rows = _exec(db, sql, params).fetchall()
                fallback_used = True
            except Exception:
                db.rollback()
                rows = []
                fallback_used = False
        else:
            fallback_used = False
    else:
        fallback_used = False
    return {
        "ok": True, "query": query,
        "fallback": fallback_used,
        "results": [
            {"doc_type": r[0], "doc_id": r[1], "investigation_id": r[2],
             "title": r[3], "snippet": r[4], "rank": round(r[5], 3)}
            for r in rows],
    }


def indexed_counts(db) -> dict:
    try:
        rows = _exec(
            db, f"SELECT doc_type, COUNT(*) FROM {FTS_TABLE} GROUP BY doc_type"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}
