"""Indeed Switzerland (French). Same platform and JSON shape as Indeed France
(pipeline/sources/indeed.py), different domain and source tag."""
import json

from pipeline.http_fetch import fetch
from pipeline.sources._common import normalize, strip_html
from pipeline.sources.indeed import _MOSAIC_RE

SEARCH = "https://ch-fr.indeed.com/jobs"


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
            source="indeed_ch",
            company=r.get("company", ""),
            title=r.get("displayTitle") or r.get("title", ""),
            url=f"https://ch-fr.indeed.com/viewjob?jk={jobkey}" if jobkey else None,
            location=r.get("formattedLocation"),
            remote_policy="remote" if r.get("remoteLocation") else None,
            salary=(r.get("salarySnippet") or {}).get("text"),
            description=strip_html(snippet) if snippet else None,
        ))
    return jobs
