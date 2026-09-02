"""Manpower Switzerland (staffing agency). Static server-rendered HTML per
city. Listed company is usually "Manpower" itself (the agency), not the end
client — real employer is typically revealed only after applying."""
from urllib.parse import urljoin

from pipeline.http_fetch import fetch
from pipeline.sources._common import normalize

BASE = "https://www.manpower.ch"
LAUSANNE_LISTING = f"{BASE}/fr/recherche/ville/lausanne"


def search_jobs(query: str, location: str, lookback_days: int = 3) -> list[dict]:
    resp = fetch(LAUSANNE_LISTING)
    return _parse_listing(resp.text)


def _parse_listing(html_text: str) -> list[dict]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "html.parser")
    jobs = []
    for item in soup.select(".job-search-result"):
        link = item.select_one(".job-position a")
        if not link:
            continue
        company = item.select_one(".job-skills .company")
        location_el = item.select_one(".job-details .location")
        type_el = item.select_one(".job-details .type")
        jobs.append(normalize(
            source="manpower",
            company=company.get_text(strip=True) if company else "Manpower",
            title=link.get_text(strip=True),
            url=urljoin(BASE, link["href"]),
            location=location_el.get_text(strip=True) if location_el else None,
            contract_type=type_el.get_text(strip=True) if type_el else None,
        ))
    return jobs
