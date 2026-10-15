"""A2A agent cards: the app's agents as callable units.

Each card names an agent, what it reads, what it returns, and the intents it
understands. ``dispatch`` runs one protocol hop directly — debugging,
agent-to-agent calls, and the future mesh peer path all go through it.

The rule that keeps this honest: every intent here is *deterministic over
stored rows*. Nothing dispatches a model call or a network fetch. A card that
needs a gateway would disappear exactly when the evidence is thinnest, and a
mesh peer invoking it would have no way to know.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

PROTOCOL = "econ-a2a/v1"

CARDS: list[dict[str, Any]] = [
    {
        "name": "taxonomy",
        "description": "The domain/channel/driver catalogue and transmission priors.",
        "intents": ["catalog", "transmission.walk"],
        "input_schema": {"domain": "domain id", "max_depth": "int",
                         "min_strength": "float 0..1"},
        "output": "domains, channels, drivers, walked paths",
    },
    {
        "name": "collector",
        "description": "Plans searches and maps candidates into the graph. Queue only.",
        "intents": ["collect.queue"],
        "input_schema": {"investigation_id": "int", "max_items": "int",
                         "max_rounds": "int"},
        "output": "job id (work runs async)",
    },
    {
        "name": "assessor",
        "description": "Deterministic transmission scoring over stored rows.",
        "intents": ["assessment.score"],
        "input_schema": {"investigation_id": "int"},
        "output": "coverage, bridged priors, confidence, exposure, overall",
    },
    {
        "name": "council",
        "description": "Six-seat deliberation: claims, objections, coverage.",
        "intents": ["council.panel", "council.run"],
        "input_schema": {"investigation_id": "int (run only)"},
        "output": "seats with blind spots, claims, unexamined domains",
    },
    {
        "name": "gaps",
        "description": "Coverage vs absence: gaps, thin domains, unbridged channels.",
        "intents": ["gaps.compute"],
        "input_schema": {"investigation_id": "int"},
        "output": "gaps, thin, unbridged_channels",
    },
    {
        "name": "frameworks",
        "description": "Policy-regime matrix scored against collected evidence.",
        "intents": ["frameworks.matrix"],
        "input_schema": {"investigation_id": "int | null (estate-wide)"},
        "output": "per-framework domain cells 0..100",
    },
    {
        "name": "manager",
        "description": "Parses a fan-out command with no side effects.",
        "intents": ["manager.parse"],
        "input_schema": {"command": "str"},
        "output": "topics (nothing created)",
    },
    {
        "name": "ask",
        "description": ("Answers any question from collected evidence "
                         "plus the live web."),
        "intents": ["ask.answer"],
        "input_schema": {"question": "str", "scope": "'all' or 'inv:<id>'",
                         "mode": "str", "parent_id": "int | null"},
        "output": "queued job (work runs async)",
    },
]


def get_agent_cards() -> list[dict[str, Any]]:
    return [{"protocol": PROTOCOL, **c} for c in CARDS]


def new_envelope(frm: str, to: str, intent: str, payload: dict | None = None,
                 *, task_id: str | None = None, note: str = "") -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "id": f"env-{uuid.uuid4().hex[:12]}",
        "task_id": task_id or f"task-{uuid.uuid4().hex[:8]}",
        "from": frm, "to": to, "intent": intent,
        "payload": payload or {}, "note": note,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _card(name: str) -> dict[str, Any]:
    for c in CARDS:
        if c["name"] == name:
            return c
    raise ValueError(f"unknown agent '{name}'. Known: "
                     f"{', '.join(c['name'] for c in CARDS)}")


def dispatch(env: dict[str, Any], db=None) -> dict[str, Any]:
    """Run one hop. Raises ValueError on unknown agent or intent."""
    to = env.get("to", "")
    intent = env.get("intent", "")
    payload = env.get("payload") or {}
    card = _card(to)
    if intent not in card["intents"]:
        raise ValueError(f"agent '{to}' does not understand intent '{intent}'. "
                         f"Known: {', '.join(card['intents'])}")
    from .database import SessionLocal
    own = db is None
    db = db or SessionLocal()
    try:
        result = _run(to, intent, payload, db)
    finally:
        if own:
            db.close()
    return {"ok": True, "protocol": PROTOCOL, "to": to, "intent": intent,
            "task_id": env.get("task_id"), "result": result}


def _run(to: str, intent: str, payload: dict, db) -> dict[str, Any]:
    from . import domains as D
    if to == "taxonomy" and intent == "catalog":
        return {"domains": D.domain_catalog(), "drivers": D.DRIVERS,
                "n_channels": len(D.CHANNELS)}
    if to == "taxonomy" and intent == "transmission.walk":
        domain = payload.get("domain", "")
        if domain not in D.DOMAINS:
            raise ValueError(f"unknown domain '{domain}'")
        rows = D.walk_transmission(
            [domain], max_depth=int(payload.get("max_depth", 3)),
            min_strength=float(payload.get("min_strength", 0.5)))
        reached = len({r["domain"] for r in rows})
        return {"seed": domain, "paths": rows, "reached": reached}
    if to == "collector" and intent == "collect.queue":
        from . import jobqueue
        inv_id = int(payload.get("investigation_id", 0))
        if not inv_id:
            raise ValueError("investigation_id is required")
        return jobqueue.enqueue(
            "collect", {"investigation_id": inv_id,
                        "max_items": int(payload.get("max_items", 12)),
                        "max_rounds": int(payload.get("max_rounds", 1)),
                        "trigger": "a2a"}, investigation_id=inv_id)
    if to == "assessor" and intent == "assessment.score":
        from .assessment import build_assessment
        inv_id = int(payload.get("investigation_id", 0))
        out = build_assessment(db, inv_id)
        return {k: out[k] for k in ("overall_pct", "coverage_pct",
                                    "confidence_pct", "exposure_pct")}
    if to == "council" and intent == "council.panel":
        from . import council as _c
        return _c.council_panel()
    if to == "council" and intent == "council.run":
        from . import council as _c
        from .assessment import _load_rows
        inv_id = int(payload.get("investigation_id", 0))
        return _c.run_council(_load_rows(db, inv_id), list(D.DOMAINS))
    if to == "gaps" and intent == "gaps.compute":
        from . import research_gaps as _g
        inv_id = int(payload.get("investigation_id", 0))
        pic = _g.compute_gaps(inv_id)
        return {"gaps": pic.get("gaps", []), "thin": pic.get("thin", []),
                "unbridged_channels": pic.get("unbridged_channels", [])}
    if to == "frameworks" and intent == "frameworks.matrix":
        from . import frameworks as _f
        inv_id = payload.get("investigation_id")
        return _f.build_matrix(db, int(inv_id) if inv_id else None)
    if to == "manager" and intent == "manager.parse":
        from . import manager as _m
        return _m.parse_command(payload.get("command", ""))
    if to == "ask" and intent == "ask.answer":
        from . import jobqueue
        question = (payload.get("question") or "").strip()
        if not question:
            raise ValueError("question is required")
        return jobqueue.enqueue(
            "ask", {"question": question,
                    "scope": payload.get("scope", "all"),
                    "mode": payload.get("mode", "explain"),
                    "parent_id": payload.get("parent_id")})
    raise ValueError(f"no handler for {to}/{intent}")
