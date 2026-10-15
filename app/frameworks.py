"""Policy-framework matrix: named regimes scored against the taxonomy.

The original app scores 34 AI-governance frameworks × 10 pillars. The
economics equivalent scores the regimes that actually govern the eleven
domains — Basel III, MiCA, the GENIUS Act, IMF surveillance, and the rest —
against what this estate has collected.

Two halves, deliberately separate:

* ``FRAMEWORKS`` — a static catalogue. Each regime declares the domains it
  governs. This is taxonomy, not evidence: editing it changes what the matrix
  *asks*, never what the corpus *says*.
* ``build_matrix`` — the evidence join. Cell (framework, domain) scores 0..100
  from stored artifacts in that domain: count capped, directional mass
  weighted. A regime whose domains have no evidence scores zero, visibly —
  never a dash that reads as "not applicable".
"""
from __future__ import annotations

from . import domains as D

# id -> regime. ``domains`` is the claim "this regime governs these domains".
FRAMEWORKS: dict[str, dict] = {
    "basel-iii": {
        "label": "Basel III / bank capital",
        "body": "BCBS",
        "domains": ["macro", "monetary_policy", "sovereign_debt"],
        "blurb": "Bank capital, liquidity and leverage rules that set credit supply.",
    },
    "mica": {
        "label": "MiCA (EU crypto-assets)",
        "body": "EU",
        "domains": ["stablecoins", "blockchain", "politics"],
        "blurb": "Reserve, disclosure and licensing for e-money tokens and CASPs.",
    },
    "genius-act": {
        "label": "GENIUS Act (US stablecoins)",
        "body": "US Congress",
        "domains": ["stablecoins", "politics", "monetary_policy"],
        "blurb": "1:1 backing from a closed reserve list; issuer chartering.",
    },
    "imf-surveillance": {
        "label": "IMF Article IV surveillance",
        "body": "IMF",
        "domains": ["macro", "fiscal_policy", "sovereign_debt", "monetary_policy"],
        "blurb": "Bilateral surveillance of fiscal, monetary and external policy.",
    },
    "bis-cpmi": {
        "label": "CPMI-IOSCO payment principles",
        "body": "BIS",
        "domains": ["stablecoins", "blockchain", "macro"],
        "blurb": "Principles for systemically important payment systems and FMIs.",
    },
    "fatf-travel": {
        "label": "FATF travel rule",
        "body": "FATF",
        "domains": ["blockchain", "stablecoins", "politics"],
        "blurb": "Originator/beneficiary information on virtual-asset transfers.",
    },
    "us-ear": {
        "label": "US export controls (EAR / chip rules)",
        "body": "BIS (Commerce)",
        "domains": ["politics", "ai_labs", "datacenters"],
        "blurb": "Compute and semiconductor restrictions deciding who scales where.",
    },
    "eu-ai-act": {
        "label": "EU AI Act",
        "body": "EU",
        "domains": ["ai_labs", "ai_agents", "agi_asi", "politics"],
        "blurb": "Risk tiers, GPAI obligations and compute-threshold triggers.",
    },
    "fsb-nbfi": {
        "label": "FSB NBFI / money-fund reforms",
        "body": "FSB",
        "domains": ["macro", "sovereign_debt", "stablecoins"],
        "blurb": ("Liquidity rules for funds that compete with stablecoins "
                  "at the bill auction."),
    },
    "oecd-pillar2": {
        "label": "OECD Pillar Two (global minimum tax)",
        "body": "OECD",
        "domains": ["fiscal_policy", "macro", "politics"],
        "blurb": "15% minimum effective rate; reshapes where labs book margin.",
    },
    "ferc-queue": {
        "label": "FERC interconnection reform",
        "body": "FERC",
        "domains": ["datacenters", "macro", "politics"],
        "blurb": "Queue reform deciding when reserved power actually energises.",
    },
    "frontier-commitments": {
        "label": "Frontier-lab safety commitments",
        "body": "Labs / Seoul / Bletchley",
        "domains": ["ai_labs", "agi_asi", "ai_agents"],
        "blurb": "Voluntary capability thresholds, evals and deployment pledges.",
    },
    "wto-rules": {
        "label": "WTO rules and disputes",
        "body": "WTO",
        "domains": ["trade", "politics", "macro"],
        "blurb": "Most-favoured-nation, dispute settlement, subsidy rules.",
    },
    "sanctions-regimes": {
        "label": "Sanctions regimes (OFAC / EU / UN)",
        "body": "OFAC / EU / UN",
        "domains": ["geopolitics", "blockchain", "stablecoins", "sovereign_debt"],
        "blurb": "Designations that freeze addresses, reserves and issuance.",
    },
}


def _cell_score(n: int, n_directional: int) -> int:
    """0..100 from evidence mass. Count caps fast; directional mass decides.

    Ten artifacts with no directional reading score lower than three with
    magnitude and horizon — background is not findings, and the matrix must
    not reward hoarding.
    """
    base = min(60, n * 12)
    bonus = min(40, n_directional * 13)
    return base + bonus


def build_matrix(db, investigation_id: int | None = None) -> dict:
    """Score every framework against stored evidence."""
    from .models import Artifact
    q = db.query(Artifact).filter(Artifact.review != "rejected")
    if investigation_id:
        q = q.filter(Artifact.investigation_id == investigation_id)
    by_domain: dict[str, list] = {}
    for a in q.all():
        if a.domain:
            by_domain.setdefault(a.domain, []).append(a)

    rows = []
    for fid, fw in FRAMEWORKS.items():
        cells = {}
        for did in fw["domains"]:
            arts = by_domain.get(did, [])
            n_dir = sum(1 for a in arts
                        if (a.direction or "") in ("up", "down"))
            cells[did] = {
                "domain": did,
                "label": D.domain_label(did),
                "artifacts": len(arts),
                "directional": n_dir,
                "score": _cell_score(len(arts), n_dir),
            }
        covered = sum(1 for c in cells.values() if c["artifacts"] > 0)
        rows.append({
            "id": fid, "label": fw["label"], "body": fw["body"],
            "blurb": fw["blurb"], "domains": fw["domains"],
            "cells": cells,
            "covered": covered,
            "of": len(cells),
            "coverage_pct": round(100 * covered / max(1, len(cells)), 1),
            "mean_score": round(sum(c["score"] for c in cells.values())
                                / max(1, len(cells)), 1),
        })
    rows.sort(key=lambda r: (-r["mean_score"], r["label"]))
    return {
        "frameworks": rows,
        "n_frameworks": len(rows),
        "investigation_id": investigation_id,
        "domains_total": len(D.DOMAINS),
    }
