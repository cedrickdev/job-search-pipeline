"""Ashby public job-board API. Reliable JSON."""
from pipeline.http_fetch import fetch_json
from pipeline.sources._common import normalize, strip_html

API = "https://api.ashbyhq.com/posting-api/job-board/{token}"


def fetch_jobs(token: str, company: str) -> list[dict]:
    data = fetch_json(API.format(token=token), params={"includeCompensation": "true"})
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(normalize(
            source="ashby",
            company=company,
            title=j.get("title", ""),
            url=j.get("jobUrl"),
            location=j.get("location"),
            remote_policy="remote" if j.get("isRemote") else None,
            contract_type=j.get("employmentType"),
            salary=(j.get("compensation") or {}).get("compensationTierSummary"),
            description=strip_html(j.get("descriptionHtml") or "") or None,
            posted_date=(j.get("publishedAt") or "")[:10] or None,
        ))
    return jobs
