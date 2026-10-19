"""Pure decisions for matching a collected opportunity to a saved search."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend.app.discovery.orchestrator import SweepReport
from backend.app.domain.opportunity import Opportunity
from backend.app.domain.search import SearchAreaKind, SearchProfile


def matches_search(profile: SearchProfile, opportunity: Opportunity) -> bool:
    """Whether a collected opportunity passes non-geographic profile filters."""
    if not profile.is_active:
        return False
    if not profile.allows_opportunity_type(opportunity.opportunity_type):
        return False
    if profile.contract_types and opportunity.contract_type is not None \
            and opportunity.contract_type not in profile.contract_types:
        return False
    if profile.workplace_modes and opportunity.workplace_mode is not None \
            and opportunity.workplace_mode not in profile.workplace_modes:
        return False
    if profile.posting_languages and opportunity.posting_language is not None \
            and opportunity.posting_language not in profile.posting_languages:
        return False
    text = f"{opportunity.title} {opportunity.description or ''}".casefold()
    if any(word.casefold() in text for word in profile.excluded_keywords):
        return False
    return not profile.title_keywords or any(
        word.casefold() in opportunity.title.casefold()
        for word in profile.title_keywords
    )


def matching_opportunities(
    profile: SearchProfile, report: SweepReport
) -> tuple[Opportunity, ...]:
    """Select postings; a country-only search requires verified country evidence.

    An international index can rank French results for a Swiss keyword query.
    Unknown country is not proof of Switzerland, even when the sweep is CH.
    """
    country_only = all(area.kind is SearchAreaKind.COUNTRY
                       for area in profile.areas)
    return tuple(
        opportunity for opportunity in report.opportunities
        if matches_search(profile, opportunity)
        and (not country_only or (
            opportunity.location is not None
            and opportunity.location.country == report.country))
    )


def canonical_target(opportunity: Opportunity) -> str | None:
    """Return a known ATS job-detail URL, never a title/company guess."""
    for candidate in (opportunity.application_url, opportunity.source.source_url):
        if candidate is None:
            continue
        parsed = urlsplit(candidate)
        host = parsed.hostname
        parts = parsed.path.strip("/").split("/")
        if parsed.scheme != "https" or host is None:
            continue
        if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
            if len(parts) != 3 or parts[1] != "jobs":
                continue
        elif host in {"jobs.lever.co", "jobs.ashbyhq.com"}:
            if len(parts) != 2:
                continue
        else:
            continue
        if not all(parts):
            continue
        return urlunsplit(("https", host, parsed.path.rstrip("/"), "", ""))
    return None


def normalize_source_url(url: str) -> str:
    """Remove fragments/tracking without conflating distinct job-detail queries."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A source URL must be HTTP or HTTPS")
    host = parsed.hostname.lower()
    port = parsed.port
    if port and (parsed.scheme, port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    query = urlencode(sorted(
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
    ))
    return urlunsplit((parsed.scheme.lower(), host, parsed.path.rstrip("/") or "/",
                       query, ""))
