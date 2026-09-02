"""Lever public postings API. Reliable JSON with plain-text descriptions."""
from datetime import datetime, timezone

from pipeline.http_fetch import fetch_json
from pipeline.sources._common import normalize

API = "https://api.lever.co/v0/postings/{token}"


def fetch_jobs(token: str, company: str) -> list[dict]:
    postings = fetch_json(API.format(token=token), params={"mode": "json"})
    jobs = []
    for p in postings:
        categories = p.get("categories") or {}
        created_ms = p.get("createdAt")
        posted = (datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
                  .date().isoformat()) if created_ms else None
        jobs.append(normalize(
            source="lever",
            company=company,
            title=p.get("text", ""),
            url=p.get("hostedUrl"),
            location=categories.get("location"),
            remote_policy=p.get("workplaceType"),
            contract_type=categories.get("commitment"),
            description=p.get("descriptionPlain") or None,
            posted_date=posted,
        ))
    return jobs
