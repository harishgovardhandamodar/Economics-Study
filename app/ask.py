"""Ask: estate-wide questions answered from collected evidence plus the live web.

An investigation asks one question over time. Ask answers any question right
now, against everything collected everywhere, plus a live web supplement for
what the corpus does not hold yet. Every answer is a first-class explanation
row — grounded, critiqued, threaded — so nothing about Ask is a side channel:
threads continue, branch, promote into investigations, and land on the graph
exactly like investigation-scoped answers do.

Three decisions shape the module:

* **Archive, not NULL.** Estate-wide answers live under a hidden "Ask archive"
  investigation rather than a NULL foreign key. A NULL would quietly exempt
  answers from every per-investigation query (coverage, gaps, dossier); an
  archive keeps them queryable and out of the sidebar at the same time.
* **Web is supplement, marked.** Live results are labeled ``origin: web`` in
  the source block and the prompt says they are unvetted. A grounded answer
  that cannot tell collected evidence from a search snippet is not grounded.
* **Diagrams are derived, not drafted.** The channel map is built from the
  answer's touched domains and the taxonomy — the model never draws, so the
  diagram cannot assert a channel the evidence does not support.
"""
from __future__ import annotations

import json

from . import domains as D
from . import grounding as G
from . import ledger as L
from .database import SessionLocal
from .models import Artifact, Explanation, Investigation

ASK_ARCHIVE_TITLE = "Ask archive"
WEB_SOURCES = 6


def get_archive(db) -> Investigation:
    """The hidden home of estate-wide answers. Get-or-create: two processes
    racing here must not produce two archives."""
    inv = (db.query(Investigation)
           .filter(Investigation.title == ASK_ARCHIVE_TITLE).first())
    if inv is None:
        try:
            inv = Investigation(
                title=ASK_ARCHIVE_TITLE,
                keywords="ask, questions, threads",
                description="Estate-wide answers asked from the Ask tab. "
                            "Hidden by design: threads live here until one is "
                            "promoted into a real investigation.",
                sources="rss,web", hidden=True, auto_save_explanations=True)
            db.add(inv)
            db.commit()
        except Exception:
            db.rollback()
            inv = (db.query(Investigation)
                   .filter(Investigation.title == ASK_ARCHIVE_TITLE).first())
    return inv


def resolve_scope(db, scope: str | None) -> tuple[Investigation, list[Artifact], str]:
    """Scope is ``all`` (default) or ``inv:<id>``. Returns the home
    investigation, the candidate rows, and a human label for the trace."""
    if scope and scope.startswith("inv:"):
        try:
            inv_id = int(scope[4:])
        except ValueError as ve:
            raise ValueError("scope must be 'all' or 'inv:<id>'") from ve
        inv = db.query(Investigation).filter(Investigation.id == inv_id).first()
        if inv is None:
            raise ValueError("no such investigation")
        rows = (db.query(Artifact)
                .filter(Artifact.investigation_id == inv.id,
                        Artifact.review != "rejected").all())
        return inv, rows, f"investigation '{inv.title[:60]}'"
    archive = get_archive(db)
    rows = (db.query(Artifact)
            .filter(Artifact.review != "rejected").all())
    return archive, rows, "all collections"


def _web_sources(question: str, start_index: int) -> list[dict]:
    """Live web supplement. Best-effort: a dead search engine degrades to
    collected evidence only, never to a failed answer."""
    try:
        from . import search as providers
        hits = providers.search_web(question, max_results=WEB_SOURCES)
    except Exception:
        return []
    out = []
    for k, h in enumerate(hits or []):
        text = f"{h.get('title', '')}. {h.get('description', '')}"[:2000]
        if not text.strip(" ."):
            continue
        out.append({
            "index": start_index + k,
            "artifact_id": None,
            "title": (h.get("title") or "(untitled)")[:300],
            "source": h.get("source") or "web",
            "url": h.get("url", ""),
            "date": "",
            "domain": None,
            "direction": None, "magnitude": None, "horizon": None,
            "summary": (h.get("description") or "")[:600],
            "text": text[:3000],
            "quote_verifiable": True,
            "origin": "web",
        })
    return out


def channel_diagram(domains: list[str]) -> str:
    """Deterministic channel map over the answer's touched domains.

    Nodes for every touched domain, edges only where the taxonomy knows a
    channel between two of them. The model never draws, so the diagram cannot
    assert a hop the evidence does not support — an empty edge set is the
    honest output when the answer never crossed a domain.
    """
    touched = [d for d in (domains or []) if d in D.DOMAINS]
    if not touched:
        return ""
    lines = ["flowchart LR"]
    for d in touched:
        lines.append(f'    {d}["{D.domain_label(d)}"]')
    seen = set()
    for c in D.CHANNELS:
        if c["src"] in touched and c["dst"] in touched \
                and (c["src"], c["dst"]) not in seen:
            seen.add((c["src"], c["dst"]))
            lines.append(f'    {c["src"]} -->|"{c["channel"]}"| {c["dst"]}')
    if not seen:
        lines.append("    %% no known channel connects these domains")
    return "\n".join(lines)


def ask(question: str, *, scope: str | None = None, mode: str = "explain",
        depth: str = "balanced", audience: str = "intermediate",
        parent_id: int | None = None, include_web: bool = True) -> int:
    """Run the whole Ask pipeline. Returns the explanation id."""
    from . import explainer as _expl
    db = SessionLocal()
    try:
        home, rows, scope_label = resolve_scope(db, scope)
        if mode not in _expl.MODES:
            raise ValueError(f"unknown mode {mode}")
        thread_id = None
        if parent_id is not None:
            parent = (db.query(Explanation)
                      .filter(Explanation.id == parent_id).first())
            if parent is None:
                raise ValueError("no such parent explanation")
            thread_id = parent.thread_id or parent.id
        exp = Explanation(investigation_id=home.id, question=question,
                          mode=mode, depth=depth, audience=audience,
                          status="running", parent_id=parent_id,
                          thread_id=thread_id)
        db.add(exp)
        db.commit()
        exp_id = exp.id

        with L.scope(f"exp-{exp_id}",
                     L.Mandate(objective=f"ask: {question[:80]}",
                               allowed_actors=["agent", "llm", "system"]),
                     label=f"ask: {question[:60]}"):
            ranked = _expl.rank_artifacts(question, rows,
                                          limit=_expl.MAX_SOURCES)
            sources = _expl._source_block(ranked)
            web = _web_sources(question, len(sources) + 1) if include_web else []
            sources = sources + web
            domains_touched = sorted({s["domain"] for s in sources
                                      if s.get("domain")})
            L.record_agent_step(
                "ask.retrieve",
                f"{len(ranked)} collected + {len(web)} web over "
                f"{len(domains_touched)} domain(s) ({scope_label})",
                {"domains": domains_touched, "scope": scope or "all",
                 "web": len(web)})

            if not sources:
                db.query(Explanation).filter(
                    Explanation.id == exp_id).delete()
                from .models import ResearchGap
                db.query(ResearchGap).filter(
                    ResearchGap.investigation_id == home.id,
                    ResearchGap.dimension == question[:180]).delete()
                db.add(ResearchGap(
                    investigation_id=home.id, dimension=question[:180],
                    why="Nothing collected and nothing on the web spoke to "
                        "this question."))
                db.commit()
                exp2 = Explanation(
                    investigation_id=home.id, question=question, mode=mode,
                    depth=depth, audience=audience, status="no_evidence",
                    parent_id=parent_id, thread_id=thread_id,
                    answer=("Nothing collected and nothing on the live web "
                            "speaks to this question, so I will not answer it "
                            "from memory. It has been filed as a research gap."),
                    sources_json=json.dumps([]), domains_json=json.dumps([]),
                    trace=json.dumps({"ask": True, "scope": scope or "all",
                                      "sources": 0}))
                db.add(exp2)
                db.commit()
                return exp2.id

            answer = _expl._draft(question, sources, mode=mode, depth=depth,
                                  audience=audience, domains=domains_touched)
            ground = G.verify_answer(answer, sources)
            L.record_agent_step(
                "grounding.check",
                f"{ground['n_verified']}/{ground['n_citations']} citations "
                f"verified, {ground['dropped_citations']} dropped",
                {"verdict": ground["verdict"], "scope": scope or "all"})
            critique = _expl._critique(question, answer, ground) \
                if ground["citations"] else ""
            roadmap = _expl._roadmap(home.id, question, sources, ground)
            confidence = _expl._confidence(ground, sources)
            diagram = channel_diagram(domains_touched)
            exp.answer = answer
            exp.sources_json = json.dumps([
                {"index": s["index"], "artifact_id": s.get("artifact_id"),
                 "title": s["title"], "source": s["source"],
                 "url": s.get("url"), "domain": s.get("domain"),
                 "origin": s.get("origin", "collected")}
                for s in sources])
            exp.critique = critique
            exp.roadmap = roadmap
            exp.confidence = confidence
            exp.dropped_citations = ground["dropped_citations"]
            exp.domains_json = json.dumps(domains_touched)
            exp.trace = json.dumps({
                "ask": True, "scope": scope or "all",
                "scope_label": scope_label,
                "mode": mode, "depth": depth, "audience": audience,
                "sources": len(sources),
                "collected": len(ranked), "web": len(web),
                "diagram": diagram,
                "grounding": {k: v for k, v in ground.items()
                              if k not in ("citations", "dropped")},
                "dropped": ground["dropped"],
                "artifact_ids": [s["artifact_id"] for s in sources
                                 if s.get("artifact_id")],
            })
            exp.status = "done"
            db.commit()
            if home.auto_save_explanations:
                _expl._save_to_graph(db, home.id, exp)
                try:
                    from . import search_index as _si
                    db.refresh(exp)
                    _si.index_explanation(db, exp)
                except Exception:
                    pass
            return exp.id
    finally:
        db.close()


def promote(db, explanation_id: int, *, collect: bool = True) -> dict:
    """Promote a thread answer into a real investigation: the question becomes
    the brief, the answer lands on the new graph as its first artifact, and a
    collection run starts gathering what the answer cited but the map lacks.

    Promotion copies, never moves: the thread stays in the archive, so the
    conversation that produced the investigation is still readable afterwards.
    """
    from . import explainer as _expl
    e = db.query(Explanation).filter(
        Explanation.id == explanation_id).first()
    if e is None:
        raise ValueError("no such explanation")
    try:
        domains = json.loads(e.domains_json or "[]")
    except Exception:
        domains = []
    from .models import Investigation
    inv = Investigation(
        title=f"Ask: {e.question[:200]}",
        keywords=", ".join(D.domain_label(d) for d in domains if d)[:500],
        description=(f"Promoted from an Ask answer.\n\nQuestion: {e.question}\n\n"
                     f"Answer (confidence {e.confidence}):\n"
                     f"{(e.answer or '')[:1500]}"),
        sources="rss,web",
        preferred_domains=",".join(d for d in domains if d in D.DOMAINS),
        shock=None)
    db.add(inv)
    db.flush()
    _expl._save_to_graph(db, inv.id, e)
    trace = {}
    try:
        trace = json.loads(e.trace or "{}")
    except Exception:
        pass
    trace["promoted_to"] = inv.id
    e.trace = json.dumps(trace)
    db.commit()
    job = None
    if collect:
        from . import jobqueue
        job = jobqueue.enqueue("collect", {"investigation_id": inv.id,
                                           "max_items": 15, "max_rounds": 2,
                                           "trigger": "ask-promote"},
                               investigation_id=inv.id)
    try:
        from . import search_index as _si
        _si.index_investigation(db, inv)
    except Exception:
        pass
    return {"investigation_id": inv.id, "title": inv.title,
            "domains": domains, "job": job, "collect_queued": bool(job)}


def thread_tree(db, explanation_id: int, *, depth: int = 0,
                _seen: set | None = None) -> dict | None:
    """A thread as a nested tree, so branches render as branches.

    Two children of one parent is a fork in the conversation, not a list —
    flattening them loses which question each one answers. Cycles cannot occur
    (a parent always predates its child) but the guard is cheap insurance.
    """
    from .models import Explanation as _E
    _seen = _seen or set()
    if explanation_id in _seen or depth > 20:
        return None
    _seen.add(explanation_id)
    e = db.query(_E).filter(_E.id == explanation_id).first()
    if e is None:
        return None
    kids = (db.query(_E).filter(_E.parent_id == e.id)
            .order_by(_E.id).all())
    return {
        "id": e.id, "question": e.question, "answer": e.answer,
        "status": e.status, "mode": e.mode, "confidence": e.confidence,
        "domains": _loads(e.domains_json, []),
        "dropped_citations": e.dropped_citations,
        "bookmarked": bool(e.bookmarked), "watched": bool(e.watched),
        "created_at": e.created_at,
        "children": [c for c in
                     (thread_tree(db, k.id, depth=depth + 1, _seen=_seen)
                      for k in kids) if c],
    }


def list_threads(db, investigation_id: int | None = None,
                 limit: int = 30) -> dict:
    """Thread roots, newest first, with shape: children count, depth, status."""
    from sqlalchemy import func

    from .models import Explanation as _E
    q = db.query(_E).filter(_E.parent_id.is_(None))
    if investigation_id is not None:
        q = q.filter(_E.investigation_id == investigation_id)
    roots = q.order_by(_E.id.desc()).limit(limit).all()
    out = []
    for r in roots:
        # Descendants carry thread_id == root id; the root itself has
        # thread_id NULL, so no off-by-one subtraction is needed. Counting
        # thread membership instead of direct children is what makes nested
        # branches count too — a reply to a reply is still part of the thread.
        n = db.query(func.count(_E.id)).filter(
            _E.thread_id == r.id).scalar() or 0
        out.append({
            "id": r.id, "thread_id": r.thread_id or r.id,
            "question": r.question, "status": r.status, "mode": r.mode,
            "confidence": r.confidence,
            "domains": _loads(r.domains_json, []),
            "investigation_id": r.investigation_id,
            "created_at": r.created_at, "replies": n,
        })
    return {"threads": out, "count": len(out)}


def _loads(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default
