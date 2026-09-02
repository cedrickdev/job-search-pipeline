"""Indeed France. Moderate reliability: Cloudflare frequently 403s plain clients
(fetch raises FetchError, which shows in discovery health). When a page renders,
jobs live in an embedded mosaic JSON blob."""
import json
import re

from pipeline.http_fetch import fetch
from pipeline.sources._common import normalize, strip_html

SEARCH = "https://fr.indeed.com/jobs"
_MOSAIC_RE = re.compile(
    r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*(\{.*?\});',
    re.DOTALL)


def search_jobs(query: str, location: str, lookback_days: int = 3) -> list[dict]:
    resp = fetch(SEARCH, params={"q": query, "l": location, "fromage": lookback_days})
    return _parse_mosaic(resp.text)


def _parse_mosaic(html_text: str) -> list[dict]:
    match = _MOSAIC_RE.search(html_text)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    results = (data.get("metaData", {})
               .get("mosaicProviderJobCardsModel", {})
               .get("results", []))
    jobs = []
    for r in results:
        jobkey = r.get("jobkey")
        snippet = r.get("snippet")
        jobs.append(normalize(
            source="indeed",
            company=r.get("company", ""),
            title=r.get("displayTitle") or r.get("title", ""),
            url=f"https://fr.indeed.com/viewjob?jk={jobkey}" if jobkey else None,
            location=r.get("formattedLocation"),
            remote_policy="remote" if r.get("remoteLocation") else None,
            salary=(r.get("salarySnippet") or {}).get("text"),
            description=strip_html(snippet) if snippet else None,
        ))
    return jobs
