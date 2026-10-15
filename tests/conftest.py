import os
import tempfile

import pytest

# Point the engine at a scratch database *before* anything imports database.py,
# so a test run can never touch the real one.
_TMP = tempfile.mkdtemp(prefix="econ-tests-")
os.environ["ECON_DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["ECON_SCHEDULER_ENABLED"] = "0"
os.environ["ECON_JOB_LEASE_S"] = "60"
# The test drains the queue itself; a background worker would race with it.
os.environ["ECON_JOB_WORKER"] = "0"

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clean(db):
    """Truncate between tests so counts are exact.

    Ledger tables are included: chains are content-addressed and append-only by
    design, so a run left over from one test would silently answer for another
    (contamination and timeline both walk every event on a chain). Domain rows
    cascade; ledger rows have to be cleared in FK order explicitly.
    """
    from app.ledger_models import (LedgerApproval, LedgerAuditDrop, LedgerClaim,
                                   LedgerEvent, LedgerRun)
    from app.models import (AgentEvent, AgentRun, Artifact, ArtifactReview,
                            CouncilRecord, EconomicAssessment, Explanation,
                            Investigation, Job, ManagerRun, QueryShapeYield,
                            Relationship, ResearchGap)
    for model in (LedgerApproval, LedgerEvent, LedgerClaim, LedgerAuditDrop,
                  LedgerRun, AgentEvent, CouncilRecord, Explanation,
                  EconomicAssessment, ManagerRun, ResearchGap, Relationship,
                  ArtifactReview, AgentRun, Artifact, Job, QueryShapeYield,
                  Investigation):
        db.query(model).delete()
    db.commit()
    yield