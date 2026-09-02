# tests/test_api_settings.py
"""GET/PUT /api/settings: corruption status surfaced; saves are hermetic (spec §5.4)."""


def test_get_settings_defaults_when_missing(api_client):
    r = api_client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    # The fixture's settings_path does not exist yet -> defaults + 'missing'.
    assert body["status"] == "missing"
    assert body["settings"]["auto_apply"] is False
    assert body["settings"]["tailor_creativity"] == "balanced"


def test_put_settings_persists_and_round_trips(api_client):
    payload = {"auto_apply": True, "auto_apply_min_score": 90,
               "auto_apply_daily_cap": 3, "tailor_creativity": "bold"}
    r = api_client.put("/api/settings", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # The 4 auto-apply fields round-trip; the schema also carries copilot-backend
    # defaults, so compare on the keys we sent rather than for exact equality.
    assert {k: body["settings"][k] for k in payload} == payload
    # A fresh GET reads back exactly what we saved.
    again = api_client.get("/api/settings").json()
    assert again["status"] == "ok"
    assert {k: again["settings"][k] for k in payload} == payload
    # Hermetic: the write landed on the fixture's tmp path, not data/settings.json.
    assert api_client.settings_path.exists()


def test_put_settings_rejects_invalid_creativity(api_client):
    payload = {"auto_apply": False, "auto_apply_min_score": 80,
               "auto_apply_daily_cap": 5, "tailor_creativity": "wild"}
    r = api_client.put("/api/settings", json=payload)
    assert r.status_code == 422
    # save() validates before writing, so nothing was persisted.
    assert not api_client.settings_path.exists()


def test_put_settings_rejects_out_of_range_score(api_client):
    payload = {"auto_apply": False, "auto_apply_min_score": 250,
               "auto_apply_daily_cap": 5, "tailor_creativity": "balanced"}
    assert api_client.put("/api/settings", json=payload).status_code == 422


def test_put_settings_persists_ollama_backend(api_client):
    payload = {"auto_apply": False, "auto_apply_min_score": 85,
               "auto_apply_daily_cap": 5, "tailor_creativity": "balanced",
               "llm_backend": "ollama", "llm_base_url": "http://localhost:11434/v1",
               "llm_model": "qwen3:8b"}
    r = api_client.put("/api/settings", json=payload)
    assert r.status_code == 200
    again = api_client.get("/api/settings").json()["settings"]
    assert again["llm_backend"] == "ollama"
    assert again["llm_model"] == "qwen3:8b"


def test_put_settings_rejects_off_mandate_backend(api_client):
    payload = {"auto_apply": False, "auto_apply_min_score": 85,
               "auto_apply_daily_cap": 5, "tailor_creativity": "balanced",
               "llm_backend": "anthropic_api", "llm_base_url": "", "llm_model": ""}
    assert api_client.put("/api/settings", json=payload).status_code == 422
    assert not api_client.settings_path.exists()


def test_get_settings_reports_invalid_corruption(api_client):
    api_client.settings_path.write_text("{not valid json")
    body = api_client.get("/api/settings").json()
    assert body["status"] == "invalid"
    # Falls back to defaults so the UI still renders.
    assert body["settings"]["tailor_creativity"] == "balanced"


def test_put_settings_persists_schedule(api_client):
    payload = {"auto_apply": False, "auto_apply_min_score": 85,
               "auto_apply_daily_cap": 5, "tailor_creativity": "balanced",
               "schedule_enabled": False, "schedule_time": "07:15",
               "schedule_cadence": "weekdays"}
    r = api_client.put("/api/settings", json=payload)
    assert r.status_code == 200
    again = api_client.get("/api/settings").json()["settings"]
    assert again["schedule_enabled"] is False
    assert again["schedule_time"] == "07:15"
    assert again["schedule_cadence"] == "weekdays"


def test_put_settings_rejects_bad_schedule_time(api_client):
    payload = {"auto_apply": False, "auto_apply_min_score": 85,
               "auto_apply_daily_cap": 5, "tailor_creativity": "balanced",
               "schedule_enabled": True, "schedule_time": "25:00",
               "schedule_cadence": "daily"}
    assert api_client.put("/api/settings", json=payload).status_code == 422
    assert not api_client.settings_path.exists()
