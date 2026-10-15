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
import re

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
        parent_id: int | None = None, include_web: bool = True,
        resume_id: int | None = None) -> int:
    """Run the whole Ask pipeline. Returns the explanation id.

    ``resume_id`` answers into an existing row — how an imported question
    (stored with status ``imported`` and no answer) becomes a real answer
    without duplicating the thread. The row keeps its parent, thread, tags
    and group; only the answer payload is produced.
    """
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
        if resume_id is not None:
            exp = db.query(Explanation).filter(
                Explanation.id == resume_id).first()
            if exp is None:
                raise ValueError("no such explanation to resume")
            home = db.query(Investigation).filter(
                Investigation.id == exp.investigation_id).first()
            if home is None:
                raise ValueError("resumed answer has no investigation")
            if question.strip():
                exp.question = question.strip()[:2000]
            question = exp.question
            exp.status = "running"
            db.commit()
            exp_id = exp.id
        else:
            if not question.strip():
                raise ValueError("question is required")
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
                no_answer = ("Nothing collected and nothing on the live web "
                             "speaks to this question, so I will not answer it "
                             "from memory. It has been filed as a research gap.")
                if resume_id is not None:
                    # A resumed row keeps its lineage: update in place rather
                    # than delete-and-recreate, which would orphan its replies.
                    exp.answer = no_answer
                    exp.status = "no_evidence"
                    exp.sources_json = json.dumps([])
                    exp.domains_json = json.dumps([])
                    exp.trace = json.dumps({"ask": True,
                                            "scope": scope or "all",
                                            "sources": 0})
                    db.commit()
                    return exp.id
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
            try:
                prior_trace = json.loads(exp.trace or "{}")
            except Exception:
                prior_trace = {}
            trace = {
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
            }
            # Import tags and group describe the question, not the run: an
            # answered import keeps them, or the threads list loses the
            # grouping the moment anyone presses Answer.
            for k in ("imported", "import_tags", "import_group",
                      "imported_at"):
                if k in prior_trace:
                    trace[k] = prior_trace[k]
            exp.trace = json.dumps(trace)
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


def _trace_of(e) -> dict:
    try:
        t = json.loads(e.trace or "{}")
        return t if isinstance(t, dict) else {}
    except Exception:
        return {}


def list_threads(db, investigation_id: int | None = None,
                 limit: int = 30, group: str | None = None) -> dict:
    """Thread roots, newest first, with shape: children count, depth, status.

    Each item carries its import ``group`` and ``tags`` (empty for pipeline
    answers), and the response lists every group with its count so the UI can
    filter without a second round trip. Groups are scanned over the latest
    200 roots — bounded, because an unbounded DISTINCT over JSON text is a
    full table walk dressed as metadata.
    """
    from sqlalchemy import func

    from .models import Explanation as _E
    q = db.query(_E).filter(_E.parent_id.is_(None))
    if investigation_id is not None:
        q = q.filter(_E.investigation_id == investigation_id)
    roots = q.order_by(_E.id.desc()).limit(limit).all()
    groups: dict[str, int] = {}
    for r in db.query(_E).filter(_E.parent_id.is_(None)).order_by(
            _E.id.desc()).limit(200).all():
        g = _trace_of(r).get("import_group") or (
            "imported" if _trace_of(r).get("imported") else "asked")
        groups[g] = groups.get(g, 0) + 1
    out = []
    for r in roots:
        # Descendants carry thread_id == root id; the root itself has
        # thread_id NULL, so no off-by-one subtraction is needed. Counting
        # thread membership instead of direct children is what makes nested
        # branches count too — a reply to a reply is still part of the thread.
        n = db.query(func.count(_E.id)).filter(
            _E.thread_id == r.id).scalar() or 0
        t = _trace_of(r)
        gname = t.get("import_group") or (
            "imported" if t.get("imported") else "asked")
        if group is not None and gname != group:
            continue
        out.append({
            "id": r.id, "thread_id": r.thread_id or r.id,
            "question": r.question, "status": r.status, "mode": r.mode,
            "confidence": r.confidence,
            "domains": _loads(r.domains_json, []),
            "group": gname, "tags": t.get("import_tags", []),
            "imported": bool(t.get("imported")),
            "investigation_id": r.investigation_id,
            "created_at": r.created_at, "replies": n,
        })
    return {"threads": out, "count": len(out), "groups": [
        {"group": g, "count": c} for g, c in
        sorted(groups.items(), key=lambda kv: -kv[1])]}


def _loads(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _loads(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


# --------------------------------------------------------------------------
# History import: Google AI Mode / Takeout search history + conversations
# --------------------------------------------------------------------------
# There is no API for AI Mode history — the way in is a Takeout export file.
# Takeout shapes drift (My Activity Search vs AI Mode vs plain lists), so the
# parser accepts every shape seen in the wild and reports what it skipped
# rather than failing the file: a half-parsed history beats a rejected one,
# and the skip count says which half.
_TAG_DISPLAY = {
    "imf": "IMF", "bis": "BIS", "oecd": "OECD", "ecb": "ECB", "fed": "Fed",
    "boe": "BoE", "boj": "BoJ", "pboc": "PBoC", "wto": "WTO", "ustr": "USTR",
    "nato": "NATO", "brics": "BRICS", "g7": "G7", "g20": "G20",
    "suez": "Suez Canal", "hormuz": "Strait of Hormuz", "sec": "SEC",
    "treasury": "US Treasury", "opec": "OPEC", "iea": "IEA", "eia": "EIA",
    "ferc": "FERC", "lbnl": "Berkeley Lab",
}


def parse_history_file(content: bytes, filename: str = "") -> dict:
    """Parse an exported history file into [{question, answer, asked_at}].

    Accepts Takeout Search JSON (title/titleUrl with q=), query objects,
    Q&A conversation pairs, and plain one-query-per-line text. Anything
    unrecognised is counted in ``skipped``, never fatal.
    """
    name = (filename or "").lower()
    text = (content or b"").decode("utf-8", errors="replace")
    items: list[dict] = []
    skipped = 0
    if name.endswith(".json") or text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except Exception:
            return {"items": [], "skipped": 1,
                    "reason": "not parseable JSON"}
        entries = doc
        if isinstance(doc, dict):
            for key in ("items", "history", "activities", "events",
                        "conversations", "queries"):
                if isinstance(doc.get(key), list):
                    entries = doc[key]
                    break
            else:
                entries = [doc]
        if not isinstance(entries, list):
            entries = [entries]
        for e in entries:
            if not isinstance(e, dict):
                if isinstance(e, str) and e.strip():
                    items.append({"question": e.strip(), "answer": None,
                                  "asked_at": None})
                else:
                    skipped += 1
                continue
            q, a, when = _entry_parts(e)
            if q:
                items.append({"question": q, "answer": a, "asked_at": when})
            else:
                skipped += 1
    else:
        for line in text.splitlines():
            line = line.strip().lstrip("\ufeff")
            if line:
                items.append({"question": line[:2000], "answer": None,
                              "asked_at": None})
            # Blank lines are separators, not skips.
    return {"items": items, "skipped": skipped}


def _entry_parts(e: dict) -> tuple[str | None, str | None, str | None]:
    """One Takeout-ish entry -> (question, answer, asked_at)."""
    when = None
    for k in ("time", "timestamp", "asked_at", "created", "date"):
        v = e.get(k)
        if isinstance(v, str) and v.strip():
            when = v.strip()[:40]
            break
    answer = None
    for k in ("answer", "response", "answer_text", "model_response"):
        v = e.get(k)
        if isinstance(v, dict):
            v = v.get("text")
        # First key present wins, but an absent key must not stop the search:
        # breaking unconditionally here once dropped every "response"-shaped
        # conversation while keeping the "answer"-shaped ones.
        if isinstance(v, str) and v.strip():
            answer = v.strip()[:20000]
            break
    # Q&A pair shape first — it names both sides explicitly.
    for k in ("question", "query_text", "prompt"):
        v = e.get(k)
        if isinstance(v, dict):
            v = v.get("query_text") or v.get("text")
        if isinstance(v, str) and v.strip():
            return v.strip()[:2000], answer, when
    q = e.get("query")
    if isinstance(q, dict):
        q = q.get("query_text") or q.get("text")
    if isinstance(q, str) and q.strip():
        return q.strip()[:2000], answer, when
    title = e.get("title")
    if isinstance(title, str) and title.strip():
        # Takeout Search rows carry the query in titleUrl's q= param; the
        # title itself ("Searched for ...") is the fallback, not the prize.
        url = e.get("titleUrl") or e.get("url") or ""
        m = re.search(r"[?&]q=([^&]+)", str(url))
        if m:
            try:
                from urllib.parse import unquote_plus
                decoded = unquote_plus(m.group(1)).strip()
                if decoded:
                    return decoded[:2000], answer, when
            except Exception:
                pass
        cleaned = re.sub(r"^(searched for|visited|asked)\s+", "", title.strip(),
                         flags=re.I)
        if cleaned:
            return cleaned[:2000], answer, when
    text = e.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()[:2000], answer, when
    return None, None, None


def auto_tags(text: str) -> list[str]:
    """Content-derived tags: taxonomy domains, named actors, drivers.

    Deterministic over the taxonomy — no model, so tagging ten thousand
    imported queries costs nothing and never varies between runs.
    """
    from . import explainer as _expl
    tags: list[str] = []
    for d in _expl._domains_in_text(text or ""):
        label = D.domain_label(d)
        if label not in tags:
            tags.append(label)
    low = f" {(text or '').lower()} "
    for alias, display in _TAG_DISPLAY.items():
        if re.search(r"\b" + re.escape(alias) + r"\b", low):
            if display not in tags:
                tags.append(display)
            continue
        expansion = D.ENTITY_ALIASES.get(alias, "")
        if expansion and expansion in low and display not in tags:
            tags.append(display)
    for drv in D.DRIVERS.values():
        label = drv["label"].lower()
        if label in low and drv["label"] not in tags:
            tags.append(drv["label"])
    return tags[:12]


def auto_group(text: str, domains: list[str]) -> str:
    """One group label per thread: the best-fitting driver, else the domains.

    A thread list grouped by thirty distinct labels is not grouped at all, so
    drivers (sixteen, stable) win over domain pairs whenever one fits.
    """
    doms = [d for d in (domains or []) if d in D.DOMAINS]
    best, best_overlap = None, 0
    for _key, drv in D.DRIVERS.items():
        overlap = len(set(drv["domains"]) & set(doms))
        if overlap > best_overlap:
            best, best_overlap = drv["label"], overlap
    if best:
        return best
    if len(doms) == 1:
        return D.domain_label(doms[0])
    if doms:
        shorts = [D.DOMAINS[d]["short"] for d in doms[:2]]
        return " + ".join(shorts)
    return "unclassified"


def _norm_question(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().strip().strip("?."))[:500]


def import_history(db, items: list[dict]) -> dict:
    """Store parsed history as tagged, grouped threads in the archive.

    Dedupe is by normalized question: re-importing the same Takeout file is
    a no-op for rows already here, so the count honestly reports what is new.
    Imported answers are stored verbatim and never auto-saved to the graph —
    they are ungrounded external text, and a pre-accepted projection artifact
    would launder them into evidence.
    """
    from .models import Explanation as _E
    archive = get_archive(db)
    existing = {_norm_question(e.question or "")
                for e in db.query(_E).filter(
                    _E.investigation_id == archive.id).all()}
    created = skipped = answered = 0
    for item in items or []:
        q = (item.get("question") or "").strip()
        if not q:
            skipped += 1
            continue
        if _norm_question(q) in existing:
            skipped += 1
            continue
        answer = (item.get("answer") or "").strip() or ""
        text = f"{q}\n{answer}" if answer else q
        from . import explainer as _expl
        domains = [d for d in _expl._domains_in_text(text)
                   if d in D.DOMAINS]
        tags = auto_tags(text)
        group = auto_group(text, domains)
        row = _E(
            investigation_id=archive.id, question=q[:2000],
            answer=answer[:20000] if answer else "",
            mode="explain", depth="balanced", audience="intermediate",
            status="imported", domains_json=json.dumps(sorted(domains)),
            sources_json=json.dumps([]),
            trace=json.dumps({
                "imported": True,
                "import_tags": tags,
                "import_group": group,
                "imported_at": item.get("asked_at"),
            }))
        db.add(row)
        existing.add(_norm_question(q))
        created += 1
        if answer:
            answered += 1
    db.commit()
    return {"created": created, "skipped": skipped, "answered": answered,
            "archive_id": archive.id}


def thread_root_id(db, explanation_id: int) -> int | None:
    """Climb to the thread root. A tree is addressed by any member."""
    from .models import Explanation as _E
    e = db.query(_E).filter(_E.id == explanation_id).first()
    if e is None:
        return None
    seen = {e.id}
    while e.parent_id is not None and e.parent_id not in seen:
        seen.add(e.parent_id)
        e = db.query(_E).filter(_E.id == e.parent_id).first()
        if e is None:
            return None
    return e.id


def thread_graph(db, explanation_id: int) -> dict | None:
    """One thread as a knowledge graph, next to the thread view.

    Three node kinds, namespaced so they cannot collide: ``e:`` messages,
    ``a:`` collected artifacts they cite, ``w:`` live-web sources. Edges are
    the conversation (reply_to), the grounding (cites), and the evidence
    structure itself (the transmits_to/support edges already on the cited
    artifacts). A thread whose answers cite nothing still graphs — messages
    joined by replies — because the conversation is the content then.
    """
    from . import domains as _D
    from .models import Explanation as _E
    from .models import Relationship as _R
    root_id = thread_root_id(db, explanation_id)
    if root_id is None:
        return None
    members = [root_id]
    frontier = [root_id]
    seen = {root_id}
    while frontier:
        kids = (db.query(_E.id).filter(_E.parent_id.in_(frontier)).all())
        frontier = [k[0] for k in kids if k[0] not in seen]
        seen.update(frontier)
        members.extend(frontier)
    rows = (db.query(_E).filter(_E.id.in_(members)).all())
    by_id = {e.id: e for e in rows}

    nodes: list[dict] = []
    edges: list[dict] = []
    cited: dict[int, dict] = {}
    for e in rows:
        nodes.append({
            "id": f"e:{e.id}",
            "label": (e.question or "")[:70] or f"answer {e.id}",
            "title": e.question or "",
            "group": "message",
            "color": {"background": "#58a6ff", "border": "#58a6ff"},
            "value": 2,
            "status": e.status,
            "confidence": e.confidence,
        })
        if e.parent_id is not None and e.parent_id in by_id:
            edges.append({
                "id": f"r:{e.parent_id}:{e.id}",
                "from": f"e:{e.parent_id}", "to": f"e:{e.id}",
                "label": "reply_to", "title": "follow-up",
                "color": {"color": "#8b949e"}, "dashes": True,
            })
        try:
            sources = json.loads(e.sources_json or "[]")
        except Exception:
            sources = []
        for s in sources:
            if not isinstance(s, dict):
                continue
            aid = s.get("artifact_id")
            if aid is not None:
                cited.setdefault(aid, {
                    "title": s.get("title", ""), "domain": s.get("domain"),
                    "source": s.get("source", "")})
                edges.append({
                    "id": f"c:{e.id}:{s.get('index')}",
                    "from": f"e:{e.id}", "to": f"a:{aid}",
                    "label": "cites", "title": s.get("title", "")[:120],
                    "color": {"color": "#3fb950"},
                })
            elif s.get("url"):
                wid = f"w:{e.id}:{s.get('index')}"
                nodes.append({
                    "id": wid,
                    "label": (s.get("title") or s.get("url", ""))[:60],
                    "title": s.get("url", ""),
                    "group": "web",
                    "color": {"background": "#d29922",
                              "border": "#d29922"},
                    "value": 1,
                    "url": s.get("url", ""),
                })
                edges.append({
                    "id": f"cw:{e.id}:{s.get('index')}",
                    "from": f"e:{e.id}", "to": wid,
                    "label": "cites", "title": "live web — unvetted",
                    "color": {"color": "#d29922"}, "dashes": True,
                })

    if cited:
        arts = {a.id: a for a in
                db.query(Artifact).filter(Artifact.id.in_(list(cited))).all()}
        for aid, meta in cited.items():
            a = arts.get(aid)
            dom = (a.domain if a else None) or meta.get("domain")
            nodes.append({
                "id": f"a:{aid}",
                "label": ((a.title if a else meta.get("title")) or "")[:60],
                "title": (a.title if a else meta.get("title")) or "",
                "group": "artifact",
                "domain": dom,
                "color": {"background": _D.domain_color(dom),
                          "border": _D.domain_color(dom)},
                "value": 2,
                "review": a.review if a else "unknown",
            })
        for r in db.query(_R).filter(
                _R.source_id.in_(list(cited)),
                _R.target_id.in_(list(cited))).all():
            meta = _D.RELATIONSHIP_TYPES.get(r.relationship_type or "", {})
            edges.append({
                "id": f"e:{r.id}", "from": f"a:{r.source_id}",
                "to": f"a:{r.target_id}",
                "label": r.relationship_type or "",
                "title": (r.description or "")[:200],
                "color": {"color": meta.get("color", "#8b949e")},
                "dashes": bool(meta.get("dashes")),
            })
    return {
        "thread_id": root_id,
        "nodes": nodes, "edges": edges,
        "counts": {
            "messages": sum(1 for n in nodes if n["group"] == "message"),
            "artifacts": sum(1 for n in nodes if n["group"] == "artifact"),
            "web": sum(1 for n in nodes if n["group"] == "web"),
        },
    }
