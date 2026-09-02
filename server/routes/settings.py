"""Auto-apply settings read/write (spec §5.4). Path threaded for hermetic tests."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from pipeline import settings as settings_store

router = APIRouter(prefix="/api")


class SettingsBody(BaseModel):
    auto_apply: bool
    auto_apply_min_score: int
    auto_apply_daily_cap: int
    tailor_creativity: str
    # Copilot model backend. Defaults keep older 4-field payloads valid; the UI
    # always sends the full object so a settings save never silently resets it.
    llm_backend: str = "claude_cli"
    llm_base_url: str = ""
    llm_model: str = ""
    # In-app scheduler. Defaults keep older payloads valid;
    # pipeline.settings.validate() is the single source of truth for the values.
    schedule_enabled: bool = True
    schedule_time: str = "08:00"
    schedule_cadence: str = "daily"


@router.get("/settings")
def get_settings(request: Request):
    current, status = settings_store.load_with_status(request.app.state.settings_path)
    return {"settings": current, "status": status}


@router.put("/settings")
def put_settings(body: SettingsBody, request: Request):
    try:
        settings_store.save(body.model_dump(), request.app.state.settings_path)
    except ValueError as exc:
        # pipeline.settings.validate rejected the candidate before any write.
        raise HTTPException(status_code=422, detail=str(exc))
    current, status = settings_store.load_with_status(request.app.state.settings_path)
    return {"settings": current, "status": status}
