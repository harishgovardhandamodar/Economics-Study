"""Engine, session factory, and the additive migration list.

SQLite with WAL so the agent's writer thread and the API's readers do not
block each other. Everything is ``check_same_thread: False`` because research
threads are long-lived and outlive the request that started them.
"""
import os

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

# Overridable so tests (and throwaway runs) can point at a scratch database
# instead of the real one.
DATABASE_URL = os.environ.get(
    "ECON_DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'econ.db')}")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added after the first release. ``create_all`` never adds a column to
# an existing table, so every later addition lives here as an additive
# ``ALTER TABLE``. Additive only, on purpose: a destructive migration would
# take an investigation's artifacts with it, and the whole value of the app is
# what it has already collected.
_MIGRATIONS = [
    # investigation
    ("investigations", "schedule_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("investigations", "schedule_cron", "VARCHAR(100)"),
    ("investigations", "schedule_max_items", "INTEGER NOT NULL DEFAULT 25"),
    ("investigations", "schedule_max_rounds", "INTEGER NOT NULL DEFAULT 2"),
    ("investigations", "last_scheduled_at", "DATETIME"),
    ("investigations", "next_run_at", "DATETIME"),
    ("investigations", "preferred_domains", "VARCHAR(500)"),
    ("investigations", "auto_save_explanations", "INTEGER NOT NULL DEFAULT 1"),
    ("investigations", "hidden", "INTEGER NOT NULL DEFAULT 0"),
    ("investigations", "driver", "VARCHAR(40)"),
    ("investigations", "shock", "TEXT"),
    # artifacts
    ("artifacts", "run_id", "INTEGER"),
    ("artifacts", "drift", "INTEGER NOT NULL DEFAULT 0"),
    ("artifacts", "domain", "VARCHAR(40)"),
    ("artifacts", "entities_json", "TEXT"),
    ("artifacts", "source_text", "TEXT"),
    ("artifacts", "magnitude", "REAL"),
    ("artifacts", "horizon", "VARCHAR(20)"),
    ("artifacts", "direction", "VARCHAR(10)"),
    ("artifacts", "stable_key", "VARCHAR(200)"),
    ("artifacts", "numeric_json", "TEXT"),
    ("artifacts", "transcript_json", "TEXT"),
    # relationships
    ("relationships", "run_id", "INTEGER"),
    ("relationships", "created_at", "DATETIME"),
    ("relationships", "confidence", "REAL"),
    ("relationships", "lag", "INTEGER"),
    # runs and events
    ("agent_runs", "trigger", "VARCHAR(20) NOT NULL DEFAULT 'manual'"),
    ("agent_runs", "started_at", "DATETIME"),
    # explanations
    ("explanations", "trace", "TEXT"),
    ("explanations", "mode", "VARCHAR(20) DEFAULT 'explain'"),
    ("explanations", "depth", "VARCHAR(20) DEFAULT 'balanced'"),
    ("explanations", "audience", "VARCHAR(20) DEFAULT 'intermediate'"),
    ("explanations", "parent_id", "INTEGER"),
    ("explanations", "thread_id", "INTEGER"),
    ("explanations", "quiz", "TEXT"),
    ("explanations", "bookmarked", "INTEGER NOT NULL DEFAULT 0"),
    ("explanations", "watched", "INTEGER NOT NULL DEFAULT 0"),
    ("explanations", "domains_json", "TEXT"),
    ("explanations", "confidence", "REAL"),
    # gaps
    ("research_gaps", "status", "VARCHAR(20) DEFAULT 'open'"),
    ("research_gaps", "evidence", "TEXT"),
    # yield ledger: added after the first run of a live collection wrote
    # "yield not recorded" for every round, because the row it tries to create
    # had nowhere to record when the shape was first seen.
    ("query_shape_yields", "created_at", "DATETIME"),
    # explanation feedback (thumbs up/down + note, as JSON list).
    ("explanations", "feedback_json", "TEXT"),
]


def ensure_columns() -> None:
    """Apply the additive migration list, skipping ones already present."""
    with engine.begin() as conn:
        existing: dict[str, set[str]] = {}
        for table in {t for t, _, _ in _MIGRATIONS}:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            existing[table] = {r[1] for r in rows}
        for table, column, ddl in _MIGRATIONS:
            if not existing.get(table):
                continue  # table not created yet; create_all is mid-flight
            if column in existing[table]:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def init_db() -> None:
    # Imported here, not at module scope: the ledger models build on Base, and
    # this module is their foundation, so a top-level import would be circular.
    from . import (
        ledger_models,  # noqa: F401
        models,  # noqa: F401
    )

    Base.metadata.create_all(bind=engine)
    ensure_columns()
