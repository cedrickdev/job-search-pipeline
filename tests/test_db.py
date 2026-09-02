def test_init_db_creates_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert {"jobs", "scores", "cv_versions", "applications", "events"} <= names


def test_runs_table_exists(conn):
    conn.execute(
        "INSERT INTO runs (kind, started_at) VALUES ('discovery', '2026-06-11T08:00:00')"
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 1


def test_migrate_widens_regen_status_and_adds_detail(tmp_path):
    """A pre-'failed' DB (narrow status CHECK, no `detail` column) is migrated
    in place: the status CHECK widens to allow 'failed', a `detail` column is
    added, and existing pending requests survive the table rebuild untouched.

    SQLite bakes CHECK constraints into the table definition, so widening one
    requires rebuilding the table — this guards that the rebuild is correct and
    idempotent (re-running init_db a second time must be a no-op)."""
    from pipeline.db import connect, init_db

    db = tmp_path / "old.db"
    raw = connect(db)
    # Simulate the shipped-earlier schema: regen_requests with the narrow CHECK
    # and no `detail` column, plus a queued request that must be preserved.
    raw.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, source TEXT, company TEXT, title TEXT,
            discovered_date TEXT, dedup_hash TEXT UNIQUE
        );
        CREATE TABLE regen_requests (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            notes TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'done')),
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            creativity TEXT NOT NULL DEFAULT 'balanced'
        );
        """
    )
    raw.execute(
        "INSERT INTO jobs (id, source, company, title, discovered_date, dedup_hash)"
        " VALUES (1, 'wtj', 'Acme', 'Sales', '2026-06-11', 'mig-h1')")
    raw.execute(
        "INSERT INTO regen_requests (id, job_id, notes, status, created_at, creativity)"
        " VALUES (5, 1, 'keep me', 'pending', '2026-06-11T08:00:00', 'bold')")
    raw.commit()
    raw.close()

    conn = connect(db)
    init_db(conn)  # SCHEMA's CREATE IF NOT EXISTS no-ops the old table; _migrate rebuilds it

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(regen_requests)")}
    assert "detail" in cols

    # The pre-existing pending row survived the rebuild, every field intact.
    row = conn.execute(
        "SELECT job_id, notes, status, creativity, created_at FROM regen_requests"
        " WHERE id = 5").fetchone()
    assert (row["job_id"], row["notes"], row["status"], row["creativity"]) == (
        1, "keep me", "pending", "bold")

    # 'failed' is now an accepted status (the whole point of the rebuild).
    conn.execute(
        "INSERT INTO regen_requests (job_id, notes, status, created_at, detail)"
        " VALUES (1, 'n', 'failed', '2026-06-11T09:00:00', 'tailoring failed')")
    conn.commit()

    # Idempotent: a second init_db must not rebuild again or lose the rows.
    init_db(conn)
    assert conn.execute(
        "SELECT status FROM regen_requests WHERE id = 5").fetchone()["status"] == "pending"
    conn.close()


def test_apply_requests_table_exists(conn):
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(apply_requests)").fetchall()
    }
    assert cols == {
        "id", "job_id", "status", "detail", "channel",
        "screenshot_path", "created_at", "resolved_at",
    }


def test_apply_requests_status_check(conn):
    job_id = conn.execute(
        "INSERT INTO jobs (source, company, title, url, discovered_date, dedup_hash) VALUES ('wtj', 'Acme', 'Sales', 'http://x', '2026-06-18', 'dedup-1')"
    ).lastrowid
    # Valid default insert works.
    conn.execute(
        "INSERT INTO apply_requests (job_id, created_at) VALUES (?, '2026-06-18T08:00:00')",
        (job_id,),
    )
    # Bad status is rejected by the CHECK constraint.
    import sqlite3
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO apply_requests (job_id, status, created_at)"
            " VALUES (?, 'bogus', '2026-06-18T08:00:00')",
            (job_id,),
        )


def test_connection_usable_across_threads(tmp_path):
    """FastAPI runs sync generator dependencies and sync endpoints on different
    threadpool threads, so a per-request connection is created on one thread and
    later used on another. connect(check_same_thread=False) must permit that;
    the default must stay thread-checked so single-threaded pipeline use is
    unaffected."""
    import concurrent.futures

    from pipeline.db import connect, init_db

    db = tmp_path / "threaded.db"
    c = connect(db, check_same_thread=False)
    init_db(c)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        # Run the query in a worker thread, NOT the thread that created `c`.
        n = ex.submit(
            lambda: c.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        ).result()
    c.close()
    assert n == 0
