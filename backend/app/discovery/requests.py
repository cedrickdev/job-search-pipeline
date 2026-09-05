"""`SearchProfile` → `DiscoveryRequest`: the one place the two models meet (§10).

§10 forbids a second search-preference model, so there is none: `SearchProfile`
stays the only thing a user edits, `DiscoveryRequest` stays the only thing a source
reads, and this module is the mapping between them. Keeping it a function rather
than a method on either model is what lets the domain stay ignorant of discovery
and discovery stay ignorant of users.

The mapping is not one-to-one, for one reason worth stating up front: a profile can
name several places, and "within 20 km of Lausanne" and "within 20 km of Geneva"
are two searches, not one request with two centres. `DiscoveryRequest.radius` is
therefore singular and this module returns a tuple.
"""
from backend.app.discovery.contracts import DiscoveryRequest, RadiusConstraint
from backend.app.domain.base import CountryCode
from backend.app.domain.search import (
    RadiusSearchArea,
    SearchArea,
    SearchAreaKind,
    SearchProfile,
)

# V1's `DEFAULT_LOOKBACK`/`MAX_LOOKBACK` (pipeline/discover.py), carried over so a
# V2 sweep asks the boards for the same window V1 asks for today.
DEFAULT_LOOKBACK_DAYS = 3
MAX_LOOKBACK_DAYS = 7

# Per source, per request. V1 has no such cap — `welcometothejungle.py` asks for 50
# hits and the HTML sources return whatever one page holds — so this is a new
# guard, set high enough not to change what today's sources return.
DEFAULT_LIMIT = 100


def countries_in_scope(profile: SearchProfile) -> tuple[CountryCode, ...]:
    """The countries a profile names, deduplicated, in declaration order.

    A `RadiusSearchArea` contributes nothing here and that is deliberate: it
    carries no country (`RadiusSearchArea` explains why — a 30 km circle around
    Geneva is in two of them), so the country a radius belongs to is the caller's
    to state. A profile made only of radius areas therefore yields an empty tuple,
    which reads correctly as "this profile does not say which country".
    """
    seen: list[CountryCode] = []
    for area in profile.areas:
        country = getattr(area, "country", None)
        if country is not None and country not in seen:
            seen.append(country)
    return tuple(seen)


def _areas_for_country(profile: SearchProfile,
                       country: CountryCode) -> tuple[SearchArea, ...]:
    """Areas this country's sweep should cover.

    A radius is included whatever the country asked for: the caller is sweeping a
    pack and the radius is inside it by assumption. That assumption is not a fact —
    Phase 7's geographic filtering is what will confirm a posting is really in
    range — and `RADIUS_NOT_SUPPORTED` is how the orchestrator says so meanwhile.
    """
    return tuple(
        area for area in profile.areas
        if area.kind is SearchAreaKind.RADIUS or getattr(area, "country", None)
        in (None, country)
    )


def _keywords(profile: SearchProfile) -> tuple[str, ...]:
    """What to type into a search box.

    `queries` are search strings; `title_keywords` are a *filter* a later stage
    applies to titles, and sending them as queries would broaden the sweep with
    words the user meant to narrow it. The fallback exists because a profile with
    title keywords and no query still has something to look for, and sending
    nothing would return a board's unfiltered front page.
    """
    return profile.queries or profile.title_keywords


def _source_keys(profile: SearchProfile) -> tuple[str, ...]:
    """The profile's allow-list, case-normalized.

    Source keys are lower case by construction (`SourceKey`), so "Jobup" in a saved
    profile means `jobup` and lowering it is not a guess. Anything still invalid is
    left to fail in `DiscoveryRequest` validation rather than being dropped here:
    silently discarding an allow-list entry *widens* a search, which is the one
    failure mode a user would not notice.
    """
    return tuple(key.lower() for key in profile.source_keys)


def discovery_requests_for(
    profile: SearchProfile,
    *,
    country: CountryCode,
    limit: int = DEFAULT_LIMIT,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    cursor: str | None = None,
) -> tuple[DiscoveryRequest, ...]:
    """Every request one country's sweep of this profile needs.

    One request for the non-radius areas together — a country and a remote-only
    scope are the same query with different post-filters — plus one per radius
    area, because each centre is its own search.

    Returns an empty tuple when the profile does not reach this country at all.
    That is a real answer and the caller reports it; inventing a country-wide
    request instead would sweep a country the user never asked about.
    """
    areas = _areas_for_country(profile, country)
    if not areas:
        return ()

    def build(locations: tuple[str, ...], *, radius: RadiusConstraint | None = None,
              remote_only: bool = False) -> DiscoveryRequest:
        """One request, with everything a profile says that is not geographic."""
        return DiscoveryRequest(
            country=country,
            keywords=_keywords(profile),
            locations=locations,
            radius=radius,
            opportunity_types=profile.opportunity_types,
            workplace_modes=profile.workplace_modes,
            include_remote=profile.includes_remote,
            remote_only=remote_only,
            lookback_days=min(lookback_days, MAX_LOOKBACK_DAYS),
            limit=limit,
            source_keys=_source_keys(profile),
            cursor=cursor,
        )

    requests: list[DiscoveryRequest] = []
    flat = tuple(area for area in areas if area.kind is not SearchAreaKind.RADIUS)
    if flat:
        requests.append(build(
            tuple(area.label for area in flat if area.label is not None),
            # A remote-only scope has to keep `include_remote` true; the model
            # refuses the incoherent combination outright.
            remote_only=all(area.kind is SearchAreaKind.REMOTE_ONLY for area in flat),
        ))

    for area in areas:
        if not isinstance(area, RadiusSearchArea):
            continue
        requests.append(build(
            # The label is the only text a source can search on — no source can
            # take coordinates, which is exactly what §11 is about. The constraint
            # travels anyway so the orchestrator can say it went unhonoured.
            (area.label,) if area.label is not None else (),
            radius=RadiusConstraint(center=area.center, radius_km=area.radius_km,
                                    label=area.label),
        ))
    return tuple(requests)
