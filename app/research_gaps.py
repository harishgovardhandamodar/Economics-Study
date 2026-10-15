"""Research gaps: the dimensions of a brief with nothing at all behind them.

The distinction from "thinly covered" is the whole point. A gap is where the
graph is *empty*, so a targeted run at it is the cheapest possible way to move
the map, and a gap in a hub domain is more valuable than three artifacts in a
leaf one.

Coverage is computed from what is actually stored, never from what the brief
asked for — a brief can name eight domains and the evidence can be in two, and
the gap list has to say so.
"""
from . import domains as D
from .database import SessionLocal
from .models import Artifact, Investigation, Relationship, ResearchGap

# A domain with fewer than this many accepted artifacts is thin rather than
# empty: worth a top-up run, but it is not a gap and calling it one would put
# it in the same queue as a domain the investigation has never touched.
THIN_THRESHOLD = 3


def compute_gaps(investigation_id: int) -> dict:
    """The current gap picture, recomputed from stored rows."""
    db = SessionLocal()
    try:
        inv = (db.query(Investigation)
               .filter(Investigation.id == investigation_id).first())
        if not inv:
            return {"ok": False, "reason": "no such investigation"}

        rows = (db.query(Artifact)
                .filter(Artifact.investigation_id == investigation_id,
                        Artifact.review != "rejected").all())
        counts: dict[str, int] = {}
        directional: dict[str, int] = {}
        for a in rows:
            if a.domain:
                counts[a.domain] = counts.get(a.domain, 0) + 1
                if a.direction and a.direction != "flat":
                    directional[a.domain] = directional.get(a.domain, 0) + 1

        gaps, thin = [], []
        for d in D.domain_catalog():
            did = d["id"]
            n = counts.get(did, 0)
            if n == 0:
                gaps.append({
                    "domain": did, "label": d["label"], "short": d["short"],
                    "color": d["color"], "hub": d["hub"],
                    "why": _why_empty(d, counts),
                    # Hub domains first: a hole in monetary policy costs more
                    # than a hole in MEV, because everything routes through it.
                    "priority": _priority(d),
                })
            elif n < THIN_THRESHOLD:
                thin.append({"domain": did, "label": d["label"], "artifacts": n})

        gaps.sort(key=lambda g: -g["priority"])

        # Channels that cross between two covered domains but have no edge
        # actually drawn: the map is missing the *mechanism*, not just the
        # documents. Computed after the counts so the covered set is known.
        unbridged = _unbridged_for(investigation_id, set(counts))

        return {
            "ok": True,
            "investigation_id": investigation_id,
            "title": inv.title,
            "gaps": gaps,
            "thin": thin,
            "unbridged_channels": unbridged,
            "coverage": {d["id"]: counts.get(d["id"], 0)
                         for d in D.domain_catalog()},
            "total_artifacts": len(rows),
            "domains_covered": sum(1 for v in counts.values() if v),
            "domains_total": len(D.DOMAINS),
            "directional_count": sum(directional.values()),
        }
    finally:
        db.close()


def _why_empty(d: dict, counts: dict) -> str:
    covered = [k for k, v in counts.items() if v]
    if not covered:
        return ("The investigation has no domain-classified evidence at all, "
                "so this is empty rather than merely unreached.")
    inbound = D.channels_into(d["id"])
    if inbound:
        srcs = ", ".join(sorted({D.domain_label(c["src"]) for c in inbound[:3]}))
        return (f"Nothing classified here, yet {srcs} are covered — the "
                f"connecting evidence is what is missing.")
    return f"Nothing classified here. {d['blurb']}"


def _priority(d: dict) -> float:
    """How much a gap in this domain hurts.

    Hub status dominates, then how many channels touch the domain: a domain
    nothing connects to cannot be reached by a fix, while a hub that is empty
    breaks every chain that would have passed through it.
    """
    inbound = len(D.channels_into(d["id"]))
    outbound = len(D.channels_from(d["id"]))
    return round((3.0 if d["hub"] else 0.0)
                 + 0.35 * min(inbound, 8) + 0.25 * min(outbound, 8), 2)


def _bridged_channels(investigation_id: int) -> set:
    """``(src, dst)`` pairs that actually have an edge drawn.

    One explicit join on both endpoint conditions — see the note in
    ``explainer._cross_domain_edges`` for why the chained-alias form is wrong.
    """
    from sqlalchemy import and_
    from sqlalchemy.orm import aliased

    src = aliased(Artifact)
    dst = aliased(Artifact)
    db = SessionLocal()
    try:
        rows = (db.query(src.domain, dst.domain)
                .join(Relationship,
                      and_(Relationship.source_id == src.id,
                           Relationship.target_id == dst.id))
                .filter(Relationship.investigation_id == investigation_id,
                        src.domain.isnot(None), dst.domain.isnot(None),
                        src.domain != dst.domain).all())
        return {(s, t) for s, t in rows}
    except Exception:
        return set()
    finally:
        db.close()


def _unbridged_for(investigation_id: int, covered: set) -> list[dict]:
    bridged = _bridged_channels(investigation_id)
    out = []
    for c in D.CHANNELS:
        if c["src"] not in covered or c["dst"] not in covered:
            continue
        if (c["src"], c["dst"]) in bridged:
            continue
        out.append({**c, "src_label": D.domain_label(c["src"]),
                    "dst_label": D.domain_label(c["dst"])})
    out.sort(key=lambda c: -c["strength"])
    return out


# --------------------------------------------------------------------------
# Gap lifecycle
# --------------------------------------------------------------------------
def persist_gaps(investigation_id: int) -> dict:
    """Write the computed gaps to rows, and close rows that are now covered.

    Additive: a gap that has been investigated stays on the record as
    ``investigating`` rather than vanishing, because "we tried this and it is
    still empty" is a finding worth keeping.
    """
    picture = compute_gaps(investigation_id)
    db = SessionLocal()
    try:
        wanted = {g["domain"]: g for g in picture["gaps"]}
        existing = (db.query(ResearchGap)
                    .filter(ResearchGap.investigation_id == investigation_id)
                    .all())
        by_domain = {g.domain: g for g in existing if g.domain}

        for did, g in wanted.items():
            row = by_domain.get(did)
            if row is None:
                db.add(ResearchGap(investigation_id=investigation_id,
                                   dimension=g["label"], domain=did,
                                   why=g["why"]))
            elif row.status == "open":
                row.why = g["why"]
        for did, row in by_domain.items():
            if did not in wanted and row.status == "open":
                # Something arrived: the gap closed itself.
                row.status = "ruled_out"
                row.evidence = "Artifacts now exist in this domain."
        db.commit()
        return picture
    finally:
        db.close()


def list_gaps(investigation_id: int) -> dict:
    db = SessionLocal()
    try:
        rows = (db.query(ResearchGap)
                .filter(ResearchGap.investigation_id == investigation_id)
                .order_by(ResearchGap.status, ResearchGap.id).all())
        return {"gaps": [
            {"id": r.id, "dimension": r.dimension, "domain": r.domain,
             "label": D.domain_label(r.domain) if r.domain else r.dimension,
             "color": D.domain_color(r.domain), "why": r.why,
             "status": r.status, "evidence": r.evidence,
             # The runnable query, computed here rather than in the client:
             # it is derived from the channel graph, and a client that rebuilt
             # it would be a second implementation of the same rule.
             "query": gap_query({"domain": r.domain, "dimension": r.dimension}),
             "created_at": r.created_at} for r in rows]}
    finally:
        db.close()


def set_status(gap_id: int, status: str) -> dict:
    if status not in ("open", "ruled_out", "investigating"):
        return {"ok": False, "reason": "bad status"}
    db = SessionLocal()
    try:
        row = db.query(ResearchGap).filter(ResearchGap.id == gap_id).first()
        if row is None:
            return {"ok": False, "reason": "no such gap"}
        row.status = status
        db.commit()
        return {"ok": True, "gap_id": gap_id, "status": status}
    finally:
        db.close()


def gap_query(gap: dict) -> str:
    """A runnable query for one gap, built from the taxonomy.

    Built from the channels rather than from the gap's label, because the useful
    question is not "tell me about stablecoins" — it is "what in the evidence
    connects monetary policy to stablecoins", which is a question a search
    engine can actually return documents for.
    """
    did = gap.get("domain")
    if did not in D.DOMAINS:
        return gap.get("dimension", "research")[:120]
    outs = D.channels_from(did)[:2]
    ins = D.channels_into(did)[:2]
    terms = []
    for c in outs[:2] + ins[:2]:
        terms.append(f"{D.domain_label(c['src'])} {c['channel'].split()[0]} "
                     f"{D.domain_label(c['dst'])}")
    d = D.DOMAINS[did]
    return (terms[0] if terms else d["label"]).strip()[:120]
