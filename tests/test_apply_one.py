import asyncio
import fcntl

import pytest

from pipeline import apply_one
from pipeline import apply_requests as ar
from pipeline.apply import ApplyResult
from tests.helpers import seed_job, seed_cv_version


def _make_apply_fn(result=None, exc=None):
    calls = []

    async def apply_fn(job, answers, headless):
        calls.append({"job": job, "headless": headless})
        if exc is not None:
            raise exc
        return result

    apply_fn.calls = calls
    return apply_fn


def test_lock_busy_resolves_needs_you_without_applying(conn, tmp_path):
    job_id, _ = seed_job(conn, status="Ready to apply")
    rid = ar.create_request(conn, job_id)
    lock_path = tmp_path / ".lock"
    # Pre-hold the lock so the worker cannot acquire it.
    holder = open(lock_path, "w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    try:
        apply_fn = _make_apply_fn(result=ApplyResult("Applied", "should not run"))
        apply_one.run_request(
            conn, rid, answers={}, apply_fn=apply_fn, lock_path=lock_path
        )
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    assert apply_fn.calls == []
    latest = ar.latest_request(conn, job_id)
    assert latest["status"] == "needs_you"
    assert "Browser in use" in latest["detail"]


def test_applied_result_maps_to_applied(conn, tmp_path):
    job_id, app_id = seed_job(conn, status="Ready to apply")
    rid = ar.create_request(conn, job_id)
    apply_fn = _make_apply_fn(
        result=ApplyResult("Applied", "Submitted", "/tmp/ok.png", "linkedin")
    )
    apply_one.run_request(
        conn, rid, answers={}, apply_fn=apply_fn, lock_path=tmp_path / ".lock"
    )
    latest = ar.latest_request(conn, job_id)
    assert latest["status"] == "applied"
    assert latest["detail"] == "Submitted"
    assert latest["screenshot_path"] == "/tmp/ok.png"
    # Application status also moved to Applied.
    app_status = conn.execute(
        "SELECT status FROM applications WHERE id = ?", (app_id,)
    ).fetchone()["status"]
    assert app_status == "Applied"


def test_needs_you_result_maps_to_needs_you(conn, tmp_path):
    job_id, _ = seed_job(conn, status="Ready to apply")
    rid = ar.create_request(conn, job_id)
    apply_fn = _make_apply_fn(
        result=ApplyResult("Needs you", "captcha", "/tmp/c.png", "wtj")
    )
    apply_one.run_request(
        conn, rid, answers={}, apply_fn=apply_fn, lock_path=tmp_path / ".lock"
    )
    latest = ar.latest_request(conn, job_id)
    assert latest["status"] == "needs_you"
    assert latest["detail"] == "captcha"


def test_exception_resolves_failed(conn, tmp_path):
    job_id, _ = seed_job(conn, status="Ready to apply")
    rid = ar.create_request(conn, job_id)
    apply_fn = _make_apply_fn(exc=RuntimeError("boom"))
    apply_one.run_request(
        conn, rid, answers={}, apply_fn=apply_fn, lock_path=tmp_path / ".lock"
    )
    latest = ar.latest_request(conn, job_id)
    assert latest["status"] == "failed"
    assert "boom" in latest["detail"]


def test_non_pending_request_is_noop(conn, tmp_path):
    job_id, _ = seed_job(conn, status="Ready to apply")
    rid = ar.create_request(conn, job_id)
    ar.resolve_request(conn, rid, "applied")
    apply_fn = _make_apply_fn(result=ApplyResult("Applied", "x"))
    apply_one.run_request(
        conn, rid, answers={}, apply_fn=apply_fn, lock_path=tmp_path / ".lock"
    )
    assert apply_fn.calls == []
    # Status is unchanged (still the terminal we set above).
    assert ar.latest_request(conn, job_id)["status"] == "applied"


def test_load_job_builds_expected_shape(conn):
    job_id, app_id = seed_job(conn, status="Ready to apply", language="en")
    seed_cv_version(conn, job_id, language="en", pdf_path="/tmp/cv_en.pdf")
    job = apply_one._load_job(conn, job_id)
    assert job["job_id"] == job_id
    assert job["application_id"] == app_id
    assert job["cv_pdf"]["en"] == "/tmp/cv_en.pdf"
    assert job["cv_pdf"]["fr"] is None
    assert set(job["cover_letter"]) == {"en", "fr"}
