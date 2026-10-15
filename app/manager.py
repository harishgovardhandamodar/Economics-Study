"""Agentic manager: one command fans out to N investigations plus a summary.

Understand parses a command into a plan with *no side effects* — parse as
often as you like before anything is created. Run creates one investigation
per topic with collection launched on each. Compile is refused while any child
is still running, and idempotent once it has run.

Economics topics come from the driver catalogue: a command naming "rate
decision", "stablecoin", "datacenter", "export control" etc. maps onto drivers
and domains deterministically. An LLM may enrich the plan, never substitute
it: a manager that needs a gateway disappears exactly when the brief is
thinnest.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from . import domains as D

MAX_TOPICS = 6


def _now():
    return datetime.now(UTC)


def split_command(command: str) -> dict[str, Any]:
    """Deterministic split: driver keywords -> topics, domains -> focus."""
    text = (command or "").lower()
    topics: list[dict] = []
    for key, drv in D.DRIVERS.items():
        label = drv["label"].lower()
        # Match on driver key fragments and label words.
        hits = [w for w in label.replace("/", " ").split()
                if len(w) > 3 and w in text]
        key_hit = key.replace("_", " ") in text or key.split("_")[0] in text
        if hits or key_hit:
            topics.append({
                "driver": key,
                "label": drv["label"],
                "domains": list(drv["domains"]),
                "shock": None,
                "keywords": ", ".join(drv["domains"][:3]),
            })
    # Domain mentions without a driver still earn a topic.
    for did, dom in D.DOMAINS.items():
        if did in text or dom["label"].lower() in text:
            if not any(did in t["domains"] for t in topics):
                topics.append({
                    "driver": None,
                    "label": dom["label"],
                    "domains": [did],
                    "shock": None,
                    "keywords": dom["label"],
                })
    # Dedupe by driver/label, cap.
    seen: set[str] = set()
    uniq = []
    for t in topics:
        k = t["driver"] or t["label"]
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    return {"topics": uniq[:MAX_TOPICS], "method": "deterministic-split-v1"}


def parse_command(command: str) -> dict[str, Any]:
    """Parse with no side effects. LLM enrichment is additive only."""
    if not (command or "").strip():
        return {"ok": False, "reason": "empty command", "topics": []}
    plan = split_command(command)
    # Optional LLM enrichment: may add a shock statement per topic, never
    # removes or replaces deterministic topics.
    try:
        from . import llm as _llm
        out = _llm.chat_json([
            {"role": "system",
             "content": "Economics research planner. Reply JSON only."},
            {"role": "user",
             "content": (f"Command: {command}\nTopics: "
                         f"{json.dumps(plan['topics'])}\n"
                         'Reply {"shocks": [{"driver": str, "shock": str}]} '
                         "with at most one shock per topic. Shocks must be "
                         "concrete events, not themes.")}],
            max_tokens=1024)
        shocks = {(s.get("driver") or ""): s.get("shock", "")[:300]
                  for s in (out.get("shocks") or []) if isinstance(s, dict)}
        for t in plan["topics"]:
            if t.get("driver") in shocks and shocks[t["driver"]]:
                t["shock"] = shocks[t["driver"]]
        plan["llm_enriched"] = bool(shocks)
    except Exception:
        plan["llm_enriched"] = False
    plan["ok"] = bool(plan["topics"])
    plan["command"] = command
    if not plan["topics"]:
        plan["reason"] = ("No driver or domain recognised. Name a driver "
                          f"({', '.join(sorted(D.DRIVERS))}) or a domain.")
    return plan


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    topics = plan.get("topics") or []
    if not topics:
        return {"ok": False, "reason": "plan has no topics"}
    if len(topics) > MAX_TOPICS:
        return {"ok": False, "reason": f"at most {MAX_TOPICS} topics"}
    for t in topics:
        if t.get("driver") and t["driver"] not in D.DRIVERS:
            return {"ok": False, "reason": f"unknown driver {t['driver']}"}
    return {"ok": True, "topics": len(topics)}


def run_plan(db, plan: dict[str, Any],
             options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create one investigation per topic and launch collection on each."""
    from .models import Investigation, ManagerRun
    options = options or {}
    v = validate_plan(plan)
    if not v.get("ok"):
        raise ValueError(v.get("reason", "invalid plan"))
    run = ManagerRun(command=plan.get("command", ""),
                     plan_json=json.dumps(plan), status="running")
    db.add(run)
    db.commit()
    # Create children and commit BEFORE enqueueing: enqueue opens its own
    # connection, and SQLite allows only one writer — holding our write txn
    # across the enqueue turns a second connection's INSERT into
    # "database is locked".
    children = []
    for t in plan["topics"]:
        title = t["label"] if t.get("driver") else f"Map {t['label']}"
        inv = Investigation(
            title=f"[manager #{run.id}] {title}"[:300],
            keywords=t.get("keywords", ""),
            description=(f"Manager topic from: {plan.get('command', '')[:400]}\n"
                         f"Driver: {t.get('driver') or 'topic'} · "
                         f"domains: {', '.join(t.get('domains', []))}"),
            sources="rss,web",
            preferred_domains=",".join(t.get("domains", [])),
            driver=t.get("driver"),
            shock=t.get("shock"))
        db.add(inv)
        db.flush()
        children.append({"investigation_id": inv.id, "driver": t.get("driver"),
                         "label": title})
    # Summary investigation nests the finished topics on compile.
    summary = Investigation(
        title=f"[manager #{run.id}] Summary: {plan.get('command', '')[:120]}",
        keywords="manager summary",
        description=f"Compiled summary of manager run #{run.id}.",
        sources="rss,web")
    db.add(summary)
    db.flush()
    run.summary_investigation_id = summary.id
    run.result_json = json.dumps({"children": children,
                                  "summary_investigation_id": summary.id})
    db.commit()
    from . import jobqueue
    for c in children:
        jobqueue.enqueue("collect", {
            "investigation_id": c["investigation_id"],
            "max_items": int(options.get("max_items", 12)),
            "max_rounds": int(options.get("max_rounds", 1)),
            "trigger": "manager"}, investigation_id=c["investigation_id"])
    return {"manager_run_id": run.id, "children": children,
            "summary_investigation_id": summary.id, "status": "running"}


class PendingChildren(Exception):
    def __init__(self, pending: list[str]):
        self.pending = pending
        super().__init__(f"{len(pending)} child investigation(s) still running")


def compile_run(db, run_id: int) -> dict[str, Any]:
    """Aggregate finished children into the summary investigation. Refused
    while any child is still running; idempotent once done."""
    from .models import AgentRun, Artifact, Investigation, ManagerRun
    run = db.query(ManagerRun).filter(ManagerRun.id == run_id).first()
    if run is None:
        raise ValueError("no such manager run")
    if run.status == "done":
        return {**json.loads(run.result_json or "{}"), "status": "done",
                "manager_run_id": run.id, "idempotent": True}
    payload = json.loads(run.result_json or "{}")
    children = payload.get("children", [])
    pending = []
    for c in children:
        running = (db.query(AgentRun)
                   .filter(AgentRun.investigation_id == c["investigation_id"],
                           AgentRun.status == "running").count())
        if running:
            pending.append(str(c["investigation_id"]))
    if pending:
        raise PendingChildren(pending)
    summary_id = payload.get("summary_investigation_id") or run.summary_investigation_id
    summary = db.query(Investigation).filter(Investigation.id == summary_id).first()
    lines = [f"# Manager summary — run #{run.id}", "",
             f"> {run.command}", ""]
    total_artifacts = 0
    for c in children:
        n = (db.query(Artifact)
             .filter(Artifact.investigation_id == c["investigation_id"]).count())
        total_artifacts += n
        lines += [f"## {c['label']} (inv {c['investigation_id']})",
                  f"- {n} artifact(s)", ""]
    lines += [f"**Total: {total_artifacts} artifacts across {len(children)} topics.**"]
    if summary is not None:
        summary.description = "\n".join(lines)
    run.status = "done"
    run.finished_at = _now()
    run.result_json = json.dumps({**payload, "total_artifacts": total_artifacts,
                                  "compiled_at": _now().isoformat()})
    db.commit()
    return {"manager_run_id": run.id, "status": "done",
            "summary_investigation_id": summary_id,
            "total_artifacts": total_artifacts, "children": children}


def run_timeline(db, run_id: int) -> dict[str, Any]:
    from .models import AgentRun, ManagerRun
    run = db.query(ManagerRun).filter(ManagerRun.id == run_id).first()
    if run is None:
        return {"ok": False, "reason": "no such manager run"}
    payload = json.loads(run.result_json or "{}")
    lanes = []
    for c in payload.get("children", []):
        runs = (db.query(AgentRun)
                .filter(AgentRun.investigation_id == c["investigation_id"])
                .order_by(AgentRun.id).all())
        lanes.append({"investigation_id": c["investigation_id"],
                      "label": c.get("label"),
                      "runs": [{"id": r.id, "status": r.status,
                                "trigger": r.trigger} for r in runs]})
    return {"ok": True, "manager_run_id": run.id, "status": run.status,
            "lanes": lanes,
            "summary_investigation_id": payload.get("summary_investigation_id")}
