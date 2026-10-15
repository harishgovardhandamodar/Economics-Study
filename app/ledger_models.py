"""Tamper-evident audit ledger tables.

One append-only, hash-chained stream per run spans *every* actor: agents, LLM
calls, MCP tool invocations, and human actions. The chain is what makes the log
auditable: each row commits to its predecessor, so an edit, a deletion or an
insertion after the fact all break verification.

Kept out of ``app/models.py`` because the ledger has a different shape from the
domain models (no investigation FK, string-keyed run ids, hash columns). It is
still registered on the same ``Base`` metadata, so ``init_db`` creates it.
"""
from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .database import Base


def _now():
    return datetime.now(UTC)


class LedgerRun(Base):
    """One audited unit of work: a mandate plus its chained event stream.

    The run id is caller-supplied (not an autoincrement) so an external
    orchestrator can key the ledger by its own correlation id.

    Two kinds of row live here, which is what makes a *session* verifiable
    without inventing a second chain implementation:

    - ``kind='task'`` (the default) is one piece of work — an investigation
      run, an explanation. Its chain holds the actual events.
    - ``kind='session'`` is the spine for a sitting. Its chain does not hold
      the work; it holds one ``run.anchor`` event per task, in the order the
      tasks were adopted. So each task still verifies on its own, and the
      session chain independently proves the *order* the tasks happened in.
    """

    __tablename__ = "ledger_runs"

    id = Column(String(64), primary_key=True)
    label = Column(String(200), nullable=True)
    kind = Column(String(20), nullable=False, default="task")  # task|session
    session_id = Column(String(64), ForeignKey("ledger_runs.id",
                                               ondelete="SET NULL"),
                        nullable=True, index=True)
    # Browser-supplied key plus last activity: a session is "the same sitting",
    # so the same key resumes its session until it goes idle.
    client_key = Column(String(64), nullable=True, index=True)
    last_seen_at = Column(DateTime, nullable=True, index=True)
    status = Column(String(20), nullable=False, default="open")  # open|closed
    mandate_json = Column(Text, nullable=True)   # JSON: policy in force
    mandate_hash = Column(String(64), nullable=True)
    genesis_hash = Column(String(64), nullable=True)
    head_hash = Column(String(64), nullable=True)  # last event hash
    head_seq = Column(Integer, nullable=False, default=-1)
    created_at = Column(DateTime, default=_now)
    closed_at = Column(DateTime, nullable=True)

    events = relationship("LedgerEvent", back_populates="run",
                          cascade="all, delete-orphan")


class LedgerEvent(Base):
    """One hash-chained step in a run's timeline.

    ``hash`` covers the full immutable core below, so any mutation to
    ``data_json`` invalidates the chain. ``proof_json`` is deliberately *not*
    hashed: it is derived state, and verification recomputes it and reports
    drift rather than trusting the stored copy.
    """

    __tablename__ = "ledger_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_ledger_event_seq"),
    )

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), ForeignKey("ledger_runs.id",
                                            ondelete="CASCADE"),
                    nullable=False, index=True)
    seq = Column(Integer, nullable=False)          # 0-based chain position
    # ISO-8601 *string*, not DateTime: audit needs the exact authored timestamp
    # with offset preserved, and lexicographic order equals chronological order.
    ts = Column(String(40), nullable=False, index=True)
    actor_type = Column(String(20), nullable=False)  # agent|llm|mcp|human|system
    actor = Column(String(120), nullable=False, index=True)
    kind = Column(String(40), nullable=False, index=True)  # llm.call|agent.hop|...
    intent = Column(String(80), nullable=True, index=True)
    verdict = Column(String(20), nullable=True, index=True)  # pass|allow|deny|...
    severity = Column(String(10), nullable=True, index=True)  # info|warn|block
    data_json = Column(Text, nullable=False)        # canonical JSON core (hashed)
    input_refs = Column(Text, nullable=True)        # JSON list of sha256
    prev_hash = Column(String(64), nullable=False)
    hash = Column(String(64), nullable=False, index=True)
    proof_json = Column(Text, nullable=True)        # derived proof artifact
    # Which request produced this event. Deliberately outside the hashed core:
    # the trace says *when* and *under which request* the record was written,
    # not what the record claims — folding it in would change every event hash
    # and break verification of every run already on disk.
    trace = Column(String(48), nullable=True, index=True)

    run = relationship("LedgerRun", back_populates="events")


class LedgerClaim(Base):
    """A content-addressed assertion, so downstream trust is traceable.

    ``claim_hash`` is a pure function of the claim's own content (text +
    citations). Two agents asserting the same thing produce the same hash,
    which makes "did B simply parrot A?" answerable, and lets a reviewer pull
    the full blast radius of a bad claim from one hash.
    """

    __tablename__ = "ledger_claims"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), ForeignKey("ledger_runs.id",
                                            ondelete="CASCADE"),
                    nullable=False, index=True)
    claim_hash = Column(String(64), nullable=False, index=True)
    seq = Column(Integer, nullable=True)             # event seq that emitted it
    actor = Column(String(120), nullable=True)
    text = Column(Text, nullable=False)
    citations_json = Column(Text, nullable=True)     # JSON [{source, quote, ...}]
    n_sources = Column(Integer, nullable=False, default=0)
    confidence = Column(String(10), nullable=True)   # high|medium|low|none
    verdict = Column(String(20), nullable=True)      # supported|uncertain|...
    verifier = Column(String(40), nullable=True)     # verbatim|corroboration|...
    created_at = Column(DateTime, default=_now)


class LedgerAuditDrop(Base):
    """An audit record the ledger failed to write.

    The chain is the product's evidence that its agents did what they said. A
    write that fails is a hole in that evidence, and a hole that exists only as
    a line on stderr is a hole nobody reads. This table is where a dropped
    record goes instead: durable, countable, and joinable to the run it
    belonged to.

    Deliberately not part of the chain. It cannot be: a row here usually exists
    *because* the chain write failed. It is the place to look when
    ``verify_chain`` reports a gap.
    """

    __tablename__ = "ledger_audit_drops"

    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime, default=_now, index=True)
    run_id = Column(String(64), nullable=True, index=True)
    recorder = Column(String(40), nullable=True, index=True)
    actor = Column(String(120), nullable=True)
    error = Column(Text, nullable=True)
    detail_json = Column(Text, nullable=True)   # redacted arg summary


class LedgerApproval(Base):
    """A human decision point. The decision is itself a chained event, so an
    approval cannot be forged into the record after the fact."""

    __tablename__ = "ledger_approvals"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), ForeignKey("ledger_runs.id",
                                            ondelete="CASCADE"),
                    nullable=False, index=True)
    seq = Column(Integer, nullable=True)             # seq of approval.request
    kind = Column(String(40), nullable=False, index=True)
    subject_hash = Column(String(64), nullable=True, index=True)
    summary = Column(Text, nullable=True)
    requested_by = Column(String(120), nullable=True)
    requested_at = Column(DateTime, default=_now)
    decision = Column(String(10), nullable=True)     # grant|deny (null = pending)
    decided_by = Column(String(120), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)
    event_hash = Column(String(64), nullable=True)
