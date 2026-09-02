"""LinkedIn guest job-search endpoint. Best-effort per the spec: LinkedIn is
aggressively anti-scraping; a 403/429 raises FetchError and shows in fetch health."""
from bs4 import BeautifulSoup

from pipeline.http_fetch import fetch
from pipeline.sources._common import normalize

GUEST_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


def search_jobs(query: str, location: str, lookback_days: int = 3) -> list[dict]:
    resp = fetch(GUEST_SEARCH, params={
        "keywords": query,
        "location": location,
        "f_TPR": f"r{lookback_days * 86400}",
        "start": 0,
    })
    return _parse_cards(resp.text)


def _parse_cards(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    jobs = []
    for card in soup.select("div.base-search-card"):
        title = card.select_one("h3.base-search-card__title")
        company = card.select_one("h4.base-search-card__subtitle")
        if not (title and company):
            continue
        link = card.select_one("a.base-card__full-link") or card.select_one("a[href]")
        location_node = card.select_one("span.job-search-card__location")
        posted = card.select_one("time[datetime]")
        jobs.append(normalize(
            source="linkedin",
            company=company.get_text(strip=True),
            title=title.get_text(strip=True),
            url=link["href"].split("?")[0] if link else None,
            location=location_node.get_text(strip=True) if location_node else None,
            posted_date=posted["datetime"] if posted else None,
        ))
    return jobs


def fetch_description(url: str) -> str | None:
    soup = BeautifulSoup(fetch(url).text, "html.parser")
    node = soup.select_one("div.show-more-less-html__markup, div.description__text")
    return node.get_text(" ", strip=True) if node else None
