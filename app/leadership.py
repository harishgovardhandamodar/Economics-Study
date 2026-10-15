"""Leadership board: the portfolio view across investigations.

Answers "what is our cross-domain exposure, where are the blocking gaps, what
is waiting on us, and is the evaluation system itself healthy" from the same
rows the runs wrote. Everything here is an aggregation — no new judgments, no
LLM, no network. A board that recomputed judgments would be a different
review; this one reads stored assessments and artifacts.

Mirrors the original leadership.py shape (risk_position, decision_queue,
assurance_health → coverage_health, exposure, system_integrity, alerts) but
scores economics: coverage, bridged priors, confidence, exposure.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _load(raw: Any, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        import json as _j
        return _j.loads(raw)
    except Exception:
        return default


def _latest_assessments(db) -> list[dict[str, Any]]:
    from .models import EconomicAssessment
    rows = (db.query(EconomicAssessment)
            .order_by(EconomicAssessment.id.desc()).all())
    seen: dict[int, Any] = {}
    for r in rows:
        if r.investigation_id not in seen:
            seen[r.investigation_id] = r
    return [_assessment_row(r) for r in seen.values()]


def _assessment_row(r) -> dict[str, Any]:
    sc = _load(r.scoring_json, {}) or {}
    return {
        "id": r.id, "investigation_id": r.investigation_id,
        "driver": r.driver, "shock": r.shock,
        "overall_pct": r.overall_pct, "coverage_pct": r.coverage_pct,
        "confidence_pct": r.confidence_pct, "exposure_pct": r.exposure_pct,
        "decision": r.decision, "decided_by": r.decided_by,
        "decided_at": r.decided_at,
        "decision_expires_at": r.decision_expires_at,
        "created_at": r.created_at,
        "scoring": sc,
    }


def exposure_position(db, investigation_id: int | None = None) -> dict[str, Any]:
    """Per-driver exposure from latest assessments. Exposure here is
    magnitude-weighted directional mass, not a risk rating."""
    rows = _latest_assessments(db)
    if investigation_id:
        rows = [r for r in rows if r["investigation_id"] == investigation_id]
    by_driver: dict[str, list[float]] = {}
    for r in rows:
        by_driver.setdefault(r["driver"] or "unassigned", []).append(
            r["exposure_pct"] or 0)
    return {
        "n_assessed": len(rows),
        "mean_exposure": round(
            sum(r["exposure_pct"] or 0 for r in rows) / max(1, len(rows)), 1),
        "mean_overall": round(
            sum(r["overall_pct"] or 0 for r in rows) / max(1, len(rows)), 1),
        "by_driver": {k: {"n": len(v), "mean": round(sum(v) / max(1, len(v)), 1),
                          "max": round(max(v) if v else 0, 1)}
                      for k, v in by_driver.items()},
        "rows": rows,
    }


def decision_queue(db, investigation_id: int | None = None) -> dict[str, Any]:
    """Assessments waiting on a human: no decision, or an expiring exception."""
    rows = _latest_assessments(db)
    if investigation_id:
        rows = [r for r in rows if r["investigation_id"] == investigation_id]
    now = datetime.now(UTC)
    queue = []
    for r in rows:
        if not r["decision"]:
            queue.append({**r, "waiting_reason": "no decision recorded"})
        elif r["decision"] == "exception" and r["decision_expires_at"]:
            try:
                exp = r["decision_expires_at"]
                exp = exp.replace(tzinfo=UTC) if exp.tzinfo is None else exp
                days = (exp - now).total_seconds() / 86400
                if days < 30:
                    queue.append({**r, "waiting_reason": (
                        f"exception expires in {days:.0f}d")})
            except Exception:
                pass
    # Highest exposure first: that is what a decision unblocks.
    queue.sort(key=lambda r: -(r["exposure_pct"] or 0))
    return {"n_waiting": len(queue), "n_assessed": len(rows), "queue": queue}


def coverage_health(db, investigation_id: int | None = None) -> dict[str, Any]:
    """Verified vs declared, time-to-close analogue: covered vs bridged."""
    from .models import Investigation
    q = db.query(Investigation)
    if investigation_id:
        q = q.filter(Investigation.id == investigation_id)
    invs = q.all()
    total_gaps = 0
    total_thin = 0
    for inv in invs:
        from . import research_gaps as gaps_mod
        try:
            pic = gaps_mod.compute_gaps(inv.id)
            total_gaps += len(pic.get("gaps", []))
            total_thin += len(pic.get("thin", []))
        except Exception:
            pass
    rows = _latest_assessments(db)
    if investigation_id:
        rows = [r for r in rows if r["investigation_id"] == investigation_id]
    mean_cov = (sum(r["coverage_pct"] or 0 for r in rows)
                  / max(1, len(rows))) if rows else 0
    return {
        "investigations": len(invs),
        "assessed": len(rows),
        "unassessed": max(0, len(invs) - len(rows)),
        "open_gaps": total_gaps,
        "thin_domains": total_thin,
        "mean_coverage_pct": round(mean_cov, 1),
    }


def system_integrity(db) -> dict[str, Any]:
    """Is the evaluation system itself healthy: ledger + jobs."""
    from . import ledger as L
    integ = L.monitor_integrity(limit=25)
    from . import jobqueue
    qs = jobqueue.queue_state()["counts"]
    return {
        "ledger": {"checked": integ.get("checked"), "healthy": integ.get("healthy"),
                   "broken": len(integ.get("broken", [])),
                   "audit_drops": integ.get("audit_drops")},
        "jobs": qs,
        "ok": not integ.get("broken") and not integ.get("audit_drops"),
    }


def alerts(db, investigation_id: int | None = None) -> dict[str, Any]:
    """Named findings, recomputed live. Acknowledgement state is not stored
    here (this build has no alert-state table yet) — dismissing an alert can
    never hide a risk that is still true, because there is nothing to dismiss
    against."""
    out: list[dict] = []
    rows = _latest_assessments(db)
    if investigation_id:
        rows = [r for r in rows if r["investigation_id"] == investigation_id]
    for r in rows:
        sc = r.get("scoring", {}) or {}
        prior_cov = sc.get("prior_coverage_pct", 100) or 100
        if prior_cov < 25 and sc.get("prior_channels", 0) > 0:
            out.append({"id": f"priors-{r['id']}", "severity": "warn",
                        "title": f"Assessment #{r['id']}: priors mostly unbridged",
                        "detail": (
                            f"{sc.get('bridged', 0)}/"
                            f"{sc.get('prior_channels', 0)} relevant "
                            "channels have evidence. The map asserts less "
                            "than it assumes."),
                        "investigation_id": r["investigation_id"]})
        if (r["confidence_pct"] or 0) < 20 and (sc.get("n_artifacts", 0) or 0) > 0:
            out.append({"id": f"conf-{r['id']}", "severity": "warn",
                        "title": f"Assessment #{r['id']}: low corroboration",
                        "detail": ("Evidence exists but distinct-source "
                                   "corroboration is thin."),
                        "investigation_id": r["investigation_id"]})
        if r["decision"] == "exception" and r["decision_expires_at"]:
            out.append({"id": f"exc-{r['id']}", "severity": "info",
                        "title": f"Assessment #{r['id']}: time-bounded exception",
                        "detail": f"Expires {r['decision_expires_at']}.",
                        "investigation_id": r["investigation_id"]})
    # Unassessed investigations with real collections behind them.
    health = coverage_health(db, investigation_id)
    if health["unassessed"] > 0:
        out.append({"id": "unassessed", "severity": "info",
                    "title": (f"{health['unassessed']} investigation(s) "
                              "with no assessment"),
                    "detail": ("Collected but never scored — the map exists, "
                               "the read does not."),
                    "investigation_id": investigation_id})
    out.sort(key=lambda a: {"block": 0, "warn": 1, "info": 2}.get(a["severity"], 3))
    return {"alerts": out, "count": len(out)}


def knowledge_graph(db, investigation_id: int | None = None) -> dict[str, Any]:
    """Portfolio-wide domain presence: which domains the whole estate covers."""
    from . import domains as D
    from .models import Artifact, Investigation
    q = db.query(Artifact)
    if investigation_id:
        q = q.filter(Artifact.investigation_id == investigation_id)
    counts: dict[str, int] = {}
    for a in q.filter(Artifact.review != "rejected").all():
        if a.domain:
            counts[a.domain] = counts.get(a.domain, 0) + 1
    n_invs = db.query(Investigation).count()
    return {
        "investigations": n_invs,
        "by_domain": counts,
        "domains_covered": len(counts),
        "domains_total": len(D.DOMAINS),
        "missing": [d for d in D.DOMAINS if d not in counts],
    }


# Cap for the estate-wide graph. A portfolio graph is large on purpose, but an
# uncapped vis-network with tens of thousands of nodes is not a view, it is a
# hang. The cap is reported, never silent.
GLOBAL_GRAPH_LIMIT = 1500


def global_graph(db, investigation_ids: list[int] | None = None) -> dict[str, Any]:
    """Every collected artifact and every relationship, across sessions.

    Node ids are namespaced (``a:`` artifacts, ``inv:`` investigations) so a
    portfolio-wide graph cannot collide with itself. Investigation hubs show
    "what collected what" at a glance instead of leaving it implied by a
    foreign key. Artifact nodes carry domain colors from the same map the
    per-investigation graph uses.
    """
    from datetime import UTC, datetime

    from . import domains as D
    from .models import Artifact, Investigation, Relationship
    aq = db.query(Artifact).order_by(Artifact.id)
    rq = db.query(Relationship)
    if investigation_ids:
        aq = aq.filter(Artifact.investigation_id.in_(investigation_ids))
        rq = rq.filter(Relationship.investigation_id.in_(investigation_ids))
    total = aq.count()
    arts = aq.limit(GLOBAL_GRAPH_LIMIT + 1).all()
    truncated = len(arts) > GLOBAL_GRAPH_LIMIT
    arts = arts[:GLOBAL_GRAPH_LIMIT]
    kept = {a.id for a in arts}
    rels = [r for r in rq.all()
            if r.source_id in kept and r.target_id in kept]
    inv_ids = sorted({a.investigation_id for a in arts})
    titles = {i.id: (i.title or "Untitled") for i in
              db.query(Investigation).filter(Investigation.id.in_(inv_ids)).all()} \
        if inv_ids else {}
    nodes = [{
        "id": f"a:{a.id}", "label": (a.title or "")[:60], "title": a.title or "",
        "group": a.artifact_type or "news",
        "domain": a.domain,
        "color": {"background": D.domain_color(a.domain),
                  "border": D.domain_color(a.domain)},
        "value": max(1, int((a.relevance or 0) * 10) + 1),
        "review": a.review or "pending", "drift": bool(a.drift),
        "direction": a.direction, "magnitude": a.magnitude,
        "investigation_id": a.investigation_id,
        "investigation": titles.get(a.investigation_id, "Unknown"),
    } for a in arts]
    nodes += [{
        "id": f"inv:{i}", "label": (titles.get(i, "") or "")[:40],
        "title": f"Investigation {i}: {titles.get(i, '')}",
        "group": "investigation",
        "color": {"background": "#6e7681", "border": "#ffffff"},
        "value": 3, "review": "accepted", "drift": False,
        "investigation_id": i, "investigation": titles.get(i, ""),
    } for i in inv_ids]
    edges = [{
        "id": f"e:{r.id}", "from": f"a:{r.source_id}", "to": f"a:{r.target_id}",
        "label": r.relationship_type or "",
        "title": (r.description or "")[:300],
        "color": {"color": (D.RELATIONSHIP_TYPES.get(
            r.relationship_type or "", {}) or {}).get("color", "#8b949e")},
    } for r in rels]
    edges += [{
        "id": f"ei:{a.id}", "from": f"a:{a.id}",
        "to": f"inv:{a.investigation_id}",
        "label": "collected_in", "title": "collected in this investigation",
        "color": {"color": "#6e7681"}, "dashes": True,
    } for a in arts]
    return {
        "nodes": nodes, "edges": edges,
        "generated": datetime.now(UTC).isoformat(),
        "truncated": truncated, "limit": GLOBAL_GRAPH_LIMIT,
        "counts": {"artifacts": total, "artifacts_shown": len(arts),
                   "investigations": len(inv_ids),
                   "relationships": len(rels)},
    }


def global_clusters(db, investigation_ids: list[int] | None = None,
                    mode: str = "category") -> dict[str, Any]:
    """Cluster the estate-wide graph two ways.

    ``category`` groups artifacts by domain — which the taxonomy already
    knows, so it is cheap and exact. ``similarity`` finds cross-session
    bridges: pairs of artifacts in *different* investigations sharing a
    distinctive entity, which is how one session's evidence turns out to bear
    on another's question.
    """
    from .models import Artifact
    q = db.query(Artifact).filter(Artifact.review != "rejected")
    if investigation_ids:
        q = q.filter(Artifact.investigation_id.in_(investigation_ids))
    arts = q.limit(GLOBAL_GRAPH_LIMIT + 1).all()
    truncated = len(arts) > GLOBAL_GRAPH_LIMIT
    arts = arts[:GLOBAL_GRAPH_LIMIT]

    by_domain: dict[str, list[int]] = {}
    for a in arts:
        if a.domain:
            by_domain.setdefault(a.domain, []).append(a.id)
    out: dict[str, Any] = {
        "mode": mode, "truncated": truncated,
        "n_artifacts": len(arts),
        "by_domain": {d: {"artifacts": len(ids), "sample": ids[:12]}
                      for d, ids in sorted(by_domain.items(),
                                           key=lambda kv: -len(kv[1]))},
    }
    if mode == "similarity":
        from .agent import _shared_entities
        bridges = []
        for i, a in enumerate(arts):
            for b in arts[i + 1:]:
                if a.investigation_id == b.investigation_id:
                    continue
                shared = _shared_entities(a.entities_json or [],
                                          b.entities_json or [])
                if shared:
                    bridges.append({
                        "a_id": a.id, "a_title": (a.title or "")[:80],
                        "a_investigation": a.investigation_id,
                        "b_id": b.id, "b_title": (b.title or "")[:80],
                        "b_investigation": b.investigation_id,
                        "shared": shared[:4],
                    })
                    if len(bridges) >= 200:
                        break
            if len(bridges) >= 200:
                break
        bridges.sort(key=lambda br: (-len(br["shared"]), br["a_id"]))
        out["bridges"] = bridges
        out["n_bridges"] = len(bridges)
        linked = {b["a_investigation"] for b in bridges} | \
                 {b["b_investigation"] for b in bridges}
        out["sessions_linked"] = len(linked)
    return out


def board(db, investigation_id: int | None = None) -> dict[str, Any]:
    """One payload for the leadership view."""
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {"investigation_id": investigation_id},
        "exposure": exposure_position(db, investigation_id),
        "decisions": decision_queue(db, investigation_id),
        "health": coverage_health(db, investigation_id),
        "integrity": system_integrity(db),
        "alerts": alerts(db, investigation_id),
        "graph": knowledge_graph(db, investigation_id),
    }


def export_markdown(db, investigation_id: int | None = None) -> str:
    b = board(db, investigation_id)
    lines = ["# Economics leadership board", "",
             f"_Generated {b['generated_at']}_", "",
             "## Exposure", "",
             f"- Assessed: {b['exposure']['n_assessed']} · "
             f"mean exposure {b['exposure']['mean_exposure']}% · "
             f"mean overall {b['exposure']['mean_overall']}%"]
    for drv, stats in b["exposure"]["by_driver"].items():
        lines.append(f"- {drv}: {stats['n']} assessment(s), "
                       f"mean {stats['mean']}%, max {stats['max']}%")
    lines += ["", "## Decisions waiting", "",
              f"{b['decisions']['n_waiting']} of "
              f"{b['decisions']['n_assessed']} assessed await a decision."]
    for q in b["decisions"]["queue"][:10]:
        lines.append(f"- Assessment #{q['id']} (inv {q['investigation_id']}, "
                     f"exposure {q['exposure_pct']}%): {q['waiting_reason']}")
    lines += ["", "## Coverage health", "",
              f"- {b['health']['assessed']}/"
              f"{b['health']['investigations']} investigations assessed · "
              f"mean coverage {b['health']['mean_coverage_pct']}% · "
              f"{b['health']['open_gaps']} open gaps · "
              f"{b['health']['thin_domains']} thin"]
    lines += ["", "## Integrity", "",
              f"- Ledger {b['integrity']['ledger']['healthy']}/"
              f"{b['integrity']['ledger']['checked']} healthy · "
              f"{b['integrity']['ledger']['audit_drops']} drops · "
              f"jobs {b['integrity']['jobs']}"]
    lines += ["", "## Alerts", ""]
    if b["alerts"]["alerts"]:
        for a in b["alerts"]["alerts"]:
            lines.append(f"- [{a['severity']}] {a['title']} — {a['detail']}")
    else:
        lines.append("- None.")
    lines += ["", "## Portfolio graph", "",
              f"- {b['graph']['domains_covered']}/"
              f"{b['graph']['domains_total']} domains covered estate-wide"]
    if b["graph"]["missing"]:
        lines.append(f"- Missing everywhere: {', '.join(b['graph']['missing'])}")
    return "\n".join(lines)
