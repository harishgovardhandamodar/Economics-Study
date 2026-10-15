"""Domain tables.

The core spine of the mapper: an investigation collects artifacts, the agent
relates them, a reviewer triages them, and the explainer answers questions over
whatever survived. Everything else in the app reads from these rows.
"""
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
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


class Investigation(Base):
    """One research question the app is answering over time.

    A brief (title, keywords, description, sources) plus everything needed to
    re-run collection: the schedule, the per-run budgets, and the optional
    *driver* — the economic event the investigation is tracking.
    """

    __tablename__ = "investigations"

    id = Column(Integer, primary_key=True)
    title = Column(String(300), nullable=False)
    keywords = Column(String(1000), default="")
    description = Column(Text, default="")
    sources = Column(String(200), default="rss,arxiv,web")
    preferred_domains = Column(String(500), default="")
    # The economics driver class, when this investigation tracks an event
    # rather than a topic (see domains.DRIVERS).
    driver = Column(String(40), nullable=True)
    # A free-text shock statement for a driver-based investigation.
    shock = Column(Text, nullable=True)
    status = Column(String(20), default="idle")  # idle|running|ready
    created_at = Column(DateTime, default=_now)
    # --- scheduling ---
    schedule_enabled = Column(Boolean, default=False)
    schedule_cron = Column(String(100), nullable=True)
    schedule_max_items = Column(Integer, default=25)
    schedule_max_rounds = Column(Integer, default=2)
    last_scheduled_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)
    # --- preferences ---
    auto_save_explanations = Column(Boolean, default=True)
    hidden = Column(Boolean, default=False)

    artifacts = relationship("Artifact", back_populates="investigation",
                             cascade="all, delete-orphan")
    runs = relationship("AgentRun", back_populates="investigation",
                        cascade="all, delete-orphan")

    @property
    def domain_list(self) -> list[str]:
        from .domains import normalize_domains
        return normalize_domains(self.preferred_domains)


class Artifact(Base):
    """One collected item of evidence.

    ``domain`` is the single taxonomy label that makes cross-domain mapping
    possible; ``entities_json`` holds the named actors (labs, central banks,
    countries, protocols) the analyst pulled out, which is what later lets a
    relationship be drawn between two documents that share no keywords.
    """

    __tablename__ = "artifacts"

    id = Column(Integer, primary_key=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id",
                                                  ondelete="CASCADE"),
                              nullable=False, index=True)
    run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"),
                    nullable=True, index=True)
    title = Column(String(500), nullable=False)
    artifact_type = Column(String(40), default="news")
    url = Column(String(1000), nullable=True)
    description = Column(Text, default="")
    # The text as the *source* published it, kept separately from
    # ``description``. Grounding verifies quotes against this, never against
    # the analyst's summary: verifying a paraphrase is circular -- the model can
    # quote the summary back and pass, or quote the real document and fail.
    # A citation dropped for "no verified quote" when the quote is genuinely in
    # the article is a false negative that teaches people to distrust the
    # checker.
    source_text = Column(Text, nullable=True)
    content = Column(Text, nullable=True)
    source = Column(String(200), default="")
    author = Column(String(200), default="")
    date_published = Column(DateTime, nullable=True)
    tags = Column(String(500), default="")
    # --- the economics layer ---
    domain = Column(String(40), nullable=True, index=True)
    entities_json = Column(JSON, nullable=True)
    # Signed, magnitude-bearing reading when the analyst extracted one:
    # direction is up/down/flat, magnitude 0..1, horizon is how long it takes
    # to matter (immediate/short/medium/long).
    direction = Column(String(10), nullable=True)
    magnitude = Column(Float, nullable=True)
    horizon = Column(String(20), nullable=True)
    numeric_json = Column(JSON, nullable=True)
    # --- triage ---
    relevance = Column(Float, default=0)
    relevance_reason = Column(Text, default="")
    sentiment = Column(Float, default=0)
    review = Column(String(20), default="pending")  # pending|accepted|rejected
    origin = Column(String(20), default="agent")   # agent|manual
    drift = Column(Integer, default=0)             # off-brief, flagged not deleted
    stable_key = Column(String(200), nullable=True, index=True)
    created_at = Column(DateTime, default=_now)

    investigation = relationship("Investigation", back_populates="artifacts")


class ArtifactReview(Base):
    """A human decision on an artifact. Kept as rows, not a flag, so the audit
    view can show *who* reviewed what and when."""

    __tablename__ = "artifact_reviews"

    id = Column(Integer, primary_key=True)
    artifact_id = Column(Integer, ForeignKey("artifacts.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    reviewer = Column(String(120), default="reviewer")
    decision = Column(String(20), nullable=False)  # accept|reject|restore
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)


class Relationship(Base):
    """An edge in the knowledge graph.

    ``confidence`` and ``lag`` exist because a cross-domain edge is a claim
    about a mechanism, not a fact about two URLs: how much of the agent's
    confidence the edge carries, and how many quarters out the effect lands.
    """

    __tablename__ = "relationships"

    id = Column(Integer, primary_key=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id",
                                                  ondelete="CASCADE"),
                              nullable=False, index=True)
    run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"),
                    nullable=True)
    source_id = Column(Integer, ForeignKey("artifacts.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    target_id = Column(Integer, ForeignKey("artifacts.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    relationship_type = Column(String(40), default="references")
    description = Column(Text, default="")
    confidence = Column(Float, nullable=True)
    lag = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_now)


class AgentRun(Base):
    """One execution of the collection loop."""

    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id",
                                                  ondelete="CASCADE"),
                              nullable=False, index=True)
    status = Column(String(20), default="running")  # running|done|error
    trigger = Column(String(20), default="manual")  # manual|schedule|gap|manager
    plan = Column(Text, nullable=True)              # JSON: rationale + queries
    stats = Column(Text, nullable=True)             # JSON: counts per stage
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, default=_now)
    finished_at = Column(DateTime, nullable=True)

    investigation = relationship("Investigation", back_populates="runs")
    events = relationship("AgentEvent", back_populates="run",
                          cascade="all, delete-orphan")


class AgentEvent(Base):
    """One progress line from the loop. The GUI polls these to draw the
    console while a run works."""

    __tablename__ = "agent_events"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"),
                    nullable=False, index=True)
    stage = Column(String(30), nullable=False, index=True)
    message = Column(Text, default="")
    data_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)

    run = relationship("AgentRun", back_populates="events")


class Explanation(Base):
    """A grounded answer over an investigation.

    ``trace`` records how the answer was produced (sources used, claims
    dropped) and ``domains_json`` which domains the answer touched — so the
    explainer can be asked "was the labour channel covered?" rather than only
    "what did it say?".
    """

    __tablename__ = "explanations"

    id = Column(Integer, primary_key=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id",
                                                  ondelete="CASCADE"),
                              nullable=False, index=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, default="")
    sources_json = Column(Text, nullable=True)   # JSON list of artifact ids used
    critique = Column(Text, nullable=True)
    roadmap = Column(Text, nullable=True)
    confidence = Column(Float, nullable=True)
    mode = Column(String(20), default="explain")
    depth = Column(String(20), default="balanced")
    audience = Column(String(20), default="intermediate")
    domains_json = Column(Text, nullable=True)
    dropped_citations = Column(Integer, default=0)
    trace = Column(Text, nullable=True)
    parent_id = Column(Integer, nullable=True, index=True)
    thread_id = Column(Integer, nullable=True, index=True)
    quiz = Column(Text, nullable=True)
    bookmarked = Column(Boolean, default=False)
    watched = Column(Boolean, default=False)
    status = Column(String(20), default="running")
    feedback_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)


class ResearchGap(Base):
    """A dimension of the brief with no evidence at all.

    The distinction from "thinly covered" matters: a gap is where the graph is
    *empty*, so a targeted run at it is the cheapest possible way to move.
    """

    __tablename__ = "research_gaps"

    id = Column(Integer, primary_key=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id",
                                                  ondelete="CASCADE"),
                              nullable=False, index=True)
    dimension = Column(String(200), nullable=False)
    domain = Column(String(40), nullable=True)
    why = Column(Text, default="")
    status = Column(String(20), default="open")  # open|ruled_out|investigating
    evidence = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)


class QueryShapeYield(Base):
    """What a *shape* of query returned, kept across runs.

    The planner works from a blank slate every run, so without this a shape
    that returned nothing last time is proposed first again. Recording the
    round that analyzed candidates and kept none is the most useful thing to
    remember and is exactly the round that used to leave no trace.
    """

    __tablename__ = "query_shape_yields"
    __table_args__ = (
        UniqueConstraint("investigation_id", "shape", name="uq_query_shape"),
    )

    id = Column(Integer, primary_key=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id",
                                                  ondelete="CASCADE"),
                              nullable=False, index=True)
    shape = Column(String(300), nullable=False)   # normalized query text
    raw_queries = Column(String(600), default="")
    sources = Column(String(120), default="")
    times_used = Column(Integer, default=0)
    llm_calls = Column(Integer, default=0)
    found = Column(Integer, default=0)
    kept = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)
    last_used_at = Column(DateTime, default=_now)


class Job(Base):
    """A long-running unit of work on the queue.

    Lease-based: a worker claims a job by writing its id and a deadline, so a
    restart re-queues anything whose lease expired rather than losing it.
    """

    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    kind = Column(String(40), nullable=False, index=True)
    payload_json = Column(Text, nullable=True)
    status = Column(String(20), default="queued", index=True)
    # queued|running|done|error|cancelled
    priority = Column(Integer, default=0)
    investigation_id = Column(Integer, nullable=True, index=True)
    result_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)
    lease_owner = Column(String(64), nullable=True)
    lease_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


class CouncilRecord(Base):
    """What the planner council did on one run.

    Stored as a row rather than recomputed: a record that recomputed would be a
    different review, and the UI needs to show what was decided *at the time*.
    """

    __tablename__ = "council_records"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"),
                    nullable=False, index=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id",
                                                  ondelete="CASCADE"),
                              nullable=False, index=True)
    payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)


class EconomicAssessment(Base):
    """A transmission assessment scoped to an investigation.

    Mirrors the shape of the original security assessment but scores
    economics: coverage across the taxonomy, supported vs prior channels,
    directional signals, hypotheses from the council, and scenarios. The
    numbers are a pure function of stored rows plus the taxonomy version, so
    a re-read after a catalog edit is visibly a different result.

    History stays immutable: a re-score creates a NEW row pointing at the old
    one via ``supersedes_id``, never edits in place.
    """

    __tablename__ = "economic_assessments"

    id = Column(Integer, primary_key=True, index=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id",
                                                  ondelete="CASCADE"),
                              nullable=False, index=True)
    run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"),
                    nullable=True, index=True)
    requested_by = Column(String(120), nullable=True)
    driver = Column(String(40), nullable=True, index=True)
    shock = Column(Text, nullable=True)
    # Headline numbers: 0..100. Confidence is corroboration-based, exposure is
    # magnitude-weighted directional mass across hub domains.
    overall_pct = Column(Float, nullable=False, default=0)
    coverage_pct = Column(Float, nullable=True)
    confidence_pct = Column(Float, nullable=True)
    exposure_pct = Column(Float, nullable=True)
    scoring_json = Column(Text, nullable=True)
    hypotheses_json = Column(Text, nullable=True)
    scenarios_json = Column(Text, nullable=True)
    channels_json = Column(Text, nullable=True)
    signals_json = Column(Text, nullable=True)
    markdown = Column(Text, nullable=False, default="")
    diagrams_json = Column(Text, nullable=True)
    # Taxonomy version fingerprint: without it a later channel edit rewrites
    # what this row saw.
    taxonomy_version = Column(String(20), nullable=True)
    taxonomy_fingerprint = Column(String(20), nullable=True)
    supersedes_id = Column(Integer, nullable=True)
    # Human decision on the assessment: accept / guardrails / reject /
    # exception, with expiry for time-bounded exceptions.
    decision = Column(String(40), nullable=True)
    decided_by = Column(String(200), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    decision_note = Column(Text, nullable=True)
    decision_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)


class ManagerRun(Base):
    """One fan-out command: one request becomes N investigations + a summary.

    ``plan_json`` is the parsed, validated plan (no side effects). ``status``
    moves queued -> running -> done | error. Compile is refused while any
    child is still running and idempotent once it has run.
    """

    __tablename__ = "manager_runs"

    id = Column(Integer, primary_key=True, index=True)
    command = Column(Text, nullable=False)
    plan_json = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="queued")
    summary_investigation_id = Column(Integer, nullable=True, index=True)
    result_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)
    finished_at = Column(DateTime, nullable=True)
