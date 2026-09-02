"""Tests for pipeline.gmail_recruiter — pending follow-up query and SMTP send."""
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from pipeline.db import connect, init_db


def _make_db(tmp_path):
    conn = connect(tmp_path / "tracker.db")
    init_db(conn)
    return conn


def _seed_applied(conn, job_id, submitted_days_ago, recruiter_email, lang="fr"):
    submitted = (datetime.now() - timedelta(days=submitted_days_ago)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO jobs (id, source, company, title, url, language, discovered_date, dedup_hash)"
        " VALUES (?, 'test', 'Acme', 'Sales', 'https://x.com', ?, '2026-06-01', ?)",
        (job_id, lang, f"hash{job_id}"),
    )
    conn.execute(
        "INSERT INTO applications (job_id, status, recruiter_email, submitted_at)"
        " VALUES (?, 'Applied', ?, ?)",
        (job_id, recruiter_email, submitted),
    )
    conn.commit()


class TestPendingFollowups:
    def test_past_delay_included(self, tmp_path):
        conn = _make_db(tmp_path)
        _seed_applied(conn, job_id=1, submitted_days_ago=8, recruiter_email="hr@acme.com")
        from pipeline.gmail_recruiter import _pending_followups
        pending = _pending_followups(conn, delay_days=7, max_followups=2)
        assert len(pending) == 1
        assert pending[0]["recruiter_email"] == "hr@acme.com"

    def test_within_delay_excluded(self, tmp_path):
        conn = _make_db(tmp_path)
        _seed_applied(conn, job_id=2, submitted_days_ago=3, recruiter_email="hr@beta.com")
        from pipeline.gmail_recruiter import _pending_followups
        pending = _pending_followups(conn, delay_days=7, max_followups=2)
        assert pending == []

    def test_no_recruiter_email_excluded(self, tmp_path):
        conn = _make_db(tmp_path)
        submitted = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO jobs (id, source, company, title, url, language, discovered_date, dedup_hash)"
            " VALUES (3, 'test', 'X', 'Sales', 'https://x.com', 'fr', '2026-06-01', 'h3')"
        )
        conn.execute(
            "INSERT INTO applications (job_id, status, submitted_at) VALUES (3, 'Applied', ?)",
            (submitted,),
        )
        conn.commit()
        from pipeline.gmail_recruiter import _pending_followups
        pending = _pending_followups(conn, delay_days=7, max_followups=2)
        assert pending == []

    def test_max_followups_cap(self, tmp_path):
        conn = _make_db(tmp_path)
        _seed_applied(conn, job_id=4, submitted_days_ago=20, recruiter_email="hr@capped.com")
        app_id = conn.execute("SELECT id FROM applications WHERE job_id = 4").fetchone()[0]
        # Simulate 2 already-sent follow-ups
        for _ in range(2):
            conn.execute(
                "INSERT INTO events (application_id, job_id, event_type, detail, source, created_at)"
                " VALUES (?, 4, 'followup_sent', 'sent', 'gmail-recruiter', '2026-06-01T10:00:00')",
                (app_id,),
            )
        conn.commit()
        from pipeline.gmail_recruiter import _pending_followups
        pending = _pending_followups(conn, delay_days=7, max_followups=2)
        assert pending == []


class TestSendEmail:
    def test_sends_via_smtp_ssl(self):
        answers = {
            "gmail": {"user_env": "GMAIL_USER", "password_env": "GMAIL_APP_PASSWORD"}
        }
        with patch.dict(os.environ, {"GMAIL_USER": "test@gmail.com", "GMAIL_APP_PASSWORD": "secret"}):
            with patch("pipeline.gmail_recruiter.smtplib.SMTP_SSL") as mock_ssl:
                mock_server = MagicMock()
                mock_ssl.return_value.__enter__ = MagicMock(return_value=mock_server)
                mock_ssl.return_value.__exit__ = MagicMock(return_value=False)
                from pipeline.gmail_recruiter import send_email
                send_email("recruiter@co.com", "Subject", "Body", answers)
                mock_ssl.assert_called_once_with("smtp.gmail.com", 465, timeout=30)
                mock_server.login.assert_called_once_with("test@gmail.com", "secret")
                mock_server.send_message.assert_called_once()

    def test_missing_env_raises(self):
        answers = {"gmail": {"user_env": "MISSING_USER", "password_env": "MISSING_PASS"}}
        with patch.dict(os.environ, {}, clear=True):
            from pipeline.gmail_recruiter import send_email
            with pytest.raises(EnvironmentError, match="GMAIL_USER"):
                send_email("to@co.com", "Sub", "Body", answers)
