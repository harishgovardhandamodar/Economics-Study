#!/usr/bin/env python3
"""Seed the app with investigations that exercise the cross-domain machinery.

Not demo filler. Each one is a real research question that needs more than one
domain to answer, chosen so that a first run produces a map with visible
structure *and* visible holes -- a seeded map that covers everything teaches
nothing about what the gaps view is for.

    python seed.py            # create anything missing
    python seed.py --reset    # drop and rebuild
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import Investigation  # noqa: E402

INVESTIGATIONS = [
    {
        "title": "Rate decision → stablecoin float → T-bill demand",
        "keywords": "federal reserve rate decision, stablecoin reserves, "
                    "T-bill auction, dollar substitution",
        "description": (
            "A policy-rate move changes the risk-free yield that anchors stablecoin "
            "reserve returns. That float is a large, short-duration private money "
            "supply whose only reserve asset is the bill. Trace how a rate decision "
            "lands on stablecoin issuance and redemption, and what that does to "
            "demand at the Treasury auction."),
        "preferred_domains":
        "monetary_policy,stablecoins,sovereign_debt,macro,politics",
        "driver": "rate_decision",
        "shock": "A 50bp hike delivered as a surprise, with no forward guidance.",
        "sources": "rss,web",
    },
    {
        "title": "AI capex → grid → CPI",
        "keywords": "data center power demand, hyperscaler capex, electricity prices, "
                    "grid interconnection, GPU supply",
        "description": (
            "Frontier-lab compute commitments become power orders, which become "
            "interconnection queues and rate cases, which become industrial "
            "electricity prices, which land in the CPI basket. Map the chain and "
            "find where it is weakest."),
        "preferred_domains":
        "ai_labs,datacenters,macro,politics,sovereign_debt,agi_asi",
        "driver": "compute_commitment",
        "shock": "A multi-billion-dollar campus reservation with a\n"
                " 2028 energisation date.",
        "sources": "rss,web",
    },
    {
        "title": "Export controls and the frontier-lab map",
        "keywords": "AI export controls, chip restrictions, compute quotas, "
                    "frontier labs, national AI policy",
        "description": (
            "Export controls decide which labs can scale, and where. Map the "
            "political layer onto the capability layer: who is constrained, by "
            "what instrument, and what it does to the compute base the capability "
            "estimates depend on."),
        "preferred_domains": "politics,ai_labs,datacenters,agi_asi,fiscal_policy",
        "driver": "export_control",
        "shock": "A new compute quota or chip restriction announced without notice.",
        "sources": "rss,web",
    },
    {
        "title": "Sovereign debt service and the fiscal reaction function",
        "keywords": "debt service ratio, sovereign issuance, term premium, "
                    "fiscal consolidation, central bank independence",
        "description": (
            "Rising debt service crowds out spending and raises the term premium the "
            "central bank has to operate against. Trace the loop between the debt "
            "stock, the budget, the bond market and the reaction function."),
        "preferred_domains":
        "sovereign_debt,fiscal_policy,monetary_policy,macro,politics",
        "driver": "issuance",
        "sources": "rss,web",
    },
    {
        "title": "AGI/ASI timeline against the labour and energy base",
        "keywords": "AGI timeline, ASI, capability jump, labour substitution, "
                    "compute scaling, electricity demand",
        "description": (
            "Capability timelines are usually stated as a model-release cadence. "
            "The binding constraints are physical: power for training, and the "
            "labour substitution that only pays out over years. Map both against "
            "the macro effect."),
        "preferred_domains": "agi_asi,ai_labs,datacenters,macro,ai_agents,politics",
        "driver": "capability_jump",
        "sources": "rss,arxiv,web",
    },
    {
        "title": "Tariffs, chokepoints and the price level",
        "keywords": "tariffs, Suez, freight rates, import prices, sanctions, "
                    "WTO dispute",
        "description": (
            "The price level increasingly arrives from outside the border: a "
            "tariff reprices imports, a strait closure reprices freight, and a "
            "sanctions package reroutes both. Trace how trade barriers, "
            "shipping disruptions and geopolitical ruptures transmit into "
            "inflation, issuance and the policy reaction."),
        "preferred_domains": "trade,geopolitics,disruptions,macro,politics",
        "driver": "tariff_shock",
        "shock": ("A 25-point tariff hike lands in the same quarter as "
                  "a strait closure."),
        "sources": "rss,web",
    },
]


def reset() -> int:
    from app.models import (
        AgentEvent,
        AgentRun,
        Artifact,
        ArtifactReview,
        CouncilRecord,
        Explanation,
        Investigation,
        QueryShapeYield,
        Relationship,
        ResearchGap,
    )
    db = SessionLocal()
    try:
        n = 0
        for model in (AgentEvent, CouncilRecord, Explanation, ResearchGap,
                      Relationship, ArtifactReview, AgentRun, Artifact,
                      QueryShapeYield, Investigation):
            n += db.query(model).delete()
        db.commit()
        return n
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true",
                    help="delete every investigation, run, artifact and answer first")
    args = ap.parse_args()

    init_db()
    if args.reset:
        n = reset()
        print(f"reset: removed {n} row(s)")

    db = SessionLocal()
    try:
        existing = {i.title for i in db.query(Investigation).all()}
        created = 0
        for spec in INVESTIGATIONS:
            if spec["title"] in existing:
                print(f"  skip  {spec['title']}")
                continue
            db.add(Investigation(**spec))
            created += 1
            print(f"  new   {spec['title']}")
        db.commit()
    finally:
        db.close()

    print(f"\n{created} investigation(s) created.")
    print("Start the app:  uvicorn app.main:app --port 8304")
    print("Then open:      http://localhost:8304")
    print("\nEach one has a research gap in most of the taxonomy by design —")
    print("that is the point of the Research gaps view.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
