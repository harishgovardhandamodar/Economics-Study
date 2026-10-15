"""Transmission assessment: the economics equivalent of a security assessment.

An investigation asks "what is true across these domains". An assessment asks
"what follows for this driver and shock": which channels the stored evidence
supports, which it leaves unexamined, what the directional mass says, and what
three futures would each falsify the read.

Scoring is deterministic over stored rows plus the taxonomy. An LLM may add
claims, never substitute them: the seats' claims are the floor, not the
fallback.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import council as council_mod
from . import domains as D


def taxonomy_fingerprint() -> tuple[str, str]:
    """Version + short hash of the channel list, so a later catalog edit is
    visibly a different result wearing the same assessment id."""
    blob = json.dumps(D.CHANNELS, sort_keys=True, separators=(",", ":"))
    fp = hashlib.sha256(blob.encode()).hexdigest()[:12]
    return ("econ-taxonomy-v1", fp)


def _load_rows(db, investigation_id: int) -> list[dict[str, Any]]:
    from .models import Artifact
    rows = (db.query(Artifact)
            .filter(Artifact.investigation_id == investigation_id,
                    Artifact.review != "rejected").all())
    out = []
    for a in rows:
        out.append({
            "id": a.id, "title": a.title, "domain": a.domain,
            "direction": a.direction, "magnitude": a.magnitude,
            "horizon": a.horizon, "relevance": a.relevance,
            "entities": a.entities_json or [], "source": a.source,
        })
    return out


def _load_edges(db, investigation_id: int) -> list[dict[str, Any]]:
    from .models import Relationship
    rows = (db.query(Relationship)
            .filter(Relationship.investigation_id == investigation_id).all())
    return [{"id": r.id, "source_id": r.source_id, "target_id": r.target_id,
             "type": r.relationship_type, "confidence": r.confidence,
             "lag": r.lag, "description": r.description} for r in rows]


def score_transmission(rows: list[dict], edges: list[dict],
                       driver: str | None) -> dict[str, Any]:
    """Deterministic scores from stored evidence + taxonomy prior."""
    by_domain: dict[str, list] = {}
    for r in rows:
        if r.get("domain"):
            by_domain.setdefault(r["domain"], []).append(r)
    domains_covered = len(by_domain)
    coverage_pct = round(100 * domains_covered / max(1, len(D.DOMAINS)), 1)

    # Channel support: transmits_to edges grouped by (src_domain, dst_domain).
    # Needs the artifact domain map, passed via rows.
    id_to_domain = {r["id"]: r.get("domain") for r in rows}
    supported: dict[tuple, list] = {}
    for e in edges:
        if e["type"] != "transmits_to":
            continue
        s, t = id_to_domain.get(e["source_id"]), id_to_domain.get(e["target_id"])
        if s and t and s != t:
            supported.setdefault((s, t), []).append(e)

    # Prior channels relevant to the driver.
    relevant_priors: list[dict] = []
    if driver and driver in D.DRIVERS:
        seeds = D.DRIVERS[driver]["domains"]
        for p in D.walk_transmission(seeds, max_depth=2):
            relevant_priors.append(p)
    else:
        for c in D.CHANNELS:
            relevant_priors.append({"domain": c["dst"], "via": c,
                                    "seed": c["src"], "depth": 1,
                                    "lag": c["lag"],
                                    "path": [c["src"], c["dst"]]})

    prior_pairs = {(p["via"]["src"], p["via"]["dst"]) for p in relevant_priors}
    supported_pairs = set(supported)
    bridged = prior_pairs & supported_pairs
    unbridged = prior_pairs - supported_pairs

    # Directional mass: magnitude-weighted, hub domains weigh double.
    mass = 0.0
    n_dir = 0
    for r in rows:
        if r.get("direction") in ("up", "down") and r.get("magnitude") is not None:
            try:
                m = float(r["magnitude"])
            except (TypeError, ValueError):
                continue
            w = 2.0 if r.get("domain") in D.HUB_DOMAINS else 1.0
            mass += m * w
            n_dir += 1
    exposure_pct = round(min(100.0, mass * 100 / max(1, len(rows))), 1) if rows else 0.0

    # Confidence follows distinct-source corroboration, capped by grounding.
    distinct = len({r.get("source") for r in rows if r.get("source")})
    confidence_pct = round(min(90.0, 18 * distinct), 1) if rows else 0.0
    if n_dir == 0 and rows:
        confidence_pct = round(confidence_pct * 0.4, 1)

    # Overall: coverage 30%, bridged priors 40%, confidence 30%.
    prior_cov = (len(bridged) / max(1, len(prior_pairs)) * 100) if prior_pairs else 0
    overall = round(0.3 * coverage_pct + 0.4 * prior_cov + 0.3 * confidence_pct, 1)

    return {
        "coverage_pct": coverage_pct,
        "domains_covered": domains_covered,
        "domains_total": len(D.DOMAINS),
        "prior_channels": len(prior_pairs),
        "bridged": len(bridged),
        "unbridged": len(unbridged),
        "prior_coverage_pct": round(prior_cov, 1),
        "exposure_pct": exposure_pct,
        "confidence_pct": confidence_pct,
        "overall_pct": overall,
        "directional": n_dir,
        "n_artifacts": len(rows),
        "n_edges": len(edges),
        "n_transmits": sum(1 for e in edges if e["type"] == "transmits_to"),
    }


def build_scenarios(driver: str | None, shock: str | None,
                    scoring: dict, council_out: dict) -> list[dict[str, Any]]:
    """Three futures that would each falsify a different part of the read."""
    base_shock = shock or (D.DRIVERS.get(driver or "", {}).get("label", "the shock")
                           if driver else "the shock")
    unexamined = council_out.get("coverage", {}).get("unexamined", [])[:3]
    return [
        {
            "id": "baseline",
            "label": "Baseline — transmission as mapped",
            "narrative": (f"{base_shock} propagates along the "
                          f"{scoring['bridged']} "
                          f"supported channel(s). Directional mass "
                          f"({scoring['directional']} "
                          f"artifacts) carries; hubs absorb the rest over "
                          f"{scoring['prior_coverage_pct']}% of priors."),
            "falsifier": ("Coverage falls below half the taxonomy with no new "
                          "directional mass within two runs."),
            "probability": 0.5,
        },
        {
            "id": "stress",
            "label": "Stress — one hop fails",
            "narrative": (f"The strongest prior breaks: evidence arrives that the "
                          f"first hop out of {driver or 'the driver'} does not "
                          f"transmit. Exposure reprices to the next supported edge."),
            "falsifier": ("A primary-source artifact contradicts the lead "
                          "transmits_to edge."),
            "probability": 0.3,
        },
        {
            "id": "fragmentation",
            "label": "Fragmentation — unexamined dominates",
            "narrative": (f"The map's {len(unexamined)} unexamined domain(s) "
                          f"({', '.join(unexamined) or 'none'}) turn out to carry "
                          f"the transmission instead. The current read is not "
                          f"wrong, it is pointed at the wrong place."),
            "falsifier": ("A targeted gap run fills an unexamined domain with "
                          "directional evidence pointing away from the baseline."),
            "probability": 0.2,
        },
    ]


def mermaid_transmission_map(driver: str | None, channels: list[dict],
                             supported_pairs: list[list]) -> str:
    """Mermaid flowchart of prior vs supported channels."""
    supported_set = {tuple(p) for p in supported_pairs}
    lines = ["flowchart LR"]
    seen: set[str] = set()

    def _nid(d: str) -> str:
        return re.sub(r"\W+", "_", d)

    import re
    for c in channels[:24]:
        s, t = c["src"], c["dst"]
        for d in (s, t):
            if d not in seen:
                seen.add(d)
                label = D.domain_label(d).replace('"', "'")
                lines.append(f'    {_nid(d)}["{label}"]')
        style = "==>" if (s, t) in supported_set else "-->"
        ch = c["channel"].replace('"', "'")[:40]
        lines.append(f'    {_nid(s)} {style} |"{ch}"| {_nid(t)}')
    return "\n".join(lines)


def build_assessment(db, investigation_id: int,
                     driver: str | None = None,
                     shock: str | None = None,
                     requested_by: str | None = None) -> dict[str, Any]:
    """Full deterministic assessment over stored rows."""
    from .models import Investigation
    inv = db.query(Investigation).filter(Investigation.id == investigation_id).first()
    if inv is None:
        raise ValueError("no such investigation")
    driver = driver or inv.driver
    shock = shock if shock is not None else inv.shock
    if driver and driver not in D.DRIVERS:
        driver = None

    rows = _load_rows(db, investigation_id)
    edges = _load_edges(db, investigation_id)
    scoring = score_transmission(rows, edges, driver)
    council_out = run_council_safe(rows)
    scenarios = build_scenarios(driver, shock, scoring, council_out)
    channels: list[dict] = []
    for (s, t), es in _supported_map(rows, edges).items():
        chan = D.CHANNEL_INDEX.get((s, t))
        channels.append({
            "src": s, "dst": t,
            "src_label": D.domain_label(s), "dst_label": D.domain_label(t),
            "channel": chan["channel"] if chan else "transmits_to",
            "strength": chan["strength"] if chan else None,
            "lag": chan["lag"] if chan else None,
            "n_edges": len(es),
            "supported": True,
        })
    # Unbridged priors for this driver.
    seeds = D.DRIVERS[driver]["domains"] if driver and driver in D.DRIVERS else []
    priors = D.walk_transmission(seeds, max_depth=2) if seeds else []
    prior_pairs = {(p["via"]["src"], p["via"]["dst"]) for p in priors}
    have = {(c["src"], c["dst"]) for c in channels}
    for (s, t) in sorted(prior_pairs - have):
        chan = D.CHANNEL_INDEX.get((s, t), {})
        channels.append({
            "src": s, "dst": t,
            "src_label": D.domain_label(s), "dst_label": D.domain_label(t),
            "channel": chan.get("channel", "?"),
            "strength": chan.get("strength"),
            "lag": chan.get("lag"),
            "n_edges": 0,
            "supported": False,
        })

    signals = sorted(
        [r for r in rows if r.get("direction") in ("up", "down")
         and r.get("magnitude") is not None],
        key=lambda r: -(r.get("magnitude") or 0))[:8]

    version, fingerprint = taxonomy_fingerprint()
    diagrams = {
        "transmission": mermaid_transmission_map(
            driver, D.CHANNELS,
            [[c["src"], c["dst"]] for c in channels if c["supported"]]),
    }
    markdown = render_markdown(
        investigation_title=inv.title, driver=driver, shock=shock,
        scoring=scoring, channels=channels, signals=signals,
        council_out=council_out, scenarios=scenarios)
    return {
        "investigation_id": investigation_id,
        "driver": driver,
        "shock": shock,
        "requested_by": requested_by,
        "scoring": scoring,
        "overall_pct": scoring["overall_pct"],
        "coverage_pct": scoring["coverage_pct"],
        "confidence_pct": scoring["confidence_pct"],
        "exposure_pct": scoring["exposure_pct"],
        "channels": channels,
        "signals": signals,
        "hypotheses": council_out,
        "scenarios": scenarios,
        "diagrams": diagrams,
        "markdown": markdown,
        "taxonomy_version": version,
        "taxonomy_fingerprint": fingerprint,
    }


def _supported_map(rows, edges) -> dict[tuple, list]:
    id_to_domain = {r["id"]: r.get("domain") for r in rows}
    out: dict[tuple, list] = {}
    for e in edges:
        if e["type"] != "transmits_to":
            continue
        s, t = id_to_domain.get(e["source_id"]), id_to_domain.get(e["target_id"])
        if s and t and s != t:
            out.setdefault((s, t), []).append(e)
    return out


def run_council_safe(rows: list[dict]) -> dict[str, Any]:
    try:
        return council_mod.run_council(rows, list(D.DOMAINS))
    except Exception:
        return {"method": "hypothesis-council-econ-v1", "claims": [],
                "coverage": {"covered": [], "unexamined": list(D.DOMAINS)},
                "disagreement": {"n_claims": 0}}


def render_markdown(*, investigation_title: str, driver: str | None,
                    shock: str | None, scoring: dict, channels: list,
                    signals: list, council_out: dict,
                    scenarios: list) -> str:
    drv = D.DRIVERS.get(driver or "", {}) if driver else {}
    lines = [f"# Transmission assessment — {investigation_title}", ""]
    if drv:
        lines.append(f"**Driver:** {drv.get('label', driver)}")
    if shock:
        lines.append(f"**Shock:** {shock}")
    lines += ["",
              f"**Overall {scoring['overall_pct']}%** — coverage "
              f"{scoring['coverage_pct']}% · priors bridged "
              f"{scoring['bridged']}/{scoring['prior_channels']} "
              f"({scoring['prior_coverage_pct']}%) · confidence "
              f"{scoring['confidence_pct']}% · "
              f"exposure {scoring['exposure_pct']}% · "
              f"{scoring['directional']}/{scoring['n_artifacts']} "
              "directional.", ""]
    lines += ["## Supported channels", ""]
    sup = [c for c in channels if c["supported"]]
    if sup:
        for c in sorted(sup, key=lambda c: -(c.get("strength") or 0)):
            lines.append(
                f"- **{c['src_label']} → {c['dst_label']}** — "
                f"{c['channel']} "
                f"({c['n_edges']} edge(s), prior {c.get('strength')}, "
                f"lag {c.get('lag')}q)")
    else:
        lines.append("- None. Every prior channel is unexamined "
                     "against stored evidence.")
    lines += ["", "## Unbridged priors", ""]
    unb = [c for c in channels if not c["supported"]]
    if unb:
        for c in unb[:12]:
            lines.append(f"- {c['src_label']} → {c['dst_label']} — {c['channel']}")
    else:
        lines.append("- None. Every relevant prior has at least one edge.")
    lines += ["", "## Strongest directional signals", ""]
    if signals:
        for s in signals:
            lines.append(
                f"- **{s['direction']}** {s.get('magnitude')} / "
                f"{s.get('horizon') or '?'} — "
                f"{s['title']} ({D.domain_label(s.get('domain'))})")
    else:
        lines.append("- No directional artifact. Without magnitude and "
                     "horizon nothing here can anchor a chain.")
    lines += ["", "## Hypotheses (council)", "",
              f"Method {council_out.get('method')}; "
              f"{council_out.get('disagreement', {}).get('n_contested', 0)} "
              "contested / "
              f"{council_out.get('disagreement', {}).get('n_claims', 0)} claims.", ""]
    for h in council_out.get("claims", [])[:12]:
        lines.append(f"- [{h['seat']}] {h['claim']}")
        lines.append(f"  - falsifier ({h.get('falsifier_grade')}): "
                     f"{h.get('falsifier')}")
        for o in h.get("objections", [])[:3]:
            lines.append(f"  - objection [{o['seat']}]: {o['why']}")
    unex = council_out.get("coverage", {}).get("unexamined", [])
    if unex:
        lines += ["", "**Unexamined:** "
                  f"{', '.join(D.domain_label(d) for d in unex)}"]
    lines += ["", "## Scenarios", ""]
    for sc in scenarios:
        lines.append(f"### {sc['label']} (p={sc['probability']})")
        lines.append("")
        lines.append(sc["narrative"])
        lines.append("")
        lines.append(f"_Falsifier:_ {sc['falsifier']}")
        lines.append("")
    return "\n".join(lines)
