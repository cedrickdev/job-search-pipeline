"""Compose launches the scheduler as a general, non-browser process."""

from pathlib import Path
import json
import os
import shutil
import subprocess

import pytest

import yaml


def test_scheduler_is_worker_profile_without_browser_privileges() -> None:
    config = yaml.safe_load(Path("docker-compose.yml").read_text())
    scheduler = config["services"]["morning-scheduler"]
    worker = config["services"]["worker"]
    assert scheduler["profiles"] == ["workers"]
    assert scheduler["build"] == worker["build"]
    assert scheduler["environment"]["DATABASE_URL"] == worker["environment"]["DATABASE_URL"]
    assert scheduler["command"] == ["python", "-m", "backend.app.cli.morning_scheduler"]
    assert "volumes" not in scheduler
    assert all("ANTHROPIC" not in key and "TOKEN" not in key
               for key in scheduler["environment"])


def test_verification_project_can_isolate_every_named_volume() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose unavailable")
    env = {**os.environ, "JOBSEARCH_VOLUME_PREFIX": "jobsearch_morning_verify"}
    result = subprocess.run(
        ["docker", "compose", "-p", "jobsearch-morning-verify", "config", "--format", "json"],
        check=True, capture_output=True, text=True, env=env,
    )
    config = json.loads(result.stdout)
    for volume in ("postgres_data", "api_data", "app_var", "frontend_node_modules", "frontend_nuxt"):
        assert config["volumes"][volume]["name"] == f"jobsearch_morning_verify_{volume}"
