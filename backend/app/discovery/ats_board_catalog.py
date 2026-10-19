"""Startup snapshot of reviewed Swiss ATS boards; never falls back to V1 config."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

import yaml

ATS_REVIEWED_BOARDS_PATH = Path(__file__).resolve().parents[3] / "config/ats_reviewed_boards.yaml"
_SOURCE_KEYS = ("greenhouse", "lever")
_REVIEW_MAX_AGE = timedelta(days=180)


class ReviewedAtsCatalogError(ValueError):
    """Invalid or missing operator review; no ATS feed may be used."""


@dataclass(frozen=True)
class ReviewedBoard:
    token: str
    company: str
    source_key: Literal["greenhouse", "lever"]
    country: Literal["CH"]
    provenance_url: str
    conditions_url: str
    access_decision: Literal["approved", "hold"]
    reviewed_at: date
    request_budget_per_day: int


@dataclass(frozen=True)
class ReviewedAtsCatalog:
    version: int
    boards: Mapping[str, tuple[ReviewedBoard, ...]]
    reviews: tuple[ReviewedBoard, ...]


def _valid_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _parse_board(raw: object, source_key: str, today: date) -> ReviewedBoard:
    if not isinstance(raw, dict) or set(raw) != {
        "token", "company", "country", "provenance_url", "conditions_url",
        "reviewed_at", "access_decision", "request_budget_per_day",
    }:
        raise ReviewedAtsCatalogError("ATS board review has invalid fields")
    token = raw["token"]
    company = raw["company"]
    if not isinstance(token, str) or not 1 <= len(token) <= 128 or not token.isascii() \
            or not all(char.isalnum() or char in "-_" for char in token):
        raise ReviewedAtsCatalogError("ATS board token is invalid")
    if not isinstance(company, str) or not 1 <= len(company.strip()) <= 200:
        raise ReviewedAtsCatalogError("ATS company name is invalid")
    if raw["country"] != "CH":
        raise ReviewedAtsCatalogError("ATS board must be Swiss")
    if not _valid_url(raw["provenance_url"]) or not _valid_url(raw["conditions_url"]):
        raise ReviewedAtsCatalogError("ATS review URLs must be public HTTPS URLs")
    if raw["access_decision"] not in ("approved", "hold"):
        raise ReviewedAtsCatalogError("ATS access decision is invalid")
    reviewed = raw["reviewed_at"]
    try:
        reviewed_date = (reviewed if isinstance(reviewed, date) else
                         date.fromisoformat(reviewed) if isinstance(reviewed, str) else None)
    except ValueError as error:
        raise ReviewedAtsCatalogError("ATS review date is invalid") from error
    if reviewed_date is None or not today - _REVIEW_MAX_AGE <= reviewed_date <= today:
        raise ReviewedAtsCatalogError("ATS board review is missing or stale")
    budget = raw["request_budget_per_day"]
    if type(budget) is not int or not 1 <= budget <= 100:
        raise ReviewedAtsCatalogError("ATS daily request budget is invalid")
    return ReviewedBoard(
        token=token, company=company.strip(), source_key=source_key,  # type: ignore[arg-type]
        country="CH", provenance_url=raw["provenance_url"],
        conditions_url=raw["conditions_url"],
        access_decision=raw["access_decision"], reviewed_at=reviewed_date,
        request_budget_per_day=budget,
    )


def load_reviewed_boards(path: Path) -> ReviewedAtsCatalog:
    """Parse a strict, immutable snapshot; malformed files refuse startup."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ReviewedAtsCatalogError("ATS review catalog is unavailable or invalid") from error
    if not isinstance(raw, dict) or set(raw) != {"version", "boards"} \
            or type(raw["version"]) is not int or raw["version"] != 1 \
            or not isinstance(raw["boards"], dict) \
            or set(raw["boards"]) != set(_SOURCE_KEYS):
        raise ReviewedAtsCatalogError("ATS review catalog schema is invalid")
    today = date.today()
    reviews: list[ReviewedBoard] = []
    approved: dict[str, tuple[ReviewedBoard, ...]] = {}
    for source_key in _SOURCE_KEYS:
        entries = raw["boards"][source_key]
        if not isinstance(entries, list):
            raise ReviewedAtsCatalogError("ATS board list is invalid")
        seen: set[str] = set()
        selected: list[ReviewedBoard] = []
        for entry in entries:
            board = _parse_board(entry, source_key, today)
            if board.token in seen:
                raise ReviewedAtsCatalogError("ATS board token is duplicated")
            seen.add(board.token)
            reviews.append(board)
            if board.access_decision == "approved":
                selected.append(board)
        approved[source_key] = tuple(selected)
    return ReviewedAtsCatalog(version=1, boards=MappingProxyType(approved),
                              reviews=tuple(reviews))
