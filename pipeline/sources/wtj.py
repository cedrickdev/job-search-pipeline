"""Welcome to the Jungle via its public Algolia search index.

The Algolia app id / search-only key are public (embedded in WTJ's frontend JS)
but rotate occasionally: extracted at runtime from the jobs page, cached per
process. Credential extraction failures fall back to last-known-good public defaults; Algolia queries require the WTJ referer header.
"""
import json
import re

from bs4 import BeautifulSoup

from pipeline.http_fetch import FetchError, fetch
from pipeline.sources._common import normalize, strip_html

JOBS_PAGE = "https://www.welcometothejungle.com/fr/jobs"
INDEX = "wttj_jobs_production_fr"
# Last-known-good public search credentials (shipped in WTJ's frontend JS).
# Used when runtime extraction fails, e.g. the jobs page is behind a WAF challenge.
DEFAULT_APP_ID = "CSEKHVMS53"
DEFAULT_API_KEY = "4bd8f6215d0cc52b26430765769e65a0"
_APP_RE = re.compile(r'"appId"\s*:\s*"([A-Z0-9]{8,12})"')
_KEY_RE = re.compile(r'"apiKey"\s*:\s*"([a-f0-9]{24,64})"')

_creds: tuple[str, str] | None = None


def _algolia_creds() -> tuple[str, str]:
    global _creds
    if _creds is None:
        try:
            page = fetch(JOBS_PAGE).text
        except FetchError:
            page = ""
        app, key = _APP_RE.search(page), _KEY_RE.search(page)
        _creds = (app.group(1), key.group(1)) if app and key \
            else (DEFAULT_APP_ID, DEFAULT_API_KEY)
    return _creds


def search_jobs(query: str, location: str, lookback_days: int = 3) -> list[dict]:
    app_id, api_key = _algolia_creds()
    url = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/{INDEX}/query"
    payload = {"query": f"{query} {location}", "hitsPerPage": 50}
    headers = {
        "X-Algolia-Application-Id": app_id,
        "X-Algolia-API-Key": api_key,
        "Referer": "https://www.welcometothejungle.com/",
    }
    data = fetch(url, method="POST", json_body=payload, headers=headers).json()
    return _parse_hits(data.get("hits", []))


def _parse_hits(hits: list[dict]) -> list[dict]:
    jobs = []
    for h in hits:
        org = h.get("organization") or {}
        cities = [o.get("city") for o in (h.get("offices") or []) if o.get("city")]
        jobs.append(normalize(
            source="wtj",
            company=org.get("name", ""),
            title=h.get("name", ""),
            url=(f"https://www.welcometothejungle.com/fr/companies/"
                 f"{org['slug']}/jobs/{h['slug']}"
                 if org.get("slug") and h.get("slug") else None),
            location=", ".join(cities) or None,
            remote_policy=h.get("remote"),
            contract_type=h.get("contract_type"),
            language=h.get("language"),
            posted_date=(h.get("published_at") or "")[:10] or None,
            description=h.get("summary") or None,
        ))
    return jobs


def fetch_description(url: str) -> str | None:
    """Job pages embed a JSON-LD JobPosting block with the full description."""
    soup = BeautifulSoup(fetch(url).text, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("@type") == "JobPosting" \
                    and item.get("description"):
                return strip_html(item["description"])
    return None
