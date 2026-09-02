"""SQLite connection and schema. Source of truth for the whole pipeline."""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    location TEXT,
    remote_policy TEXT,
    contract_type TEXT,
    salary TEXT,
    description TEXT,
    language TEXT,
    posted_date TEXT,
    discovered_date TEXT NOT NULL,
    dedup_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    reasoning TEXT,
    red_flags TEXT,
    scorer_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cv_versions (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id),
    language TEXT NOT NULL CHECK (language IN ('en', 'fr')),
    pdf_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    diff_summary TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
    cv_version_id INTEGER REFERENCES cv_versions(id),
    cover_letter_path TEXT,
    status TEXT NOT NULL,
    channel TEXT,
    submitted_at TEXT,
    confirmation_screenshot TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    application_id INTEGER REFERENCES applications(id),
    job_id INTEGER REFERENCES jobs(id),
    event_type TEXT NOT NULL,
    detail TEXT,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS regen_requests (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    notes TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'done', 'failed')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    creativity TEXT NOT NULL DEFAULT 'balanced',  -- conservative | balanced | bold
    detail TEXT  -- failure reason (null for pending/done)
);

CREATE TABLE IF NOT EXISTS apply_requests (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','in_progress','applied','needs_you','failed')),
    detail TEXT,
    channel TEXT,
    screenshot_path TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS prep_notes (
    application_id INTEGER PRIMARY KEY REFERENCES applications(id),
    notes_md       TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prep_cache (
    application_id        INTEGER PRIMARY KEY REFERENCES applications(id),
    likely_questions_json TEXT,
    company_research_json TEXT,
    talking_points_json   TEXT,
    generated_at          TEXT
);

CREATE TABLE IF NOT EXISTS interview_log (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    round_label    TEXT NOT NULL,
    scheduled_for  TEXT,
    outcome        TEXT,
    notes          TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followup_overrides (
    application_id INTEGER PRIMARY KEY REFERENCES applications(id),
    snooze_until   TEXT,
    dismissed      INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    scope             TEXT NOT NULL,
    scope_id          INTEGER NOT NULL DEFAULT 0,  -- global scope uses 0, never NULL
    claude_session_id TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (scope, scope_id)
);

-- Persisted copilot turns so a conversation survives leaving the chat (§6.1).
-- Keyed by (scope, scope_id) like chat_sessions; ordered by id.
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY,
    scope      TEXT NOT NULL,
    scope_id   INTEGER NOT NULL DEFAULT 0,  -- global scope uses 0, never NULL
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_scope
    ON chat_messages (scope, scope_id, id);
"""


def connect(db_path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a pipeline connection.

    ``check_same_thread`` defaults to True (sqlite's normal guard) so every
    single-threaded pipeline caller is unaffected. The web server passes False:
    FastAPI runs a sync generator dependency and the sync endpoint on different
    threadpool threads, so a per-request connection is created on one thread and
    used on another. That is safe here because each request owns its connection
    and accesses it serially — never two threads at once.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations — safe to re-run on existing DBs."""
    try:
        conn.execute(
            "ALTER TABLE cv_versions ADD COLUMN phone_screen_pct INTEGER")
    except Exception:
        pass  # column already exists
    try:
        conn.execute(
            "ALTER TABLE applications ADD COLUMN recruiter_email TEXT")
    except Exception:
        pass  # column already exists
    try:
        conn.execute(
            "ALTER TABLE regen_requests ADD COLUMN creativity TEXT NOT NULL DEFAULT 'balanced'")
    except Exception:
        pass  # column already exists
    try:
        # `track` splits the pipeline into two boards: 'job' (student jobs in
        # Vaud) and 'travail' (remote dev/DevOps work). Existing rows default to
        # 'job'; the dev-track sources insert with track='travail'.
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN track TEXT NOT NULL DEFAULT 'job'")
    except Exception:
        pass  # column already exists
    _migrate_regen_status(conn)
    _migrate_stage_timestamps(conn)


def _migrate_stage_timestamps(conn: sqlite3.Connection) -> None:
    """Add per-stage timestamp columns to applications (idempotent).

    Each column records the first time an application entered that stage so
    you can see exactly how long each step took across the funnel.
    """
    new_cols = [
        "created_at",       # Discovered
        "scored_at",        # Scored / Borderline / Archived
        "tailored_at",      # Ready to apply
        "approved_at",      # Approved
        "phone_screen_at",  # Phone Screen / Recruiter reply
        "interview_at",     # Interview scheduled
        "offer_at",         # Offer
        "rejected_at",      # Rejected / Ghosted
    ]
    for col in new_cols:
        try:
            conn.execute(f"ALTER TABLE applications ADD COLUMN {col} TEXT")
        except Exception:
            pass  # already exists

    # Backfill from the events log wherever possible.
    conn.executescript("""
        UPDATE applications SET created_at = (
            SELECT MIN(e.created_at) FROM events e
            WHERE e.application_id = applications.id
        ) WHERE created_at IS NULL;

        UPDATE applications SET scored_at = (
            SELECT MIN(e.created_at) FROM events e
            WHERE e.application_id = applications.id AND e.source = 'scoring'
        ) WHERE scored_at IS NULL;

        UPDATE applications SET tailored_at = (
            SELECT MIN(e.created_at) FROM events e
            WHERE e.application_id = applications.id AND e.source = 'tailoring'
        ) WHERE tailored_at IS NULL;

        UPDATE applications SET approved_at = (
            SELECT MIN(e.created_at) FROM events e
            WHERE e.application_id = applications.id
              AND e.event_type = 'status_change'
              AND (e.detail LIKE '-> Approved%' OR e.detail LIKE 'auto-approved%')
        ) WHERE approved_at IS NULL;

        UPDATE applications SET phone_screen_at = (
            SELECT MIN(e.created_at) FROM events e
            WHERE e.application_id = applications.id
              AND e.event_type = 'status_change'
              AND (e.detail LIKE '-> Phone Screen%' OR e.detail LIKE '-> Recruiter reply%')
        ) WHERE phone_screen_at IS NULL;

        UPDATE applications SET interview_at = (
            SELECT MIN(e.created_at) FROM events e
            WHERE e.application_id = applications.id
              AND e.event_type = 'status_change'
              AND e.detail LIKE '-> Interview%'
        ) WHERE interview_at IS NULL;

        UPDATE applications SET offer_at = (
            SELECT MIN(e.created_at) FROM events e
            WHERE e.application_id = applications.id
              AND e.event_type = 'status_change'
              AND e.detail LIKE '-> Offer%'
        ) WHERE offer_at IS NULL;

        UPDATE applications SET rejected_at = (
            SELECT MIN(e.created_at) FROM events e
            WHERE e.application_id = applications.id
              AND e.event_type = 'status_change'
              AND (e.detail LIKE '-> Rejected%' OR e.detail LIKE '-> Ghosted%')
        ) WHERE rejected_at IS NULL;

        -- For rows whose status was set by direct SQL (bypassing set_status),
        -- fall back to discovered_date so the column is never NULL for active rows.
        UPDATE applications SET phone_screen_at = (
            SELECT j.discovered_date FROM jobs j
            WHERE j.id = applications.job_id
        ) WHERE phone_screen_at IS NULL
          AND status IN ('Phone Screen', 'Recruiter reply');

        UPDATE applications SET rejected_at = (
            SELECT j.discovered_date FROM jobs j
            WHERE j.id = applications.job_id
        ) WHERE rejected_at IS NULL
          AND status IN ('Rejected', 'Ghosted');
    """)


def _migrate_regen_status(conn: sqlite3.Connection) -> None:
    """Widen regen_requests.status to allow 'failed' and add a ``detail`` column.

    SQLite bakes CHECK constraints into the table definition, so the status set
    cannot be widened with ALTER — the table must be rebuilt. Detect the
    pre-'failed' definition from ``sqlite_master`` and rebuild once; re-running
    finds 'failed' already present and is a no-op. No table references
    regen_requests, so the rebuild is safe with ``foreign_keys = ON``; the
    INSERT ... SELECT preserves every existing request (e.g. queued ones).

    Runs after the ``creativity`` ALTER above so the source table is guaranteed
    to have that column to copy.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'regen_requests'"
    ).fetchone()
    if row is None or "'failed'" in row["sql"]:
        return  # fresh DB already has the widened schema, or already migrated
    conn.executescript(
        """
        ALTER TABLE regen_requests RENAME TO regen_requests_old;
        CREATE TABLE regen_requests (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            notes TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'done', 'failed')),
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            creativity TEXT NOT NULL DEFAULT 'balanced',
            detail TEXT
        );
        INSERT INTO regen_requests
            (id, job_id, notes, status, created_at, resolved_at, creativity)
        SELECT id, job_id, notes, status, created_at, resolved_at, creativity
        FROM regen_requests_old;
        DROP TABLE regen_requests_old;
        """
    )
