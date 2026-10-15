"""The API.

One FastAPI app over the core spine: investigations, the collection loop, the
knowledge graph, review, the explainer, research gaps, the ledger, and the
taxonomy the whole thing is organised around.

Two cross-cutting choices:

* **Auditing is fail-open.** The ledger session dependency and the client-label
  middleware are both wrapped so a ledger or gateway outage degrades to
  unaudited work, never to a 500. The product must keep working when its own
  audit trail is broken — that is exactly when you need it working.
* **Every long job goes through the queue.** A request that would block for
  minutes enqueues and returns a job id.
"""
import json
import os
from collections import defaultdict
from datetime import UTC, datetime

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from . import domains as D
from . import drift as drift_mod
from . import explainer as expl
from . import jobqueue, ledger_api, llm, scheduler
from . import ledger as L
from . import research_gaps as gaps_mod
from . import yield_ as yld
from .database import SessionLocal, get_db, init_db
from .models import (
    AgentEvent,
    AgentRun,
    Artifact,
    ArtifactReview,
    CouncilRecord,
    EconomicAssessment,
    Explanation,
    Investigation,
    Job,
    ManagerRun,
    Relationship,
    ResearchGap,
)

os.makedirs("data", exist_ok=True)

app = FastAPI(title="Economics Cross-Domain Knowledge Mapper",
              dependencies=[Depends(ledger_api.open_session)])

app.include_router(ledger_api.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"],
    allow_headers=["*"],
)

_static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir, html=True),
              name="static")

    @app.get("/", include_in_schema=False)
    def index():
        from fastapi.responses import FileResponse
        return FileResponse(os.path.join(_static_dir, "index.html"))

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_json():
        return app.openapi()


@app.middleware("http")
async def _context_middleware(request: Request, call_next):
    """Carry the operator's frontend and this request's trace id into the work
    it causes.

    Research threads inherit both via copy_context; queued workers re-bind the
    label from the job payload. Both tokens are reset per request, so one
    request's label or trace can never leak into another's events -- a leaked
    trace would silently merge two unrelated chains in the audit view.
    """
    label = llm.set_client_label(request.headers.get("X-Econ-Client"))
    trace = L.set_trace(ledger_api.trace_id())
    try:
        response = await call_next(request)
    finally:
        L.reset_trace(trace)
        llm.reset_client_label(label)
    return response


@app.on_event("startup")
def _startup():
    init_db()
    jobqueue.start_worker()
    requeued = jobqueue.recover_stale()
    if requeued:
        print(f"[jobs] re-queued {requeued} job(s) with an expired lease")
    scheduler.start()


@app.on_event("shutdown")
def _shutdown():
    scheduler.stop()


# ==========================================================================
# health
# ==========================================================================
@app.get("/api/health")
def health():
    return {"ok": True, "service": "economics-mapper",
            "llm": llm.health(),
            "scheduler": scheduler.state(),
            "jobs": jobqueue.queue_state()["counts"]}


@app.get("/api/llm/health")
def llm_health():
    return llm.health()


# ==========================================================================
# taxonomy
# ==========================================================================
@app.get("/api/domains")
def list_domains():
    return {
        "domains": D.domain_catalog(),
        "hubs": list(D.HUB_DOMAINS),
        "channels": [
            {**c, "src_label": D.domain_label(c["src"]),
             "dst_label": D.domain_label(c["dst"])}
            for c in D.CHANNELS],
        "drivers": D.DRIVERS,
        "artifact_types": D.ARTIFACT_TYPES,
        "relationship_types": D.RELATIONSHIP_TYPES,
    }


@app.get("/api/domains/{domain_id}")
def domain_detail(domain_id: str):
    if domain_id not in D.DOMAINS:
        raise HTTPException(404, "no such domain")
    d = D.DOMAINS[domain_id]
    return {
        **d,
        "id": domain_id,
        "outbound": [{**c, "dst_label": D.domain_label(c["dst"])}
                     for c in D.channels_from(domain_id)],
        "inbound": [{**c, "src_label": D.domain_label(c["src"])}
                    for c in D.channels_into(domain_id)],
        "reached_by": [r for r in D.walk_transmission([domain_id], max_depth=3)],
    }


@app.get("/api/transmission/{domain_id}")
def transmission_from(domain_id: str, max_depth: int = 3,
                      min_strength: float = 0.5):
    """Walk the channel graph outward from a domain.

    This is the endpoint the domain engines build on: it is the prior, and the
    evidence a run collected either supports each hop or does not.
    """
    if domain_id not in D.DOMAINS:
        raise HTTPException(404, "no such domain")
    rows = D.walk_transmission([domain_id], max_depth=max_depth,
                               min_strength=min_strength)
    for r in rows:
        r["seed_label"] = D.domain_label(r["seed"])
        r["domain_label"] = D.domain_label(r["domain"])
        r["channel_label"] = r["via"]["channel"]
    return {"seed": domain_id, "seed_label": D.domain_label(domain_id),
            "reached": len({r["domain"] for r in rows}), "paths": rows}


# ==========================================================================
# investigations
# ==========================================================================
class InvestigationIn(BaseModel):
    title: str
    keywords: str = ""
    description: str = ""
    sources: str = "rss,web"
    preferred_domains: str = ""
    driver: str | None = None
    shock: str | None = None
    schedule_cron: str | None = None


class InvestigationUpdate(BaseModel):
    title: str | None = None
    keywords: str | None = None
    description: str | None = None
    sources: str | None = None
    preferred_domains: str | None = None
    driver: str | None = None
    shock: str | None = None
    auto_save_explanations: bool | None = None
    hidden: bool | None = None


def _inv_dict(inv: Investigation, db: Session) -> dict:
    counts = db.query(Artifact).filter(
        Artifact.investigation_id == inv.id).count()
    rels = db.query(Relationship).filter(
        Relationship.investigation_id == inv.id).count()
    runs = db.query(AgentRun).filter(
        AgentRun.investigation_id == inv.id).count()
    pending = db.query(Artifact).filter(
        Artifact.investigation_id == inv.id, Artifact.review == "pending").count()
    return {
        "id": inv.id, "title": inv.title, "keywords": inv.keywords,
        "description": inv.description, "sources": inv.sources,
        "preferred_domains": inv.preferred_domains,
        "domains": inv.domain_list, "driver": inv.driver, "shock": inv.shock,
        "status": inv.status, "created_at": inv.created_at,
        "schedule_enabled": inv.schedule_enabled, "schedule_cron": inv.schedule_cron,
        "auto_save_explanations": inv.auto_save_explanations,
        "hidden": inv.hidden,
        "counts": {"artifacts": counts, "relationships": rels, "runs": runs,
                   "pending": pending},
    }


@app.get("/api/investigations")
def list_investigations(db: Session = Depends(get_db),
                        include_hidden: bool = False):
    q = db.query(Investigation)
    if not include_hidden:
        q = q.filter(Investigation.hidden.is_(False))
    return {"investigations": [_inv_dict(i, db) for i in
                               q.order_by(Investigation.created_at.desc()).all()]}


@app.post("/api/investigations")
def create_investigation(body: InvestigationIn, db: Session = Depends(get_db)):
    if not (body.title or "").strip():
        raise HTTPException(400, "title is required")
    doms = D.normalize_domains(body.preferred_domains)
    unknown = [d for d in (body.preferred_domains or "").split(",")
               if d.strip() and d.strip().lower() not in D.DOMAINS]
    inv = Investigation(
        title=body.title.strip()[:300], keywords=body.keywords or "",
        description=body.description or "", sources=body.sources or "rss,web",
        preferred_domains=",".join(doms) if doms else "",
        driver=body.driver if body.driver in (D.DRIVERS or {}) else None,
        shock=body.shock,
        schedule_enabled=bool(body.schedule_cron), schedule_cron=body.schedule_cron)
    db.add(inv)
    db.commit()
    out = _inv_dict(inv, db)
    if unknown:
        # Naming a domain the taxonomy does not have is a mistake worth
        # reporting: the run would silently collect nothing for it.
        out["warnings"] = [f"unknown domain(s) ignored: {', '.join(unknown)}"]
    scheduler._sync_job(inv.id)
    try:
        from . import search_index as _si
        _si.index_investigation(db, inv)
    except Exception:
        pass
    return out


def _get_inv(db: Session, inv_id: int) -> Investigation:
    inv = db.query(Investigation).filter(Investigation.id == inv_id).first()
    if inv is None:
        raise HTTPException(404, "no such investigation")
    return inv


@app.get("/api/investigations/{inv_id}")
def get_investigation(inv_id: int, db: Session = Depends(get_db)):
    return _inv_dict(_get_inv(db, inv_id), db)


@app.put("/api/investigations/{inv_id}")
def update_investigation(inv_id: int, body: InvestigationUpdate,
                         db: Session = Depends(get_db)):
    inv = _get_inv(db, inv_id)
    data = body.model_dump(exclude_none=True)
    if "preferred_domains" in data:
        data["preferred_domains"] = ",".join(
            D.normalize_domains(data["preferred_domains"]))
    for k, v in data.items():
        setattr(inv, k, v)
    db.commit()
    return _inv_dict(inv, db)


@app.delete("/api/investigations/{inv_id}")
def delete_investigation(inv_id: int, db: Session = Depends(get_db)):
    inv = _get_inv(db, inv_id)
    db.delete(inv)
    db.commit()
    return {"ok": True, "deleted": inv_id}


class RunRequest(BaseModel):
    max_items: int = 25
    max_rounds: int = 2


@app.post("/api/investigations/{inv_id}/run")
def start_run(inv_id: int, body: RunRequest = Body(default=RunRequest()),
              db: Session = Depends(get_db)):
    _get_inv(db, inv_id)
    if not jobqueue.HANDLERS:
        raise HTTPException(503, "job queue is not running")
    job = jobqueue.enqueue("collect", {"investigation_id": inv_id,
                                       "max_items": body.max_items,
                                       "max_rounds": body.max_rounds,
                                       "trigger": "manual"},
                            investigation_id=inv_id)
    return {**job, "queued": True}


class ScheduleIn(BaseModel):
    enabled: bool
    cron: str | None = None
    max_items: int = 25
    max_rounds: int = 2


@app.put("/api/investigations/{inv_id}/schedule")
def set_schedule(inv_id: int, body: ScheduleIn):
    out = scheduler.set_schedule(inv_id, body.enabled, body.cron,
                                 body.max_items, body.max_rounds)
    if not out.get("ok"):
        raise HTTPException(400, out.get("reason", "cannot set schedule"))
    return out


# ==========================================================================
# runs and events
# ==========================================================================
def _run_dict(r: AgentRun) -> dict:
    return {"id": r.id, "investigation_id": r.investigation_id,
            "status": r.status, "trigger": r.trigger,
            "plan": json.loads(r.plan) if r.plan else None,
            "stats": json.loads(r.stats) if r.stats else None,
            "error": r.error, "started_at": r.started_at,
            "finished_at": r.finished_at}


@app.get("/api/runs")
def list_runs(investigation_id: int | None = None, limit: int = 50,
              db: Session = Depends(get_db)):
    q = db.query(AgentRun)
    if investigation_id:
        q = q.filter(AgentRun.investigation_id == investigation_id)
    return {"runs": [_run_dict(r) for r in
                     q.order_by(AgentRun.id.desc()).limit(limit).all()]}


@app.get("/api/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    r = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if r is None:
        raise HTTPException(404, "no such run")
    out = _run_dict(r)
    out["events"] = [{"id": e.id, "stage": e.stage, "message": e.message,
                      "data": json.loads(e.data_json) if e.data_json else None,
                      "created_at": e.created_at}
                     for e in db.query(AgentEvent)
                     .filter(AgentEvent.run_id == run_id)
                     .order_by(AgentEvent.id).all()]
    return out


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: int, since: int = 0, db: Session = Depends(get_db)):
    rows = (db.query(AgentEvent).filter(AgentEvent.run_id == run_id,
                                        AgentEvent.id > since)
            .order_by(AgentEvent.id).all())
    return {"events": [{"id": e.id, "stage": e.stage, "message": e.message,
                        "data": json.loads(e.data_json) if e.data_json else None,
                        "created_at": e.created_at} for e in rows],
            "cursor": rows[-1].id if rows else since}


# ==========================================================================
# artifacts and review
# ==========================================================================
def _artifact_dict(a: Artifact, db: Session | None = None) -> dict:
    return {
        "id": a.id, "investigation_id": a.investigation_id, "run_id": a.run_id,
        "title": a.title, "artifact_type": a.artifact_type, "url": a.url,
        "description": a.description, "source": a.source, "author": a.author,
        "date_published": a.date_published, "tags": a.tags,
        "domain": a.domain,
        "domain_label": D.domain_label(a.domain) if a.domain else None,
        "entities": a.entities_json or [],
        "direction": a.direction, "magnitude": a.magnitude,
        "horizon": a.horizon,
        "relevance": a.relevance, "relevance_reason": a.relevance_reason,
        "sentiment": a.sentiment, "review": a.review, "origin": a.origin,
        "drift": bool(a.drift), "created_at": a.created_at,
    }


@app.get("/api/investigations/{inv_id}/artifacts")
def list_artifacts(inv_id: int, review: str | None = None,
                   artifact_type: str | None = None, domain: str | None = None,
                   source: str | None = None, drift: bool | None = None,
                   search: str | None = None, limit: int = 500,
                   db: Session = Depends(get_db)):
    _get_inv(db, inv_id)
    q = db.query(Artifact).filter(Artifact.investigation_id == inv_id)
    if review:
        q = q.filter(Artifact.review == review)
    if artifact_type:
        q = q.filter(Artifact.artifact_type == artifact_type)
    if domain:
        q = q.filter(Artifact.domain == domain)
    if source:
        q = q.filter(Artifact.source.ilike(f"%{source}%"))
    if drift is not None:
        q = q.filter(Artifact.drift == (1 if drift else 0))
    if search:
        like = f"%{search}%"
        q = q.filter(or_(Artifact.title.ilike(like),
                         Artifact.description.ilike(like),
                         Artifact.tags.ilike(like)))
    rows = q.order_by(Artifact.date_published.desc().nullslast(),
                      Artifact.id.desc()).limit(limit).all()
    return {"artifacts": [_artifact_dict(a) for a in rows], "count": len(rows)}


@app.get("/api/artifacts/{artifact_id}")
def get_artifact(artifact_id: int, db: Session = Depends(get_db)):
    a = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    if a is None:
        raise HTTPException(404, "no such artifact")
    out = _artifact_dict(a)
    out["content"] = a.content
    rels = (db.query(Relationship)
            .filter((Relationship.source_id == a.id) |
                    (Relationship.target_id == a.id)).all())
    peer_ids = {r.target_id if r.source_id == a.id else r.source_id
                for r in rels}
    peers = {p.id: p for p in db.query(Artifact)
             .filter(Artifact.id.in_(peer_ids)).all()} if peer_ids else {}
    # Peer titles ride along so the overlay names connections instead of
    # numbering them. A connection list of bare ids is a lookup task handed
    # to the reader; titles make it a map you can walk.
    out["relationships"] = []
    for r in rels:
        peer_id = r.target_id if r.source_id == a.id else r.source_id
        peer = peers.get(peer_id)
        out["relationships"].append({
            "id": r.id, "type": r.relationship_type,
            "description": r.description, "confidence": r.confidence,
            "lag": r.lag, "peer_id": peer_id,
            "peer_title": peer.title if peer else "(deleted artifact)",
            "peer_domain": peer.domain if peer else None,
            "direction": "out" if r.source_id == a.id else "in",
        })
    out["reviews"] = [
        {"reviewer": r.reviewer, "decision": r.decision, "note": r.note,
         "created_at": r.created_at}
        for r in db.query(ArtifactReview)
        .filter(ArtifactReview.artifact_id == a.id)
        .order_by(ArtifactReview.created_at).all()]
    return out


class ArtifactIn(BaseModel):
    title: str
    url: str | None = None
    description: str = ""
    content: str | None = None
    source: str = "manual"
    author: str = ""
    artifact_type: str = "news"
    domain: str | None = None
    tags: str = ""
    entities: list[str] | None = None
    direction: str | None = None
    magnitude: float | None = None
    horizon: str | None = None


@app.post("/api/investigations/{inv_id}/artifacts")
def add_artifact(inv_id: int, body: ArtifactIn, db: Session = Depends(get_db)):
    _get_inv(db, inv_id)
    doms = D.normalize_domains(body.domain)
    a = Artifact(
        investigation_id=inv_id, title=body.title[:500], url=body.url,
        description=body.description[:2000], content=body.content,
        source=body.source[:200], author=body.author[:200],
        artifact_type=body.artifact_type, domain=doms[0] if doms else None,
        tags=body.tags[:500],
        entities_json=(body.entities or [])[:6] or None,
        direction=body.direction, magnitude=body.magnitude, horizon=body.horizon,
        relevance=1.0, review="accepted", origin="manual")
    db.add(a)
    db.commit()
    try:
        from . import search_index as _si
        _si.index_artifact(db, a)
    except Exception:
        pass
    return _artifact_dict(a)


class ReviewIn(BaseModel):
    decision: str           # accept|reject|restore
    reviewer: str = "operator"
    note: str = ""


@app.post("/api/artifacts/{artifact_id}/review")
def review_artifact(artifact_id: int, body: ReviewIn,
                    db: Session = Depends(get_db)):
    a = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    if a is None:
        raise HTTPException(404, "no such artifact")
    if body.decision == "accept":
        a.review = "accepted"
    elif body.decision == "reject":
        a.review = "rejected"
    elif body.decision == "restore":
        a.review = "pending"
    else:
        raise HTTPException(400, "decision must be accept, reject or restore")
    db.add(ArtifactReview(artifact_id=a.id, reviewer=body.reviewer,
                          decision=body.decision, note=body.note))
    db.commit()
    with L.scope(f"review-{a.id}",
                 L.Mandate(objective="human artifact review"),
                 label=f"review artifact {a.id}"):
        L.record_human_action("artifact.review",
                              {"artifact_id": a.id, "decision": body.decision,
                               "note": body.note}, actor=body.reviewer)
    return _artifact_dict(a)


@app.delete("/api/artifacts/{artifact_id}")
def delete_artifact(artifact_id: int, db: Session = Depends(get_db)):
    a = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    if a is None:
        raise HTTPException(404, "no such artifact")
    db.delete(a)
    db.commit()
    try:
        from . import search_index as _si
        _si.delete_doc(db, "artifact", artifact_id)
    except Exception:
        pass
    return {"ok": True, "deleted": artifact_id}


@app.post("/api/artifacts/{artifact_id}/clear-drift")
def clear_drift(artifact_id: int):
    return drift_mod.clear_drift(artifact_id)


@app.post("/api/investigations/{inv_id}/detect-drift")
def detect_drift(inv_id: int, limit: int = 25, db: Session = Depends(get_db)):
    _get_inv(db, inv_id)
    return drift_mod.detect_artifact_drift(inv_id, limit=limit)


@app.get("/api/investigations/{inv_id}/drift")
def drift_state(inv_id: int, db: Session = Depends(get_db)):
    _get_inv(db, inv_id)
    return drift_mod.drift_summary(inv_id)


# ==========================================================================
# relationships and the graph
# ==========================================================================
class RelationshipIn(BaseModel):
    investigation_id: int
    source_id: int
    target_id: int
    relationship_type: str = "references"
    description: str = ""
    confidence: float | None = None
    lag: int | None = None


@app.post("/api/relationships")
def create_relationship(body: RelationshipIn, db: Session = Depends(get_db)):
    if body.relationship_type not in D.RELATIONSHIP_TYPES:
        raise HTTPException(400, "unknown relationship type")
    ids = db.query(Artifact.id).filter(
        Artifact.id.in_([body.source_id, body.target_id])).all()
    if len(set(ids)) != 2:
        raise HTTPException(400, "both artifacts must exist")
    exists = db.query(Relationship).filter(
        Relationship.investigation_id == body.investigation_id,
        Relationship.source_id == body.source_id,
        Relationship.target_id == body.target_id,
        Relationship.relationship_type == body.relationship_type).first()
    if exists:
        raise HTTPException(409, "that edge already exists")
    r = Relationship(investigation_id=body.investigation_id,
                     source_id=body.source_id, target_id=body.target_id,
                     relationship_type=body.relationship_type,
                     description=body.description[:1000],
                     confidence=body.confidence, lag=body.lag)
    db.add(r)
    db.commit()
    return {"ok": True, "id": r.id}


@app.delete("/api/relationships/{rel_id}")
def delete_relationship(rel_id: int, db: Session = Depends(get_db)):
    r = db.query(Relationship).filter(Relationship.id == rel_id).first()
    if r is None:
        raise HTTPException(404, "no such relationship")
    db.delete(r)
    db.commit()
    return {"ok": True, "deleted": rel_id}


@app.get("/api/investigations/{inv_id}/graph")
def graph(inv_id: int, include_pending: bool = True, domain: str | None = None,
          db: Session = Depends(get_db)):
    """The knowledge graph, with domain on every node.

    ``include_pending`` is on by default because a map under triage is the
    honest state of the work; the graph does not pretend rejected material was
    never seen, it dims it.
    """
    _get_inv(db, inv_id)
    q = db.query(Artifact).filter(Artifact.investigation_id == inv_id)
    if not include_pending:
        q = q.filter(Artifact.review == "accepted")
    if domain:
        q = q.filter(Artifact.domain == domain)
    rows = q.all()
    ids = {a.id for a in rows}

    nodes = []
    for a in rows:
        nodes.append({
            "id": a.id, "label": a.title, "group": a.artifact_type,
            "domain": a.domain,
            "color": {"background": D.domain_color(a.domain),
                      "border": D.domain_color(a.domain),
                      "highlight": {"background": D.domain_color(a.domain),
                                    "border": "#ffffff"}},
            "value": max(1, int((a.relevance or 0) * 10) + 1),
            "size": 10 + (a.relevance or 0) * 16,
            "title": (f"{a.title}\n"
                      f"{D.domain_label(a.domain) if a.domain else 'unclassified'}"
                      f" · {a.source} · relevance {a.relevance or 0:.2f}"
                      + ("\n[drift flagged]" if a.drift else "")),
            "opacity": 0.28 if a.review == "rejected" else (
                0.6 if a.drift else 1.0),
            "review": a.review, "drift": bool(a.drift),
            "direction": a.direction, "magnitude": a.magnitude,
            "horizon": a.horizon, "tags": a.tags,
            "entities": a.entities_json or [],
            "url": a.url, "source": a.source,
            "date_published": str(a.date_published or ""),
        })

    edges = []
    for r in db.query(Relationship).filter(
            Relationship.investigation_id == inv_id).all():
        if r.source_id not in ids or r.target_id not in ids:
            continue
        meta = D.RELATIONSHIP_TYPES.get(r.relationship_type, {})
        edges.append({
            "id": r.id, "from": r.source_id, "to": r.target_id,
            "label": r.relationship_type, "title": r.description[:300],
            "dashes": bool(meta.get("dashes")),
            "color": {"color": meta.get("color", "#8b949e")},
            "confidence": r.confidence, "lag": r.lag,
        })

    return {
        "investigation_id": inv_id,
        "nodes": nodes, "edges": edges,
        "legend": {
            "domains": D.domain_catalog(),
            "types": D.ARTIFACT_TYPES,
            "relationships": D.RELATIONSHIP_TYPES,
        },
        "stats": _graph_stats(nodes, edges),
    }


def _graph_stats(nodes: list[dict], edges: list[dict]) -> dict:
    by_domain: dict[str, int] = defaultdict(int)
    for n in nodes:
        if n["domain"]:
            by_domain[n["domain"]] += 1
    cross = sum(1 for e in edges if e.get("label") == "transmits_to")
    return {
        "nodes": len(nodes), "edges": len(edges),
        "domains": len(by_domain),
        "by_domain": dict(by_domain),
        "cross_domain_edges": cross,
        # The number that matters for a cross-domain map: how many domains are
        # connected to each other rather than merely present.
        "connected_domains": len([
            d for d in by_domain
            if any(n["domain"] == d for n in nodes)]),
    }


@app.get("/api/investigations/{inv_id}/graph/clusters")
def graph_clusters(inv_id: int, db: Session = Depends(get_db)):
    """Domain clusters plus the communities inside them.

    Two levels because they answer different questions: *which domains are
    under-covered* (cluster sizes) and *within a domain, what is actually
    connected* (communities). A map with ten nodes and nine isolated ones has
    ten domains and zero structure, and only the second number says so.
    """
    _get_inv(db, inv_id)
    rows = db.query(Artifact).filter(Artifact.investigation_id == inv_id).all()
    ids = {a.id for a in rows}
    rels = [r for r in db.query(Relationship)
            .filter(Relationship.investigation_id == inv_id).all()
            if r.source_id in ids and r.target_id in ids]

    adj: dict[int, set] = defaultdict(set)
    for r in rels:
        adj[r.source_id].add(r.target_id)
        adj[r.target_id].add(r.source_id)

    # Communities: one label-propagation-ish sweep from each unvisited node.
    seen: set[int] = set()
    communities = []
    for a in sorted(rows, key=lambda x: -(len(adj[x.id]))):
        if a.id in seen:
            continue
        group, frontier = [], [a.id]
        seen.add(a.id)
        while frontier:
            cur = frontier.pop()
            group.append(cur)
            for nxt in adj[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        arts = [r for r in rows if r.id in group]
        communities.append({
            "size": len(group),
            "domains": sorted({r.domain for r in arts if r.domain}),
            "domain": arts[0].domain if arts else None,
            "label": (arts[0].title[:80] if arts else ""),
            "artifact_ids": group,
        })
    communities.sort(key=lambda c: -c["size"])

    isolated = [a.id for a in rows if not adj[a.id]]
    cross = _cross_domain_edge_count(inv_id)
    return {
        "investigation_id": inv_id,
        "communities": communities,
        "isolated": isolated,
        "isolated_count": len(isolated),
        # Density only means something alongside the isolated count: a dense
        # component plus fifty singletons reads as "connected" at a glance and
        # is not.
        "clustering": round(
            2 * len(rels) / max(1, len(rows) * (len(rows) - 1)), 4),
        "cross_domain_edges": cross,
        "cross_domain_ratio": round(cross / max(1, len(rels)), 3),
        "by_domain": _graph_stats([{"domain": a.domain} for a in rows], []),
    }


def _cross_domain_edge_count(inv_id: int) -> int:
    """Edges whose endpoints are in different domains.

    One explicit join on both endpoint conditions; see the note in
    ``explainer._cross_domain_edges`` for why the chained-alias form silently
    returns zero.
    """
    from sqlalchemy import and_
    from sqlalchemy.orm import aliased

    src = aliased(Artifact)
    dst = aliased(Artifact)
    db = SessionLocal()
    try:
        return (db.query(src.id)
                .join(Relationship,
                      and_(Relationship.source_id == src.id,
                           Relationship.target_id == dst.id))
                .filter(Relationship.investigation_id == inv_id,
                        src.domain.isnot(None), dst.domain.isnot(None),
                        src.domain != dst.domain)
                .count())
    finally:
        db.close()


# ==========================================================================
# search
# ==========================================================================
@app.get("/api/search")
def search(q: str, domain: str | None = None, limit: int = 40,
           db: Session = Depends(get_db)):
    """Search across every investigation's artifacts."""
    like = f"%{q}%"
    query = (db.query(Artifact)
             .filter(or_(Artifact.title.ilike(like),
                         Artifact.description.ilike(like),
                         Artifact.tags.ilike(like)))
             .limit(limit))
    if domain:
        query = query.filter(Artifact.domain == domain)
    rows = query.all()
    inv_ids = {a.investigation_id for a in rows}
    titles = {i.id: i.title for i in
              db.query(Investigation).filter(Investigation.id.in_(inv_ids)).all()} \
        if inv_ids else {}
    return {"results": [{**_artifact_dict(a),
                         "investigation_title": titles.get(a.investigation_id)}
                        for a in rows]}


@app.get("/api/search/suggest")
def suggest(q: str, limit: int = 8, db: Session = Depends(get_db)):
    """Suggestions drawn from stored entities and tags, not from a hardcoded
    list — the useful queries are the ones this corpus can actually answer."""
    like = f"%{q}%"
    tags: set[str] = set()
    for a in db.query(Artifact).filter(
            Artifact.tags.ilike(like)).limit(200).all():
        tags.update(t.strip() for t in (a.tags or "").split(",") if t.strip())
    ents: set[str] = set()
    for a in db.query(Artifact).limit(300).all():
        for e in (a.entities_json or []):
            if q.lower() in str(e).lower():
                ents.add(str(e))
    doms = [D.DOMAINS[d]["label"] for d in D.DOMAIN_IDS if q.lower() in d
            or q.lower() in D.DOMAINS[d]["label"].lower()]
    return {"suggestions": (sorted(tags)[:limit] + sorted(ents)[:limit]
                            + doms[:limit])}


# ==========================================================================
# explainer
# ==========================================================================
class ExplainIn(BaseModel):
    question: str
    mode: str = "explain"
    depth: str = "balanced"
    audience: str = "intermediate"


@app.post("/api/investigations/{inv_id}/explain")
def explain(inv_id: int, body: ExplainIn, db: Session = Depends(get_db)):
    _get_inv(db, inv_id)
    if not (body.question or "").strip():
        raise HTTPException(400, "question is required")
    if body.mode not in expl.MODES:
        raise HTTPException(400, f"mode must be one of {sorted(expl.MODES)}")
    if body.audience not in expl.AUDIENCE_HINTS:
        raise HTTPException(400, f"audience must be one of "
                                 f"{sorted(expl.AUDIENCE_HINTS)}")
    if body.depth not in expl.DEPTH_PLAN:
        raise HTTPException(400, f"depth must be one of {sorted(expl.DEPTH_PLAN)}")
    job = jobqueue.enqueue("explain", {
        "investigation_id": inv_id, "question": body.question,
        "mode": body.mode, "depth": body.depth, "audience": body.audience},
        investigation_id=inv_id)
    return {**job, "queued": True}


def _exp_dict(e: Explanation, full: bool = False) -> dict:
    out = {
        "id": e.id, "investigation_id": e.investigation_id,
        "question": e.question, "status": e.status, "mode": e.mode,
        "depth": e.depth, "audience": e.audience,
        "confidence": e.confidence, "domains": json.loads(e.domains_json or "[]"),
        "dropped_citations": e.dropped_citations,
        "bookmarked": bool(e.bookmarked), "watched": bool(e.watched),
        "parent_id": e.parent_id, "thread_id": e.thread_id,
        "created_at": e.created_at,
        "answer": e.answer,
    }
    if full:
        out.update({"sources": json.loads(e.sources_json or "[]"),
                    "critique": e.critique, "roadmap": e.roadmap,
                    "quiz": json.loads(e.quiz) if e.quiz else None,
                    "trace": json.loads(e.trace or "{}")})
    return out


@app.get("/api/investigations/{inv_id}/explanations")
def list_explanations(inv_id: int, limit: int = 50,
                      db: Session = Depends(get_db)):
    _get_inv(db, inv_id)
    rows = (db.query(Explanation)
            .filter(Explanation.investigation_id == inv_id)
            .order_by(Explanation.id.desc()).limit(limit).all())
    return {"explanations": [_exp_dict(e) for e in rows]}


@app.get("/api/explanations/{exp_id}")
def get_explanation(exp_id: int, db: Session = Depends(get_db)):
    e = db.query(Explanation).filter(Explanation.id == exp_id).first()
    if e is None:
        raise HTTPException(404, "no such explanation")
    return _exp_dict(e, full=True)


@app.delete("/api/explanations/{exp_id}")
def delete_explanation(exp_id: int, db: Session = Depends(get_db)):
    e = db.query(Explanation).filter(Explanation.id == exp_id).first()
    if e is None:
        raise HTTPException(404, "no such explanation")
    db.delete(e)
    db.commit()
    try:
        from . import search_index as _si
        _si.delete_doc(db, "explanation", exp_id)
    except Exception:
        pass
    return {"ok": True, "deleted": exp_id}


class FollowupIn(BaseModel):
    question: str


@app.post("/api/explanations/{exp_id}/followup")
def followup(exp_id: int, body: FollowupIn, db: Session = Depends(get_db)):
    e = db.query(Explanation).filter(Explanation.id == exp_id).first()
    if e is None:
        raise HTTPException(404, "no such explanation")
    job = jobqueue.enqueue("explain", {"investigation_id": e.investigation_id,
                                       "question": body.question,
                                       "mode": e.mode, "depth": e.depth,
                                       "audience": e.audience,
                                       "parent_id": e.id},
                            investigation_id=e.investigation_id)
    return {**job, "queued": True}


@app.get("/api/explanations/{exp_id}/thread")
def explanation_thread(exp_id: int, db: Session = Depends(get_db)):
    e = db.query(Explanation).filter(Explanation.id == exp_id).first()
    if e is None:
        raise HTTPException(404, "no such explanation")
    tid = e.thread_id or e.id
    rows = (db.query(Explanation)
            .filter((Explanation.id == tid) |
                    (Explanation.thread_id == tid) |
                    (Explanation.parent_id == e.id))
            .order_by(Explanation.id).all())
    return {"thread_id": tid, "explanations": [_exp_dict(r, full=True)
                                                for r in rows]}


@app.post("/api/explanations/{exp_id}/quiz")
def make_quiz(exp_id: int, n: int = 3):
    items = expl.quiz_for(exp_id, n=n)
    return {"quiz": items, "count": len(items)}


@app.post("/api/explanations/{exp_id}/bookmark")
def bookmark(exp_id: int, db: Session = Depends(get_db)):
    e = db.query(Explanation).filter(Explanation.id == exp_id).first()
    if e is None:
        raise HTTPException(404, "no such explanation")
    e.bookmarked = not e.bookmarked
    db.commit()
    return {"ok": True, "bookmarked": bool(e.bookmarked)}


@app.post("/api/explanations/{exp_id}/watch")
def watch(exp_id: int, watched: bool = True, db: Session = Depends(get_db)):
    return drift_mod.mark_watched(exp_id, watched)


@app.get("/api/investigations/{inv_id}/roadmap")
def roadmap(inv_id: int, db: Session = Depends(get_db)):
    _get_inv(db, inv_id)
    return expl.investigation_roadmap(inv_id)


@app.get("/api/investigations/{inv_id}/suggestions")
def suggestions(inv_id: int, n: int = 5, db: Session = Depends(get_db)):
    _get_inv(db, inv_id)
    return {"questions": expl.question_suggestions(inv_id, n=n)}


# ==========================================================================
# research gaps
# ==========================================================================
@app.get("/api/investigations/{inv_id}/research-gaps")
def research_gaps(inv_id: int, refresh: bool = False,
                  db: Session = Depends(get_db)):
    """Two lists, never one merged list.

    ``picture`` is the coverage computed live from stored rows; ``gaps`` is the
    lifecycle rows (status, evidence, whether a run was launched at them).
    Collapsing them would lose the status, and merging them under one key would
    silently drop one of the two — which is how "nothing is stored" ends up
    looking like "nothing is missing".
    """
    _get_inv(db, inv_id)
    if refresh:
        gaps_mod.persist_gaps(inv_id)
    picture = gaps_mod.compute_gaps(inv_id)
    stored = gaps_mod.list_gaps(inv_id)
    return {**picture, "stored": stored["gaps"], "rows": stored["gaps"]}


class GapStatusIn(BaseModel):
    status: str


@app.post("/api/investigations/{inv_id}/research-gaps/status")
def gap_status(inv_id: int, body: GapStatusIn, gap_id: int = 0,
               db: Session = Depends(get_db)):
    if not gap_id:
        raise HTTPException(400, "gap_id is required")
    out = gaps_mod.set_status(gap_id, body.status)
    if not out.get("ok"):
        raise HTTPException(400, out.get("reason"))
    return out


@app.post("/api/investigations/{inv_id}/research-gaps/run")
def run_gap(inv_id: int, gap_id: int, db: Session = Depends(get_db)):
    _get_inv(db, inv_id)
    gap = db.query(ResearchGap).filter(ResearchGap.id == gap_id).first()
    if gap is None:
        raise HTTPException(404, "no such gap")
    goal = gaps_mod.gap_query({"domain": gap.domain, "dimension": gap.dimension})
    gap.status = "investigating"
    db.commit()
    job = jobqueue.enqueue("collect", {"investigation_id": inv_id,
                                       "max_items": 12, "max_rounds": 2,
                                       "trigger": "gap", "goal": goal},
                            investigation_id=inv_id)
    return {**job, "goal": goal, "queued": True}


# ==========================================================================
# yield
# ==========================================================================
@app.get("/api/investigations/{inv_id}/query-yields")
def query_yields(inv_id: int, db: Session = Depends(get_db)):
    _get_inv(db, inv_id)
    return yld.investigation_yield(db, inv_id)


# ==========================================================================
# ledger / audit
# ==========================================================================
@app.get("/api/investigations/{inv_id}/timeline")
def investigation_timeline(inv_id: int, limit: int = 500,
                           db: Session = Depends(get_db)):
    """Interleave every ledger chain an investigation touched.

    One stream rather than per-run chains, because the question an operator
    actually has is "what happened to this investigation", and that spans runs,
    explanations and reviews.
    """
    _get_inv(db, inv_id)
    chains = []
    for r in db.query(AgentRun).filter(
            AgentRun.investigation_id == inv_id).all():
        chains.append(f"run-{r.id}")
    for e in db.query(Explanation).filter(
            Explanation.investigation_id == inv_id).all():
        chains.append(f"exp-{e.id}")

    events = []
    verified = {}
    for cid in chains:
        tl = L.timeline(cid, limit=limit)
        if not tl.get("ok"):
            continue
        verified[cid] = tl.get("verified", {}).get("ok")
        events.extend(tl.get("events", []))
    events.sort(key=lambda e: e.get("ts") or "", reverse=True)
    return {"investigation_id": inv_id, "chains": chains,
            "verified": verified, "events": events[:limit]}


@app.get("/api/integrity")
def integrity(limit: int = 50):
    return L.monitor_integrity(limit=limit)


# ==========================================================================
# jobs
# ==========================================================================
@app.get("/api/jobs")
def jobs():
    return jobqueue.queue_state()


@app.get("/api/jobs/worker")
def worker_state():
    """Is the background worker alive? The queue depth tells you work is
    waiting; this tells you anything is coming to get it. Split out because
    'queued but nothing runs' has two completely different causes (dead
    worker vs one giant synchronous job) and the fix differs."""
    import threading
    names = sorted(t.name for t in threading.enumerate())
    return {"started": jobqueue._started,
            "worker_enabled": jobqueue.WORKER_ENABLED,
            "threads": names,
            "worker_alive": any(n == "job-worker" for n in names)}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if job is None:
        raise HTTPException(404, "no such job")
    if job.status == "running":
        raise HTTPException(409, "a running job cannot be cancelled; its lease "
                                 "will expire and it will be retried")
    job.status = "cancelled"
    job.finished_at = datetime.now(UTC)
    db.commit()
    return {"ok": True, "job_id": job_id, "status": "cancelled"}


@app.get("/api/council-records/{run_id}")
def council_record(run_id: int, db: Session = Depends(get_db)):
    row = (db.query(CouncilRecord)
           .filter(CouncilRecord.run_id == run_id).first())
    if row is None:
        return {"ok": False, "reason": "no council record for this run",
                "record": None}
    return {"ok": True, "record": json.loads(row.payload_json or "{}")}


# ==========================================================================
# summary
# ==========================================================================
@app.get("/api/investigations/{inv_id}/summary")
def summary(inv_id: int, db: Session = Depends(get_db)):
    """The executive summary: what is in the map and what is missing from it.

    Read from stored rows so every number traces to something a reviewer can
    click — the same discipline the rest of the app follows.
    """
    inv = _get_inv(db, inv_id)
    rows = db.query(Artifact).filter(Artifact.investigation_id == inv_id).all()
    accepted = [a for a in rows if a.review == "accepted"]
    pending = [a for a in rows if a.review == "pending"]

    by_domain: dict[str, list] = defaultdict(list)
    for a in rows:
        if a.domain:
            by_domain[a.domain].append(a)

    directional = [a for a in rows if a.direction and a.direction != "flat"]
    by_driver = [a for a in rows if a.direction]
    strongest = sorted([a for a in by_driver if a.magnitude is not None],
                       key=lambda a: -a.magnitude)[:5]

    rels = db.query(Relationship).filter(
        Relationship.investigation_id == inv_id).all()

    return {
        "investigation_id": inv_id, "title": inv.title,
        "brief": {"keywords": inv.keywords, "description": inv.description,
                  "driver": inv.driver, "shock": inv.shock},
        "counts": {"artifacts": len(rows), "accepted": len(accepted),
                   "pending": len(pending),
                   "rejected": sum(1 for a in rows if a.review == "rejected"),
                   "drift": sum(1 for a in rows if a.drift),
                   "relationships": len(rels)},
        "domains": [
            {"id": d["id"], "label": d["label"], "short": d["short"],
             "color": d["color"], "artifacts": len(by_domain.get(d["id"], [])),
             "accepted": sum(1 for a in by_domain.get(d["id"], [])
                             if a.review == "accepted"),
             "hub": d["hub"],
             "top": [{"id": a.id, "title": a.title, "relevance": a.relevance,
                      "direction": a.direction, "magnitude": a.magnitude,
                      "horizon": a.horizon}
                     for a in sorted(by_domain.get(d["id"], []),
                                     key=lambda a: -(a.relevance or 0))[:3]]}
            for d in D.domain_catalog()],
        "strongest_signals": [
            {"id": a.id, "title": a.title, "domain": a.domain,
             "domain_label": D.domain_label(a.domain),
             "direction": a.direction, "magnitude": a.magnitude,
             "horizon": a.horizon, "source": a.source, "url": a.url}
            for a in strongest],
        "directional_count": len(directional),
        "explanations": db.query(Explanation).filter(
            Explanation.investigation_id == inv_id).count(),
        "runs": db.query(AgentRun).filter(
            AgentRun.investigation_id == inv_id).count(),
    }


@app.get("/api/investigations/{inv_id}/dossier")
def dossier(inv_id: int, fmt: str = "markdown", db: Session = Depends(get_db)):
    """The full working document, as Markdown.

    States the brief verbatim, every run's plan, every artifact with why it was
    kept, and the coverage picture — the same material as the summary, with the
    working shown.
    """
    inv = _get_inv(db, inv_id)
    s = summary(inv_id, db=db)
    lines = [f"# {inv.title}", ""]
    if inv.description:
        lines += ["> " + inv.description.replace("\n", "\n> "), ""]
    lines += [
        "## Brief", "",
        f"- **Keywords**: {inv.keywords or '—'}",
        f"- **Domains**: {', '.join(inv.domain_list) or 'any'}",
        f"- **Sources**: {inv.sources}",
    ]
    if inv.driver:
        d = D.DRIVERS.get(inv.driver, {})
        lines += [f"- **Driver**: {d.get('label', inv.driver)}"
                  + (f" — {d.get('description','')}" if d.get("description") else "")]
    if inv.shock:
        lines += [f"- **Shock**: {inv.shock}"]
    lines += ["", "## Coverage by domain", "",
              "| Domain | Artifacts | Accepted | Role |",
              "|---|---:|---:|---|"]
    for d in s["domains"]:
        if d["artifacts"] or d["hub"]:
            lines.append(f"| {d['label']} | {d['artifacts']} | {d['accepted']} "
                         f"| {'hub' if d['hub'] else ''} |")
    missing = [d["label"] for d in s["domains"] if not d["artifacts"]]
    if missing:
        lines += ["", f"**No evidence at all**: {', '.join(missing)}"]

    if s["strongest_signals"]:
        lines += ["", "## Strongest directional signals", ""]
        for sig in s["strongest_signals"]:
            lines.append(f"- **{sig['direction']}** / magnitude "
                         f"{sig['magnitude']} / {sig['horizon'] or '?'} — "
                         f"{sig['title']} ({sig['domain_label']})")

    runs = db.query(AgentRun).filter(
        AgentRun.investigation_id == inv_id).order_by(AgentRun.id).all()
    if runs:
        lines += ["", "## Runs", ""]
        for r in runs:
            plan = json.loads(r.plan) if r.plan else {}
            stats = json.loads(r.stats) if r.stats else {}
            lines.append(f"### Run {r.id} ({r.status}, {r.trigger})")
            lines.append("")
            lines.append(f"- kept {stats.get('artifacts_kept', '?')} artifacts, "
                         f"{stats.get('relationships', '?')} relationships in "
                         f"{stats.get('rounds', '?')} round(s)")
            if plan.get("rationale"):
                lines.append(f"- rationale: {plan['rationale']}")
            for q in (plan.get("queries") or []):
                srcs = ", ".join(q.get("sources") or [])
                lines.append(f"- `{q.get('text')}` via {srcs}"
                             + (f" → {q['domain']}" if q.get("domain") else ""))
            lines.append("")

    rows = db.query(Artifact).filter(
        Artifact.investigation_id == inv_id).order_by(Artifact.id).all()
    if rows:
        lines += ["", "## Artifacts", ""]
        for a in rows:
            lines.append(f"### [{a.id}] {a.title}")
            bits = [f"**{a.review}**", a.source or "?"]
            if a.domain:
                bits.append(D.domain_label(a.domain))
            if a.direction:
                bits.append(f"{a.direction}/{a.horizon or '?'}"
                            + (f"/{a.magnitude}" if a.magnitude is not None else ""))
            lines.append(f"*{' · '.join(bits)}*")
            if a.description:
                lines += ["", a.description[:800]]
            if a.relevance_reason:
                lines += ["", f"_Kept because_: {a.relevance_reason}"]
            if a.entities_json:
                lines += ["", "Entities: " + ", ".join(a.entities_json)]
            if a.url:
                lines += ["", f"<{a.url}>"]
            lines.append("")

    text = "\n".join(lines)
    if fmt == "markdown":
        return Response(text, media_type="text/markdown")
    return {"dossier": text, "lines": len(lines)}


# ==========================================================================
# transmission assessments (economics equivalent of security assessments)
# ==========================================================================
class AssessIn(BaseModel):
    driver: str | None = None
    shock: str | None = None


def _assessment_dict(r: EconomicAssessment, full: bool = False) -> dict:
    out = {
        "id": r.id, "investigation_id": r.investigation_id,
        "driver": r.driver,
        "driver_label": (D.DRIVERS.get(r.driver, {}).get("label")
                         if r.driver else None),
        "shock": r.shock,
        "overall_pct": r.overall_pct, "coverage_pct": r.coverage_pct,
        "confidence_pct": r.confidence_pct, "exposure_pct": r.exposure_pct,
        "taxonomy_version": r.taxonomy_version,
        "taxonomy_fingerprint": r.taxonomy_fingerprint,
        "decision": r.decision, "decided_by": r.decided_by,
        "decided_at": r.decided_at, "decision_note": r.decision_note,
        "decision_expires_at": r.decision_expires_at,
        "supersedes_id": r.supersedes_id,
        "created_at": r.created_at,
    }
    if full:
        out.update({
            "scoring": json.loads(r.scoring_json or "{}"),
            "hypotheses": json.loads(r.hypotheses_json or "{}"),
            "scenarios": json.loads(r.scenarios_json or "[]"),
            "channels": json.loads(r.channels_json or "[]"),
            "signals": json.loads(r.signals_json or "[]"),
            "diagrams": json.loads(r.diagrams_json or "{}"),
            "markdown": r.markdown,
        })
    return out


@app.post("/api/investigations/{inv_id}/assess")
def request_assessment(inv_id: int, body: AssessIn = Body(default=AssessIn()),
                       db: Session = Depends(get_db)):
    inv = _get_inv(db, inv_id)
    driver = body.driver or inv.driver
    if driver and driver not in D.DRIVERS:
        raise HTTPException(400, f"unknown driver. Known: {sorted(D.DRIVERS)}")
    job = jobqueue.enqueue("assess", {
        "investigation_id": inv_id, "driver": driver,
        "shock": body.shock if body.shock is not None else inv.shock,
    }, investigation_id=inv_id)
    return {**job, "queued": True}


@app.get("/api/investigations/{inv_id}/assessments")
def list_assessments(inv_id: int, db: Session = Depends(get_db)):
    _get_inv(db, inv_id)
    rows = (db.query(EconomicAssessment)
            .filter(EconomicAssessment.investigation_id == inv_id)
            .order_by(EconomicAssessment.id.desc()).all())
    return {"assessments": [_assessment_dict(r) for r in rows]}


@app.get("/api/assessments/{assessment_id}")
def get_assessment(assessment_id: int, db: Session = Depends(get_db)):
    r = db.query(EconomicAssessment).filter(
        EconomicAssessment.id == assessment_id).first()
    if r is None:
        raise HTTPException(404, "no such assessment")
    return _assessment_dict(r, full=True)


@app.get("/api/assessments/{assessment_id}/analysis")
def assessment_analysis(assessment_id: int, db: Session = Depends(get_db)):
    """Scoring + hypotheses + scenarios without the markdown body."""
    r = db.query(EconomicAssessment).filter(
        EconomicAssessment.id == assessment_id).first()
    if r is None:
        raise HTTPException(404, "no such assessment")
    full = _assessment_dict(r, full=True)
    return {k: full[k] for k in ("id", "investigation_id", "driver",
                                 "driver_label", "shock", "scoring",
                                 "hypotheses", "scenarios", "channels",
                                 "signals", "diagrams", "overall_pct",
                                 "coverage_pct", "confidence_pct",
                                 "exposure_pct")}


@app.get("/api/assessments/{assessment_id}/markdown")
def assessment_markdown(assessment_id: int, db: Session = Depends(get_db)):
    r = db.query(EconomicAssessment).filter(
        EconomicAssessment.id == assessment_id).first()
    if r is None:
        raise HTTPException(404, "no such assessment")
    return Response(r.markdown or "", media_type="text/markdown")


@app.get("/api/assessments/{assessment_id}/hypotheses")
def assessment_hypotheses(assessment_id: int, db: Session = Depends(get_db)):
    r = db.query(EconomicAssessment).filter(
        EconomicAssessment.id == assessment_id).first()
    if r is None:
        raise HTTPException(404, "no such assessment")
    return json.loads(r.hypotheses_json or "{}")


@app.get("/api/assessments/{assessment_id}/scenarios")
def assessment_scenarios(assessment_id: int, db: Session = Depends(get_db)):
    r = db.query(EconomicAssessment).filter(
        EconomicAssessment.id == assessment_id).first()
    if r is None:
        raise HTTPException(404, "no such assessment")
    return {"scenarios": json.loads(r.scenarios_json or "[]")}


@app.get("/api/council/panel")
def council_panel():
    from . import council as council_mod
    return council_mod.council_panel()


class DecisionIn(BaseModel):
    decision: str  # accept|guardrails|reject|exception
    decided_by: str = "operator"
    note: str = ""
    expires_at: str | None = None


@app.post("/api/assessments/{assessment_id}/decision")
def assessment_decision(assessment_id: int, body: DecisionIn,
                        db: Session = Depends(get_db)):
    r = db.query(EconomicAssessment).filter(
        EconomicAssessment.id == assessment_id).first()
    if r is None:
        raise HTTPException(404, "no such assessment")
    if body.decision not in ("accept", "guardrails", "reject", "exception"):
        raise HTTPException(400, "decision must be accept|guardrails|reject|exception")
    r.decision = body.decision
    r.decided_by = body.decided_by
    r.decision_note = body.note
    r.decided_at = datetime.now(UTC)
    if body.expires_at:
        try:
            r.decision_expires_at = datetime.fromisoformat(body.expires_at)
        except ValueError as ve:
            raise HTTPException(400, "expires_at must be ISO-8601") from ve
    elif body.decision == "exception":
        raise HTTPException(400, "exception requires expires_at")
    else:
        r.decision_expires_at = None
    db.commit()
    with L.scope(f"assess-{r.id}",
                 L.Mandate(objective="human assessment decision"),
                 label=f"decision on assessment {r.id}"):
        L.record_human_action("assessment.decision",
                              {"assessment_id": r.id, "decision": body.decision,
                               "note": body.note}, actor=body.decided_by)
    return _assessment_dict(r)


@app.post("/api/assessments/{assessment_id}/rescore")
def assessment_rescore(assessment_id: int, db: Session = Depends(get_db)):
    """Persisted re-score creates a NEW row pointing at the old one, never
    edits in place. History stays immutable."""
    old = db.query(EconomicAssessment).filter(
        EconomicAssessment.id == assessment_id).first()
    if old is None:
        raise HTTPException(404, "no such assessment")
    from . import assessment as assess_mod
    out = assess_mod.build_assessment(
        db, old.investigation_id, driver=old.driver, shock=old.shock)
    import json as _json
    rec = EconomicAssessment(
        investigation_id=old.investigation_id, driver=out.get("driver"),
        shock=out.get("shock"), requested_by="rescore",
        overall_pct=out["overall_pct"], coverage_pct=out["coverage_pct"],
        confidence_pct=out["confidence_pct"], exposure_pct=out["exposure_pct"],
        scoring_json=_json.dumps(out["scoring"]),
        hypotheses_json=_json.dumps(out["hypotheses"]),
        scenarios_json=_json.dumps(out["scenarios"]),
        channels_json=_json.dumps(out["channels"]),
        signals_json=_json.dumps(out["signals"]),
        markdown=out["markdown"], diagrams_json=_json.dumps(out["diagrams"]),
        taxonomy_version=out["taxonomy_version"],
        taxonomy_fingerprint=out["taxonomy_fingerprint"],
        supersedes_id=old.id)
    db.add(rec)
    db.commit()
    drifted = (rec.taxonomy_fingerprint != old.taxonomy_fingerprint
               or rec.overall_pct != old.overall_pct)
    return {"ok": True, "assessment_id": rec.id, "supersedes": old.id,
            "drifted": drifted, "overall_pct": rec.overall_pct,
            "old_overall_pct": old.overall_pct}


@app.get("/api/assessments/{assessment_id}/compare/{other_id}")
def assessment_compare(assessment_id: int, other_id: int,
                       db: Session = Depends(get_db)):
    a = db.query(EconomicAssessment).filter(
        EconomicAssessment.id == assessment_id).first()
    b = db.query(EconomicAssessment).filter(
        EconomicAssessment.id == other_id).first()
    if a is None or b is None:
        raise HTTPException(404, "no such assessment")
    sa, sb = json.loads(a.scoring_json or "{}"), json.loads(b.scoring_json or "{}")
    keys = ("overall_pct", "coverage_pct", "confidence_pct", "exposure_pct",
            "prior_coverage_pct", "directional", "bridged", "prior_channels")
    delta = {k: round((sa.get(k) or 0) - (sb.get(k) or 0), 2) for k in keys}
    return {"a": _assessment_dict(a), "b": _assessment_dict(b),
            "delta_a_minus_b": delta,
            "taxonomy_drifted": a.taxonomy_fingerprint != b.taxonomy_fingerprint}


# ==========================================================================
# leadership board (portfolio view, aggregations only — no new judgments)
# ==========================================================================
@app.get("/api/leadership/board")
def leadership_board(investigation_id: int | None = None,
                     db: Session = Depends(get_db)):
    from . import leadership as _lead
    return _lead.board(db, investigation_id)


@app.get("/api/leadership/board.md")
def leadership_board_md(investigation_id: int | None = None,
                        db: Session = Depends(get_db)):
    from . import leadership as _lead
    return Response(_lead.export_markdown(db, investigation_id),
                    media_type="text/markdown")


@app.get("/api/leadership/exposure")
def leadership_exposure(investigation_id: int | None = None,
                        db: Session = Depends(get_db)):
    from . import leadership as _lead
    return _lead.exposure_position(db, investigation_id)


@app.get("/api/leadership/decision-queue")
def leadership_decisions(investigation_id: int | None = None,
                         db: Session = Depends(get_db)):
    from . import leadership as _lead
    return _lead.decision_queue(db, investigation_id)


@app.get("/api/leadership/health")
def leadership_health(investigation_id: int | None = None,
                      db: Session = Depends(get_db)):
    from . import leadership as _lead
    return {"coverage": _lead.coverage_health(db, investigation_id),
            "integrity": _lead.system_integrity(db)}


@app.get("/api/leadership/alerts")
def leadership_alerts(investigation_id: int | None = None,
                      db: Session = Depends(get_db)):
    from . import leadership as _lead
    return _lead.alerts(db, investigation_id)


@app.get("/api/leadership/graph")
def leadership_graph(investigation_id: int | None = None,
                     db: Session = Depends(get_db)):
    from . import leadership as _lead
    return _lead.knowledge_graph(db, investigation_id)


@app.get("/api/leadership/knowledge-graph")
def leadership_knowledge_graph(investigation_ids: str | None = None,
                               db: Session = Depends(get_db)):
    """The global knowledge graph across sessions, with investigation hubs.

    Node ids are namespaced (``a:`` / ``inv:``). Pass a comma-separated
    ``investigation_ids`` to scope it; omit for the whole estate. Capped with
    ``truncated: true`` when the estate outgrows the view.
    """
    from . import leadership as _lead
    ids = _parse_ids(investigation_ids)
    return _lead.global_graph(db, ids)


@app.get("/api/leadership/knowledge-graph/clusters")
def leadership_knowledge_graph_clusters(
        mode: str = "category", investigation_ids: str | None = None,
        db: Session = Depends(get_db)):
    """Cluster the estate-wide graph: ``category`` (by domain) or
    ``similarity`` (cross-session entity bridges)."""
    if mode not in ("category", "similarity"):
        raise HTTPException(400, "mode must be category or similarity")
    from . import leadership as _lead
    ids = _parse_ids(investigation_ids)
    return _lead.global_clusters(db, ids, mode)


def _parse_ids(raw: str | None) -> list[int] | None:
    """Comma-separated investigation ids, or None for the whole estate."""
    if not raw:
        return None
    try:
        return [int(x) for x in raw.split(",") if x.strip()]
    except ValueError as ve:
        raise HTTPException(
            400, "investigation_ids must be comma-separated ints") from ve


# ==========================================================================
# design documents (served from design/, never from user-supplied paths)
# ==========================================================================
@app.get("/api/design/docs")
def design_docs():
    from . import design_docs as _docs
    return _docs.list_docs()


@app.get("/api/design/docs/{doc_id}")
def design_doc(doc_id: str):
    from . import design_docs as _docs
    out = _docs.get_doc(doc_id)
    if not out.get("ok"):
        raise HTTPException(404, out.get("reason", "no such document"))
    return out


# ==========================================================================
# agentic manager (one command -> N investigations + a summary)
# ==========================================================================
class ManagerParseIn(BaseModel):
    command: str


@app.post("/api/manager/parse")
def manager_parse(body: ManagerParseIn):
    from . import manager as _mgr
    return _mgr.parse_command(body.command)


@app.post("/api/manager/run")
def manager_run(body: ManagerParseIn, max_items: int = 12, max_rounds: int = 1,
                db: Session = Depends(get_db)):
    from . import manager as _mgr
    plan = _mgr.parse_command(body.command)
    v = _mgr.validate_plan(plan)
    if not v.get("ok"):
        raise HTTPException(400, plan.get("reason", v.get("reason")))
    return _mgr.run_plan(db, plan, {"max_items": max_items,
                                    "max_rounds": max_rounds})


@app.get("/api/manager/runs")
def manager_runs(db: Session = Depends(get_db)):
    rows = (db.query(ManagerRun).order_by(ManagerRun.id.desc()).limit(50).all())
    return {"runs": [{"id": r.id, "command": r.command, "status": r.status,
                      "summary_investigation_id": r.summary_investigation_id,
                      "created_at": r.created_at, "finished_at": r.finished_at}
                     for r in rows]}


@app.get("/api/manager/runs/{run_id}")
def manager_get(run_id: int, db: Session = Depends(get_db)):
    from . import manager as _mgr
    return _mgr.run_timeline(db, run_id)


@app.post("/api/manager/runs/{run_id}/compile")
def manager_compile(run_id: int, db: Session = Depends(get_db)):
    from . import manager as _mgr
    try:
        return _mgr.compile_run(db, run_id)
    except _mgr.PendingChildren as e:
        raise HTTPException(
            409, f"still running: {', '.join(e.pending)}") from e
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


# ==========================================================================
# small surfaces ported from the original (hide, prefs, feedback, similar,
# recommendations, compare, control)
# ==========================================================================
class HiddenIn(BaseModel):
    hidden: bool = True


@app.patch("/api/investigations/{inv_id}/hidden")
def set_hidden(inv_id: int, body: HiddenIn, db: Session = Depends(get_db)):
    inv = _get_inv(db, inv_id)
    inv.hidden = bool(body.hidden)
    db.commit()
    return {"ok": True, "id": inv_id, "hidden": bool(inv.hidden)}


@app.get("/api/investigations/{inv_id}/prefs")
def get_prefs(inv_id: int, db: Session = Depends(get_db)):
    inv = _get_inv(db, inv_id)
    return {"investigation_id": inv_id,
            "auto_save_explanations": bool(inv.auto_save_explanations),
            "sources": inv.sources,
            "preferred_domains": inv.preferred_domains}


class PrefsIn(BaseModel):
    auto_save_explanations: bool | None = None
    sources: str | None = None
    preferred_domains: str | None = None


@app.put("/api/investigations/{inv_id}/prefs")
def put_prefs(inv_id: int, body: PrefsIn, db: Session = Depends(get_db)):
    inv = _get_inv(db, inv_id)
    data = body.model_dump(exclude_none=True)
    if "preferred_domains" in data:
        data["preferred_domains"] = ",".join(
            D.normalize_domains(data["preferred_domains"]))
    for k, v in data.items():
        setattr(inv, k, v)
    db.commit()
    return get_prefs(inv_id, db)


class FeedbackIn(BaseModel):
    value: str  # up|down
    note: str = ""


@app.post("/api/explanations/{exp_id}/feedback")
def explanation_feedback(exp_id: int, body: FeedbackIn,
                         db: Session = Depends(get_db)):
    e = db.query(Explanation).filter(Explanation.id == exp_id).first()
    if e is None:
        raise HTTPException(404, "no such explanation")
    if body.value not in ("up", "down"):
        raise HTTPException(400, "value must be up or down")
    try:
        items = json.loads(e.feedback_json or "[]")
    except Exception:
        items = []
    items.append({"value": body.value, "note": body.note[:500],
                  "at": datetime.now(UTC).isoformat()})
    e.feedback_json = json.dumps(items[-50:])
    db.commit()
    with L.scope(f"exp-{e.id}",
                 L.Mandate(objective="human explanation feedback"),
                 label=f"feedback on explanation {e.id}"):
        L.record_human_action("explanation.feedback",
                              {"explanation_id": e.id, "value": body.value,
                               "note": body.note[:200]}, actor="operator")
    ups = sum(1 for i in items if i.get("value") == "up")
    return {"ok": True, "up": ups, "down": len(items) - ups, "total": len(items)}


@app.post("/api/explanations/{exp_id}/save_to_graph")
def explanation_save_to_graph(exp_id: int, db: Session = Depends(get_db)):
    """Persist an answer as a projection artifact (idempotent-ish: skips when
    the same question was already saved)."""
    e = db.query(Explanation).filter(Explanation.id == exp_id).first()
    if e is None:
        raise HTTPException(404, "no such explanation")
    existing = (db.query(Artifact)
                .filter(Artifact.investigation_id == e.investigation_id,
                        Artifact.artifact_type == "projection",
                        Artifact.title == f"Q: {e.question[:200]}").first())
    if existing:
        return {"ok": True, "artifact_id": existing.id, "deduped": True}
    from .explainer import _save_to_graph
    aid = _save_to_graph(db, e.investigation_id, e)
    return {"ok": True, "artifact_id": aid, "deduped": False}


@app.post("/api/explanations/{exp_id}/investigate_gaps")
def explanation_investigate_gaps(exp_id: int, db: Session = Depends(get_db)):
    """Launch a targeted run per uncovered domain in the answer's roadmap."""
    e = db.query(Explanation).filter(Explanation.id == exp_id).first()
    if e is None:
        raise HTTPException(404, "no such explanation")
    try:
        domains = json.loads(e.domains_json or "[]")
    except Exception:
        domains = []
    missing = [d for d in D.DOMAINS if d not in (domains or [])][:4]
    jobs = []
    for did in missing:
        goal = f"{D.DOMAINS[did]['label']}: evidence for '{e.question[:80]}'"
        jobs.append(jobqueue.enqueue(
            "collect", {"investigation_id": e.investigation_id,
                        "max_items": 8, "max_rounds": 1,
                        "trigger": "gap", "goal": goal},
            investigation_id=e.investigation_id))
    return {"ok": True, "queued": len(jobs), "domains": missing, "jobs": jobs}


@app.get("/api/artifacts/{artifact_id}/similar")
def similar_artifacts(artifact_id: int, limit: int = 8,
                      db: Session = Depends(get_db)):
    """Same domain + shared entities/tags, scored deterministically."""
    a = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    if a is None:
        raise HTTPException(404, "no such artifact")
    from .agent import _shared_entities
    peers = (db.query(Artifact)
             .filter(Artifact.investigation_id == a.investigation_id,
                     Artifact.id != a.id).all())
    a_tags = {t.strip().lower() for t in (a.tags or "").split(",") if t.strip()}
    scored = []
    for p in peers:
        s = 0.0
        if p.domain and p.domain == a.domain:
            s += 3.0
        shared = _shared_entities(a.entities_json or [], p.entities_json or [])
        s += 2.0 * len(shared)
        p_tags = {t.strip().lower() for t in (p.tags or "").split(",") if t.strip()}
        s += 1.0 * len(a_tags & p_tags)
        if s > 0:
            scored.append((s, p, shared))
    scored.sort(key=lambda t: -t[0])
    return {"artifact_id": artifact_id,
            "similar": [{"id": p.id, "title": p.title, "domain": p.domain,
                         "score": round(s, 2), "shared_entities": shared}
                        for s, p, shared in scored[:limit]]}


@app.get("/api/investigations/{inv_id}/recommendations")
def recommendations(inv_id: int, db: Session = Depends(get_db)):
    """Next queries worth asking: dead shapes pruned, gaps first."""
    _get_inv(db, inv_id)
    shapes = yld.investigation_yield(db, inv_id)
    from . import research_gaps as _gaps
    pic = _gaps.compute_gaps(inv_id)
    recs = []
    for g in (pic.get("gaps") or [])[:4]:
        recs.append({"kind": "gap", "domain": g["domain"], "label": g["label"],
                     "query": _gaps.gap_query(
                         {"domain": g["domain"], "dimension": g["label"]}),
                     "why": g["why"][:200]})
    for t in (pic.get("thin") or [])[:3]:
        recs.append({"kind": "thin", "domain": t["domain"], "label": t["label"],
                     "query": f"{t['label']} evidence transmission",
                     "why": f"Only {t['artifacts']} artifact(s)."})
    return {"recommendations": recs,
            "useless_shapes": shapes.get("useless", [])[:5],
            "best_shape": shapes.get("best")}


@app.get("/api/investigations/{inv_id}/compare")
def compare_runs(inv_id: int, run_a: int, run_b: int,
                 db: Session = Depends(get_db)):
    """Artifact counts per run side by side. Runs, not content: content drift
    is the drift endpoint's job."""
    _get_inv(db, inv_id)
    out = {}
    for rid in (run_a, run_b):
        r = db.query(AgentRun).filter(
            AgentRun.id == rid,
            AgentRun.investigation_id == inv_id).first()
        if r is None:
            raise HTTPException(404, f"no such run {rid} in this investigation")
        arts = db.query(Artifact).filter(
            Artifact.investigation_id == inv_id, Artifact.run_id == rid).all()
        by_domain: dict[str, int] = {}
        for a in arts:
            if a.domain:
                by_domain[a.domain] = by_domain.get(a.domain, 0) + 1
        out[str(rid)] = {"status": r.status,
                         "stats": json.loads(r.stats) if r.stats else None,
                         "artifacts": len(arts), "by_domain": by_domain}
    return {"investigation_id": inv_id, "run_a": run_a, "run_b": run_b,
            **out}


@app.get("/api/investigations/{inv_id}/control")
def get_control(inv_id: int, db: Session = Depends(get_db)):
    """The mandate the next run will operate under: stages, actors, domain
    scope. Read-only preview — the run enforces it, this endpoint explains it."""
    from .agent import AGENT_STAGES, ALLOWED_ACTORS
    inv = _get_inv(db, inv_id)
    return {"investigation_id": inv_id,
            "planned_intents": AGENT_STAGES,
            "allowed_actors": ALLOWED_ACTORS,
            "allowed_domains": inv.domain_list or None,
            "budgets": {"schedule_max_items": inv.schedule_max_items,
                        "schedule_max_rounds": inv.schedule_max_rounds}}


# ==========================================================================
# policy-framework matrix (standards-coverage equivalent)
# ==========================================================================
@app.get("/api/frameworks")
def frameworks_catalog():
    from . import frameworks as _fw
    return {"frameworks": [
        {"id": fid, **{k: v for k, v in fw.items() if k != "domains"},
         "domains": fw["domains"],
         "domain_labels": [D.domain_label(d) for d in fw["domains"]]}
        for fid, fw in _fw.FRAMEWORKS.items()]}


@app.get("/api/frameworks/matrix")
def frameworks_matrix(investigation_id: int | None = None,
                      db: Session = Depends(get_db)):
    """Every regime scored 0..100 per governed domain from stored evidence.

    Pass an investigation id for one map's matrix, or nothing for the whole
    estate. A regime whose domains have no evidence scores zero, visibly —
    never a dash that reads as not-applicable.
    """
    from . import frameworks as _fw
    if investigation_id is not None:
        _get_inv(db, investigation_id)
    return _fw.build_matrix(db, investigation_id)


# ==========================================================================
# A2A agent cards (debugging + agent-to-agent calls)
# ==========================================================================
class AgentInvokeIn(BaseModel):
    to: str
    intent: str
    payload: dict | None = None
    task_id: str | None = None


@app.get("/.well-known/econ-agents")
def well_known_agents():
    from . import agents as _agents
    return {"protocol": _agents.PROTOCOL, "agents": _agents.get_agent_cards()}


@app.get("/api/agents/cards")
def agent_cards():
    from . import agents as _agents
    return {"protocol": _agents.PROTOCOL, "agents": _agents.get_agent_cards()}


@app.post("/api/agents/invoke")
def agent_invoke(body: AgentInvokeIn, db: Session = Depends(get_db)):
    """Invoke one protocol hop directly. Every intent is deterministic over
    stored rows — nothing here calls a model or the network."""
    from . import agents as _agents
    env = _agents.new_envelope("api-caller", body.to, body.intent,
                               body.payload or {}, task_id=body.task_id,
                               note="direct invoke")
    try:
        return _agents.dispatch(env, db)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ==========================================================================
# PDF export (reportlab, no system dependencies)
# ==========================================================================
def _pdf_response(filename: str, pdf: bytes) -> Response:
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})


@app.get("/api/investigations/{inv_id}/dossier/pdf")
def dossier_pdf(inv_id: int, db: Session = Depends(get_db)):
    _get_inv(db, inv_id)
    from . import dossier_pdf as _pdf
    try:
        filename, pdf = _pdf.build_investigation_pdf(db, inv_id)
    except ValueError as e:
        # Missing row -> 404. A render failure is a 500 and must not wear
        # a 404's clothes: "not found" sends the operator looking for a
        # row that exists.
        raise HTTPException(404 if str(e).startswith("no such") else 500,
                            str(e)) from e
    return _pdf_response(filename, pdf)


@app.get("/api/assessments/{assessment_id}/pdf")
def assessment_pdf(assessment_id: int, db: Session = Depends(get_db)):
    from . import dossier_pdf as _pdf
    try:
        filename, pdf = _pdf.build_assessment_pdf(db, assessment_id)
    except ValueError as e:
        raise HTTPException(404 if str(e).startswith("no such") else 500,
                            str(e)) from e
    return _pdf_response(filename, pdf)


@app.get("/api/leadership/board.pdf")
def leadership_board_pdf(investigation_id: int | None = None,
                         db: Session = Depends(get_db)):
    from . import dossier_pdf as _pdf
    filename, pdf = _pdf.build_board_pdf(db, investigation_id)
    return _pdf_response(filename, pdf)


# ==========================================================================
# Ask: estate-wide questions over collected evidence + the live web
# ==========================================================================
class AskIn(BaseModel):
    question: str = ""
    scope: str = "all"  # "all" or "inv:<id>"
    mode: str = "explain"
    depth: str = "balanced"
    audience: str = "intermediate"
    parent_id: int | None = None
    include_web: bool = True
    explanation_id: int | None = None  # resume: answer into this row


@app.post("/api/ask")
def ask_question(body: AskIn, db: Session = Depends(get_db)):
    """Ask anything of everything collected, plus the live web.

    Scope ``all`` searches every investigation's artifacts; ``inv:<id>``
    scopes to one. Pass ``explanation_id`` to answer into an existing row
    (e.g. an imported question) instead of starting a new thread. Long work
    goes through the queue like everything else.
    """
    if body.explanation_id is not None:
        row = db.query(Explanation).filter(
            Explanation.id == body.explanation_id).first()
        if row is None:
            raise HTTPException(404, "no such explanation to resume")
        question = (body.question or "").strip() or row.question
    else:
        if not (body.question or "").strip():
            raise HTTPException(400, "question is required")
        question = body.question
    if body.mode not in expl.MODES:
        raise HTTPException(400, f"mode must be one of {sorted(expl.MODES)}")
    if body.scope != "all":
        if not body.scope.startswith("inv:"):
            raise HTTPException(400, "scope must be 'all' or 'inv:<id>'")
        _get_inv(db, int(body.scope[4:]) if body.scope[4:].isdigit() else -1)
    if body.parent_id is not None and db.query(Explanation).filter(
            Explanation.id == body.parent_id).first() is None:
        raise HTTPException(404, "no such parent explanation")
    job = jobqueue.enqueue("ask", {
        "question": question, "scope": body.scope, "mode": body.mode,
        "depth": body.depth, "audience": body.audience,
        "parent_id": body.parent_id, "include_web": body.include_web,
        "resume_id": body.explanation_id})
    return {**job, "queued": True}


@app.get("/api/ask/threads")
def ask_threads(investigation_id: int | None = None, limit: int = 30,
                group: str | None = None, db: Session = Depends(get_db)):
    """Thread roots, newest first. Roots live in the Ask archive until one is
    promoted — pass an investigation id to see only its threads, or a group
    to filter by the content-derived grouping."""
    from . import ask as _ask
    return _ask.list_threads(db, investigation_id, limit=limit, group=group)


@app.post("/api/ask/import")
async def ask_import(request: Request, db: Session = Depends(get_db)):
    """Import a Google AI Mode / Takeout history export as tagged threads.

    Accepts Takeout Search JSON (title/titleUrl), query objects, Q&A
    conversation pairs, or plain one-query-per-line text — anything else in
    the file is counted in ``skipped``, never fatal. Every stored question is
    tagged and grouped from its own content (domains, named actors, drivers),
    deduplicated against the archive, and left unanswered until asked:
    imported answers are stored verbatim and never auto-saved to the graph.
    """
    from . import ask as _ask
    try:
        form = await request.form()
    except Exception as e:
        raise HTTPException(400, "multipart file upload is required") from e
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(400, "multipart field 'file' is required")
    content = await upload.read()
    if not content or len(content) > 10_000_000:
        raise HTTPException(400, "empty file or over the 10MB cap")
    parsed = _ask.parse_history_file(content, getattr(upload, "filename", ""))
    if not parsed["items"] and parsed["skipped"]:
        return {"created": 0, "skipped": parsed["skipped"],
                "reason": parsed.get("reason", "nothing recognisable")}
    out = _ask.import_history(db, parsed["items"])
    out["skipped"] += parsed["skipped"]
    with L.scope(f"import-{out['archive_id']}",
                 L.Mandate(objective="human history import"),
                 label="history import"):
        L.record_human_action("history.import", out, actor="operator")
    return out


@app.get("/api/explanations/{exp_id}/tree")
def explanation_tree(exp_id: int, db: Session = Depends(get_db)):
    """One thread as a nested tree, so branches render as branches."""
    from . import ask as _ask
    # Climb to the root: a tree is addressed by any member.
    e = db.query(Explanation).filter(Explanation.id == exp_id).first()
    if e is None:
        raise HTTPException(404, "no such explanation")
    root_id = exp_id
    seen = {exp_id}
    while e.parent_id is not None and e.parent_id not in seen:
        seen.add(e.parent_id)
        e = db.query(Explanation).filter(Explanation.id == e.parent_id).first()
        if e is None:
            break
        root_id = e.id
    tree = _ask.thread_tree(db, root_id)
    if tree is None:
        raise HTTPException(404, "no such explanation")
    return {"thread_id": root_id, "tree": tree}


@app.get("/api/explanations/{exp_id}/graph")
def explanation_graph(exp_id: int, db: Session = Depends(get_db)):
    """One thread as a knowledge graph next to the thread view: messages,
    cited artifacts, web sources, and the evidence edges between them."""
    from . import ask as _ask
    g = _ask.thread_graph(db, exp_id)
    if g is None:
        raise HTTPException(404, "no such explanation")
    return g


class PromoteIn(BaseModel):
    explanation_id: int
    collect: bool = True


@app.post("/api/ask/promote")
def ask_promote(body: PromoteIn, db: Session = Depends(get_db)):
    """Promote a thread answer into a real investigation: the question becomes
    the brief, the answer lands as its first artifact, collection starts."""
    from . import ask as _ask
    try:
        return _ask.promote(db, body.explanation_id, collect=body.collect)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


# ==========================================================================
# Elastic search: one ranked index over artifacts, answers, assessments
# ==========================================================================
@app.get("/api/search/all")
def search_all(q: str, doc_type: str | None = None,
               investigation_id: int | None = None, limit: int = 20,
               db: Session = Depends(get_db)):
    """Full-text search across everything: evidence, answers, assessments,
    briefs. Hits carry <mark> snippets showing why they matched."""
    from . import search_index as _si
    if not (q or "").strip():
        raise HTTPException(400, "q is required")
    out = _si.search(db, q, doc_type=doc_type,
                     investigation_id=investigation_id, limit=limit)
    if not out.get("ok"):
        raise HTTPException(400, out.get("reason", "search failed"))
    return out


@app.post("/api/search/reindex")
def search_reindex(db: Session = Depends(get_db)):
    """Rebuild the full-text index from stored rows. The catch-up path: after
    this, the index *is* the database by construction."""
    from . import search_index as _si
    return _si.reindex_all(db)


@app.get("/api/search/index-state")
def search_index_state(db: Session = Depends(get_db)):
    from . import search_index as _si
    return {"indexed": _si.indexed_counts(db),
            "fts": _si.ensure_table(db)}
