"""JobScout24.ch. Static server-rendered HTML, independent of the JobCloud
group (jobup.ch/jobs.ch share one database — this doesn't), so it surfaces
genuinely different listings. The per-city page already aggregates a radius
around that city rather than just the city itself (376 results for
"Yverdon-les-Bains" alone confirms this), which matches this pipeline's
"Yverdon + ~20-30km" scope well. The `?term=` query parameter does not
appear to filter server-side (same result count/title with or without it),
so results are only gated by the generic title-keyword filter downstream —
same pattern as sources without a working keyword param."""
from urllib.parse import urljoin

from pipeline.http_fetch import fetch
from pipeline.sources._common import normalize

BASE = "https://www.jobscout24.ch"
YVERDON_LISTING = f"{BASE}/fr/jobs-%C3%A0-yverdon-les-bains/"


def search_jobs(query: str, location: str, lookback_days: int = 3) -> list[dict]:
    resp = fetch(YVERDON_LISTING)
    return _parse_listing(resp.text)


def _parse_listing(html_text: str) -> list[dict]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "html.parser")
    jobs = []
    for item in soup.select("li.job-list-item"):
        link = item.select_one("a.job-title")
        if not link:
            continue
        title = link.get("title") or link.get_text(strip=True)
        attrs = [s.get_text(strip=True) for s in item.select("p.job-attributes span")]
        company = attrs[0] if attrs else None
        place = attrs[1] if len(attrs) > 1 else None
        tags = [t.get_text(strip=True) for t in item.select(".job-tags .tag")]
        percent = next((t for t in tags if "%" in t), None)
        jobs.append(normalize(
            source="jobscout24",
            company=company or "",
            title=title,
            url=urljoin(BASE, link["href"]),
            location=place,
            salary=percent,
        ))
    return jobs
