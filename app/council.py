"""Economics council: seven seats, each owning a slice of the taxonomy.

Why this exists, in one paragraph: a single drafting call at low temperature
returns the model's *modal* view of economics — the consensus of the training
corpus: "rates up, inflation down", "deficits matter", "AI raises
productivity". Not wrong, but the part of the field every paper already
covers. Real panels are distinct disciplines with distinct blind spots, and
the disagreements are half the information. This module is that panel as
deterministic engine code, not a prompt.

* ``SEATS`` — named reviewers, each owning domains and declaring the weakness
  it is known to have. A panel that publishes its blind spots is auditable.
* ``draft_seat_claims`` — per-seat drafting, grounded in stored rows. Never
  invents; a seat with no evidence stays silent.
* ``cross_examine`` — every claim attacked by non-owning seats on checkable
  grounds: unfalsifiable test, self-graded magnitude, rival explanation,
  single-domain support.
* ``coverage_matrix`` — domain x claim. Domains with no claim are
  ``unexamined``, emphatically not ``safe``.
* ``panel_disagreement`` — agreement on contested claims.

Nothing here raises, invents evidence, or reaches the network. The seats always
run; the model only ever adds to them.
"""
from __future__ import annotations

import re
from typing import Any

SEATS: tuple[dict[str, Any], ...] = (
    {
        "id": "monetary",
        "discipline": "Monetary economics",
        "remit": "What do rates, reserves and the dollar transmit, and with what lag?",
        "owns": ("monetary_policy", "macro", "sovereign_debt"),
        "blind_spot": ("Reads everything as a monetary phenomenon. Says nothing "
                       "about compute constraints or protocol design."),
    },
    {
        "id": "fiscal-state",
        "discipline": "Public finance / political economy",
        "remit": "What do budgets, issuance and elections transmit?",
        "owns": ("fiscal_policy", "politics", "sovereign_debt"),
        "blind_spot": ("Treats labs and chains as policy objects. Says nothing "
                       "about capability timelines."),
    },
    {
        "id": "crypto-money",
        "discipline": "Crypto-economics / market microstructure",
        "remit": "What do stablecoin float, reserves and settlement demand transmit?",
        "owns": ("stablecoins", "blockchain", "monetary_policy"),
        "blind_spot": ("Over-weights on-chain visibility. Says nothing about "
                       "off-chain fiscal arithmetic."),
    },
    {
        "id": "compute-capacity",
        "discipline": "Compute / energy systems",
        "remit": "What do capex, power and interconnection transmit?",
        "owns": ("datacenters", "ai_labs", "agi_asi"),
        "blind_spot": ("Treats demand as exogenous. Says nothing about the "
                       "monetary conditions financing the build-out."),
    },
    {
        "id": "labour-agents",
        "discipline": "Labour / AI agents",
        "remit": "What do agentic deployment and capability jumps transmit to work?",
        "owns": ("ai_agents", "agi_asi", "macro"),
        "blind_spot": ("Reads capability as deployment. Says nothing about "
                       "reserve regimes or issuance mechanics."),
    },
    {
        "id": "statecraft",
        "discipline": "Geoeconomics / statecraft",
        "remit": "What do borders, tariffs and ruptures transmit?",
        "owns": ("trade", "geopolitics", "disruptions"),
        "blind_spot": ("Reads everything as statecraft. Says nothing about "
                       "balance sheets or capability curves."),
    },
    {
        "id": "measurement",
        "discipline": "Measurement science",
        "remit": "Owns no domain. Exists only to attack the other five's proposals.",
        "owns": (),
        "blind_spot": ("Produces no claims of its own. A panel of only this "
                       "seat would say nothing at all."),
    },
)

SEAT_BY_ID = {s["id"]: s for s in SEATS}


def _seat_owning(domain: str) -> str | None:
    for s in SEATS:
        if domain in s["owns"]:
            return s["id"]
    return None


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9\-]{2,}", (text or "").lower()))


def falsifier_audit(falsifier: str) -> dict[str, Any]:
    """Grade a refutation test on three decidable questions, not on vibes.

    A test that settles nothing ("review the deployment") used to score the
    same as one that would ("request 20 rows back verbatim"). Three checks:
    names an observation, names a threshold, is not only abstract.
    """
    text = (falsifier or "").strip()
    toks = _tokens(text)
    # Observation: names something you could go and look at.
    observation_words = {"auction", "holdings", "issuance", "redemption", "tail",
                         "spread", "rate", "mw", "gw", "queue", "release",
                         "bill", "reserve", "capex", "interconnection", "vote",
                         "sanction", "address", "float", "data", "report"}
    has_observation = bool(toks & observation_words) or bool(
        re.search(r"\d", text))
    # Threshold: a number with a unit, or an explicit comparison.
    has_threshold = bool(re.search(
        r"\d+\s*(bp|%|bn|tn|mw|gw|q\b|quarters?|days?|weeks?)", text.lower())) \
        or bool(re.search(r"\b(above|below|exceeds?|under|over|wider|narrower)\b",
                          text.lower()))
    # Only-abstract: no named actor, no number, no date.
    abstract_markers = {"framework", "paradigm", "landscape", "ecosystem",
                        "holistic", "robust", "leverage", "synergy"}
    only_abstract = bool(toks & abstract_markers) and not has_observation
    score = (0.5 if has_observation else 0.0) + (0.4 if has_threshold else 0.0) \
        + (0.1 if (text and not only_abstract) else 0.0)
    if only_abstract:
        score = min(score, 0.2)
    if not text:
        score = 0.0
    return {
        "grade": round(score, 2),
        "has_observation": has_observation,
        "has_threshold": has_threshold,
        "only_abstract": only_abstract,
        "reason": ("names observation + threshold" if score >= 0.8
                   else "names observation, no threshold" if has_observation
                   else "abstract only" if only_abstract else "empty or vague"),
    }


def _default_falsifier(domains: list[str], supporting: list[str]) -> str:
    dom = "/".join(domains[:2]) if domains else "the domain"
    cited = ', '.join(supporting[:2]) or 'no source yet'
    return (f"Collect 5+ primary-source artifacts in {dom} over the next run; "
            f"if none supports the claim ({cited}), "
            f"drop it to unexamined.")


def draft_seat_claims(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One claim per seat per domain-cluster with evidence, deterministically.

    The seats' claims are the floor, not the fallback: a council that needs a
    gateway disappears exactly when the evidence is thinnest.
    """
    by_domain: dict[str, list[dict]] = {}
    for r in rows:
        d = r.get("domain")
        if d:
            by_domain.setdefault(d, []).append(r)
    claims: list[dict] = []
    for seat in SEATS:
        if not seat["owns"]:
            continue
        for domain in seat["owns"]:
            items = by_domain.get(domain, [])
            if not items:
                continue
            directional = [x for x in items if x.get("direction") in ("up", "down")]
            n = len(items)
            titles = [x.get("title", "")[:70] for x in items[:3]]
            claim_text = (
                f"{domain}: {n} artifact(s), {len(directional)} directional. "
                f"Top: {'; '.join(titles)}"
            )
            falsifier = _default_falsifier([domain], titles)
            audit = falsifier_audit(falsifier)
            claims.append({
                "seat": seat["id"],
                "discipline": seat["discipline"],
                "domains": [domain],
                "claim": claim_text,
                "supporting": [x.get("id") for x in items[:5]],
                "n_supporting": n,
                "n_directional": len(directional),
                "falsifier": falsifier,
                "falsifier_grade": audit["grade"],
                "falsifier_audit": audit,
                "objections": [],
                "peer_scrutiny": 0.0,
            })
    return claims


def cross_examine(claim: dict[str, Any],
                  all_claims: list[dict[str, Any]]) -> dict[str, Any]:
    """Other seats object on checkable grounds. Silence scores zero."""
    objections: list[dict] = []
    my_seat = claim.get("seat")

    def _raise(seat_id: str, ground: str, why: str, weight: float) -> None:
        objections.append({"seat": seat_id, "ground": ground,
                           "why": why, "weight": weight})

    # Single-domain support: a cross-domain claim on one domain's evidence.
    if len(claim.get("domains", [])) <= 1 and claim.get("n_supporting", 0) >= 1:
        for s in SEATS:
            if s["id"] != my_seat and s["id"] != "measurement":
                _raise(s["id"], "single_domain",
                       f"Cross-domain language on {claim['domains']} evidence alone.",
                       0.3)
                break
    # Self-graded magnitude: no independent threshold.
    audit = claim.get("falsifier_audit") or {}
    if audit.get("grade", 0) < 0.5:
        _raise("measurement", "weak_falsifier",
               f"Falsifier grades {audit.get('grade')} ({audit.get('reason')}).",
               0.4)
    # No directional mass behind a directional-sounding claim.
    if claim.get("n_directional", 0) == 0:
        _raise("measurement", "no_directional",
               "No directional artifact behind a transmission claim.", 0.3)
    # Rival seat with evidence in an adjacent domain the claim ignores.
    claimed_domains = set(claim.get("domains", []))
    for other in all_claims:
        if other is claim:
            continue
        if set(other.get("domains", [])) - claimed_domains:
            _raise(other["seat"], "rival_coverage",
                   f"{other['seat']} holds evidence in {other['domains']} "
                   "this claim ignores.",
                   0.2)
            break
    claim = dict(claim)
    claim["objections"] = objections
    # Peer scrutiny is a scored dimension: unattacked scores zero, no neutral
    # fallback. Unattacked is not unrefutable.
    claim["peer_scrutiny"] = round(min(1.0, sum(o["weight"] for o in objections)), 2)
    return claim


def coverage_matrix(claims: list[dict[str, Any]],
                    domains: list[str]) -> dict[str, Any]:
    """Domain x claim. Domains with no claim are ``unexamined``."""
    covered = {c["domains"][0] for c in claims if c.get("domains")}
    return {
        "domains": domains,
        "covered": sorted(covered),
        "unexamined": [d for d in domains if d not in covered],
        "n_claims": len(claims),
    }


def panel_disagreement(claims: list[dict[str, Any]]) -> dict[str, Any]:
    contested = [c for c in claims if c.get("objections")]
    return {
        "n_claims": len(claims),
        "n_contested": len(contested),
        "contest_rate": round(len(contested) / max(1, len(claims)), 3),
        "mean_objections": round(
            sum(len(c.get("objections", [])) for c in claims) / max(1, len(claims)), 2),
    }


def run_council(rows: list[dict[str, Any]],
                domains: list[str]) -> dict[str, Any]:
    """Full deterministic council pass over stored rows."""
    base = draft_seat_claims(rows)
    examined = [cross_examine(c, base) for c in base]
    return {
        "method": "hypothesis-council-econ-v1",
        "seats": [{"id": s["id"], "discipline": s["discipline"],
                   "owns": list(s["owns"]), "blind_spot": s["blind_spot"]}
                  for s in SEATS],
        "claims": examined,
        "coverage": coverage_matrix(examined, domains),
        "disagreement": panel_disagreement(examined),
    }


def council_panel() -> dict[str, Any]:
    return {
        "method": "hypothesis-council-econ-v1",
        "seats": [{"id": s["id"], "discipline": s["discipline"],
                   "owns": list(s["owns"]), "blind_spot": s["blind_spot"]}
                  for s in SEATS],
    }
