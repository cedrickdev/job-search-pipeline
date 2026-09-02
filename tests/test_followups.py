"""Follow-up engine (spec §9): due_followups, set_override, draft_followup.

Timing is driven by an explicit `now` so the tests are deterministic.
"""
import json
from datetime import datetime

from server import followups
from tests.helpers import seed_job

NOW = datetime(2026, 6, 16, 8, 0, 0)


def _followup_sent(conn, application_id, job_id, created_at):
    """Insert an outbound follow-up marker (mirrors gmail_recruiter.log_event)."""
    conn.execute(
        "INSERT INTO events (application_id, job_id, event_type, detail, source,"
        " created_at) VALUES (?, ?, 'followup_sent', 'Sent', 'gmail-recruiter', ?)",
        (application_id, job_id, created_at))
    conn.commit()


# --- due_followups: applied_no_reply --------------------------------------

def test_applied_past_threshold_with_no_reply_is_due(conn):
    job_id, app_id = seed_job(conn, company="Globex", title="Shift Lead",
                              status="Applied", applied_at="2026-06-05T09:00:00")
    due = followups.due_followups(conn, now=NOW)
    assert len(due) == 1
    item = due[0]
    assert item["kind"] == "applied_no_reply"
    assert item["application_id"] == app_id
    assert item["job_id"] == job_id
    assert item["company"] == "Globex"
    assert item["title"] == "Shift Lead"
    assert item["days"] == 11
    assert item["since"] == "2026-06-05"


def test_applied_within_threshold_is_not_due(conn):
    seed_job(conn, company="Fresh", status="Applied",
             applied_at="2026-06-15T09:00:00")          # 1 day -> under 7
    assert followups.due_followups(conn, now=NOW) == []


# --- due_followups: recruiter_no_outbound ---------------------------------

def test_recruiter_reply_with_no_outbound_is_due(conn):
    job_id, app_id = seed_job(conn, company="Initech", title="Sales Assistant",
                              status="Recruiter reply",
                              created_at="2026-06-12T09:00:00")  # 4 days ago
    due = followups.due_followups(conn, now=NOW)
    assert len(due) == 1
    item = due[0]
    assert item["kind"] == "recruiter_no_outbound"
    assert item["application_id"] == app_id
    assert item["days"] == 4
    assert item["since"] == "2026-06-12"


def test_recruiter_reply_within_threshold_is_not_due(conn):
    seed_job(conn, company="Initech", status="Recruiter reply",
             created_at="2026-06-15T20:00:00")          # < 2 days ago
    assert followups.due_followups(conn, now=NOW) == []


def test_outbound_after_reply_clears_recruiter_followup(conn):
    job_id, app_id = seed_job(conn, company="Initech", status="Recruiter reply",
                              created_at="2026-06-12T09:00:00")
    _followup_sent(conn, app_id, job_id, "2026-06-13T09:00:00")  # after reply
    assert followups.due_followups(conn, now=NOW) == []


# --- overrides ------------------------------------------------------------

def test_dismiss_hides_a_due_followup(conn):
    job_id, app_id = seed_job(conn, company="Globex", status="Applied",
                              applied_at="2026-06-05T09:00:00")
    followups.set_override(conn, app_id, dismissed=True, now=NOW)
    assert followups.due_followups(conn, now=NOW) == []


def test_future_snooze_hides_then_expires(conn):
    job_id, app_id = seed_job(conn, company="Globex", status="Applied",
                              applied_at="2026-06-05T09:00:00")
    followups.set_override(conn, app_id, snooze_until="2026-06-20T00:00:00", now=NOW)
    assert followups.due_followups(conn, now=NOW) == []
    # A clock past the snooze window surfaces it again.
    later = datetime(2026, 6, 21, 8, 0, 0)
    assert len(followups.due_followups(conn, now=later)) == 1


def test_set_override_merges_partial_updates(conn):
    job_id, app_id = seed_job(conn, company="Globex", status="Applied",
                              applied_at="2026-06-05T09:00:00")
    followups.set_override(conn, app_id, snooze_until="2026-06-20T00:00:00", now=NOW)
    followups.set_override(conn, app_id, dismissed=True, now=NOW)  # keep snooze
    row = conn.execute(
        "SELECT snooze_until, dismissed FROM followup_overrides WHERE application_id=?",
        (app_id,)).fetchone()
    assert row["snooze_until"] == "2026-06-20T00:00:00"
    assert row["dismissed"] == 1


# --- ordering -------------------------------------------------------------

def test_due_followups_sorted_most_overdue_first(conn):
    seed_job(conn, company="Older", status="Applied",
             applied_at="2026-06-01T09:00:00")          # 15 days
    seed_job(conn, company="Newer", status="Applied",
             applied_at="2026-06-07T09:00:00")          # 9 days
    due = followups.due_followups(conn, now=NOW)
    assert [d["company"] for d in due] == ["Older", "Newer"]


# --- draft_followup -------------------------------------------------------

def test_draft_followup_fills_template_and_clears_gate(conn, monkeypatch, tmp_path):
    from pipeline import paths
    cfg = tmp_path / "mandate.json"
    cfg.write_text(json.dumps({"forbidden": [], "aliases": {}}), encoding="utf-8")
    monkeypatch.setattr(paths, "MANDATE_CONFIG", cfg)

    job_id, app_id = seed_job(conn, company="Globex", title="Shift Lead",
                              language="en", status="Applied",
                              applied_at="2026-06-05T09:00:00")
    draft = followups.draft_followup(conn, job_id, phone="+33 6", email="me@x.io")
    assert "Shift Lead" in draft["subject"]
    assert "Globex" in draft["subject"]
    assert "Shift Lead" in draft["body"]
    assert "Globex" in draft["body"]
    assert "2026-06-05" in draft["body"]
    # Signature block filled from the caller, not from the template.
    assert "+33 6" in draft["body"]
    assert "me@x.io" in draft["body"]
    # No unresolved placeholder survived the format() call.
    assert "{" not in draft["body"]
    assert draft["language"] == "en"
    assert draft["mandate_ok"] is True
    assert draft["flags"] == []


def test_draft_followup_flags_forbidden_client_name(conn, monkeypatch, tmp_path):
    from pipeline import paths
    cfg = tmp_path / "mandate.json"
    cfg.write_text(json.dumps({"forbidden": ["Globex"], "aliases": {}}),
                   encoding="utf-8")
    monkeypatch.setattr(paths, "MANDATE_CONFIG", cfg)

    job_id, _ = seed_job(conn, company="Globex", title="Shift Lead",
                         language="en", status="Applied",
                         applied_at="2026-06-05T09:00:00")
    draft = followups.draft_followup(conn, job_id, phone="+33 6", email="me@x.io")
    assert draft["mandate_ok"] is False
    assert "forbidden_client:Globex" in draft["flags"]


def _mandate_cfg(monkeypatch, tmp_path):
    from pipeline import paths
    cfg = tmp_path / "mandate.json"
    cfg.write_text(json.dumps({"forbidden": [], "aliases": {}}), encoding="utf-8")
    monkeypatch.setattr(paths, "MANDATE_CONFIG", cfg)


def test_draft_followup_omits_date_clause_when_no_submission_date(conn, monkeypatch, tmp_path):
    # Imported/migrated applications carry no submitted_at; the draft must not
    # render a dangling "submitted on ." clause.
    _mandate_cfg(monkeypatch, tmp_path)
    job_id, _ = seed_job(conn, company="Initech", title="Analyst",
                         language="en", status="Recruiter reply")
    draft = followups.draft_followup(conn, job_id)
    assert "at Initech." in draft["body"]
    assert "submitted on" not in draft["body"]


def test_draft_followup_fr_omits_date_clause_when_no_submission_date(conn, monkeypatch, tmp_path):
    _mandate_cfg(monkeypatch, tmp_path)
    job_id, _ = seed_job(conn, company="Initech", title="Analyste",
                         language="fr", status="Recruiter reply")
    draft = followups.draft_followup(conn, job_id)
    assert "chez Initech." in draft["body"]
    assert "déposée le" not in draft["body"]
