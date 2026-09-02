"""Prep store: notes upsert, prep cache, interview log (spec §4 tables)."""
from datetime import datetime

from pipeline.db import connect
from server import prep_store
from server.actions import resolve_application_id
from tests.helpers import seed_job

NOW = datetime(2026, 6, 11, 9, 0, 0)


def _conn(api_client):
    c = connect(api_client.db_path)
    c.execute("PRAGMA busy_timeout = 5000")
    return c


def test_notes_upsert_roundtrip(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    app_id = resolve_application_id(conn, job_id, create=True)
    prep_store.upsert_notes(conn, app_id, "first", now=NOW)
    assert prep_store.get_prep(conn, app_id)["notes_md"] == "first"
    prep_store.upsert_notes(conn, app_id, "second", now=NOW)
    assert prep_store.get_prep(conn, app_id)["notes_md"] == "second"  # updated, not duplicated


def test_prep_cache_set_and_read(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    app_id = resolve_application_id(conn, job_id, create=True)
    prep_store.set_prep_cache(
        conn, app_id,
        likely_questions=["Why us?"], company_research=["Founded 2015"],
        talking_points=["Led ETL"], now=NOW)
    prep = prep_store.get_prep(conn, app_id)
    assert prep["likely_questions"] == ["Why us?"]
    assert prep["company_research"] == ["Founded 2015"]
    assert prep["talking_points"] == ["Led ETL"]
    assert prep["generated_at"] == NOW.isoformat()


def test_prep_defaults_when_empty(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    app_id = resolve_application_id(conn, job_id, create=True)
    prep = prep_store.get_prep(conn, app_id)
    assert prep["notes_md"] == ""
    assert prep["likely_questions"] is None
    assert prep["interviews"] == []


def test_interview_log_add_update_list(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    app_id = resolve_application_id(conn, job_id, create=True)
    iid = prep_store.add_interview(
        conn, app_id, round_label="Phone screen",
        scheduled_for="2026-06-15T14:00:00", notes="30 min", now=NOW)
    rows = prep_store.list_interviews(conn, app_id)
    assert len(rows) == 1 and rows[0]["round_label"] == "Phone screen"
    prep_store.update_interview(conn, iid, outcome="passed", notes="went well")
    updated = prep_store.list_interviews(conn, app_id)[0]
    assert updated["outcome"] == "passed" and updated["notes"] == "went well"


def test_upcoming_interviews_excludes_past_and_outcome(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, company="Beta")
    app_id = resolve_application_id(conn, job_id, create=True)
    prep_store.add_interview(conn, app_id, round_label="Future", scheduled_for="2026-06-20T10:00:00", notes=None, now=NOW)
    prep_store.add_interview(conn, app_id, round_label="Past", scheduled_for="2026-06-01T10:00:00", notes=None, now=NOW)
    done = prep_store.add_interview(conn, app_id, round_label="Done", scheduled_for="2026-06-25T10:00:00", notes=None, now=NOW)
    prep_store.update_interview(conn, done, outcome="passed")
    up = prep_store.upcoming_interviews(conn, now=NOW)
    labels = {r["round_label"] for r in up}
    assert labels == {"Future"}  # past excluded, decided excluded
    assert up[0]["company"] == "Beta"
