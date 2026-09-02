"""Migros (jobs.migros.ch). Static server-rendered HTML — no SPA/WAF
obstacles, unlike jobup.ch. Each Migros cooperative (regional entity) has its
own listing page; only the Vaud cooperative's page is queried since that is
the only region in scope for this pipeline."""
from urllib.parse import urljoin

from pipeline.http_fetch import fetch
from pipeline.sources._common import normalize

BASE = "https://jobs.migros.ch"
VAUD_LISTING = f"{BASE}/fr/nos-entreprises/societe-cooperative-migros-vaud/postes-vacants"


def search_jobs(query: str, location: str, lookback_days: int = 3) -> list[dict]:
    resp = fetch(VAUD_LISTING)
    return _parse_listing(resp.text)


def _parse_listing(html_text: str) -> list[dict]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "html.parser")
    jobs = []
    for item in soup.select("li.search-layout-list-item"):
        link = item.select_one("a[href]")
        if not link:
            continue
        heading = item.select_one("h3")
        title = heading.select_one("span.font-bold").get_text(strip=True) if heading else ""
        percent = heading.select_one("span:not(.font-bold)") if heading else None
        details = [li.get_text(strip=True) for li in item.select("ul.dot-list li")]
        place = details[0] if details else None
        contract = details[1] if len(details) > 1 else None
        company_tag = item.select_one("p")
        jobs.append(normalize(
            source="migros",
            company=company_tag.get_text(strip=True) if company_tag else "Migros",
            title=title,
            url=urljoin(BASE, link["href"]),
            location=place,
            contract_type=contract,
            salary=percent.get_text(strip=True) if percent else None,
        ))
    return jobs
