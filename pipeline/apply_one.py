"""Background worker for a single apply-now request.

`python -m pipeline.apply_one --request-id N` drives the application for one
job, guarded by an exclusive browser lock so it never collides with a live
debugging session, the morning-run applier, or another apply. Applies run
strictly one at a time.
"""
import argparse
import asyncio
import fcntl

from pipeline import paths
from pipeline.apply._common import detect_platform, load_answers
from pipeline.applier import _apply_one, _record_result
from pipeline.apply_requests import claim_request, resolve_request
from pipeline.db import connect, init_db

# Exclusive advisory lock for the shared browser profile, a sibling of the
# LinkedIn persistent-context dir (data/browser_state/linkedin).
_LOCK_PATH = paths.DATA_DIR / "browser_state" / ".lock"
_BUSY_REASON = "Browser in use. Close the live session and retry."
_STATUS_MAP = {"Applied": "applied", "Needs you": "needs_you", "Failed": "failed"}


def _load_job(conn, job_id):
    """Build the applier job dict for ONE job, regardless of status."""
    row = conn.execute(
        "SELECT a.id AS application_id, j.id AS job_id, j.company, j.title,"
        " j.url, j.language"
        " FROM applications a JOIN jobs j ON j.id = a.job_id"
        " WHERE j.id = ? ORDER BY a.id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    job = dict(row)
    cv_pdf = {}
    for lang in ("en", "fr"):
        cv = conn.execute(
            "SELECT pdf_path FROM cv_versions WHERE job_id = ? AND language = ?"
            " ORDER BY id DESC LIMIT 1",
            (job_id, lang),
        ).fetchone()
        cv_pdf[lang] = cv["pdf_path"] if cv is not None else None
    job["cv_pdf"] = cv_pdf
    cover_letter = {}
    for lang in ("en", "fr"):
        path = paths.COVER_LETTERS_DIR / f"{job_id}_{lang}.md"
        cover_letter[lang] = str(path) if path.exists() else None
    job["cover_letter"] = cover_letter
    return job


def run_request(conn, request_id, *, answers=None, apply_fn=None, lock_path=None):
    """Drive one apply request to a terminal state. Testable core of the worker."""
    apply_fn = apply_fn if apply_fn is not None else _apply_one
    lock_path = lock_path if lock_path is not None else _LOCK_PATH

    row = conn.execute(
        "SELECT job_id, status FROM apply_requests WHERE id = ?", (request_id,)
    ).fetchone()
    if row is None:
        return
    if row["status"] != "pending":
        return  # already claimed or resolved

    job = _load_job(conn, row["job_id"])
    if job is None:
        resolve_request(conn, request_id, "failed", "No application for this job.")
        return
    if answers is None:
        answers = load_answers()
    channel = detect_platform(job["url"] or "")

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            resolve_request(conn, request_id, "needs_you", _BUSY_REASON)
            return
        try:
            claim_request(conn, request_id, channel)
            result = asyncio.run(apply_fn(job, answers, headless=True))
            _record_result(conn, job, result)
            resolve_request(
                conn,
                request_id,
                _STATUS_MAP.get(result.status, "failed"),
                result.detail,
                result.screenshot_path,
            )
        except Exception as exc:  # noqa: BLE001 - worker must never get stuck
            resolve_request(conn, request_id, "failed", str(exc))
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def main():
    parser = argparse.ArgumentParser(description="Apply to a single job in the background.")
    parser.add_argument("--request-id", type=int, required=True)
    args = parser.parse_args()
    conn = connect(paths.DB_PATH)
    init_db(conn)
    run_request(conn, args.request_id)


if __name__ == "__main__":
    main()
