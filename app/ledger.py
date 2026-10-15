"""The hash chain.

Every agent step, model call and human action in the app lands here as one
event, and every event commits to its predecessor. That is what makes the log
worth something: ``verify_chain`` re-derives the whole thing from the stored
rows and names the exact event where it breaks, so a tampered payload fails its
own run without implicating any other.

Design decisions worth keeping in mind when reading:

* **The hashed core is small and explicit.** ``CORE_FIELDS`` is the whole
  commitment. Anything that can be recomputed (``proof_json``) or that is an
  observation *about* the record rather than part of it (``trace``) lives
  outside it on purpose — folding either in would change every hash already on
  disk.
* **Auditing is fail-open.** A ledger outage degrades to unaudited work, never
  to a 500. Nothing here is allowed to raise into a caller that was doing
  useful work; failures land in ``LedgerAuditDrop`` instead.
* **Policy is data.** A ``Mandate`` is hashed at run creation, so the rules in
  force when an action happened are provable afterwards rather than inferred
  from whatever the code says today.
"""
import contextlib
import contextvars
import hashlib
import json
import os
import re
import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from .database import SessionLocal
from .ledger_models import (
    LedgerApproval,
    LedgerAuditDrop,
    LedgerClaim,
    LedgerEvent,
    LedgerRun,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
GENESIS = "0" * 64

CORE_FIELDS = ("run_id", "seq", "ts", "actor_type", "actor", "kind", "intent",
               "verdict", "severity", "data_json")

ACTOR_TYPES = frozenset({"agent", "llm", "mcp", "human", "system"})

# Kinds the app itself emits. A kind outside this set is still recorded — the
# chain is not a whitelist, it is an append-only log — but it is flagged in
# ``verify_chain`` so a typo'd kind shows up as a finding rather than passing.
INTERNAL_KINDS = frozenset({
    "run.start", "run.end", "run.anchor", "run.resume",
    "agent.step", "agent.hop", "planner.query", "search.query", "search.result",
    "analyze.batch", "map.edge", "refine.query",
    "llm.call", "mcp.call",
    "claim.emit", "claim.verify", "grounding.check",
    "human.action", "approval.request", "approval.decision",
    "gate.check", "gate.decision", "policy.check",
    "gap.open", "gap.close", "artifact.review", "explainer.answer",
    "session.start", "session.end", "drift.detected",
})

VALID_VERDICTS = frozenset({"pass", "allow", "deny", "flag", "hold", "block",
                            "grant", "supported", "uncertain", "unsupported"})
VALID_SEVERITIES = frozenset({"info", "warn", "block"})
CLAIM_VERDICTS = frozenset({"supported", "unsupported", "uncertain", "unverified"})

# Prompts and completions are hashed, not stored, unless this is explicitly
# turned on. The default is the safe one.
CAPTURE_PAYLOADS = os.environ.get("LEDGER_CAPTURE_PAYLOADS", "").lower() in (
    "1", "true", "yes")

_SECRET_KEY = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|credential|authorization|"
    r"private[_-]?key|session[_-]?key|bearer|cookie)", re.I)
_SECRET_IF_STRING = re.compile(r"(^|[_-])(token|tokens|auth|bearer)([_-]|$)", re.I)
_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{12,}|"
    r"eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})")

_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(UTC).isoformat()


def canon(obj: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace, UTF-8 preserved.

    Sorted keys matter — two machines must produce the same bytes for the same
    logical event or the chain diverges for a reason that has nothing to do
    with the data.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def digest(obj: Any) -> str:
    return hashlib.sha256(canon(obj).encode("utf-8")).hexdigest()


def text_digest(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and re.fullmatch(
        r"[0-9a-f]{64}", value) is not None


def _redact(value: Any) -> Any:
    """Strip anything credential-shaped, recursively.

    Redaction happens *before* anything reaches immutable storage: a hash chain
    is append-only, so a secret that lands in one can never be taken back out.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY.search(k):
                out[k] = "[redacted]"
            elif isinstance(k, str) and _SECRET_IF_STRING.search(k) and isinstance(
                    v, str):
                out[k] = "[redacted]"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, tuple):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[redacted]", value)
    return value


def _payload(value: Any) -> Any:
    """What actually goes into the hashed core for a payload argument."""
    if CAPTURE_PAYLOADS:
        return _redact(value)
    # Default: hashes only. Enough to prove two events carried the same
    # content; not enough to read the content back out of the chain.
    if isinstance(value, str):
        return {"sha256": text_digest(value), "bytes": len(value)}
    return {"sha256": digest(value), "type": type(value).__name__}


# Keys whose value is raw model or document content. The *name* is the signal,
# not the length: a key called "prompt" is raw content whether it is 20
# characters or 20KB. Callers that want a short readable label under one of
# these names should pick a different key -- there are plenty, and the cost of
# guessing wrong here is a conversation stored forever in an append-only chain.
# "text" and "output" are deliberately excluded: they are used for short
# human-readable audit lines all over the app.
_CONTENT_KEYS = frozenset({
    "prompt", "completion", "answer", "content", "body", "raw", "transcript",
    "prompt_text", "output_text", "raw_html", "page_text",
})


def _safe_payload(value: Any) -> Any:
    """Redact secrets, and digest anything that looks like raw content.

    Two independent protections on purpose. Redaction stops a credential being
    stored; content hashing stops a conversation being stored. They catch
    different mistakes and neither subsumes the other.
    """
    if CAPTURE_PAYLOADS:
        return _redact(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _CONTENT_KEYS \
                    and isinstance(v, str):
                out[k] = _payload(v)
            else:
                out[k] = _safe_payload(v)
        return out
    if isinstance(value, list):
        return [_safe_payload(v) for v in value]
    return _redact(value)


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return canon(value)


def _loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def event_core(*, run_id: str, seq: int, ts: str, actor_type: str, actor: str,
               kind: str, intent: str | None, verdict: str | None,
               severity: str | None, data_json: str, prev_hash: str) -> dict:
    """The exact dict that gets hashed. Nothing else contributes to the hash."""
    return {
        "run_id": run_id, "seq": seq, "ts": ts, "actor_type": actor_type,
        "actor": actor, "kind": kind, "intent": intent, "verdict": verdict,
        "severity": severity, "data_json": data_json, "prev_hash": prev_hash,
    }


def chain_hash(core: dict) -> str:
    return digest(core)


# --------------------------------------------------------------------------
# Verdict / Mandate
# --------------------------------------------------------------------------
class Verdict:
    """A gate outcome. Carries the reason on the event so a refusal can never
    be scrolled past as a silent omission."""

    def __init__(self, decision: str = "allow", reason: str = "",
                 severity: str = "info"):
        self.decision = decision
        self.reason = reason
        self.severity = severity

    def __repr__(self):
        return f"Verdict({self.decision!r}, {self.reason!r})"


class Mandate:
    """The policy in force for a run.

    Four kinds of rule, deliberately different in force:

    * ``allowed_actors`` / ``allowed_intents`` / ``allowed_tools`` /
      ``allowed_domains`` — **hard boundary**. A violation is a denial.
    * ``planned_intents`` — **soft shape**. Using an undeclared intent is a
      drift flag, not a refusal: a run that discovers a new step is fine, and a
      run that quietly stopped doing the work it declared is not.
    * ``max_events`` / ``max_llm_calls`` — budget.
    * ``human_gates`` — kinds held for sign-off.
    """

    __slots__ = ("objective", "allowed_actors", "allowed_intents",
                 "planned_intents", "allowed_tools", "allowed_domains",
                 "max_events", "max_llm_calls", "human_gates")

    def __init__(self, objective: str = "", *, allowed_actors=None,
                 allowed_intents=None, planned_intents=None, allowed_tools=None,
                 allowed_domains=None, max_events=None, max_llm_calls=None,
                 human_gates=None):
        self.objective = objective
        self.allowed_actors = set(allowed_actors or ())
        self.allowed_intents = set(allowed_intents or ())
        self.planned_intents = set(planned_intents or ())
        self.allowed_tools = set(allowed_tools or ())
        self.allowed_domains = set(allowed_domains or ())
        self.max_events = max_events
        self.max_llm_calls = max_llm_calls
        self.human_gates = set(human_gates or ())

    def to_dict(self) -> dict:
        return {
            "objective": self.objective,
            "allowed_actors": sorted(self.allowed_actors),
            "allowed_intents": sorted(self.allowed_intents),
            "planned_intents": sorted(self.planned_intents),
            "allowed_tools": sorted(self.allowed_tools),
            "allowed_domains": sorted(self.allowed_domains),
            "max_events": self.max_events,
            "max_llm_calls": self.max_llm_calls,
            "human_gates": sorted(self.human_gates),
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "Mandate":
        d = d or {}
        return cls(
            objective=d.get("objective", ""),
            allowed_actors=d.get("allowed_actors"),
            allowed_intents=d.get("allowed_intents"),
            planned_intents=d.get("planned_intents"),
            allowed_tools=d.get("allowed_tools"),
            allowed_domains=d.get("allowed_domains"),
            max_events=d.get("max_events"),
            max_llm_calls=d.get("max_llm_calls"),
            human_gates=d.get("human_gates"),
        )

    def fingerprint(self) -> str:
        return digest(self.to_dict())


# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------
_current_run: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "econ_ledger_run", default=None)
_current_mandate: contextvars.ContextVar[Mandate | None] = contextvars.ContextVar(
    "econ_ledger_mandate", default=None)
_current_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "econ_ledger_session", default=None)
_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "econ_ledger_trace", default=None)


def current_run() -> str | None:
    return _current_run.get()


def current_mandate() -> Mandate | None:
    return _current_mandate.get()


def current_session() -> str | None:
    return _current_session.get()


def set_trace(trace: str | None):
    return _trace_id.set(trace)


def reset_trace(token) -> None:
    _trace_id.reset(token)


def current_trace() -> str | None:
    return _trace_id.get()


# --------------------------------------------------------------------------
# Drops — a failed audit write must leave a trace, not a stack trace
# --------------------------------------------------------------------------
def _record_drop(recorder: str, error: str, *, run_id: str | None = None,
                 actor: str | None = None, detail: Any = None) -> None:
    try:
        db = SessionLocal()
        try:
            db.add(LedgerAuditDrop(
                run_id=run_id or current_run(),
                recorder=recorder[:40],
                actor=(actor or "")[:120] or None,
                error=str(error)[:2000],
                detail_json=canon(_redact(detail))[:4000] if detail else None,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass  # even the drop table is unavailable; nothing more we can do


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------
def _check(run_id: str, mandate: Mandate | None, *, actor_type: str, actor: str,
           kind: str, intent: str | None, tool: str | None,
           domain: str | None) -> Verdict | None:
    """Evaluate the mandate. Returns ``None`` when there is nothing to say."""
    if mandate is None:
        return None

    if actor_type not in ACTOR_TYPES:
        return Verdict("deny", f"unknown actor_type '{actor_type}'", "block")

    if mandate.allowed_actors and actor_type not in mandate.allowed_actors:
        # A model call passes as the "llm" *role* even though it is named after
        # the client that made it, so the check is on the type, not the name.
        return Verdict("deny", f"actor_type '{actor_type}' not permitted",
                       "block")

    if intent and mandate.allowed_intents and intent not in mandate.allowed_intents:
        return Verdict("deny", f"intent '{intent}' outside allowed intents",
                       "block")

    if tool and mandate.allowed_tools and tool not in mandate.allowed_tools:
        return Verdict("deny", f"tool '{tool}' not permitted", "block")

    if domain and mandate.allowed_domains and domain not in mandate.allowed_domains:
        return Verdict("deny", f"domain '{domain}' not permitted", "block")

    # Soft: an intent nobody declared. Not a refusal — a flag.
    if (intent and mandate.planned_intents and intent not in mandate.planned_intents
            and not (mandate.allowed_intents and intent in mandate.allowed_intents)):
        return Verdict("flag", f"intent '{intent}' not in the planned shape", "warn")

    if mandate.max_events is not None or mandate.max_llm_calls is not None:
        counts = _counts(run_id=run_id)
        if (mandate.max_events is not None
                and counts.get("events", 0) >= mandate.max_events):
            return Verdict("deny", "event budget exhausted", "block")
        if (kind == "llm.call" and mandate.max_llm_calls is not None
                and counts.get("llm_calls", 0) >= mandate.max_llm_calls):
            return Verdict("deny", "model-call budget exhausted", "block")

    return None


def _counts(run_id: str) -> dict:
    db = SessionLocal()
    try:
        events = db.query(LedgerEvent).filter(LedgerEvent.run_id == run_id).all()
        return {
            "events": len(events),
            "llm_calls": sum(1 for e in events if e.kind == "llm.call"),
        }
    except Exception:
        return {}
    finally:
        db.close()


# --------------------------------------------------------------------------
# Run lifecycle
# --------------------------------------------------------------------------
def get_run(run_id: str, db=None):
    own = db is None
    db = db or SessionLocal()
    try:
        return db.query(LedgerRun).filter(LedgerRun.id == run_id).first()
    finally:
        if own:
            db.close()


def ensure_run(run_id: str, mandate: Mandate | None = None, *, label=None,
               kind: str = "task", session_id: str | None = None,
               client_key: str | None = None, db=None) -> LedgerRun:
    own = db is None
    db = db or SessionLocal()
    try:
        run = db.query(LedgerRun).filter(LedgerRun.id == run_id).first()
        if run is None:
            run = LedgerRun(id=run_id, kind=kind, session_id=session_id,
                            client_key=client_key,
                            mandate_json=_dumps(mandate.to_dict()) if mandate else None,
                            mandate_hash=mandate.fingerprint() if mandate else None,
                            genesis_hash=GENESIS, head_hash=GENESIS, head_seq=-1,
                            label=label, last_seen_at=_parse_ts(_now()))
            db.add(run)
            db.commit()
        else:
            if label and not run.label:
                run.label = label
            if mandate is not None and run.mandate_hash is None:
                run.mandate_json = _dumps(mandate.to_dict())
                run.mandate_hash = mandate.fingerprint()
            db.commit()
        return run
    finally:
        if own:
            db.close()


def _resolve_mandate(run_id: str, db) -> Mandate | None:
    run = db.query(LedgerRun).filter(LedgerRun.id == run_id).first()
    if run is None or not run.mandate_json:
        return None
    return Mandate.from_dict(_loads(run.mandate_json, {}))


def _parse_ts(ts: str | None):
    try:
        return datetime.fromisoformat(ts) if ts else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# append
# --------------------------------------------------------------------------
def append(run_id: str, kind: str, actor: str, *, actor_type: str = "system",
           intent: str | None = None, verdict: str | None = None,
           severity: str | None = None, data: Any = None, input_refs=None,
           mandate: Mandate | None = None, db=None) -> dict | None:
    """Append one event to a run's chain. Returns the stored row as a dict.

    Never raises: this is called from inside the research loop, and a failed
    audit write must not take down the work it was recording.
    """
    own = db is None
    db = db or SessionLocal()
    try:
        if verdict is not None and verdict not in VALID_VERDICTS:
            verdict = None
        if severity is not None and severity not in VALID_SEVERITIES:
            severity = None

        policy = _check(run_id, mandate, actor_type=actor_type, actor=actor,
                        kind=kind, intent=intent, tool=None, domain=None)
        if policy is not None:
            # Every refusal also emits a gate.check event, so a violation is
            # impossible to scroll past.
            if policy.decision == "deny":
                _append_one(db, run_id, "gate.check", actor, actor_type=actor_type,
                            intent=intent, verdict="deny", severity=policy.severity,
                            data={"reason": policy.reason, "kind": kind,
                                  "objective": (mandate.objective if mandate else "")})
                return None
            # flag/hold do not block; they are recorded as the event's own
            # verdict so the chain carries the warning with the action.

        row = _append_one(
            db, run_id, kind, actor, actor_type=actor_type, intent=intent,
            verdict=verdict or (policy.decision if policy else None),
            severity=severity or (policy.severity if policy else None),
            data=data, input_refs=input_refs)

        if policy is not None and policy.decision != "allow":
            _append_one(db, run_id, "gate.check", actor, actor_type="system",
                        intent=intent, verdict=policy.decision,
                        severity=policy.severity,
                        data={"reason": policy.reason, "kind": kind})

        if mandate is not None and mandate.human_gates and kind in mandate.human_gates:
            row = _append_one(db, run_id, "approval.request", actor,
                              actor_type="system", intent=intent,
                              verdict="hold", severity="warn",
                              data={"kind": kind, "actor": actor,
                                    "summary": policy.reason if policy else
                                    "held for sign-off"},
                              input_refs=[row["hash"]] if row else None)
        return row
    except Exception as e:
        _record_drop("append", e, run_id=run_id, actor=actor,
                     detail={"kind": kind, "intent": intent})
        return None
    finally:
        if own:
            db.close()


def _append_one(db, run_id: str, kind: str, actor: str, *, actor_type: str,
                intent: str | None, verdict: str | None, severity: str | None,
                data: Any, input_refs=None) -> dict | None:
    run = db.query(LedgerRun).filter(LedgerRun.id == run_id).first()
    if run is None:
        run = LedgerRun(id=run_id, genesis_hash=GENESIS, head_hash=GENESIS,
                        head_seq=-1)
        db.add(run)
        db.flush()

    seq = (run.head_seq if run.head_seq is not None else -1) + 1
    prev_hash = run.head_hash or GENESIS
    ts = _now()
    data_json = canon(_safe_payload(data) if data is not None else {})
    core = event_core(run_id=run_id, seq=seq, ts=ts, actor_type=actor_type,
                      actor=actor, kind=kind, intent=intent, verdict=verdict,
                      severity=severity, data_json=data_json, prev_hash=prev_hash)
    h = chain_hash(core)

    ev = LedgerEvent(
        run_id=run_id, seq=seq, ts=ts, actor_type=actor_type, actor=actor,
        kind=kind, intent=intent, verdict=verdict, severity=severity,
        data_json=data_json,
        input_refs=_dumps(list(input_refs)) if input_refs else None,
        prev_hash=prev_hash, hash=h, trace=current_trace())
    db.add(ev)

    run.head_seq = seq
    run.head_hash = h
    run.genesis_hash = run.genesis_hash or GENESIS
    run.last_seen_at = _parse_ts(ts)
    db.commit()

    ev.proof_json = _dumps(build_proof(run_id=run_id, seq=seq, event_hash=h,
                                       prev_hash=prev_hash, kind=kind,
                                       actor=actor, ts=ts))
    db.commit()

    return {
        "run_id": run_id, "seq": seq, "ts": ts, "actor_type": actor_type,
        "actor": actor, "kind": kind, "intent": intent, "verdict": verdict,
        "severity": severity, "data": _loads(data_json, {}),
        "prev_hash": prev_hash, "hash": h, "trace": ev.trace,
    }


# --------------------------------------------------------------------------
# Proofs
# --------------------------------------------------------------------------
def build_proof(*, run_id: str, seq: int, event_hash: str, prev_hash: str,
                kind: str, actor: str, ts: str, claim: str | None = None,
                inputs: Iterable[str] | None = None) -> dict:
    """A re-derivable artifact attached to an event.

    Nothing here is trusted on read: ``proof_checks`` recomputes every field
    from the stored event and reports drift.
    """
    body = {
        "run_id": run_id, "seq": seq, "event_hash": event_hash,
        "prev_hash": prev_hash, "kind": kind, "actor": actor, "ts": ts,
        "ts_day": (ts or "")[:10],
    }
    if claim:
        body["claim_sha256"] = text_digest(claim)
    if inputs:
        body["input_refs"] = sorted(inputs)
    body["proof_hash"] = digest(body)
    return body


def attach_proof(run_id: str, seq: int, proof: dict) -> None:
    db = SessionLocal()
    try:
        ev = db.query(LedgerEvent).filter(
            LedgerEvent.run_id == run_id, LedgerEvent.seq == seq).first()
        if ev is not None:
            ev.proof_json = _dumps(proof)
            db.commit()
    except Exception:
        pass
    finally:
        db.close()


def attach_event_proof(run_id: str, event_hash: str, *, claim: str | None = None,
                       inputs: Iterable[str] | None = None) -> dict | None:
    """(Re)build and attach the proof for an event, by hash."""
    db = SessionLocal()
    try:
        ev = db.query(LedgerEvent).filter(
            LedgerEvent.run_id == run_id, LedgerEvent.hash == event_hash).first()
        if ev is None:
            return None
        proof = build_proof(run_id=run_id, seq=ev.seq, event_hash=ev.hash,
                            prev_hash=ev.prev_hash, kind=ev.kind, actor=ev.actor,
                            ts=ev.ts, claim=claim, inputs=inputs)
        ev.proof_json = _dumps(proof)
        db.commit()
        return proof
    except Exception:
        return None
    finally:
        db.close()


def proof_checks(proof: dict, *, data: dict, event_hash: str,
                 prev_hash: str) -> dict:
    """Recompute a stored proof. Returns named checks, not a boolean."""
    checks = []

    def _add(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    # The body excludes ``proof_hash`` (that is what it commits to), so the
    # stored value has to be read from the proof itself, not from ``body``.
    body = {k: v for k, v in (proof or {}).items() if k != "proof_hash"}
    _add("proof_hash", proof.get("proof_hash") == digest(body),
         "recomputed proof hash")
    _add("event_hash", proof.get("event_hash") == event_hash,
         "proof binds this event")
    _add("prev_hash", proof.get("prev_hash") == prev_hash,
         "proof binds this predecessor")

    claim_sha = proof.get("claim_sha256")
    if claim_sha:
        text = (data or {}).get("claim") or (data or {}).get("text")
        if isinstance(text, str):
            _add("claim", claim_sha == text_digest(text), "claim digest matches")

    refs = proof.get("input_refs")
    if refs:
        _add("input_refs", all(is_hash(r) for r in refs),
             f"{len(refs)} lineage reference(s)")

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "proof_hash": (proof or {}).get("proof_hash"),
    }


def verify_proof(run_id: str, seq: int, db=None) -> dict:
    own = db is None
    db = db or SessionLocal()
    try:
        ev = db.query(LedgerEvent).filter(
            LedgerEvent.run_id == run_id, LedgerEvent.seq == seq).first()
        if ev is None:
            return {"ok": False, "reason": "no such event", "checks": []}
        proof = _loads(ev.proof_json, {}) or {}
        return proof_checks(proof, data=_loads(ev.data_json, {}) or {},
                            event_hash=ev.hash, prev_hash=ev.prev_hash)
    finally:
        if own:
            db.close()


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------
def verify_events(events: list, *, extra_refs: Iterable[str] | None = None,
                  refs: set | None = None) -> dict:
    """Walk a list of events in order and re-derive the chain.

    Reports the *exact* break rather than a boolean, and stops there: once a
    link is broken nothing after it can be trusted, so continuing would report
    a cascade of failures that are really one.
    """
    refs = set(refs or ())
    if extra_refs:
        refs |= set(extra_refs)
    problems = []
    prev = GENESIS
    expected_seq = 0
    for ev in events:
        core = event_core(
            run_id=ev.run_id, seq=ev.seq, ts=ev.ts, actor_type=ev.actor_type,
            actor=ev.actor, kind=ev.kind, intent=ev.intent,
            verdict=ev.verdict, severity=ev.severity,
            data_json=ev.data_json, prev_hash=ev.prev_hash)
        recomputed = chain_hash(core)

        if ev.seq != expected_seq:
            problems.append({"seq": ev.seq, "problem": "seq_gap",
                             "detail": f"expected seq {expected_seq}"})
            return {"ok": False, "events": len(events), "problems": problems}
        if ev.prev_hash != prev:
            problems.append({"seq": ev.seq, "problem": "broken_link",
                             "detail": "prev_hash does not match the previous event"})
            return {"ok": False, "events": len(events), "problems": problems}
        if ev.hash != recomputed:
            problems.append({"seq": ev.seq, "problem": "hash_mismatch",
                             "detail": "stored hash differs from the recomputed core"})
            return {"ok": False, "events": len(events), "problems": problems}

        for ref in _loads(ev.input_refs, []) or []:
            if not is_hash(ref):
                problems.append({"seq": ev.seq, "problem": "bad_ref",
                                 "detail": f"input_ref {ref!r} is not a sha256"})
                return {"ok": False, "events": len(events), "problems": problems}
            if refs and ref not in refs:
                problems.append({"seq": ev.seq, "problem": "unresolved_ref",
                                 "detail": f"input_ref {ref[:12]}… is not in the run"})
                return {"ok": False, "events": len(events), "problems": problems}

        prev = ev.hash
        expected_seq += 1
    return {"ok": not problems, "events": len(events), "problems": problems,
            "head_hash": prev}


def verify_chain(run_id: str, db=None) -> dict:
    """Full verification of one run, plus the checks a chain alone cannot make."""
    own = db is None
    db = db or SessionLocal()
    try:
        run = db.query(LedgerRun).filter(LedgerRun.id == run_id).first()
        if run is None:
            return {"ok": False, "run_id": run_id, "events": 0,
                    "problems": [{"problem": "no_such_run"}]}
        events = (db.query(LedgerEvent).filter(LedgerEvent.run_id == run_id)
                  .order_by(LedgerEvent.seq).all())
        claim_hashes = {c.claim_hash for c in db.query(LedgerClaim).filter(
            LedgerClaim.run_id == run_id).all()}
        result = verify_events(events, refs=claim_hashes)
        result["run_id"] = run_id
        result["head_hash"] = run.head_hash
        result["kind"] = run.kind
        result["mandate_hash"] = run.mandate_hash

        # The recorded head must agree with the last event actually stored —
        # otherwise a deletion at the tail would still verify.
        if events and run.head_hash != events[-1].hash:
            result.setdefault("problems", []).append({
                "problem": "head_mismatch",
                "detail": "stored head_hash is not the last event"})
            result["ok"] = False
        if not events and run.head_hash not in (None, GENESIS):
            result.setdefault("problems", []).append({
                "problem": "head_orphaned",
                "detail": "run claims events but none are stored"})
            result["ok"] = False

        # Proof drift is reported, not fatal: a proof is derived state, so a
        # missing one degrades the audit story without breaking the chain.
        proof_drift = 0
        for ev in events:
            proof = _loads(ev.proof_json, None)
            if not proof:
                proof_drift += 1
                continue
            chk = proof_checks(proof, data=_loads(ev.data_json, {}) or {},
                               event_hash=ev.hash, prev_hash=ev.prev_hash)
            if not chk["ok"]:
                proof_drift += 1
        result["proof_drift"] = proof_drift
        result["unknown_kinds"] = sorted({
            e.kind for e in events if e.kind not in INTERNAL_KINDS})
        return result
    finally:
        if own:
            db.close()


def monitor_integrity(limit: int = 50, db=None) -> dict:
    """Re-verify every recent chain, plus the drop table."""
    own = db is None
    db = db or SessionLocal()
    try:
        runs = (db.query(LedgerRun).order_by(LedgerRun.created_at.desc())
                .limit(limit).all())
        results = []
        for r in runs:
            if r.kind == "session":
                continue
            v = verify_chain(r.id, db=db)
            results.append({"run_id": r.id, "label": r.label, "ok": v.get("ok"),
                            "events": v.get("events", 0),
                            "problems": v.get("problems", [])})
        drops = db.query(LedgerAuditDrop).count()
        return {
            "checked": len(results),
            "healthy": sum(1 for r in results if r["ok"]),
            "broken": [r for r in results if not r["ok"]],
            "audit_drops": drops,
            "verdicts": results,
        }
    finally:
        if own:
            db.close()


# --------------------------------------------------------------------------
# Claims
# --------------------------------------------------------------------------
def claim_hash(text: str, citations: list | None = None) -> str:
    """Pure function of the claim's own content, so two agents asserting the
    same thing collide by design."""
    return digest({
        "text": (text or "").strip(),
        "citations": sorted(
            ((c or {}).get("source", ""), (c or {}).get("quote", "").strip()[:200])
            for c in (citations or [])),
    })


def emit_claim(run_id: str, text: str, citations: list | None = None, *,
               actor: str = "agent", confidence: str = "medium",
               actor_type: str = "agent", db=None) -> dict | None:
    own = db is None
    db = db or SessionLocal()
    try:
        ch = claim_hash(text, citations)
        n_src = len({(c or {}).get("source") for c in (citations or [])
                     if (c or {}).get("source")})
        row = LedgerClaim(run_id=run_id, claim_hash=ch, actor=actor[:120],
                          text=text, citations_json=_dumps(citations or []),
                          n_sources=n_src, confidence=confidence, verdict="unverified")
        db.add(row)
        db.flush()
        ev = _append_one(db, run_id, "claim.emit", actor, actor_type=actor_type,
                         intent="claim.emit", verdict=None, severity=None,
                         data={"claim_hash": ch, "n_sources": n_src,
                               "confidence": confidence},
                         input_refs=[ch])
        if ev:
            row.seq = ev["seq"]
        db.commit()
        return {"claim_hash": ch, "seq": row.seq, "n_sources": n_src}
    except Exception as e:
        _record_drop("emit_claim", e, run_id=run_id, actor=actor)
        return None
    finally:
        if own:
            db.close()


def verify_claim(run_id: str, claim_hash_value: str, verdict: str, *,
                 verifier: str = "reviewer", detail: Any = None,
                 actor: str = "reviewer", db=None) -> dict:
    """Record a grounding verdict on a claim, as a chained event."""
    own = db is None
    db = db or SessionLocal()
    try:
        if verdict not in CLAIM_VERDICTS:
            verdict = "unverified"
        row = db.query(LedgerClaim).filter(
            LedgerClaim.run_id == run_id,
            LedgerClaim.claim_hash == claim_hash_value).first()
        if row is None:
            return {"ok": False, "reason": "no such claim"}
        row.verdict = verdict
        row.verifier = verifier[:40]
        ev = _append_one(db, run_id, "claim.verify", actor, actor_type="human",
                         intent="claim.verify", verdict=verdict,
                         severity="warn" if verdict in ("unsupported",
                                                       "unverified") else None,
                         data={"claim_hash": claim_hash_value, "verifier": verifier,
                               "detail": detail},
                         input_refs=[claim_hash_value])
        db.commit()
        return {"ok": True, "verdict": verdict, "event_hash":
                ev["hash"] if ev else None}
    finally:
        if own:
            db.close()


def claim_text(run_id: str, claim_hash_value: str) -> str | None:
    db = SessionLocal()
    try:
        row = db.query(LedgerClaim).filter(
            LedgerClaim.run_id == run_id,
            LedgerClaim.claim_hash == claim_hash_value).first()
        return row.text if row else None
    finally:
        db.close()


def contamination(run_id: str, ref: str, *, max_depth: int = 12) -> dict:
    """Blast radius: what consumed this hash, transitively.

    Answers "if this claim was wrong, what did it reach?" across every event
    that cited it as an input, to a bounded depth.
    """
    db = SessionLocal()
    try:
        seen = {ref}
        frontier = [ref]
        edges = []
        depth = 0
        while frontier and depth < max_depth:
            nxt = []
            for r in frontier:
                events = db.query(LedgerEvent).filter(
                    LedgerEvent.run_id == run_id).all()
                for ev in events:
                    refs_in = _loads(ev.input_refs, []) or []
                    if r in refs_in and ev.hash not in seen:
                        seen.add(ev.hash)
                        edges.append({"hash": ev.hash, "seq": ev.seq,
                                      "kind": ev.kind, "actor": ev.actor,
                                      "ts": ev.ts, "depth": depth + 1})
                        nxt.append(ev.hash)
            frontier = nxt
            depth += 1
        return {"root": ref, "reached": len(edges), "edges": edges,
                "truncated": bool(frontier)}
    finally:
        db.close()


# --------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------
def request_approval(run_id: str, kind: str, subject_hash: str, *,
                     summary: str = "", requested_by: str = "agent",
                     db=None) -> dict | None:
    own = db is None
    db = db or SessionLocal()
    try:
        ev = _append_one(db, run_id, "approval.request", requested_by,
                         actor_type="system", intent="approval.request",
                         verdict="hold", severity="warn",
                         data={"kind": kind, "summary": summary,
                               "subject_hash": subject_hash},
                         input_refs=[subject_hash] if is_hash(subject_hash) else None)
        ap = LedgerApproval(run_id=run_id, seq=ev["seq"] if ev else None,
                            kind=kind, subject_hash=subject_hash,
                            summary=summary, requested_by=requested_by)
        db.add(ap)
        db.commit()
        return {"approval_id": ap.id, "event_hash": ev["hash"] if ev else None}
    finally:
        if own:
            db.close()


def decide_approval(approval_id: int, decision: str, *, decided_by: str,
                    note: str = "", db=None) -> dict:
    own = db is None
    db = db or SessionLocal()
    try:
        ap = db.query(LedgerApproval).filter(
            LedgerApproval.id == approval_id).first()
        if ap is None:
            return {"ok": False, "reason": "no such approval"}
        ap.decision = "grant" if decision in ("allow", "grant", "accept") else "deny"
        ap.decided_by = decided_by[:120]
        ap.decided_at = datetime.now(UTC)
        ap.note = note
        ev = _append_one(db, ap.run_id, "approval.decision", decided_by,
                         actor_type="human", intent="approval.decision",
                         verdict=ap.decision,
                         severity="warn" if ap.decision == "deny" else None,
                         data={"approval_id": approval_id, "note": note,
                               "subject_hash": ap.subject_hash},
                         input_refs=[ap.subject_hash] if is_hash(
                             ap.subject_hash) else None)
        ap.event_hash = ev["hash"] if ev else None
        db.commit()
        return {"ok": True, "decision": ap.decision, "event_hash": ap.event_hash}
    finally:
        if own:
            db.close()


def pending_approval(run_id: str, subject_hash: str):
    db = SessionLocal()
    try:
        return db.query(LedgerApproval).filter(
            LedgerApproval.run_id == run_id,
            LedgerApproval.subject_hash == subject_hash,
            LedgerApproval.decision.is_(None)).first()
    finally:
        db.close()


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
SESSION_IDLE_SECONDS = int(os.environ.get("ECON_SESSION_IDLE_S", "1800"))


def ensure_session(session_id: str, *, client_key: str | None = None,
                   label: str | None = None, db=None):
    own = db is None
    db = db or SessionLocal()
    try:
        run = db.query(LedgerRun).filter(LedgerRun.id == session_id).first()
        if run is None:
            run = LedgerRun(id=session_id, kind="session", client_key=client_key,
                            label=label, genesis_hash=GENESIS, head_hash=GENESIS,
                            head_seq=-1, last_seen_at=datetime.now(UTC))
            db.add(run)
            db.commit()
        else:
            run.last_seen_at = datetime.now(UTC)
            if client_key and not run.client_key:
                run.client_key = client_key
            db.commit()
        return run
    finally:
        if own:
            db.close()


def resolve_session(client_key: str | None, *, label: str | None = None,
                    db=None) -> str | None:
    """Resume the caller's session, or start one.

    A client key that has been idle past the cutoff starts a *new* sitting: the
    old chain stays intact and verifiable, and the new work anchors to a fresh
    spine rather than to a chain that has been quiet for an hour.
    """
    if not client_key:
        return None
    own = db is None
    db = db or SessionLocal()
    try:
        cutoff = datetime.now(UTC) - timedelta(seconds=SESSION_IDLE_SECONDS)
        run = (db.query(LedgerRun)
               .filter(LedgerRun.kind == "session",
                       LedgerRun.client_key == client_key)
               .order_by(LedgerRun.created_at.desc()).first())
        if run is not None:
            last = run.last_seen_at or run.created_at
            if last is not None and last.replace(tzinfo=UTC) >= cutoff:
                run.last_seen_at = datetime.now(UTC)
                db.commit()
                return run.id
        sid = "ses-" + text_digest(f"{client_key}:{datetime.now(UTC).isoformat()}")[:16]
        ensure_session(sid, client_key=client_key, label=label, db=db)
        return sid
    finally:
        if own:
            db.close()


def anchor_run(run_id: str, *, session_id: str | None = None, label: str | None = None,
               mandate: Mandate | None = None, db=None) -> dict | None:
    """Attach a task to a session spine, in order.

    This is what makes cross-run order provable: the session chain proves the
    order tasks were adopted in, while each task chain still verifies alone.
    """
    own = db is None
    db = db or SessionLocal()
    try:
        ensure_run(run_id, mandate, label=label, db=db)
        if not session_id:
            return None
        ensure_session(session_id, db=db)
        return _append_one(db, session_id, "run.anchor", label or run_id,
                           actor_type="system", intent="run.anchor",
                           verdict="pass", severity=None,
                           data={"run_id": run_id, "label": label})
    finally:
        if own:
            db.close()


def list_sessions(limit: int = 50, db=None) -> list:
    own = db is None
    db = db or SessionLocal()
    try:
        runs = (db.query(LedgerRun).filter(LedgerRun.kind == "session")
                .order_by(LedgerRun.last_seen_at.desc().nullslast())
                .limit(limit).all())
        out = []
        for r in runs:
            tasks = db.query(LedgerEvent).filter(
                LedgerEvent.run_id == r.id, LedgerEvent.kind == "run.anchor").count()
            out.append({"session_id": r.id, "label": r.label,
                        "created_at": r.created_at, "last_seen_at": r.last_seen_at,
                        "events": r.head_seq + 1, "tasks": tasks,
                        "status": r.status})
        return out
    finally:
        if own:
            db.close()


def session_timeline(session_id: str, limit: int = 2000, db=None) -> dict:
    own = db is None
    db = db or SessionLocal()
    try:
        run = db.query(LedgerRun).filter(LedgerRun.id == session_id).first()
        if run is None:
            return {"ok": False, "reason": "no such session", "events": []}
        events = (db.query(LedgerEvent).filter(LedgerEvent.run_id == session_id)
                  .order_by(LedgerEvent.seq.desc()).limit(limit).all())
        return {"ok": True, "session_id": session_id,
                "verified": verify_chain(session_id, db=db),
                "events": [_event_dict(e) for e in events]}
    finally:
        if own:
            db.close()


def global_timeline(limit: int = 200, db=None) -> dict:
    """Every chain interleaved into one newest-first stream."""
    own = db is None
    db = db or SessionLocal()
    try:
        events = (db.query(LedgerEvent)
                  .order_by(LedgerEvent.ts.desc()).limit(limit).all())
        labels = {r.id: r.label for r in db.query(LedgerRun).all()}
        return {"ok": True, "events": [
            dict(_event_dict(e), run_label=labels.get(e.run_id)) for e in events]}
    finally:
        if own:
            db.close()


def verify_session(session_id: str, db=None) -> dict:
    return verify_chain(session_id, db=db)


def session_insights(session_id: str, db=None) -> dict:
    """Per-session health: verified?, what it touched, where it was refused."""
    own = db is None
    db = db or SessionLocal()
    try:
        run = db.query(LedgerRun).filter(LedgerRun.id == session_id).first()
        if run is None:
            return {"ok": False, "reason": "no such session"}
        events = (db.query(LedgerEvent).filter(LedgerEvent.run_id == session_id)
                  .order_by(LedgerEvent.seq).all())
        by_kind: dict[str, int] = {}
        by_actor: dict[str, int] = {}
        llm_calls = tokens_in = tokens_out = 0
        for e in events:
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
            by_actor[e.actor] = by_actor.get(e.actor, 0) + 1
            if e.kind == "llm.call":
                llm_calls += 1
                data = _loads(e.data_json, {}) or {}
                tokens_in += int(data.get("tokens_in") or 0)
                tokens_out += int(data.get("tokens_out") or 0)
        tasks = [_loads(e.data_json, {}) or {} for e in events
                 if e.kind == "run.anchor"]
        return {
            "ok": True, "session_id": session_id, "label": run.label,
            "created_at": run.created_at, "last_seen_at": run.last_seen_at,
            "events": len(events), "by_kind": by_kind, "by_actor": by_actor,
            "llm_calls": llm_calls, "tokens_in": tokens_in, "tokens_out": tokens_out,
            "tasks": [t.get("run_id") for t in tasks],
            "verified": verify_chain(session_id, db=db),
        }
    finally:
        if own:
            db.close()


def _event_dict(e: LedgerEvent) -> dict:
    return {
        "run_id": e.run_id, "seq": e.seq, "ts": e.ts,
        "actor_type": e.actor_type, "actor": e.actor, "kind": e.kind,
        "intent": e.intent, "verdict": e.verdict, "severity": e.severity,
        "data": _loads(e.data_json, {}), "input_refs": _loads(e.input_refs, []),
        "prev_hash": e.prev_hash, "hash": e.hash, "trace": e.trace,
        "has_proof": bool(e.proof_json),
    }


def timeline(run_id: str, *, kinds: list | None = None, limit: int = 2000,
             db=None) -> dict:
    own = db is None
    db = db or SessionLocal()
    try:
        run = db.query(LedgerRun).filter(LedgerRun.id == run_id).first()
        if run is None:
            return {"ok": False, "reason": "no such run", "events": []}
        q = db.query(LedgerEvent).filter(LedgerEvent.run_id == run_id)
        if kinds:
            q = q.filter(LedgerEvent.kind.in_(kinds))
        events = q.order_by(LedgerEvent.seq.desc()).limit(limit).all()
        return {"ok": True, "run_id": run_id, "label": run.label,
                "mandate": _loads(run.mandate_json, None),
                "verified": verify_chain(run_id, db=db),
                "events": [_event_dict(e) for e in events]}
    finally:
        if own:
            db.close()


def summary(run_id: str) -> dict:
    db = SessionLocal()
    try:
        run = db.query(LedgerRun).filter(LedgerRun.id == run_id).first()
        if run is None:
            return {"ok": False}
        events = db.query(LedgerEvent).filter(LedgerEvent.run_id == run_id).all()
        kinds: dict[str, int] = {}
        for e in events:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        claims = db.query(LedgerClaim).filter(LedgerClaim.run_id == run_id).all()
        by_verdict: dict[str, int] = {}
        for c in claims:
            by_verdict[c.verdict or "unverified"] = by_verdict.get(
                c.verdict or "unverified", 0) + 1
        return {
            "ok": True, "run_id": run_id, "label": run.label, "kind": run.kind,
            "status": run.status, "events": len(events), "kinds": kinds,
            "claims": len(claims), "claim_verdicts": by_verdict,
            "head_hash": run.head_hash,
            "verified": verify_chain(run_id, db=db),
        }
    finally:
        db.close()


def export_run(run_id: str) -> dict:
    """A self-verifying bundle: events, claims, mandate, and the verification
    result. Hand it to an auditor with no database attached."""
    db = SessionLocal()
    try:
        run = db.query(LedgerRun).filter(LedgerRun.id == run_id).first()
        if run is None:
            return {"ok": False, "reason": "no such run"}
        events = (db.query(LedgerEvent).filter(LedgerEvent.run_id == run_id)
                  .order_by(LedgerEvent.seq).all())
        claims = db.query(LedgerClaim).filter(LedgerClaim.run_id == run_id).all()
        doc = {
            "version": 1,
            "exported_at": _now(),
            "run": {"id": run.id, "label": run.label, "kind": run.kind,
                    "created_at": str(run.created_at), "status": run.status,
                    "mandate": _loads(run.mandate_json, None),
                    "mandate_hash": run.mandate_hash,
                    "genesis_hash": run.genesis_hash, "head_hash": run.head_hash},
            "events": [
                {"seq": e.seq, "ts": e.ts, "actor_type": e.actor_type,
                 "actor": e.actor, "kind": e.kind, "intent": e.intent,
                 "verdict": e.verdict, "severity": e.severity,
                 "data_json": e.data_json,
                 "input_refs": _loads(e.input_refs, []),
                 "prev_hash": e.prev_hash, "hash": e.hash}
                for e in events],
            "claims": [
                {"claim_hash": c.claim_hash, "seq": c.seq, "actor": c.actor,
                 "text": c.text, "citations": _loads(c.citations_json, []),
                 "n_sources": c.n_sources, "confidence": c.confidence,
                 "verdict": c.verdict, "verifier": c.verifier}
                for c in claims],
        }
        doc["verification"] = verify_chain(run_id, db=db)
        # The bundle hash commits to the *content* only: verification is
        # derived from that content and would otherwise be a self-referential
        # field that changes the hash it is supposed to check.
        doc["bundle_hash"] = digest({k: v for k, v in doc.items()
                                     if k not in ("bundle_hash", "verification")})
        return doc
    finally:
        db.close()


def verify_export(doc: dict) -> dict:
    """Re-verify an exported bundle offline, with no database attached."""
    try:
        claim_hashes = {c["claim_hash"] for c in doc.get("claims", [])}

        class _Row:
            def __init__(self, d):
                self.run_id = doc["run"]["id"]
                self.seq = d["seq"]
                self.ts = d["ts"]
                self.actor_type = d["actor_type"]
                self.actor = d["actor"]
                self.kind = d["kind"]
                self.intent = d["intent"]
                self.verdict = d["verdict"]
                self.severity = d["severity"]
                self.data_json = d["data_json"]
                self.input_refs = _dumps(d.get("input_refs", []))
                self.prev_hash = d["prev_hash"]
                self.hash = d["hash"]

        rows = [_Row(d) for d in doc.get("events", [])]
        result = verify_events(rows, refs=claim_hashes)
        result["bundle_hash_ok"] = (
            digest({k: v for k, v in doc.items()
                    if k not in ("bundle_hash", "verification")})
            == doc.get("bundle_hash"))
        result["ok"] = result["ok"] and result["bundle_hash_ok"]
        return result
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# --------------------------------------------------------------------------
# Convenience recorders used across the app
# --------------------------------------------------------------------------
def record_agent_step(stage: str, message: str, data: dict | None = None,
                      *, actor: str = "agent") -> None:
    run_id = current_run()
    if not run_id:
        return
    append(run_id, "agent.step", actor, actor_type="agent", intent=stage,
           data={"stage": stage, "message": message[:2000], **(data or {})})


def record_llm_call(model: str, prompt_text: str, output_text: str, *,
                    latency_ms: int = 0, tokens_in: int = 0, tokens_out: int = 0,
                    gateway: dict | None = None, purpose: str = "") -> None:
    """The single choke point for every model call in the app.

    Stores digests, never content: enough for the timeline to show *that* a
    model was consulted on a step, and not enough to read the conversation out
    of an append-only chain.
    """
    run_id = current_run()
    if not run_id:
        return
    append(run_id, "llm.call", model, actor_type="llm", intent=purpose or None,
           data={
               "model": model,
               "prompt_sha256": text_digest(prompt_text),
               "output_sha256": text_digest(output_text),
               "prompt_bytes": len(prompt_text or ""),
               "output_bytes": len(output_text or ""),
               "latency_ms": latency_ms,
               "tokens_in": tokens_in, "tokens_out": tokens_out,
               "gateway": _redact(gateway or {}),
           })


def record_human_action(action: str, detail: Any = None, *,
                        actor: str = "operator") -> None:
    run_id = current_run()
    if not run_id:
        return
    append(run_id, "human.action", actor, actor_type="human", intent=action,
           data={"action": action, "detail": detail})


def emit(text: str, citations: list | None = None, *, actor: str = "agent",
         confidence: str = "medium") -> dict | None:
    """Emit a claim on the current run and return its hash."""
    run_id = current_run()
    if not run_id:
        return None
    return emit_claim(run_id, text, citations, actor=actor,
                      confidence=confidence)


# --------------------------------------------------------------------------
# Scopes
# --------------------------------------------------------------------------
class RunCtx:
    """Context manager that opens a ledger run for a block of work.

    Pins the run id, the mandate and the session for everything inside, so an
    LLM call in a research thread lands on the same chain as the agent step that
    caused it without either side having to pass ids around.
    """

    def __init__(self, run_id: str, mandate: Mandate | None = None, *,
                 label: str | None = None, session_id: str | None = None,
                 anchor: bool = True):
        self.run_id = run_id
        self.mandate = mandate
        self.label = label
        self.session_id = session_id or current_session()
        self.anchor = anchor
        self._toks: list = []

    def __enter__(self):
        self._toks = [
            _current_run.set(self.run_id),
            _current_mandate.set(self.mandate),
            _current_session.set(self.session_id),
        ]
        try:
            ensure_run(self.run_id, self.mandate, label=self.label)
            append(self.run_id, "run.start", self.label or self.run_id,
                   actor_type="system", intent="run.start", verdict="pass",
                   data={"mandate_hash": self.mandate.fingerprint()
                         if self.mandate else None,
                         "objective": self.mandate.objective if self.mandate else ""})
            if self.session_id and self.anchor:
                anchor_run(self.run_id, session_id=self.session_id,
                           label=self.label, mandate=self.mandate)
        except Exception:
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc is not None:
                append(self.run_id, "run.end", self.label or self.run_id,
                       actor_type="system", intent="run.end", verdict="deny",
                       severity="warn",
                       data={"error": str(exc)[:500]})
            else:
                append(self.run_id, "run.end", self.label or self.run_id,
                       actor_type="system", intent="run.end", verdict="pass")
        except Exception:
            pass
        for tok in reversed(self._toks):
            tok.var.reset(tok)
        return False

    # -- passthroughs so callers do not need a second import --
    def step(self, stage: str, message: str, data: dict | None = None) -> None:
        record_agent_step(stage, message, data)

    def llm(self, model: str, prompt_text: str, output_text: str, **kw) -> None:
        record_llm_call(model, prompt_text, output_text, **kw)

    def claim(self, text: str, citations: list | None = None, **kw):
        return emit(text, citations, **kw)


def scope(run_id: str, mandate: Mandate | None = None, *, label=None,
          session_id=None, anchor: bool = True) -> RunCtx:
    return RunCtx(run_id, mandate, label=label, session_id=session_id,
                  anchor=anchor)


@contextlib.contextmanager
def session(session_id: str, *, label: str | None = None):
    ensure_session(session_id, label=label)
    token = _current_session.set(session_id)
    try:
        yield session_id
    finally:
        _current_session.reset(token)


def run(run_id: str, mandate: Mandate | None = None, **kw) -> RunCtx:
    return scope(run_id, mandate, **kw)
