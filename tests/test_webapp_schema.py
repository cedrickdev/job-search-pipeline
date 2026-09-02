# tests/test_webapp_schema.py
"""The 5 webapp tables must be created by init_db (additive, in SCHEMA)."""
import pytest

WEBAPP_TABLES = [
    "prep_notes", "prep_cache", "interview_log",
    "followup_overrides", "chat_sessions",
]


@pytest.mark.parametrize("table", WEBAPP_TABLES)
def test_webapp_table_exists(conn, table):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    assert row is not None, f"table {table} was not created by init_db"


def test_chat_sessions_composite_pk(conn):
    # scope+scope_id is the primary key; inserting the same pair twice fails.
    # Global scope uses scope_id=0 (never NULL) — a composite PK does NOT enforce
    # uniqueness across NULLs in SQLite (NULLs compare distinct), and the schema
    # declares scope_id NOT NULL DEFAULT 0, so production always passes an int.
    conn.execute(
        "INSERT INTO chat_sessions (scope, scope_id, claude_session_id, updated_at)"
        " VALUES ('global', 0, 'sess-1', '2026-06-16T08:00:00')")
    conn.commit()
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO chat_sessions (scope, scope_id, claude_session_id, updated_at)"
            " VALUES ('global', 0, 'sess-2', '2026-06-16T08:01:00')")
        conn.commit()


def test_prep_notes_default_empty(conn):
    conn.execute("INSERT INTO jobs (source, company, title, url, location,"
                 " language, description, discovered_date, dedup_hash)"
                 " VALUES ('wtj','C','T','u','Lausanne','en','d','2026-06-16','h1')")
    job_id = conn.execute("SELECT id FROM jobs").fetchone()["id"]
    from pipeline.statuses import create_application
    app_id = create_application(conn, job_id, source="test")
    conn.execute("INSERT INTO prep_notes (application_id, updated_at)"
                 " VALUES (?, '2026-06-16T08:00:00')", (app_id,))
    conn.commit()
    row = conn.execute("SELECT notes_md FROM prep_notes WHERE application_id=?",
                       (app_id,)).fetchone()
    assert row["notes_md"] == ""
