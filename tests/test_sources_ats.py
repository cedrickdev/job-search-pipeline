import json
from pathlib import Path

import pytest

from pipeline.jobs import JOB_COLUMNS
from pipeline.sources import greenhouse
from pipeline.sources._common import normalize, strip_html

FIXTURES = Path(__file__).parent / "fixtures"


def test_normalize_fills_all_job_columns():
    job = normalize(source="x", company="Acme", title="Sales")
    assert set(job) == set(JOB_COLUMNS)
    assert job["salary"] is None


def test_normalize_rejects_unknown_columns():
    with pytest.raises(ValueError):
        normalize(compant="Acme")


def test_strip_html_flattens_markup():
    assert strip_html("<p>Sell <b>fresh</b> produce.</p>") == "Sell fresh produce."


def test_greenhouse_fetch_jobs(monkeypatch):
    payload = json.loads((FIXTURES / "greenhouse_jobs.json").read_text())
    monkeypatch.setattr(greenhouse, "fetch_json", lambda url, **kw: payload)
    jobs = greenhouse.fetch_jobs("acme", "Acme")
    assert len(jobs) == 2
    first = jobs[0]
    assert first["source"] == "greenhouse"
    assert first["company"] == "Acme"
    assert first["title"] == "Senior Sales Assistant"
    assert first["url"] == "https://boards.greenhouse.io/acme/jobs/101"
    assert first["location"] == "Lausanne, Suisse"
    assert "Sell fresh produce for" in first["description"]
    assert first["posted_date"] == "2026-06-01"


def test_lever_fetch_jobs(monkeypatch):
    from pipeline.sources import lever
    payload = json.loads((FIXTURES / "lever_postings.json").read_text())
    monkeypatch.setattr(lever, "fetch_json", lambda url, **kw: payload)
    jobs = lever.fetch_jobs("acme", "Acme")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["source"] == "lever"
    assert job["title"] == "Sales Assistant, Senior"
    assert job["url"] == "https://jobs.lever.co/acme/abc-123"
    assert job["location"] == "Lausanne"
    assert job["remote_policy"] == "hybrid"
    assert job["contract_type"] == "Full-time"
    assert job["description"] == "Own experimentation end to end."
    assert job["posted_date"] == "2025-06-01"


def test_ashby_fetch_jobs(monkeypatch):
    from pipeline.sources import ashby
    payload = json.loads((FIXTURES / "ashby_jobs.json").read_text())
    monkeypatch.setattr(ashby, "fetch_json", lambda url, **kw: payload)
    jobs = ashby.fetch_jobs("acme", "Acme")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["source"] == "ashby"
    assert job["title"] == "Shift Supervisor"
    assert job["url"] == "https://jobs.ashbyhq.com/acme/uuid-1"
    assert job["remote_policy"] is None
    assert job["contract_type"] == "FullTime"
    assert job["salary"] == "22-26 CHF/h"
    assert job["description"] == "Stack the shelves."
    assert job["posted_date"] == "2026-06-02"
