"""The explainer: a grounded answer over an investigation.

Asks a question against what the investigation actually holds and returns an
answer that can only cite those sources. Everything the model writes is checked
afterwards — quotes verified verbatim, citation markers resolved, dropped
citations counted — because the only thing that makes an LLM answer worth
showing is that you can tell how much of it is load-bearing.

Pipeline::

    question + mode/depth/audience
      → RETRIEVE  rank artifacts by domain, entities, tags and text overlap
      → DRAFT     the model answers, citing [n] markers into the retrieved set
      → GROUND    verify_quote on every quote; drop unresolvable citations
      → CRITIQUE  a second pass marks each section SUPPORTED or UNCERTAIN
      → ROADMAP   what the question needs next, drawn from the taxonomy
      → STORE     the answer, its sources, and the counts

The critique pass is not decoration. It runs on the *grounded* answer, so it
sees what survived verification rather than the model's own account of itself,
and an answer whose claims all came back UNCERTAIN is visibly weak instead of
merely verbose.
"""
import json
import re

from sqlalchemy import and_
from sqlalchemy.orm import Session

from . import domains as D
from . import grounding as G
from . import ledger as L
from . import llm
from .database import SessionLocal
from .models import Artifact, Explanation, Investigation, ResearchGap

MODES = {
    "explain": "Explain the answer directly, plainly, and with the mechanism.",
    "deep-dive": "Go deep: mechanism, evidence, magnitudes, and what would "
                 "change the conclusion.",
    "compare": "Compare the positions or outcomes side by side, fairly, then "
               "say which the evidence favours.",
    "tutor": "Teach: build up from first principles, one step at a time, with "
             "a check on understanding at the end.",
    "critique": "Interrogate the answer: what is weak, what is assumed, what "
                "would falsify it.",
    "tldr": "Three sentences. What happened, why it matters, what to watch.",
    "glossary": "Define the terms the question uses, precisely.",
    "channel-map": "Map the transmission: which domain affects which, through "
                   "what channel, with what lag and what magnitude.",
}

AUDIENCE_HINTS = {
    "beginner": "The reader knows no economics jargon. Define every term on "
                "first use and prefer the concrete over the technical.",
    "intermediate": "The reader follows markets and macro but not every "
                    "specialism. Use standard vocabulary without defining the "
                    "obvious.",
    "advanced": "The reader wants the channel, the estimate and the "
                "uncertainty. Use precise terminology and quantify wherever "
                "the evidence allows.",
}

DEPTH_PLAN = {
    "shallow": "Two or three sentences. State the answer and the main driver.",
    "balanced": "A few paragraphs. Answer, mechanism, evidence, and the "
                "caveat that matters most.",
    "deep": "As long as the evidence supports. Cover each transmission "
            "channel separately, give magnitudes and horizons, and be explicit "
            "about what is unknown.",
}

MAX_SOURCES = 12


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------
_STOP = {"the", "and", "for", "with", "from", "that", "this", "what", "which",
         "about", "into", "how", "why", "does", "are", "was", "were", "will",
         "has", "have", "had", "can", "could", "should", "would", "than",
         "then", "they", "their", "its", "but", "not", "you", "our"}


def _terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9\-]{2,}", (text or "").lower())
            if w not in _STOP}


def rank_artifacts(question: str, rows: list[Artifact],
                   *, limit: int = MAX_SOURCES,
                   domain_hint: str | None = None) -> list[tuple[Artifact, float]]:
    """Rank an investigation's artifacts against a question.

    Three signals, in this order of trust:

    1. **Explicit domain match.** If the question names a domain the taxonomy
       knows, artifacts in it are the point of the question, and they lead.
    2. **Entity overlap.** A question about stablecoins and a document that
       names Circle should be connected even with no shared vocabulary.
    3. **Term overlap.** The fallback, and the weakest — which is why it is last
       rather than first.

    Already-accepted artifacts outrank pending ones at equal relevance: a
    reviewer accepting an artifact is the strongest relevance signal available
    and ignoring it would make human triage decorative.
    """
    q_terms = _terms(question)
    q_domains = _domains_in_text(question)
    if domain_hint:
        q_domains.add(domain_hint)
    q_lower = question.lower()

    scored = []
    for a in rows:
        text = f"{a.title} {a.description or ''} {a.tags or ''}"
        score = 0.0

        if a.domain and a.domain in q_domains:
            score += 6.0
        elif a.domain and a.domain in D.HUB_DOMAINS:
            score += 0.5

        entities = {str(e).lower() for e in (a.entities_json or [])}
        ent_hits = sum(1 for e in entities if len(e) > 3 and e in q_lower)
        score += ent_hits * 2.5

        a_terms = _terms(text)
        overlap = q_terms & a_terms
        score += len(overlap) * 1.0

        if a.direction and any(w in q_lower for w in
                               ("effect", "impact", "consequence", "what",
                                "happen", "transmit", "channel", "so what")):
            score += 0.5

        score += float(a.relevance or 0) * 1.5
        if a.review == "accepted":
            score += 1.5
        elif a.review == "rejected":
            score -= 3.0
        if a.drift:
            score -= 1.0

        if score > 0:
            scored.append((a, score))
    scored.sort(key=lambda kv: -kv[1])
    return scored[:limit]


def _domains_in_text(text: str) -> set[str]:
    """Which domains a *question* is about.

    Four signals, because a real question names its subjects in four different
    ways and only matching the obvious one would make the retrieval work on
    the easy half of the corpus:

    1. the domain id or label, in any obvious plural or singular form;
    2. the domain *short* name ("Sov. debt");
    3. a **driver** — "rate decision", "stablecoin flow", "export control" are
       how people actually ask, and each driver already declares the domains it
       moves;
    4. a channel's leading word — "the duration repricing channel" names two
       domains without naming either.
    """
    t = (text or "").lower()
    out: set[str] = set()

    for did, d in D.DOMAINS.items():
        label = d["label"].lower()
        short = d["short"].lower()
        if did in t or did.replace("_", " ") in t:
            out.add(did)
            continue
        # Match on the label's stem so "stablecoin float" hits "Stablecoins".
        stem = label.rstrip("s")
        if len(stem) > 4 and (stem in t or label in t):
            out.add(did)
            continue
        if len(short) > 4 and short in t:
            out.add(did)

    for d in D.DRIVERS.values():
        label = d["label"].lower()
        stem = label.rstrip("s")
        if label in t or (len(stem) > 4 and stem in t):
            out.update(dom for dom in d["domains"] if dom in D.DOMAINS)

    for c in D.CHANNELS:
        first = c["channel"].split()[0].lower()
        if len(first) > 3 and first in t:
            out.add(c["src"])
            out.add(c["dst"])
    return out


def _source_block(rows: list[tuple[Artifact, float]]) -> list[dict]:
    """Build the numbered source list the model answers from.

    ``text`` is the *verifiable* corpus -- the title, the published snippet and
    any extracted body. ``summary`` is the analyst's one-line reading, offered
    as orientation only. They are kept apart on purpose: the grounding check
    runs against ``text``, and feeding it the analyst's paraphrase would make
    the whole thing circular -- the model could quote the summary back at it
    and pass verification without ever having read the source.
    """
    out = []
    for i, (a, _) in enumerate(rows, start=1):
        parts = [a.title or ""]
        verified_text = True
        if a.source_text:
            parts.append(a.source_text[:2000])
        elif a.content:
            parts.append(a.content[:2000])
        elif a.description:
            # Artifacts collected before source_text existed fall back to the
            # summary. Better than no text at all; weaker than real, and the
            # flag below lets the explanation say so rather than quietly
            # reporting a drop count that mixes two kinds of failure.
            parts.append(a.description[:2000])
            verified_text = False
        out.append({
            "index": i, "artifact_id": a.id, "title": a.title,
            "source": a.source, "date": str(a.date_published or ""),
            "domain": a.domain, "direction": a.direction,
            "magnitude": a.magnitude, "horizon": a.horizon,
            "summary": (a.description or "")[:600],
            "text": " ".join(parts)[:3000],
            "quote_verifiable": verified_text,
        })
    return out


# --------------------------------------------------------------------------
# Drafting
# --------------------------------------------------------------------------
def _draft(question: str, sources: list[dict], *, mode: str, depth: str,
           audience: str, domains: list[str]) -> str:
    mode_p = MODES.get(mode, MODES["explain"])
    sys_p = (
        "You are a macro-financial analyst writing a grounded answer. You may "
        "only use the sources you are given, and you must cite them with "
        "[n] markers matching the numbered list. If the sources do not answer "
        "the question, say so and name what is missing — never fill the gap "
        "from your own knowledge. Never invent a citation marker.\n"
        f"Mode: {mode_p}\n"
        f"Depth: {DEPTH_PLAN.get(depth, DEPTH_PLAN['balanced'])}\n"
        f"Audience: {AUDIENCE_HINTS.get(audience, AUDIENCE_HINTS['intermediate'])}"
    )
    cat = "\n".join(f"- {d['id']}: {d['label']} — {d['blurb']}"
                    for d in D.domain_catalog())
    chan = "\n".join(f"- {c['src']} -> {c['dst']}: {c['channel']} "
                     f"(strength {c['strength']}, typical lag {c['lag']}q) "
                     f"— {c['note']}"
                     for c in sorted(D.CHANNELS, key=lambda c: -c["strength"])[:24])
    src_text = "\n\n".join(
        f"[{s['index']}] {s['title']}\n"
        f"Source: {s['source']} ({s['date']})\n"
        f"Domain: {s.get('domain') or 'unclassified'}"
        + (f" | {s['direction']} / {s.get('horizon') or '?'} / "
           f"magnitude {s.get('magnitude')}" if s.get("direction") else "") + "\n"
        + (f"Analyst's reading: {s['summary']}\n" if s.get("summary") else "")
        + f"Source text:\n{s['text']}" for s in sources) \
        or "(no sources were retrieved)"
    user = (
        f"Question: {question}\n\n"
        f"Taxonomy domains in play: {', '.join(domains) or '(none named)'}\n"
        f"Domain catalogue:\n{cat}\n\n"
        f"Known transmission channels (use these to name the mechanism, but "
        f"only assert one the sources support):\n{chan}\n\n"
        f"Sources:\n{src_text}\n\n"
        "Write the answer. Use markdown. Cite [n] after every factual claim.\n"
        "**Quote from the SOURCE TEXT of each source, not from the analyst's "
        "reading of it** -- quotes are checked verbatim against the source text, "
        "and a quote taken from the analyst's summary will be discarded.\n"
        "Where a channel connects two domains, name both domains explicitly — "
        "that is what makes the answer useful to the map.")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user}],
                    temperature=0.3, max_tokens=4096)


def _critique(question: str, answer: str, ground: dict) -> str:
    """A second pass over the *grounded* answer.

    It sees the verification result, not just the answer, so it can say "this
    claim rests on one source" rather than guessing which claims are thin.
    """
    sys_p = ("You are a hostile reviewer of macro-financial analysis. For each "
             "material claim, decide whether the cited evidence supports it or "
             "whether it is an inference the evidence does not carry. Be "
             "specific about which claim and which source. Reply with markdown "
             "only — no preamble.")
    verified = {c["index"] for c in ground["citations"]}
    unverified = [d.get("index") for d in ground["dropped"]]
    user = (
        f"Question: {question}\n\n"
        f"Answer:\n{G.strip_markers(answer)[:6000]}\n\n"
        f"Citation verification: {len(verified)} citation(s) carry a verbatim "
        f"quote; {len(unverified)} were dropped as unverifiable "
        f"({unverified}).\n\n"
        "Produce:\n"
        "## Supported\nClaims the evidence carries, with the claim's number.\n\n"
        "## Uncertain\nClaims that are inference, extrapolation, or rest on a "
        "single source. Say what would settle each one.\n\n"
        "## Missing\nWhat the question needed that no source supplied.")
    try:
        return llm.chat([{"role": "system", "content": sys_p},
                         {"role": "user", "content": user}],
                        temperature=0.2, max_tokens=2048)
    except Exception:
        return ""


def _roadmap(investigation_id: int, question: str, sources: list[dict],
             ground: dict) -> str:
    """What this question needs next, drawn from the taxonomy rather than
    invented — so a roadmap step is always a run somebody could actually
    launch."""
    covered = {s.get("domain") for s in sources if s.get("domain")}
    missing = [d for d in D.DOMAIN_IDS if d not in covered]
    top = missing[:5]
    lines = []
    for did in top:
        d = D.DOMAINS[did]
        lines.append(f"- **{d['label']}** — {d['blurb']} "
                     f"(no source in this answer touched it)")
    if not top:
        lines.append("- Every taxonomy domain is represented in this answer; "
                     "the gap is depth, not coverage.")
    if ground["dropped_citations"]:
        lines.append(f"- **{ground['dropped_citations']} citation(s) could "
                     f"not be verified** — re-collect against those sources "
                     f"before relying on the claim.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def launch_explanation(investigation_id: int, question: str, *,
                       mode: str = "explain", depth: str = "balanced",
                       audience: str = "intermediate",
                       max_sources: int = MAX_SOURCES,
                       parent_id: int | None = None) -> int:
    """Run the whole pipeline and return the new explanation's id.

    Returns an id rather than the ORM row on purpose: the session is closed on
    the way out, so a detached row would raise the moment a caller (or a queued
    worker that outlives the request) touched an attribute. Callers that want
    the content read it back through ``GET /api/explanations/{id}``.

    ``parent_id`` threads the answer into a conversation: the thread is the
    parent's thread (or the parent itself for a first follow-up). Without it a
    queued follow-up lands as a root and the thread silently goes flat — which
    is how "continue as thread" breaks without anyone noticing.
    """
    db = SessionLocal()
    try:
        inv = (db.query(Investigation)
               .filter(Investigation.id == investigation_id).first())
        thread_id = None
        if parent_id is not None:
            parent = (db.query(Explanation)
                      .filter(Explanation.id == parent_id).first())
            if parent is None:
                raise ValueError("no such parent explanation")
            if parent.investigation_id != investigation_id:
                raise ValueError("parent belongs to another investigation")
            thread_id = parent.thread_id or parent.id
        exp = Explanation(investigation_id=investigation_id, question=question,
                          mode=mode, depth=depth, audience=audience,
                          status="running", parent_id=parent_id,
                          thread_id=thread_id)
        db.add(exp)
        db.commit()
        exp_id = exp.id

        with L.scope(f"exp-{exp_id}",
                     L.Mandate(objective=f"explain: {question[:80]}",
                               allowed_actors=["agent", "llm", "system"]),
                     label=f"explain: {question[:60]}"):
            rows = (db.query(Artifact)
                    .filter(Artifact.investigation_id == investigation_id,
                            Artifact.review != "rejected")
                    .all())
            ranked = rank_artifacts(question, rows, limit=max_sources,
                                    domain_hint=(inv.preferred_domains
                                                 if inv else None))
            sources = _source_block(ranked)
            domains_touched = sorted({s["domain"] for s in sources
                                      if s.get("domain")})
            L.record_agent_step(
                "explainer.retrieve",
                f"{len(sources)} sources retrieved over "
                f"{len(domains_touched)} domain(s)",
                {"domains": domains_touched,
                 "artifact_ids": [s["artifact_id"] for s in sources]})

            if not sources:
                # Refusing is the correct answer, and it is a *recorded* one:
                # a gap row is created so the next step is obvious.
                db.query(ResearchGap).filter(
                    ResearchGap.investigation_id == investigation_id,
                    ResearchGap.dimension == question[:180]).delete()
                db.add(ResearchGap(
                    investigation_id=investigation_id,
                    dimension=question[:180],
                    why="No collected artifact matched this question."))
                db.commit()
                answer = ("I have nothing collected that speaks to this "
                          "question, so I will not answer it from memory.\n\n"
                          "The question has been recorded as a research gap — "
                          "run a targeted collection against it from the "
                          "**Research gaps** view.")
                exp.answer = answer
                exp.status = "no_evidence"
                exp.sources_json = json.dumps([])
                exp.domains_json = json.dumps([])
                exp.trace = json.dumps({"sources": 0, "reason": "empty_retrieval"})
                db.commit()
                return exp.id

            answer = _draft(question, sources, mode=mode, depth=depth,
                            audience=audience, domains=domains_touched)
            ground = G.verify_answer(answer, sources)
            L.record_agent_step(
                "grounding.check",
                f"{ground['n_verified']}/{ground['n_citations']} citations "
                f"verified, {ground['dropped_citations']} dropped",
                {"verdict": ground["verdict"],
                 "dropped": ground["dropped"],
                 "distinct_sources": ground["distinct_sources"]})
            if ground["citations"]:
                L.emit(answer[:1500],
                       [{"source": c["title"], "quote": c["quote"]}
                        for c in ground["citations"]],
                       actor="explainer",
                       confidence=("high" if ground["distinct_sources"] >= 3
                                   else "medium" if ground["citations"] else "low"))

            critique = (_critique(question, answer, ground)
                        if ground["citations"] else "")
            roadmap = _roadmap(investigation_id, question, sources, ground)

            confidence = _confidence(ground, sources)
            exp.answer = answer
            exp.sources_json = json.dumps([
                {"index": s["index"], "artifact_id": s["artifact_id"],
                 "title": s["title"], "source": s["source"],
                 "domain": s.get("domain")}
                for s in sources])
            exp.critique = critique
            exp.roadmap = roadmap
            exp.confidence = confidence
            exp.dropped_citations = ground["dropped_citations"]
            exp.domains_json = json.dumps(domains_touched)
            exp.trace = json.dumps({
                "mode": mode, "depth": depth, "audience": audience,
                "sources": len(sources),
                "grounding": {k: v for k, v in ground.items()
                              if k not in ("citations", "dropped")},
                "dropped": ground["dropped"],
                "artifact_ids": [s["artifact_id"] for s in sources],
                # Quotes are checked against the published snippet, not the
                # analyst's paraphrase. When an artifact predates that split
                # there is no snippet, and a citation against it can fail for
                # the wrong reason -- so say which sources that applies to
                # rather than letting the drop count carry the ambiguity.
                "summary_only_sources": [s["index"] for s in sources
                                         if not s["quote_verifiable"]],
            })
            exp.status = "done"
            db.commit()
            if inv is not None and inv.auto_save_explanations:
                _save_to_graph(db, inv.id, exp)
            try:
                from . import search_index as _si
                db.refresh(exp)
                _si.index_explanation(db, exp)
            except Exception:
                pass
            return exp.id
    finally:
        db.close()


def _confidence(ground: dict, sources: list[dict]) -> float:
    """Confidence from corroboration, not from how confident the prose sounds.

    A distinct-source count is the only signal here that a model cannot
    inflate, so it dominates; the grounding rate scales it rather than adding
    to it.
    """
    n = ground["distinct_sources"]
    if n == 0:
        return 0.0
    base = min(0.9, 0.18 * n)
    rate = ((ground["n_verified"] / ground["n_citations"])
            if ground["n_citations"] else 0.0)
    # Every citation unverified caps it hard: an answer whose citations do not
    # verify is not partially trustworthy, it is differently wrong.
    if rate < 0.5:
        base *= 0.4
    directional = sum(1 for s in sources if s.get("direction"))
    return round(min(0.95, base + 0.02 * directional), 3)


def _save_to_graph(db: Session, investigation_id: int, exp: Explanation) -> int:
    """An answer is evidence too.

    Stored as an artifact of type ``projection`` so a later explanation can cite
    it — otherwise the explainer is a dead end that can never build on itself.
    """
    artifact = Artifact(
        investigation_id=investigation_id,
        title=f"Q: {exp.question[:200]}",
        artifact_type="projection",
        description=exp.answer[:2000],
        content=exp.answer[:8000],
        source="explainer",
        tags=",".join(json.loads(exp.domains_json or "[]"))[:500] or "explanation",
        domain=(json.loads(exp.domains_json or "[]") or [None])[0],
        relevance=exp.confidence or 0.5,
        relevance_reason=f"explainer answer (confidence {exp.confidence})",
        review="accepted",  # the app's own analysis, pre-accepted
        origin="agent",
    )
    db.add(artifact)
    db.commit()
    try:
        from . import search_index as _si
        _si.index_artifact(db, artifact)
    except Exception:
        pass
    if exp.trace:
        try:
            trace = json.loads(exp.trace)
            trace["artifact_id"] = artifact.id
            exp.trace = json.dumps(trace)
            db.commit()
        except Exception:
            pass
    return artifact.id


# --------------------------------------------------------------------------
# Threads, follow-ups, quiz
# --------------------------------------------------------------------------
def followup(explanation_id: int, question: str, **kw) -> int:
    db = SessionLocal()
    try:
        parent = (db.query(Explanation)
                  .filter(Explanation.id == explanation_id).first())
        if parent is None:
            raise ValueError("no such explanation")
        kw.setdefault("mode", parent.mode)
        kw.setdefault("depth", parent.depth)
        kw.setdefault("audience", parent.audience)
        inv_id, parent_id = parent.investigation_id, parent.id
    finally:
        db.close()
    child_id = launch_explanation(inv_id, question, parent_id=parent_id, **kw)
    return child_id


def quiz_for(explanation_id: int, n: int = 3) -> list[dict]:
    """Generate comprehension checks from a stored answer.

    Questions about *this* answer, not the topic: a quiz that tests general
    knowledge tells the reader nothing about whether they understood what they
    just read.
    """
    db = SessionLocal()
    try:
        exp = (db.query(Explanation)
               .filter(Explanation.id == explanation_id).first())
        if not exp or not exp.answer:
            return []
        sys_p = ("You write comprehension checks. Each question must be "
                 "answerable from the text alone, and must test understanding "
                 "of a specific claim rather than vocabulary. "
                 "Reply JSON only.")
        user = (f"Text:\n{exp.answer[:6000]}\n\n"
                f"Reply {{\"quiz\": [{{\"question\": str, \"answer\": str, "
                f"\"why_wrong\": str}}]}} with {n} items.")
        out = llm.chat_json([{"role": "system", "content": sys_p},
                             {"role": "user", "content": user}], max_tokens=1536)
        items = out.get("quiz") if isinstance(out, dict) else None
        if not isinstance(items, list):
            return []
        exp.quiz = json.dumps(items[:n])
        db.commit()
        return items[:n]
    except Exception:
        return []
    finally:
        db.close()


def investigation_roadmap(investigation_id: int) -> dict:
    """Coverage across the ten domains, and what is missing.

    Coverage is reported as two lists, never one: ``uncovered`` (a domain the
    graph says nothing about) and ``thin`` (a domain with some evidence but too
    little to lean on). Collapsing them would let "nobody looked" read as
    "there is nothing there".
    """
    db = SessionLocal()
    try:
        rows = (db.query(Artifact)
                .filter(Artifact.investigation_id == investigation_id,
                        Artifact.review != "rejected").all())
        counts: dict[str, int] = {}
        accepted: dict[str, int] = {}
        for a in rows:
            if a.domain:
                counts[a.domain] = counts.get(a.domain, 0) + 1
                if a.review == "accepted":
                    accepted[a.domain] = accepted.get(a.domain, 0) + 1
        cov = []
        for d in D.domain_catalog():
            did = d["id"]
            n = counts.get(did, 0)
            cov.append({**d, "artifacts": n, "accepted": accepted.get(did, 0),
                        "covered": n > 0})
        uncovered = [c for c in cov if not c["covered"]]
        thin = [c for c in cov if 0 < c["artifacts"] < 3]
        return {
            "investigation_id": investigation_id,
            "coverage": cov,
            "uncovered": [c["id"] for c in uncovered],
            "thin": [c["id"] for c in thin],
            "total_artifacts": len(rows),
            "domains_covered": sum(1 for c in cov if c["covered"]),
            "domains_total": len(cov),
            "cross_domain_edges": _cross_domain_edges(investigation_id),
        }
    finally:
        db.close()


def _cross_domain_edges(investigation_id: int) -> int:
    """Edges that actually cross a domain boundary.

    One explicit join with both endpoint conditions. The obvious alternative —
    two chained ``join()`` calls on aliased entities — silently returns zero,
    because the aliases are already in the FROM clause and re-joining them
    builds a condition no row satisfies. A cross-domain count of 0 that is
    really a broken query is the worst kind of bug in this app: it looks
    exactly like a finding.
    """
    from sqlalchemy.orm import aliased

    from .models import Relationship
    src = aliased(Artifact)
    dst = aliased(Artifact)
    db = SessionLocal()
    try:
        return (db.query(src.id)
                .join(Relationship,
                      and_(Relationship.source_id == src.id,
                           Relationship.target_id == dst.id))
                .filter(Relationship.investigation_id == investigation_id,
                        src.domain.isnot(None), dst.domain.isnot(None),
                        src.domain != dst.domain)
                .count())
    except Exception:
        return 0
    finally:
        db.close()


def question_suggestions(investigation_id: int, n: int = 5) -> list[str]:
    """Questions worth asking *of this investigation*, drawn from its own gaps.

    Generated from the domains it has and has not covered, so the suggestions
    are a research plan rather than a list of generic prompts.
    """
    rm = investigation_roadmap(investigation_id)
    missing = (rm["uncovered"] + rm["thin"])[:n]
    out = []
    for did in missing:
        d = D.DOMAINS[did]
        outs = D.channels_into(did)[:2]
        if outs:
            ch = outs[0]
            out.append(
                f"How does this propagate into {d['label'].lower()} "
                f"({ch['channel']} from {D.domain_label(ch['src'])}), and "
                f"over what horizon?")
        else:
            out.append(f"What does this investigation have on {d['label'].lower()}?")
    if not out:
        covered = [c["short"].lower() for c in rm["coverage"] if c["covered"]][:3]
        if covered:
            out.append("How do " + ", ".join(covered)
                       + " interact in this map, and which link is weakest?")
    return out[:n]
