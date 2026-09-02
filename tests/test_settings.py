import json

import pytest

from pipeline.settings import DEFAULTS, load, load_with_status, save


def test_defaults_when_file_missing(tmp_path):
    settings, status = load_with_status(tmp_path / "settings.json")
    assert settings == {"auto_apply": False, "auto_apply_min_score": 85,
                        "auto_apply_daily_cap": 5, "tailor_creativity": "balanced",
                        "llm_backend": "claude_cli", "llm_base_url": "", "llm_model": "",
                        "schedule_enabled": True, "schedule_time": "08:00",
                        "schedule_cadence": "daily"}
    assert status == "missing"


def test_save_load_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    save({"auto_apply": True, "auto_apply_min_score": 90,
          "auto_apply_daily_cap": 3}, path)
    settings, status = load_with_status(path)
    assert status == "ok"
    assert settings["auto_apply"] is True
    assert settings["auto_apply_min_score"] == 90
    assert settings["auto_apply_daily_cap"] == 3


def test_load_ignores_unknown_keys(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"auto_apply": True, "mystery": 1}))
    settings, status = load_with_status(path)
    assert status == "ok"
    assert "mystery" not in settings
    assert settings["auto_apply_min_score"] == 85


@pytest.mark.parametrize("bad", [
    {"auto_apply_min_score": 101},
    {"auto_apply_min_score": -1},
    {"auto_apply_daily_cap": 0},
    {"auto_apply": "yes"},
    {"auto_apply_min_score": True},
])
def test_save_rejects_invalid_values(tmp_path, bad):
    path = tmp_path / "settings.json"
    candidate = dict(DEFAULTS)
    candidate.update(bad)
    with pytest.raises(ValueError):
        save(candidate, path)
    assert not path.exists()


def test_corrupt_json_falls_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    settings, status = load_with_status(path)
    assert settings == DEFAULTS
    assert status == "invalid"


def test_invalid_values_in_file_fall_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"auto_apply_min_score": 250}))
    settings, status = load_with_status(path)
    assert settings == DEFAULTS
    assert status == "invalid"


def test_load_returns_settings_only(tmp_path):
    assert load(tmp_path / "settings.json") == DEFAULTS


# --- copilot model backend (local CLI / Ollama / LM Studio) ----------------

def test_defaults_include_local_claude_backend(tmp_path):
    settings, _ = load_with_status(tmp_path / "settings.json")
    assert settings["llm_backend"] == "claude_cli"
    assert settings["llm_base_url"] == ""
    assert settings["llm_model"] == ""


def test_save_load_ollama_backend(tmp_path):
    path = tmp_path / "settings.json"
    save({**DEFAULTS, "llm_backend": "ollama",
          "llm_base_url": "http://localhost:11434/v1", "llm_model": "qwen3:8b"}, path)
    settings, status = load_with_status(path)
    assert status == "ok"
    assert settings["llm_backend"] == "ollama"
    assert settings["llm_base_url"] == "http://localhost:11434/v1"
    assert settings["llm_model"] == "qwen3:8b"


@pytest.mark.parametrize("bad", [
    {"llm_backend": "anthropic_api"},   # remote API is off-mandate -> rejected
    {"llm_backend": "openai"},
    {"llm_base_url": "ftp://localhost"},
    {"llm_base_url": 123},
    {"llm_model": 5},
    {"llm_base_url": "http://example.com:11434/v1"},  # off-box host -> rejected
    {"llm_base_url": "http://169.254.169.254/latest"},  # link-local metadata target
])
def test_save_rejects_invalid_backend(tmp_path, bad):
    candidate = dict(DEFAULTS)
    candidate.update(bad)
    with pytest.raises(ValueError):
        save(candidate, tmp_path / "settings.json")


@pytest.mark.parametrize("url", [
    "", "http://localhost:11434/v1", "http://127.0.0.1:1234/v1", "http://[::1]:11434/v1",
])
def test_save_accepts_local_endpoints(tmp_path, url):
    path = tmp_path / "settings.json"
    save({**DEFAULTS, "llm_backend": "ollama", "llm_base_url": url}, path)
    assert load_with_status(path)[1] == "ok"


# --- in-app scheduler (manual trigger + daily schedule) --------------------

def test_defaults_include_schedule(tmp_path):
    settings, _ = load_with_status(tmp_path / "settings.json")
    assert settings["schedule_enabled"] is True
    assert settings["schedule_time"] == "08:00"
    assert settings["schedule_cadence"] == "daily"


def test_save_load_schedule_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    save({**DEFAULTS, "schedule_enabled": False, "schedule_time": "07:30",
          "schedule_cadence": "weekdays"}, path)
    settings, status = load_with_status(path)
    assert status == "ok"
    assert settings["schedule_enabled"] is False
    assert settings["schedule_time"] == "07:30"
    assert settings["schedule_cadence"] == "weekdays"


@pytest.mark.parametrize("bad", [
    {"schedule_enabled": "yes"},
    {"schedule_time": "25:00"},
    {"schedule_time": "8:0"},
    {"schedule_time": 800},
    {"schedule_cadence": "hourly"},
])
def test_save_rejects_invalid_schedule(tmp_path, bad):
    candidate = dict(DEFAULTS)
    candidate.update(bad)
    with pytest.raises(ValueError):
        save(candidate, tmp_path / "settings.json")


@pytest.mark.parametrize("schedule_time", ["00:00", "08:00", "20:00", "23:59"])
def test_save_accepts_valid_schedule_times(tmp_path, schedule_time):
    path = tmp_path / "settings.json"
    save({**DEFAULTS, "schedule_time": schedule_time}, path)
    settings, status = load_with_status(path)
    assert status == "ok"
    assert settings["schedule_time"] == schedule_time
