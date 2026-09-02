"""Jooble aggregator. Official public API (https://jooble.org/api/about) —
the only source in this package backed by a documented, key-based API rather
than scraping. Aggregates listings from many boards (including some this
pipeline cannot scrape directly, e.g. JobCloud-network sites), so it is a
higher-value source per request than any single board.

Requires a free API key: sign up at https://jooble.org/api/about and put it
in a `.env` file at the project root as `JOOBLE_API_KEY=...` (see .env.example).
Missing key raises FetchError, which discovery reports as `ok: false` for
this source without aborting the run — the same fail-soft behavior as every
other source here."""
import os

from dotenv import load_dotenv

from pipeline.http_fetch import fetch, FetchError
from pipeline.sources._common import normalize

load_dotenv()

API = "https://jooble.org/api/{key}"


def search_jobs(query: str, location: str, lookback_days: int = 3) -> list[dict]:
    key = os.environ.get("JOOBLE_API_KEY")
    if not key:
        raise FetchError("JOOBLE_API_KEY not set (see .env.example)")
    resp = fetch(API.format(key=key), method="POST",
                 json_body={"keywords": query, "location": location})
    return _parse_jobs(resp.json())


def _parse_jobs(payload: dict) -> list[dict]:
    jobs = []
    for r in payload.get("jobs", []):
        jobs.append(normalize(
            source="jooble",
            company=r.get("company") or "",
            title=r.get("title") or "",
            url=r.get("link"),
            location=r.get("location"),
            salary=r.get("salary") or None,
            description=r.get("snippet"),
            posted_date=(r.get("updated") or "")[:10] or None,
        ))
    return jobs
