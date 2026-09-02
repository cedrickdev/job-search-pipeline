"""jobup.ch (JobCloud AG). Best-effort: no documented public API, and the
search JSON API (job-search-api.jobup.ch) returns 401 for anonymous callers
behind an AWS WAF challenge. Results are instead read from the page's
server-rendered `__INIT__` state blob, which carries the full result set for
the free-text query.

There is no public regionId lookup for cantons, so location is folded into
the free-text `term` (jobup's search ranks by relevance rather than hard
filtering) — expect some near-miss locations in the results; the generic
scope/scoring gates downstream catch those.

Fragile by nature (undocumented frontend internals, WAF-gated). Failures
surface as `ok: false` in discovery health rather than aborting the run,
same as linkedin/indeed today."""
import json
import re

from pipeline.http_fetch import fetch, FetchError
from pipeline.sources._common import normalize, extract_json_object

SEARCH = "https://www.jobup.ch/fr/emplois/"
_INIT_RE = re.compile(r"__INIT__\s*=\s*")


def search_jobs(query: str, location: str, lookback_days: int = 3) -> list[dict]:
    resp = fetch(SEARCH, params={"term": f"{query} {location}".strip()})
    return _parse_init(resp.text)


def _parse_init(html_text: str) -> list[dict]:
    match = _INIT_RE.search(html_text)
    if not match:
        return []
    start = html_text.find("{", match.end())
    if start == -1:
        return []
    blob = extract_json_object(html_text, start)
    if blob is None:
        return []
    try:
        data = json.loads(blob.replace(":undefined", ":null"))
    except json.JSONDecodeError:
        raise FetchError(f"{SEARCH}: unparseable __INIT__ blob (page layout changed?)")
    results = (data.get("vacancy", {}).get("results", {})
               .get("main", {}).get("results", []))
    jobs = []
    for r in results:
        job_id = r.get("id")
        jobs.append(normalize(
            source="jobup",
            company=(r.get("company") or {}).get("name") or "",
            title=r.get("title") or "",
            url=f"https://www.jobup.ch/fr/emplois/detail/{job_id}/" if job_id else None,
            location=r.get("place"),
            posted_date=(r.get("initialPublicationDate") or "")[:10] or None,
        ))
    return jobs
