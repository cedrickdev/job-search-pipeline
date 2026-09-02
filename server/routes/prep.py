"""Prep workspace + interview endpoints."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from pipeline import settings as settings_store
from server import chat, prep_gen, prep_store
from server.actions import JobNotFound, resolve_application_id
from server.deps import get_conn

router = APIRouter(prefix="/api")


class NotesBody(BaseModel):
    notes_md: str


class InterviewBody(BaseModel):
    round_label: str
    scheduled_for: str | None = None
    notes: str | None = None


class InterviewPatch(BaseModel):
    round_label: str | None = None
    scheduled_for: str | None = None
    outcome: str | None = None
    notes: str | None = None


def _now(value: str | None) -> datetime:
    return datetime.fromisoformat(value) if value else datetime(2026, 6, 11, 9, 0, 0)


@router.get("/jobs/{job_id}/prep")
def get_prep(job_id: int, conn=Depends(get_conn)):
    try:
        app_id = resolve_application_id(conn, job_id, create=True)
    except JobNotFound:
        raise HTTPException(404, "job not found")
    return prep_store.get_prep(conn, app_id)


@router.post("/jobs/{job_id}/prep/generate")
async def generate_prep(job_id: int, request: Request, now: str | None = Query(None),
                        conn=Depends(get_conn)):
    """Have the copilot draft interview prep, run it through the §6.2 mandate gate,
    and cache it. Fails closed: a draft that doesn't pass the gate is returned to
    the caller (with mandate_ok=False + flags) but NOT persisted, so a later GET
    never resurfaces unverified prose as ready."""
    try:
        app_id = resolve_application_id(conn, job_id, create=True)
    except JobNotFound:
        raise HTTPException(404, "job not found")

    prompt = prep_gen.build_prep_prompt(chat.build_context(conn, "job", job_id))
    llm_settings = settings_store.load(request.app.state.settings_path)
    result = await chat.collect_turn(prompt, settings=llm_settings)
    fields = prep_gen.parse_prep_reply(result["text"])
    when = _now(now)

    if result["mandate_ok"]:
        prep_store.set_prep_cache(
            conn, app_id,
            likely_questions=fields["likely_questions"],
            company_research=fields["company_research"],
            talking_points=fields["talking_points"],
            now=when)

    prep = prep_store.get_prep(conn, app_id)
    # Always surface the fresh draft; generated_at is None when unverified so the
    # UI shows it as a draft, not a saved/ready artifact.
    prep.update(
        likely_questions=fields["likely_questions"],
        company_research=fields["company_research"],
        talking_points=fields["talking_points"],
        generated_at=when.isoformat() if result["mandate_ok"] else None,
        mandate_ok=result["mandate_ok"],
        flags=result["flags"])
    return prep


@router.put("/jobs/{job_id}/prep/notes")
def put_notes(job_id: int, body: NotesBody, now: str | None = Query(None), conn=Depends(get_conn)):
    try:
        app_id = resolve_application_id(conn, job_id, create=True)
    except JobNotFound:
        raise HTTPException(404, "job not found")
    prep_store.upsert_notes(conn, app_id, body.notes_md, now=_now(now))
    return {"ok": True}


@router.post("/jobs/{job_id}/interviews")
def add_interview(job_id: int, body: InterviewBody, now: str | None = Query(None), conn=Depends(get_conn)):
    try:
        app_id = resolve_application_id(conn, job_id, create=True)
    except JobNotFound:
        raise HTTPException(404, "job not found")
    iid = prep_store.add_interview(
        conn, app_id, round_label=body.round_label,
        scheduled_for=body.scheduled_for, notes=body.notes, now=_now(now))
    return {"id": iid}


@router.patch("/interviews/{interview_id}")
def patch_interview(interview_id: int, body: InterviewPatch, conn=Depends(get_conn)):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    prep_store.update_interview(conn, interview_id, **fields)
    return {"ok": True}
