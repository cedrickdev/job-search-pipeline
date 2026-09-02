"""Cheap keyword scope filter. Title-only: geography and contract nuance are
judged by Claude at scoring; this gate just keeps obvious noise out of the DB."""
import yaml

from pipeline import paths


def load_searches() -> dict:
    with open(paths.SEARCHES_PATH) as f:
        return yaml.safe_load(f)


def matches_scope(job: dict, cfg: dict) -> bool:
    title = (job.get("title") or "").lower()
    if not title:
        return False
    if not any(kw in title for kw in cfg.get("title_keywords", [])):
        return False
    if any(kw in title for kw in cfg.get("exclude_keywords", [])):
        return False
    return True
