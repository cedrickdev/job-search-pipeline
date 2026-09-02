"""Tests for pipeline.apply._common — platform detection, answer loading, result dataclass."""
import os
import tempfile
from pathlib import Path

import pytest
import yaml

from pipeline.apply._common import ApplyResult, detect_platform, load_answers


class TestDetectPlatform:
    def test_linkedin(self):
        assert detect_platform("https://www.linkedin.com/jobs/view/123") == "linkedin"

    def test_wtj(self):
        assert detect_platform("https://www.welcometothejungle.com/en/companies/acme/jobs/ds") == "wtj"

    def test_greenhouse(self):
        assert detect_platform("https://boards.greenhouse.io/acme/jobs/123") == "greenhouse"

    def test_greenhouse_short(self):
        assert detect_platform("https://grnh.se/abc123") == "greenhouse"

    def test_greenhouse_job_boards(self):
        assert detect_platform("https://job-boards.greenhouse.io/globex/jobs/4127339002") == "greenhouse"

    def test_lever(self):
        assert detect_platform("https://jobs.lever.co/acme/123-senior-ds") == "lever"

    def test_ashby(self):
        assert detect_platform("https://jobs.ashbyhq.com/acme/123") == "ashby"

    def test_generic_fallback(self):
        assert detect_platform("https://careers.example.com/jobs/123") == "generic"

    def test_empty_url(self):
        assert detect_platform("") == "generic"

    def test_none_like_empty(self):
        assert detect_platform("https://unknown-ats.io/apply") == "generic"


class TestLoadAnswers:
    def test_loads_from_path(self, tmp_path):
        data = {
            "personal": {"name": "Test Person", "email": "test@example.com"},
            "salary": {"min": 22, "max": 28},
        }
        f = tmp_path / "answers.yaml"
        f.write_text(yaml.dump(data))
        result = load_answers(str(f))
        assert result["personal"]["name"] == "Test Person"
        assert result["salary"]["min"] == 22

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_answers(str(tmp_path / "nonexistent.yaml"))

    def test_returns_dict(self, tmp_path):
        f = tmp_path / "answers.yaml"
        f.write_text(yaml.dump({"key": "value"}))
        result = load_answers(str(f))
        assert isinstance(result, dict)


class TestApplyResult:
    def test_applied_status(self):
        r = ApplyResult(status="Applied", detail="Confirmed at URL")
        assert r.status == "Applied"
        assert r.detail == "Confirmed at URL"
        assert r.screenshot_path is None
        assert r.channel == "applier"

    def test_needs_you_status(self):
        r = ApplyResult(status="Needs you", detail="Login required")
        assert r.status == "Needs you"

    def test_failed_status(self):
        r = ApplyResult(status="Failed", detail="Timeout", screenshot_path="/tmp/shot.png")
        assert r.status == "Failed"
        assert r.screenshot_path == "/tmp/shot.png"

    def test_custom_channel(self):
        r = ApplyResult(status="Applied", detail="ok", channel="linkedin")
        assert r.channel == "linkedin"
