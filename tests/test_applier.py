"""Tests for pipeline.applier — approved job query and result recording."""
import sqlite3
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipeline.apply._common import ApplyResult
from pipeline.db import connect, init_db


def _make_db(tmp_path):
    conn = connect(tmp_path / "tracker.db")
    init_db(conn)
    return conn


def _seed_job(conn, job_id=1, url="https://boards.greenhouse.io/co/jobs/1"):
    conn.execute(
        "INSERT INTO jobs (id, source, company, title, url, language, discovered_date, dedup_hash)"
        " VALUES (?, 'test', 'Acme', 'Sales', ?, 'fr', '2026-06-01', ?)",
        (job_id, url, f"hash{job_id}"),
    )
    conn.execute(
        "INSERT INTO applications (job_id, status) VALUES (?, 'Approved')",
        (job_id,),
    )
    conn.commit()


class TestApprovedJobs:
    def test_returns_approved_jobs(self, tmp_path):
        conn = _make_db(tmp_path)
        _seed_job(conn, job_id=1)
        # Second job not approved
        conn.execute(
            "INSERT INTO jobs (id, source, company, title, url, language, discovered_date, dedup_hash)"
            " VALUES (2, 'test', 'Beta', 'ML', 'https://lever.co/b/1', 'en', '2026-06-01', 'hash2')"
        )
        conn.execute("INSERT INTO applications (job_id, status) VALUES (2, 'Pending')")
        conn.commit()

        with patch("pipeline.applier.paths") as mock_paths:
            mock_paths.DB_PATH = tmp_path / "tracker.db"
            mock_paths.COVER_LETTERS_DIR = tmp_path / "cover_letters"
            mock_paths.COVER_LETTERS_DIR.mkdir()
            from pipeline.applier import _approved_jobs
            jobs = _approved_jobs(conn)

        assert len(jobs) == 1
        assert jobs[0]["company"] == "Acme"

    def test_no_approved_returns_empty(self, tmp_path):
        conn = _make_db(tmp_path)
        conn.execute(
            "INSERT INTO jobs (id, source, company, title, url, language, discovered_date, dedup_hash)"
            " VALUES (1, 'test', 'X', 'Y', 'https://example.com', 'fr', '2026-06-01', 'h1')"
        )
        conn.execute("INSERT INTO applications (job_id, status) VALUES (1, 'Applied')")
        conn.commit()

        with patch("pipeline.applier.paths") as mock_paths:
            mock_paths.DB_PATH = tmp_path / "tracker.db"
            mock_paths.COVER_LETTERS_DIR = tmp_path / "cl"
            mock_paths.COVER_LETTERS_DIR.mkdir()
            from pipeline.applier import _approved_jobs
            jobs = _approved_jobs(conn)

        assert jobs == []


class TestRecordResult:
    def test_applied_sets_submitted_at(self, tmp_path):
        conn = _make_db(tmp_path)
        _seed_job(conn, job_id=1)
        app_id = conn.execute(
            "SELECT id FROM applications WHERE job_id = 1"
        ).fetchone()[0]

        job = {"application_id": app_id, "job_id": 1}
        result = ApplyResult("Applied", "Confirmed", screenshot_path="/tmp/s.png", channel="greenhouse")

        with patch("pipeline.applier.set_status"), patch("pipeline.applier.log_event"):
            from pipeline.applier import _record_result
            _record_result(conn, job, result)

        row = conn.execute(
            "SELECT submitted_at, channel, confirmation_screenshot FROM applications WHERE id = ?",
            (app_id,),
        ).fetchone()
        assert row["channel"] == "greenhouse"
        assert row["confirmation_screenshot"] == "/tmp/s.png"
        assert row["submitted_at"] is not None

    def test_needs_you_records_screenshot(self, tmp_path):
        conn = _make_db(tmp_path)
        _seed_job(conn, job_id=2)
        app_id = conn.execute(
            "SELECT id FROM applications WHERE job_id = 2"
        ).fetchone()[0]

        job = {"application_id": app_id, "job_id": 2}
        result = ApplyResult("Needs you", "Login required", screenshot_path="/tmp/ny.png")

        with patch("pipeline.applier.set_status"), patch("pipeline.applier.log_event"):
            from pipeline.applier import _record_result
            _record_result(conn, job, result)

        row = conn.execute(
            "SELECT submitted_at, confirmation_screenshot FROM applications WHERE id = ?",
            (app_id,),
        ).fetchone()
        assert row["submitted_at"] is None
        assert row["confirmation_screenshot"] == "/tmp/ny.png"
