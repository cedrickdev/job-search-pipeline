"""Typed state-change operations behind the action endpoints.

Every transition is guarded server-side against the current status (spec §5.3).
resolve_application_id is the single job_id->application_id resolution point (§4.1).
"""
from pipeline.apply_requests import create_or_get_request as create_or_get_apply_request
from pipeline.regen import create_request
from pipeline.statuses import STATUSES, create_application, mark_applied, set_status

SOURCE = "webapp"


class TransitionError(Exception):
    def __init__(self, current, target):
        self.current = current
        self.target = target
        super().__init__(f"cannot move from {current} to {target}")


class JobNotFound(Exception):
    pass


def resolve_application_id(conn, job_id, *, create=False):
    row = conn.execute(
        "SELECT id FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
        (job_id,)).fetchone()
    if row:
        return row["id"]
    if not create:
        return None
    job = conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise JobNotFound(job_id)
    return create_application(conn, job_id, source=SOURCE)


def _status(conn, app_id):
    return conn.execute(
        "SELECT status FROM applications WHERE id=?", (app_id,)).fetchone()["status"]


def go(conn, job_id):
    app_id = resolve_application_id(conn, job_id, create=True)
    cur = _status(conn, app_id)
    if cur != "Ready to apply":
        raise TransitionError(cur, "Approved")
    set_status(conn, app_id, "Approved", source=SOURCE)
    return _status(conn, app_id)


def applied(conn, job_id, channel="manual"):
    app_id = resolve_application_id(conn, job_id)
    if app_id is None:
        raise JobNotFound(job_id)
    cur = _status(conn, app_id)
    if cur not in ("Approved", "Needs you"):
        raise TransitionError(cur, "Applied")
    mark_applied(conn, app_id, channel=channel)
    return _status(conn, app_id)


def skip(conn, job_id):
    app_id = resolve_application_id(conn, job_id, create=True)
    set_status(conn, app_id, "Archived", source=SOURCE)
    return _status(conn, app_id)


def set_manual_status(conn, job_id, status, detail=None):
    if status not in STATUSES:
        raise TransitionError(None, status)
    app_id = resolve_application_id(conn, job_id, create=True)
    set_status(conn, app_id, status, source=SOURCE, detail=detail)
    return _status(conn, app_id)


def regen(conn, job_id, notes, creativity="balanced"):
    # ValueError on empty notes / no job; creativity is normalized in create_request.
    return create_request(conn, job_id, notes, creativity)


def apply_now(conn, job_id):
    """Enqueue a background apply for one job.

    Returns ``(request_id, created)``. ``created`` is ``False`` when an
    in-flight (pending/in_progress) request already existed, so the caller can
    skip spawning a redundant worker. Eligible only from 'Ready to apply'/
    'Approved' and only when a tailored CV exists (no point launching a worker
    that will fail at upload).
    """
    app_id = resolve_application_id(conn, job_id)
    if app_id is None:
        raise JobNotFound(job_id)
    current = _status(conn, app_id)
    if current not in ("Ready to apply", "Approved"):
        raise TransitionError(current, "Apply now")
    cv = conn.execute(
        "SELECT id FROM cv_versions WHERE job_id = ? LIMIT 1", (job_id,)
    ).fetchone()
    if cv is None:
        raise TransitionError(current, "Apply now (no tailored CV)")
    # One atomic insert-or-get: ``created`` comes from whether this call
    # actually inserted the row, so the route can gate worker dispatch without
    # a separate read that could race a concurrent enqueue.
    request_id, created = create_or_get_apply_request(conn, job_id)
    return request_id, created
