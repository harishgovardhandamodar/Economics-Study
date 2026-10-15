"""The collection loop.

```
brief (keywords + description)
  → PLAN      LLM drafts ≤6 targeted queries, picking rss/arxiv/web per query
  → SEARCH    RSS feeds · arXiv API · DuckDuckGo HTML (dedupe by URL/title)
  → ANALYZE   LLM batches: relevance 0..1, keep/drop, domain, entities,
              direction/magnitude/horizon, relates_to[] against what is
              already collected
  → MAP       persist top artifacts + agent relationships (+ domain-overlap
              fallback for edges the model did not name)
  → REFINE    if yield is low, planner drafts follow-up queries (≤ max_rounds)
```

What is different from a generic research agent, and why:

* **The domain is an output, not an input.** The analyst classifies each
  candidate into one of the ten taxonomy domains, because a cross-domain graph
  cannot be built from documents that do not say which domain they belong to.
* **Entities are extracted alongside.** A "stablecoin float" document and a
  "T-bill auction" document share almost no vocabulary; they are related
  because both name Tether and Circle vs the Fed. Entity overlap is what draws
  the edge keyword search cannot.
* **Direction and horizon are first-class.** "Rates up" is not an insight. "Up,
  magnitude 0.6, matters over 2-4 quarters" is something a transmission chain
  can be built on, so the analyst returns it and it is stored.
* **Yield is credited before the early exits.** A round that analyzed
  candidates and kept none is the round worth remembering.
"""
import contextvars
import json
import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from . import domains as D
from . import ledger as L
from . import llm
from . import search as providers
from . import yield_ as yld
from .database import SessionLocal
from .models import AgentEvent, AgentRun, Artifact, Investigation, Relationship

ANALYZE_BATCH = 5
KEEP_THRESHOLD = 0.4

AGENT_STAGES = ["plan", "search", "analyze", "map", "summary"]

ALLOWED_ACTORS = ["agent", "llm", "user", "system", "planner", "analyzer"]


def ledger_run_id(run_id: int) -> str:
    """The ledger id for an agent run row.

    Prefixed rather than bare so a ledger chain is self-describing when it turns
    up in an export with no other context.
    """
    return f"run-{run_id}"


def _event(db: Session, run_id: int, stage: str, message: str,
           data: dict | None = None) -> None:
    try:
        ev = AgentEvent(run_id=run_id, stage=stage, message=message[:2000],
                        data_json=json.dumps(data) if data else None)
        db.add(ev)
        db.commit()
    except Exception:
        db.rollback()
    L.record_agent_step(stage, message, data)


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
_DOMAIN_HINT = ", ".join(f"{d['id']}={d['label']}" for d in D.domain_catalog())
_TYPE_HINT = "|".join(D.ARTIFACT_TYPES)
_REL_HINT = "|".join(D.RELATIONSHIP_TYPES)
_CHANNEL_HINT = "; ".join(
    f"{c['src']}->{c['dst']} ({c['channel']})"
    for c in sorted(D.CHANNELS, key=lambda c: -c["strength"])[:18])


def _plan_queries(inv: Investigation) -> dict:
    """Ask the model for a search plan, then never let it fail a run.

    The deterministic fallback flows through the same review as a model plan:
    a keyword-derived plan is a serviceable start, not a different class of
    thing.
    """
    sys_p = ("You are an economics research search planner. Given an "
             "investigation brief, produce targeted search queries that can "
             "surface evidence from more than one domain. Reply with JSON only.")
    user = (
        f"Title: {inv.title}\nKeywords: {inv.keywords}\n"
        f"Brief: {inv.description}\n"
        f"Domains of interest: {inv.preferred_domains or '(any)'}\n"
        f"Driver: {inv.driver or '(none — this is a topic, not an event)'}\n"
        f"Enabled sources: {inv.sources}\n\n"
        "Reply JSON: {\"rationale\": str, \"queries\": "
        "[{\"text\": str, \"sources\": [\"rss\"|\"arxiv\"|\"web\"], "
        "\"domain\": \"<one domain id>\"}]}. "
        "Max 6 queries, each 2-6 words. "
        "Match sources to query type: official releases and commentary -> rss, "
        "academic work -> arxiv, everything else -> web. "
        "Only use enabled sources. "
        "At least one query must come from an official source (central bank, "
        "IMF/BIS/OECD, treasury, regulator) rather than commentary about it.\n"
        f"Available domains: {_DOMAIN_HINT}\n"
        f"Cross-domain channels you may deliberately probe: {_CHANNEL_HINT}")
    enabled = {s.strip() for s in (inv.sources or "").split(",") if s.strip()}

    def _deterministic_plan() -> dict:
        first = (inv.keywords.split(",")[0].strip() or inv.title or "").strip()
        return {"rationale": "deterministic fallback (model output unusable)",
                "queries": [{"text": (first[:120] or "research"),
                             "sources": sorted(enabled) or ["web"]}]}

    plan = None
    # A thinking model burns tokens on reasoning, so a small cap truncates the
    # JSON mid-string. Retry at a larger cap before giving up on the model.
    for mt in (2048, 4096):
        try:
            plan = llm.chat_json(
                [{"role": "system", "content": sys_p},
                 {"role": "user", "content": user}],
                max_tokens=mt, temperature=0.2)
            if isinstance(plan, dict) and plan.get("queries"):
                break
            plan = None
        except Exception:
            plan = None
    if plan is None:
        plan = _deterministic_plan()

    clean = []
    for q in (plan.get("queries") or [])[:6]:
        if not isinstance(q, dict) or not q.get("text"):
            continue
        srcs = [s for s in (q.get("sources") or []) if s in enabled] \
            or sorted(enabled) or ["web"]
        entry = {"text": q["text"][:120], "sources": srcs,
                 "seat": "planner"}
        dom = q.get("domain")
        if isinstance(dom, str) and dom.strip().lower() in D.DOMAINS:
            entry["domain"] = dom.strip().lower()
        clean.append(entry)

    _ensure_official_query(inv, clean, enabled)
    return {"rationale": plan.get("rationale", ""), "queries": clean}


def _ensure_official_query(inv: Investigation, clean: list, enabled: set) -> None:
    """Guarantee one query aimed at a primary source.

    Commentary about a decision and the decision itself are different evidence:
    the first tells you what people thought, the second tells you what happened.
    A plan that only ever asks the first kind cannot support a claim about the
    second, so the ensurer adds the query the planner left out.
    """
    if "rss" not in enabled:
        return
    official_terms = ("central bank", "federal reserve", "ecb", "imf", "bis",
                      "treasury", "official statistics", "regulator", "ministry",
                      "budget", "rate decision", "auction")
    haystack = " ".join(q["text"].lower() for q in clean)
    subject = (inv.keywords.split(",")[0].strip() or inv.title or "").strip()
    for term in official_terms:
        if term in haystack:
            return
    text = f"{subject} {official_terms[0]}".strip()[:120]
    if len(clean) >= 6:
        clean[-1] = {"text": text, "sources": ["rss"], "seat": "official-ensurer"}
    else:
        clean.append({"text": text, "sources": ["rss"], "seat": "official-ensurer"})


# --------------------------------------------------------------------------
# Searching
# --------------------------------------------------------------------------
def _search_one(args) -> list:
    query, src = args
    try:
        if src == "rss":
            return providers.search_rss(query)
        if src == "arxiv":
            return providers.search_arxiv(query)
        if src == "web":
            return providers.search_web(query)
    except Exception:
        pass
    return []


def _run_searches(queries: list, seen_urls: set) -> list:
    """Fan out across threads: sequential would be minutes (6 queries × 3
    sources)."""
    tasks = [(q["text"], src) for q in queries for src in q["sources"]]
    if not tasks:
        return []
    found = []
    with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as pool:
        for hits in pool.map(_search_one, tasks):
            for h in hits:
                key = (h.get("url") or "").strip().lower() \
                    or (h.get("title") or "").lower()
                if not key or key in seen_urls:
                    continue
                seen_urls.add(key)
                found.append(h)
    return found


def _prefilter(inv: Investigation, found: list, keep_n: int) -> list:
    """Cheap keyword pre-rank so the LLM only sees the top candidates."""
    terms = _keyword_terms(inv)
    scored = []
    for c in found:
        title = (c.get("title") or "").lower()
        desc = (c.get("description") or "").lower()
        s = sum((2 if t in title else 0) + (1 if t in desc else 0) for t in terms)
        # A primary source outranks a write-up about it, and a document from a
        # domain the brief cares about outranks one that is merely on-topic.
        if c.get("primary"):
            s += 2
        qd = (c.get("query") or "")
        for q in inv.domain_list:
            if q in qd.lower() or q in title:
                s += 1
        scored.append((s, c))
    scored.sort(key=lambda kv: -kv[0])
    return [c for _, c in scored[:keep_n]]


def _keyword_terms(inv: Investigation) -> list:
    raw = f"{inv.title} {inv.keywords} {inv.description}"
    words = re.findall(r"[a-z0-9][a-z0-9\-]{2,}", raw.lower())
    stop = {"the", "and", "for", "with", "from", "that", "this", "what",
            "which", "about", "into", "collect", "recent", "work", "should",
            "relevant", "including", "between", "such", "are", "how", "why"}
    terms = [w for w in words if w not in stop]
    kw = [t.strip().lower() for t in (inv.keywords or "").split(",") if t.strip()]
    return kw + [t for t in terms if t not in kw]


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
def _analyze_batch(inv: Investigation, batch: list, existing: list) -> list:
    """LLM relevance + economics attribute extraction for a batch of candidates."""
    sys_p = ("You are a macro-financial research analyst. Judge candidates "
             "against the brief, and extract the economics: which domain the "
             "item belongs to, which named actors it involves, and — where it "
             "makes a directional claim — its direction, magnitude and horizon. "
             "Reply with JSON only: a list with one object per candidate.")
    items = []
    for i, c in enumerate(batch):
        items.append(f"[{i}] {c['title']}\nSource: {c.get('source','')}\n"
                     f"URL: {c.get('url','')}\n"
                     f"{(c.get('description') or '')[:800]}")
    ctx = ""
    if existing:
        ctx = ("Already collected (reference by id for relates_to):\n"
               + "\n".join(f"- id {a['id']}: {a['title']} [{a.get('tags','')}]"
                           for a in existing[:40]))
    user = (
        f"Brief: {inv.title} | {inv.keywords} | {inv.description}\n"
        f"Domains of interest: {inv.preferred_domains or '(any)'}\n{ctx}\n\n"
        f"Candidates:\n" + "\n\n".join(items) +
        "\n\nReply JSON list, one object per candidate: "
        '{"index": i, "relevance": 0..1, "keep": bool, "reason": str, '
        f'"artifact_type": {_TYPE_HINT}, '
        f'"domain": one of [{_DOMAIN_HINT}], '
        '"entities": [up to 6 proper nouns: institutions, central banks, '
        'countries, companies, protocols, tickers], '
        '"tags": [max 5 lowercase], "sentiment": -1..1, '
        '"summary": str (1-2 sentences), '
        '"direction": "up"|"down"|"flat"|null (the direction of the economic '
        'claim it makes, not of the market for the article), '
        '"magnitude": 0..1 or null (how big the claimed effect is), '
        '"horizon": "immediate"|"short"|"medium"|"long"|null, '
        f'"relates_to": [{{"id": artifact_id, "relationship_type": {_REL_HINT}, '
        '"description": str}}], '
        '"drift": bool (true if about a topic entirely outside this brief)}')
    try:
        verdicts = llm.chat_json(
            [{"role": "system", "content": sys_p},
             {"role": "user", "content": user}], max_tokens=4096)
    except Exception:
        return []
    return verdicts if isinstance(verdicts, list) else []


def _persist(db: Session, inv_id: int, kept: list,
             run_id: int | None = None) -> list:
    """Insert kept candidates, return the row summaries the mapper needs."""
    rows = []
    for cand, v in kept:
        dom = D.normalize_domains(v.get("domain"))
        direction = v.get("direction")
        if direction not in ("up", "down", "flat"):
            direction = None
        horizon = v.get("horizon")
        if horizon not in ("immediate", "short", "medium", "long"):
            horizon = None
        magnitude = v.get("magnitude")
        try:
            magnitude = float(magnitude) if magnitude is not None else None
        except (TypeError, ValueError):
            magnitude = None
        entities = v.get("entities") or []
        if not isinstance(entities, list):
            entities = []
        a = Artifact(
            investigation_id=inv_id, run_id=run_id,
            title=(cand.get("title") or "(untitled)")[:500],
            artifact_type=v.get("artifact_type") or cand.get("artifact_type")
            or "news",
            url=(cand.get("url") or "")[:1000] or None,
            description=(v.get("summary") or cand.get("description") or "")[:2000],
            # The snippet the source actually published, which is what the
            # grounding check has to verify against.
            source_text=((cand.get("description") or "") or
                         (cand.get("content") or "") or None),
            content=(cand.get("content") or "")[:8000] or None,
            source=(cand.get("source") or "")[:200],
            author=(cand.get("author") or "")[:200],
            date_published=cand.get("date_published"),
            tags=", ".join(str(t) for t in (v.get("tags") or [])[:5])[:500],
            domain=dom[0] if dom else None,
            entities_json=[str(e)[:80] for e in entities][:6] or None,
            direction=direction, magnitude=magnitude, horizon=horizon,
            relevance=float(v.get("relevance", 0) or 0),
            relevance_reason=(v.get("reason") or "")[:500],
            sentiment=float(v.get("sentiment", 0) or 0),
            review="pending", origin="agent",
            drift=1 if v.get("drift") else 0,
        )
        db.add(a)
        db.flush()
        rows.append({"id": a.id, "title": a.title, "tags": a.tags or "",
                     "domain": a.domain, "entities": a.entities_json or [],
                     "relates_to": v.get("relates_to") or []})
    db.commit()
    # Index for full-text search. Best-effort by contract: a failed index
    # write degrades to "searchable after reindex", never to lost evidence.
    try:
        from . import search_index as _si
        for r in db.query(Artifact).filter(
                Artifact.investigation_id == inv_id,
                Artifact.id.in_([x["id"] for x in rows])).all():
            _si.index_artifact(db, r)
    except Exception:
        pass
    return rows


# --------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------
def _map_relationships(db: Session, inv_id: int, rows: list,
                       pre_existing_ids: set, run_id: int | None = None) -> int:
    """Persist agent-named edges, then add the ones it did not name.

    The fallback is not a consolation prize. Two documents about a Fed decision
    and a Tether attestation share almost no vocabulary; what they share is a
    *domain*, and the channel between those two domains is the whole reason
    this app exists. If the model named no edge, the taxonomy still says there
    is one, and a graph with no such edge would be lying about coverage.
    """
    valid = pre_existing_ids | {r["id"] for r in rows}
    made, pairs = 0, set()
    allowed = set(D.RELATIONSHIP_TYPES)

    def _edge(src, dst, rtype, desc, confidence=None, lag=None):
        nonlocal made
        if src is None or dst is None or src == dst:
            return
        if src not in valid or dst not in valid:
            return
        key = (src, dst, rtype)
        if key in pairs:
            return
        exists = db.query(Relationship).filter(
            Relationship.investigation_id == inv_id,
            Relationship.source_id == src, Relationship.target_id == dst,
            Relationship.relationship_type == rtype).first()
        if exists:
            pairs.add(key)
            return
        db.add(Relationship(investigation_id=inv_id, run_id=run_id,
                            source_id=src, target_id=dst,
                            relationship_type=rtype, description=(desc or "")[:1000],
                            confidence=confidence, lag=lag))
        pairs.add(key)
        made += 1

    by_id = {r["id"]: r for r in rows}

    for r in rows:
        for rel in r["relates_to"]:
            if not isinstance(rel, dict):
                continue
            rtype = rel.get("relationship_type")
            if rtype not in allowed:
                continue
            try:
                tgt = int(rel.get("id"))
            except (TypeError, ValueError):
                continue
            _edge(r["id"], tgt, rtype, rel.get("description", ""),
                  confidence=r.get("domain_confidence"))

    # --- fallback: cross-domain edges the taxonomy says exist -------------
    this_round = [by_id[i] for i in by_id]
    for a in this_round:
        for b in this_round:
            if a["id"] >= b["id"]:
                continue
            if not a.get("domain") or not b.get("domain"):
                continue
            if a["domain"] == b["domain"]:
                continue
            chan = D.CHANNEL_INDEX.get((a["domain"], b["domain"])) or \
                D.CHANNEL_INDEX.get((b["domain"], a["domain"]))
            if not chan:
                continue
            src, dst = ((a, b) if chan["src"] == a["domain"] else (b, a))
            # Only draw the edge when the two documents actually share an actor.
            # Domain adjacency alone would connect everything to everything.
            shared = _shared_entities(a.get("entities") or [],
                                      b.get("entities") or [])
            if not shared:
                continue
            _edge(src["id"], dst["id"], "transmits_to",
                  f"{chan['channel']}: {a['domain']} → {b['domain']} "
                  f"(shared: {', '.join(shared[:3])})",
                  confidence=chan["strength"], lag=chan["lag"])
    db.commit()
    return made


def _norm_entity(value: str) -> str:
    """Canonical form of a named entity for comparison.

    Punctuation is dropped rather than merely normalised, because the same
    institution is written "US Treasury", "U.S. Treasury" and "U.S. Treasury"
    by three different sources. Matching on raw strings misses every one of
    those pairs, and a missed pair means a missing edge on a map whose whole
    purpose is those edges -- so the normalisation is aggressive on purpose and
    conservative about *what* it will accept afterwards.
    """
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


# Names too generic to justify an edge on their own. "Bank", "Treasury" and
# "market" connect every document in finance to every other one, which is
# exactly the all-to-all graph the entity rule exists to prevent.
_GENERIC = frozenset({
    "bank", "banks", "market", "markets", "group", "groups", "report",
    "reports", "committee", "national", "states", "united", "us", "usa",
    "department", "office", "working", "paper", "data", "news", "system",
    "council", "commission", "authority", "agency", "government",
    "economy", "policy", "capital", "finance", "money", "analysis",
})
# "reserve", "federal" and "treasury" are deliberately absent. They read as
# generic in the abstract, but as words in a named institution they are among
# the most load-bearing entities in this corpus — filtering them left
# "U.S. Treasury" with no tokens at all, so it could never link to anything.
# The exact-canonical pass is what guards against short generic strings; the
# token pass does not have to carry that burden as well.


def _tokens(items) -> set[str]:
    """Distinctive lowercase tokens, abbreviations expanded.

    An acronym is kept on a separate rule from a word: IMF, ECB and BIS are
    three characters long and load-bearing, while EU and US are three
    characters long and connect everything to everything. "Was it written in
    capitals" is the only cheap discriminator that separates them, and it
    separates them perfectly in this corpus.

    Alias expansion (``domains.ENTITY_ALIASES``) is what links "IMF" to
    "International Monetary Fund" -- in an economics corpus the abbreviations
    *are* most of the named actors, so without it the entity rule silently
    drops the edges that matter most.
    """
    out = set()
    for raw in (items or []):
        for part in re.split(r"[^A-Za-z0-9]+", str(raw)):
            if not part or part.lower() in _GENERIC:
                continue
            if len(part) > 3 or (len(part) >= 3 and part.isupper()):
                out.update(D.entity_tokens(part))
    return out


def _shared_entities(a: list, b: list) -> list[str]:
    """Entities two documents have in common.

    Two passes. Exact canonical match first — the strong signal. Then token
    overlap, for near-misses like "Nellie Liang" / "Liang" where the canonical
    forms differ but the person is plainly the same one.

    The returned strings are *display* forms taken from the inputs, not the
    normalised keys: these end up in an edge description a human reads, and
    "liang" is not how either document spells it.
    """
    def norm(items):
        return {_norm_entity(e): str(e).strip()
                for e in (items or []) if _norm_entity(e)}

    na, nb = norm(a), norm(b)

    exact = sorted(na[k] for k in (set(na) & set(nb)) if len(k) > 3)
    if exact:
        return exact

    shared = _tokens(a) & _tokens(b)
    if not shared:
        return []
    # Name what matched the way the documents spell it, preferring the first
    # list so the edge reads consistently from one side of the pair.
    out = set()
    for token in shared:
        for items in (a, b):
            for raw in (items or []):
                if token in [t.lower() for t in
                             re.split(r"[^A-Za-z0-9]+", str(raw)) if t]:
                    out.add(str(raw).strip())
                    break
            else:
                continue
            break
    return sorted(out)


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------
def run_investigation_agent(investigation_id: int, max_items: int = 25,
                            max_rounds: int = 2, trigger: str = "manual",
                            run_id: int | None = None, goal: str | None = None):
    db = SessionLocal()
    rid = None
    try:
        inv = (db.query(Investigation)
               .filter(Investigation.id == investigation_id).first())
        if not inv:
            return
        if run_id is None:
            run = AgentRun(investigation_id=inv.id, status="running",
                           trigger=trigger)
            db.add(run)
            db.commit()
        else:
            run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
            if not run:
                return
            run.trigger = trigger
            if run.plan is None and goal:
                run.plan = json.dumps({"goal": goal})
            db.commit()
        run_id = run.id
        inv.status = "running"
        db.commit()

        rid = ledger_run_id(run.id)
        mandate = L.Mandate(
            objective=f"map the investigation '{inv.title}'",
            planned_intents=AGENT_STAGES,
            allowed_actors=ALLOWED_ACTORS,
            allowed_domains=(set(inv.domain_list) or None),
        )
        run_label = f"map: {inv.title[:60]}"
        with L.scope(rid, mandate, label=run_label):
            # ---------------- PLAN ----------------
            _event(db, run.id, "plan",
                   f"Planning search for '{inv.title}'…")
            try:
                plan = _plan_queries(inv)
            except Exception as e:
                # `from e` keeps the planner's own traceback attached. Without
                # it the stored error reads "planner failed: …" with the
                # cause inlined as text and no way to walk to the frame that
                # actually failed.
                raise RuntimeError(f"planner failed: {e}") from e
            if goal:
                plan["goal"] = plan.get("goal") or goal
            run.plan = json.dumps(plan)
            db.commit()
            _event(db, run.id, "plan",
                   plan.get("rationale") or f"{len(plan['queries'])} queries planned.",
                   {"queries": plan["queries"]})

            existing_rows = (db.query(Artifact)
                             .filter(Artifact.investigation_id == inv.id).all())
            seen = {(a.url or "").strip().lower() for a in existing_rows if a.url}
            seen |= {a.title.lower() for a in existing_rows}

            total_kept = total_rels = rounds = llm_calls_spent = 0
            # Spend the first round on what has paid off before.
            queries = yld.rank_queries(db, inv.id, plan["queries"])

            while rounds < max_rounds and total_kept < max_items:
                rounds += 1

                # ---------------- SEARCH ----------------
                _event(db, run.id, "search",
                       f"Round {rounds}: searching {len(queries)} queries…")
                found = _run_searches(queries, seen)
                _event(db, run.id, "search",
                       f"Round {rounds}: {len(found)} candidates.",
                       {"count": len(found),
                        "sample": [f["title"][:80] for f in found[:10]]})
                if not found:
                    break

                # Pre-rank cheaply: the LLM analyzes only the top slice.
                #
                # Sized against what is still *needed*, not against the target.
                # A run asked for 12 items that already holds 10 does not need
                # 36 candidates analysed, it needs 12 -- and each analysed batch
                # is a model call, so a generous cap spends two thirds of the
                # budget on a tail it is about to truncate away.
                needed = max(1, max_items - total_kept)
                analyze_cap = max(needed * 2, 8)
                if len(found) > analyze_cap:
                    found = _prefilter(inv, found, analyze_cap)
                    _event(db, run.id, "search",
                           f"Round {rounds}: pre-ranked to top {analyze_cap} "
                           f"for analysis.")

                existing = [{"id": a.id, "title": a.title, "tags": a.tags or "",
                             "domain": a.domain}
                            for a in db.query(Artifact)
                            .filter(Artifact.investigation_id == inv.id).all()]
                pre_ids = {e["id"] for e in existing}

                # ---------------- ANALYZE ----------------
                kept: list = []
                batches = 0
                for i in range(0, len(found), ANALYZE_BATCH):
                    batch = found[i:i + ANALYZE_BATCH]
                    batches += 1
                    llm_calls_spent += 1
                    verdicts = _analyze_batch(inv, batch, existing)
                    by_idx = {v.get("index"): v for v in verdicts
                              if isinstance(v, dict)
                              and isinstance(v.get("index"), int)}
                    for j, cand in enumerate(batch):
                        v = by_idx.get(j)
                        if not v:
                            continue
                        try:
                            rel = float(v.get("relevance", 0))
                        except (TypeError, ValueError):
                            rel = 0
                        if (v.get("keep") or rel >= KEEP_THRESHOLD) and rel > 0:
                            kept.append((cand, v))
                    _event(db, run.id, "analyze",
                           f"Round {rounds}: analyzed "
                           f"{min(i + ANALYZE_BATCH, len(found))}/{len(found)}"
                           f"… ({len(kept)} kept)")
                    # Stop once the keepers comfortably exceed what this round
                    # can persist. Candidates arrive pre-ranked, so the ones
                    # still unanalysed are the weaker tail by construction --
                    # and the extra ones are recorded in yield either way, so a
                    # round stopped here still leaves a trace.
                    if len(kept) >= needed + ANALYZE_BATCH:
                        _event(db, run.id, "analyze",
                               f"Round {rounds}: stopping early — {len(kept)} "
                               f"keepers is more than the {needed} this round "
                               f"can take, so analysing the rest of the tail "
                               f"could not change what is persisted.")
                        break

                # Credit this round to the shapes that were asked, *before*
                # any early exit below.
                try:
                    yld.record_yield(db, inv.id, queries, found=len(found),
                                     kept=len(kept), llm_calls=batches)
                except Exception as yexc:
                    _event(db, run.id, "analyze",
                           f"Round {rounds}: yield not recorded ({yexc}).")

                kept.sort(key=lambda kv: -float(kv[1].get("relevance", 0) or 0))
                kept = kept[:max(0, max_items - total_kept)]
                if not kept:
                    _event(db, run.id, "analyze",
                           f"Round {rounds}: nothing worth keeping.")
                    break

                # ---------------- MAP ----------------
                rows = _persist(db, inv.id, kept, run_id=run.id)
                rels = _map_relationships(db, inv.id, rows, pre_ids, run_id=run.id)
                total_kept += len(rows)
                total_rels += rels
                _event(db, run.id, "map",
                       f"Round {rounds}: kept {len(rows)}, mapped {rels} "
                       f"relationships.",
                       {"kept": [r["title"][:80] for r in rows]})
                for r in rows:
                    existing.append({"id": r["id"], "title": r["title"],
                                     "tags": r["tags"], "domain": r["domain"]})

                if total_kept >= max_items or rounds >= max_rounds:
                    break

                # ---------------- REFINE ----------------
                try:
                    follow = llm.chat_json([
                        {"role": "system",
                         "content": "Economics research search planner. "
                                    "Reply JSON only."},
                        {"role": "user",
                         "content": (
                             f"Brief: {inv.title} | {inv.keywords} | "
                             f"{inv.description}\n"
                             f"Kept so far ({total_kept}): "
                             f"{[e['title'][:60] for e in existing[-8:]]}\n"
                             "Reply {\"queries\": [{\"text\": str, "
                             "\"sources\": [...], \"domain\": \"...\"}]} "
                             "with max 3 follow-up queries. "
                             "Prefer domains not yet represented in what was "
                             "kept — the point of a second round is "
                             "coverage the first round missed.\n"
                             f"Domains: {_DOMAIN_HINT}\n"
                             f"Sources allowed: {inv.sources}")}],
                        max_tokens=1024)
                    queries = []
                    for q in (follow.get("queries") or [])[:3]:
                        if not isinstance(q, dict) or not q.get("text"):
                            continue
                        entry = {"text": q["text"][:120],
                                 "sources": q.get("sources") or ["web"]}
                        if isinstance(q.get("domain"), str) and \
                                q["domain"].lower() in D.DOMAINS:
                            entry["domain"] = q["domain"].lower()
                        queries.append(entry)
                    if not queries:
                        break
                    queries = yld.rank_queries(db, inv.id, queries)
                    _event(db, run.id, "plan",
                           f"Refinement: {len(queries)} follow-up queries.",
                           {"queries": queries})
                except Exception:
                    break

            # ---------------- SUMMARY ----------------
            shapes = yld.shape_stats(db, inv.id)
            stats = {"rounds": rounds, "artifacts_kept": total_kept,
                     "relationships": total_rels, "llm_calls": llm_calls_spent,
                     "query_shapes": len(shapes)}
            run.stats = json.dumps(stats)
            run.status = "done"
            run.finished_at = datetime.now(UTC)
            inv.status = "ready"
            db.commit()
            _event(db, run.id, "summary",
                   f"Done: {total_kept} artifacts, {total_rels} relationships "
                   f"in {rounds} round(s).", stats)
    except Exception as e:
        try:
            run.status = "error"
            run.error = f"{e}\n{traceback.format_exc()[-2000:]}"
            run.finished_at = datetime.now(UTC)
            inv = (db.query(Investigation)
                   .filter(Investigation.id == investigation_id).first())
            if inv:
                inv.status = "ready"
            db.commit()
            _event(db, run.id, "summary", f"Run failed: {e}")
            # The scope above has closed, so reopen it briefly: a run that died
            # is exactly the run whose reason you need on the chain.
            if rid:
                with L.scope(rid, mandate, label=run_label):
                    L.record_agent_step("summary", f"Run failed: {e}",
                                        {"error": str(e)[:200],
                                         "trigger": trigger})
        except Exception:
            db.rollback()
    finally:
        db.close()


def launch_run(investigation_id: int, max_items: int = 25, max_rounds: int = 2,
               trigger: str = "manual"):
    # copy_context carries the session in: a thread started here does not
    # inherit contextvars, so without this the run would anchor to no session.
    ctx = contextvars.copy_context()
    t = threading.Thread(target=ctx.run,
                         args=(run_investigation_agent, investigation_id,
                               max_items, max_rounds, trigger),
                         daemon=True)
    t.start()
    return t


def launch_run_with_goal(investigation_id: int, goal: str, max_items: int = 12,
                         max_rounds: int = 2, trigger: str = "gap"):
    """Launch a run aimed at one specific question (a research gap)."""
    ctx = contextvars.copy_context()
    t = threading.Thread(target=ctx.run,
                         args=(run_investigation_agent, investigation_id,
                               max_items, max_rounds, trigger, None, goal),
                         daemon=True)
    t.start()
    return t
