"""Action endpoints — all state changes pass through here with server-side guards."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from pipeline.apply._common import load_answers
from server import actions, followups
from server.deps import get_conn

router = APIRouter(prefix="/api/jobs")


class StatusBody(BaseModel):
    status: str
    detail: str | None = None  # spec §5.2: optional note logged with the status event


class RegenBody(BaseModel):
    notes: str
    creativity: str = "balanced"  # conservative | balanced | bold; normalized server-side


class AppliedBody(BaseModel):
    channel: str = "manual"


class SnoozeBody(BaseModel):
    days: int = 7


def _parse_now(now: str | None) -> datetime:
    return datetime.fromisoformat(now) if now else datetime.now()


def _application_id(conn, job_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    return row[0] if row else None


def _personal() -> tuple[str, str, str]:
    """Candidate name/phone/email for the draft signature; empty on any read error."""
    try:
        p = load_answers().get("personal", {})
        return p.get("name", ""), p.get("phone", ""), p.get("email", "")
    except Exception:
        return "", "", ""


def _conflict(exc: actions.TransitionError):
    raise HTTPException(status_code=409, detail={
        "error": "invalid_transition", "current": exc.current, "target": exc.target})


@router.post("/{job_id}/go")
def post_go(job_id: int, conn=Depends(get_conn)):
    try:
        return {"status": actions.go(conn, job_id)}
    except actions.JobNotFound:
        raise HTTPException(404, "job not found")
    except actions.TransitionError as e:
        _conflict(e)


@router.post("/{job_id}/applied")
def post_applied(job_id: int, body: AppliedBody | None = None, conn=Depends(get_conn)):
    channel = body.channel if body else "manual"
    try:
        return {"status": actions.applied(conn, job_id, channel=channel)}
    except actions.JobNotFound:
        raise HTTPException(404, "job not found")
    except actions.TransitionError as e:
        _conflict(e)


@router.post("/{job_id}/skip")
def post_skip(job_id: int, conn=Depends(get_conn)):
    try:
        return {"status": actions.skip(conn, job_id)}
    except actions.JobNotFound:
        raise HTTPException(404, "job not found")


@router.post("/{job_id}/status")
def post_status(job_id: int, body: StatusBody, conn=Depends(get_conn)):
    try:
        return {"status": actions.set_manual_status(conn, job_id, body.status, detail=body.detail)}
    except actions.JobNotFound:
        raise HTTPException(404, "job not found")
    except actions.TransitionError as e:
        _conflict(e)


@router.post("/{job_id}/regen")
def post_regen(job_id: int, body: RegenBody, conn=Depends(get_conn)):
    try:
        return {"request_id": actions.regen(conn, job_id, body.notes, body.creativity)}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/{job_id}/followup/snooze")
def post_followup_snooze(job_id: int, body: SnoozeBody | None = None,
                         now: str | None = None, conn=Depends(get_conn)):
    app_id = _application_id(conn, job_id)
    if app_id is None:
        raise HTTPException(404, "application not found")
    clock = _parse_now(now)
    days = body.days if body else 7
    snooze_until = (clock + timedelta(days=days)).isoformat(timespec="seconds")
    followups.set_override(conn, app_id, snooze_until=snooze_until, now=clock)
    return {"ok": True, "snooze_until": snooze_until}


@router.post("/{job_id}/followup/dismiss")
def post_followup_dismiss(job_id: int, now: str | None = None,
                          conn=Depends(get_conn)):
    app_id = _application_id(conn, job_id)
    if app_id is None:
        raise HTTPException(404, "application not found")
    followups.set_override(conn, app_id, dismissed=True, now=_parse_now(now))
    return {"ok": True}


@router.post("/{job_id}/apply-now", status_code=202)
async def post_apply_now(job_id: int, request: Request, conn=Depends(get_conn)):
    try:
        request_id, created = actions.apply_now(conn, job_id)
    except actions.JobNotFound:
        raise HTTPException(404, "job not found")
    except actions.TransitionError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "invalid_transition", "current": e.current, "target": e.target},
        )
    if created:
        await request.app.state.apply_dispatcher.dispatch(request_id)
    return {"request_id": request_id}


@router.post("/{job_id}/draft_followup")
def post_draft_followup(job_id: int, conn=Depends(get_conn)):
    name, phone, email = _personal()
    try:
        return followups.draft_followup(conn, job_id, name=name, phone=phone, email=email)
    except followups.FollowupNotFound:
        raise HTTPException(404, "application not found")
