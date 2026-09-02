"""Greenhouse public board API. Reliable JSON; descriptions arrive HTML-escaped."""
import html

from pipeline.http_fetch import fetch_json
from pipeline.sources._common import normalize, strip_html

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


def fetch_jobs(token: str, company: str) -> list[dict]:
    data = fetch_json(API.format(token=token), params={"content": "true"})
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(normalize(
            source="greenhouse",
            company=company,
            title=j.get("title", ""),
            url=j.get("absolute_url"),
            location=(j.get("location") or {}).get("name"),
            description=strip_html(html.unescape(j.get("content") or "")) or None,
            posted_date=(j.get("updated_at") or "")[:10] or None,
        ))
    return jobs
