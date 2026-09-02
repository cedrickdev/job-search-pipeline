"""Humanization gate: deterministic scan for AI-sounding phrasing.
Banned list lives in cv/style_rules.yaml and is user-editable."""
import yaml

from pipeline import paths


def load_style_rules() -> list[str]:
    with open(paths.STYLE_RULES_PATH) as f:
        data = yaml.safe_load(f) or {}
    return data.get("banned_substrings", [])


def style_violations(text: str, banned: list[str]) -> list[str]:
    lowered = text.lower()
    return [b for b in banned if b.lower() in lowered]
