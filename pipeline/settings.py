"""Auto-apply settings in data/settings.json. Absent file means defaults."""
import argparse
import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from pipeline import paths

CREATIVITY_LEVELS = ("conservative", "balanced", "bold")

# Which model serves the copilot. "claude_cli" is the local Claude Code binary
# (no API key). "ollama"/"lmstudio" are local model servers reached over their
# OpenAI-compatible HTTP endpoint. There is deliberately NO remote-API option:
# the tool stays on-machine and never sees an Anthropic key.
LLM_BACKENDS = ("claude_cli", "ollama", "lmstudio")

SCHEDULE_CADENCES = ("daily", "weekdays")
_SCHEDULE_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DEFAULTS = {
    "auto_apply": False,
    "auto_apply_min_score": 85,
    "auto_apply_daily_cap": 5,
    "tailor_creativity": "balanced",
    "llm_backend": "claude_cli",
    "llm_base_url": "",   # empty -> use the backend's conventional localhost endpoint
    "llm_model": "",      # empty -> use the backend's default model
    "schedule_enabled": True,        # in-process daily scheduler on/off
    "schedule_time": "08:00",        # 24h local HH:MM the full run fires
    "schedule_cadence": "daily",     # "daily" | "weekdays"
}


def validate(settings: dict) -> None:
    if not isinstance(settings["auto_apply"], bool):
        raise ValueError("auto_apply must be true or false")
    floor = settings["auto_apply_min_score"]
    if isinstance(floor, bool) or not isinstance(floor, int) or not 0 <= floor <= 100:
        raise ValueError("auto_apply_min_score must be an integer from 0 to 100")
    cap = settings["auto_apply_daily_cap"]
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ValueError("auto_apply_daily_cap must be an integer of at least 1")
    creativity = settings["tailor_creativity"]
    if creativity not in CREATIVITY_LEVELS:
        raise ValueError(
            f"tailor_creativity must be one of {CREATIVITY_LEVELS}, got {creativity!r}"
        )
    backend = settings["llm_backend"]
    if backend not in LLM_BACKENDS:
        raise ValueError(
            f"llm_backend must be one of {LLM_BACKENDS}, got {backend!r}"
        )
    base_url = settings["llm_base_url"]
    if not isinstance(base_url, str):
        raise ValueError("llm_base_url must be a string")
    _validate_local_url(base_url)
    if not isinstance(settings["llm_model"], str):
        raise ValueError("llm_model must be a string")
    if not isinstance(settings["schedule_enabled"], bool):
        raise ValueError("schedule_enabled must be true or false")
    schedule_time = settings["schedule_time"]
    if not isinstance(schedule_time, str) or not _SCHEDULE_TIME_RE.match(schedule_time):
        raise ValueError("schedule_time must be a 24-hour HH:MM string")
    cadence = settings["schedule_cadence"]
    if cadence not in SCHEDULE_CADENCES:
        raise ValueError(
            f"schedule_cadence must be one of {SCHEDULE_CADENCES}, got {cadence!r}"
        )


def _validate_local_url(base_url: str) -> None:
    """An empty URL means "use the backend's default localhost endpoint". A
    non-empty URL must be an http(s) endpoint ON THIS MACHINE (localhost or a
    loopback address). The tool is local-only and must never ship job data
    off-box — the same reason no remote-API backend is offered — so a non-local
    host (including link-local metadata addresses) is rejected outright."""
    if not base_url:
        return
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("llm_base_url must be an http(s) URL or empty")
    host = urlparse(base_url).hostname
    if not host:
        raise ValueError("llm_base_url must include a hostname")
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass  # not an IP literal and not "localhost" -> not a recognized local host
    raise ValueError(
        "llm_base_url must be a local endpoint (localhost or a loopback address); "
        "the tool keeps all job data on-machine"
    )


def load_with_status(path: str | Path | None = None) -> tuple[dict, str]:
    """Return (settings, status). Status: 'ok', 'missing', or 'invalid'.
    On 'missing' or 'invalid' the settings are the defaults."""
    path = Path(path) if path is not None else paths.SETTINGS_PATH
    if not path.exists():
        return dict(DEFAULTS), "missing"
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("settings root must be a JSON object")
        merged = dict(DEFAULTS)
        merged.update({key: raw[key] for key in DEFAULTS if key in raw})
        validate(merged)
        return merged, "ok"
    except (json.JSONDecodeError, ValueError):
        return dict(DEFAULTS), "invalid"


def load(path: str | Path | None = None) -> dict:
    return load_with_status(path)[0]


def save(settings: dict, path: str | Path | None = None) -> None:
    merged = dict(DEFAULTS)
    merged.update({key: settings[key] for key in DEFAULTS if key in settings})
    validate(merged)
    path = Path(path) if path is not None else paths.SETTINGS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline settings")
    parser.add_argument("--show", action="store_true", required=True,
                        help="print current settings as JSON")
    args = parser.parse_args()
    if args.show:
        settings, status = load_with_status()
        print(json.dumps({"settings": settings, "status": status}, indent=2))


if __name__ == "__main__":
    main()
