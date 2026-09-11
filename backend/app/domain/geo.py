"""Geographic search, geocoding and remote semantics — the Phase 7 vocabulary.

`common` holds the *values* a place is made of (`GeoPoint`, `GeoDistance`,
`Location` and its provenance). This module holds what a *search* over those
values is: the query object a geo repository takes, the explicit remote policy,
the status a result reports when no distance can be computed, and the
request/result pair a geocoder speaks in.

The split is deliberate. `Location` is embedded in `Opportunity`, `Company` and
`CandidateProfile`, so it has to sit early in the dependency order; a search over
opportunities cannot, because it names `OpportunityType` and `WorkplaceMode`.
Putting the query object here — after `search`, whose `SearchArea` it is mapped
from — is what keeps every import edge pointing backwards.

Three rules this module exists to make unavoidable:

**Distance is the database's answer, not this module's.** Nothing here computes a
distance. `GeoSearchQuery` describes what to ask PostGIS; `ST_DWithin` decides
membership and `ST_Distance` produces the number
(`backend/app/repositories/sqlalchemy_repositories.py`). A Haversine helper here
would immediately become a second, disagreeing answer to the same question —
which is exactly what Phase 7 §15 forbids.

**Remote is a policy, never an inference.** `RemotePolicy` has to be stated, and
`RemoteScope` records how far a remote posting actually reaches. "Remote" on a
posting is not a claim about worldwide eligibility: whether the candidate may
legally take it is `WorkAuthorization`'s question and Phase 9's answer. Phase 7
only decides whether the posting is *discoverable* from where the candidate is
looking.

**"I cannot tell" is a value.** `GeoStatus.UNRESOLVED` is why a posting whose
location is the string `"Lausanne"` and nothing else can be returned at all
without either crashing or being silently claimed to be 0 km away (§12).
"""
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import Field, model_validator

from backend.app.domain.base import (
    CountryCode,
    DomainModel,
    LanguageCode,
    NonEmptyStr,
    UtcDatetime,
)
from backend.app.domain.common import (
    GeoBounds,
    GeocodingConfidence,
    GeoDistance,
    GeoPoint,
    Location,
    LocationPrecision,
    LocationProvenance,
)
from backend.app.domain.opportunity import OpportunityType, WorkplaceMode
from backend.app.domain.search import (
    SearchArea,
    SearchAreaKind,
)

# How many results a geo query returns when the caller does not say. Smaller than
# the repositories' `DEFAULT_LIMIT` of 100 because a geo result carries a company
# and a resolved location per row, and because the first screen of a map or a list
# is what this default serves — §17 asks for pagination, not for one big page.
DEFAULT_GEO_LIMIT: Final[int] = 50

# The ceiling a caller may ask for. A hard cap rather than a suggestion: the geo
# query joins and sorts, and an unbounded `limit` from a query string is how a
# public endpoint becomes a denial-of-service primitive.
MAX_GEO_LIMIT: Final[int] = 200


class RemotePolicy(StrEnum):
    """What a search wants done about remote work (Phase 7 §10).

    Three members, and each is a different question rather than a different
    strictness of the same one:

    - `EXCLUDE_REMOTE` — only postings with a real place, judged geographically.
      A candidate who wants an office does not want a list of remote roles.
    - `INCLUDE_REMOTE` — the geographic result *plus* remote postings the search's
      country restriction admits. The union, not a relaxation of the radius.
    - `REMOTE_ONLY` — remote postings and nothing else. No distance predicate
      applies at all, which is why §3 says a REMOTE_ONLY area needs no centre.

    There is no default in the enum on purpose. `GeoSearchQuery` states one, and
    `geo_query_for_areas` derives it from the saved areas — but a caller
    constructing a query by hand has to have written the word.
    """

    EXCLUDE_REMOTE = "EXCLUDE_REMOTE"
    INCLUDE_REMOTE = "INCLUDE_REMOTE"
    REMOTE_ONLY = "REMOTE_ONLY"


class RemoteScope(StrEnum):
    """How far a remote posting actually reaches (Phase 7 §10, §11).

    Derived from the posting rather than stored, by `remote_scope_of` below, so it
    cannot drift from the workplace mode and location beside it.

    `HYBRID` is a member of this enum and *not* a kind of remote: a hybrid role
    keeps a geographic anchor, is judged by distance like any on-site role, and
    §11 forbids treating it as fully remote. It is listed here because "how far
    does this reach" is the same question, and the answer for hybrid is "as far as
    a commute".

    The three genuinely remote members are ordered from the strongest claim to the
    weakest, and the derivation is deliberately reluctant to reach
    `REMOTE_ANYWHERE`: a posting that names a country or a region while saying
    "remote" has told us something, and §10 forbids reading bare "Remote" as
    worldwide eligibility.
    """

    REMOTE_ANYWHERE = "REMOTE_ANYWHERE"
    REMOTE_COUNTRY_RESTRICTED = "REMOTE_COUNTRY_RESTRICTED"
    REMOTE_REGION_RESTRICTED = "REMOTE_REGION_RESTRICTED"
    HYBRID = "HYBRID"


def remote_scope_of(workplace_mode: WorkplaceMode | None,
                    location: Location | None) -> RemoteScope | None:
    """Classify one posting's geographic reach, or `None` when it is on site.

    Deterministic and total, which matters because it is what an API response
    exposes and what a test can pin:

    - `HYBRID` mode is `HYBRID`, whatever the location says;
    - `REMOTE` mode narrows as far as the location allows — a named city or region
      makes it `REMOTE_REGION_RESTRICTED`, a bare country
      `REMOTE_COUNTRY_RESTRICTED`, and only a location that names no geography at
      all (or no location) makes it `REMOTE_ANYWHERE`;
    - `ON_SITE` and an unclassified mode are `None`, because "not remote" is not a
      degree of remoteness.

    Reading a city on a remote posting as a regional restriction is the
    conservative direction and is the intended one: boards print the hiring
    office's city on remote roles, and treating that as *less* information than it
    is would be the §10 mistake — claiming a worldwide role from a posting that
    named a place.
    """
    if workplace_mode is WorkplaceMode.HYBRID:
        return RemoteScope.HYBRID
    if workplace_mode is not WorkplaceMode.REMOTE:
        return None
    if location is None:
        return RemoteScope.REMOTE_ANYWHERE
    if location.city or location.region or location.postal_code:
        return RemoteScope.REMOTE_REGION_RESTRICTED
    if location.country:
        return RemoteScope.REMOTE_COUNTRY_RESTRICTED
    return RemoteScope.REMOTE_ANYWHERE


class GeoStatus(StrEnum):
    """Why a result has the distance it has — or has none (Phase 7 §12).

    The member that earns the enum is `UNRESOLVED`. Without it, a caller reading
    `distance_km = null` cannot tell "this posting is remote, distance does not
    apply" from "we do not know where this posting is" from "it is outside the
    radius" — and the third is not even in the list, because a row outside the
    radius is not returned at all. §12 requires those to be distinguishable, and a
    nullable float cannot do it.

    `COMPANY_FALLBACK` is the other one that has to be visible. A posting with no
    coordinates of its own, placed at its employer's office (§9, §31), is a
    weaker claim than one that geocoded its own address; a UI that draws both as
    the same pin is overstating what is known, and an operator debugging "why is
    this job on the map at all" needs the answer in the payload rather than in the
    query plan.
    """

    RESOLVED = "RESOLVED"
    COMPANY_FALLBACK = "COMPANY_FALLBACK"
    REMOTE = "REMOTE"
    UNRESOLVED = "UNRESOLVED"


class RadiusFilter(DomainModel):
    """One "within this far of here" clause of a geo query.

    The persistence-facing twin of `RadiusSearchArea`, and the difference is the
    unit: the saved area states kilometres because that is what a user types, and
    this states a `GeoDistance` because `ST_DWithin` on a `geography` column takes
    metres. The conversion happens once, in `geo_query_for_areas`, instead of at
    every call site that builds a predicate (§1).

    `label` is carried through so a multi-area result can say *which* area matched
    without the caller re-deriving it from coordinates.
    """

    center: GeoPoint
    radius: GeoDistance
    label: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _a_radius_encloses_something(self) -> Self:
        if self.radius.meters <= 0.0:
            raise ValueError("a RadiusFilter needs a radius greater than zero; a "
                             "zero-metre disc matches only an exact coordinate")
        return self


class GeoSearchQuery(DomainModel):
    """Everything a geo repository needs, as one value (Phase 7 §14).

    A query object rather than a method with a dozen parameters, for two reasons
    beyond readability. It makes the one-area API request and the multi-area saved
    profile *the same shape*, so §27's "areas combine as OR, with no duplicates" is
    one implementation instead of two; and it can be validated, so a contradictory
    search (`REMOTE_ONLY` with a radius) fails where it was built rather than
    producing an empty page nobody can explain.

    The geographic clauses combine as **OR**: a posting matches if it is inside any
    radius, or in any listed country, or — under `INCLUDE_REMOTE` — remote and
    admitted by `remote_countries`. `bounds`, `opportunity_types` and
    `workplace_modes` are **AND** narrowings applied to whatever that union
    produced; a viewport is not another place to look, it is the part of the result
    the screen can show.

    `remote_countries` is what makes "remote, but contracted in CH" expressible
    (§10). Empty means the remote branch is not narrowed by country — which is a
    decision the caller states, not a default that leaks worldwide postings into a
    Swiss search: `geo_query_for_areas` fills it from the saved
    `RemoteOnlySearchArea.country`.
    """

    radii: tuple[RadiusFilter, ...] = ()
    countries: tuple[CountryCode, ...] = ()
    bounds: GeoBounds | None = None
    remote_policy: RemotePolicy = RemotePolicy.EXCLUDE_REMOTE
    remote_countries: tuple[CountryCode, ...] = ()
    opportunity_types: tuple[OpportunityType, ...] = ()
    workplace_modes: tuple[WorkplaceMode, ...] = ()
    limit: Annotated[int, Field(ge=1, le=MAX_GEO_LIMIT)] = DEFAULT_GEO_LIMIT
    offset: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _the_query_asks_for_somewhere(self) -> Self:
        """A geo search has a scope, and a remote-only one has no geography.

        The first clause mirrors `SearchProfile.areas`' `min_length=1`: a query
        with no radius, no country and no bounds is a full table scan wearing the
        name of a search. The second is §3's rule — a `REMOTE_ONLY` search needs no
        distance, so a radius or a viewport beside it is a contradiction rather
        than a narrowing, and silently ignoring it would return results the caller
        did not ask for.
        """
        if self.remote_policy is RemotePolicy.REMOTE_ONLY:
            if self.radii or self.countries or self.bounds is not None:
                raise ValueError(
                    "a REMOTE_ONLY query states no geography: remote work has no "
                    "distance to a centre, so a radius, a country or bounds "
                    "beside it contradicts the policy")
            return self
        if not self.radii and not self.countries and self.bounds is None:
            raise ValueError(
                "a geo query needs at least one radius, country or bounds; use "
                "RemotePolicy.REMOTE_ONLY to search location-independent work")
        if self.remote_policy is RemotePolicy.EXCLUDE_REMOTE and self.remote_countries:
            raise ValueError("remote_countries narrows the remote branch, which "
                             "EXCLUDE_REMOTE does not have")
        return self

    @property
    def has_center(self) -> bool:
        """Whether any distance can be computed for this query at all (§16)."""
        return bool(self.radii)

    @property
    def includes_remote(self) -> bool:
        """Whether remote postings may appear in the result."""
        return self.remote_policy is not RemotePolicy.EXCLUDE_REMOTE

    def page(self, *, limit: int, offset: int) -> "GeoSearchQuery":
        """The same search, one page further on.

        Used by callers that hold a query built from a saved profile and need the
        second page: rebuilding it from the areas would re-derive the policy, and a
        derivation run twice is a derivation that can differ twice.
        """
        return self.model_copy(update={"limit": limit, "offset": offset})


def geo_query_for_areas(
    areas: tuple[SearchArea, ...],
    *,
    opportunity_types: tuple[OpportunityType, ...] = (),
    workplace_modes: tuple[WorkplaceMode, ...] = (),
    bounds: GeoBounds | None = None,
    limit: int = DEFAULT_GEO_LIMIT,
    offset: int = 0,
) -> GeoSearchQuery:
    """Map saved `SearchArea`s onto one executable query (Phase 7 §3).

    This is the whole reason Phase 7 needs no second geographic model: the saved
    representation stays the three-member discriminated union Phase 1 defined, and
    everything the engine needs is *derived* here, in one function, deterministically.

    The remote policy is the part worth stating precisely, because it is a
    judgement rather than a translation:

    - every area is `REMOTE_ONLY` → `REMOTE_ONLY`. There is nowhere to measure
      from, and §3 says such a search needs no distance.
    - at least one `REMOTE_ONLY` area beside a geographic one → `INCLUDE_REMOTE`.
      The user asked for both, so the result is the union.
    - no `REMOTE_ONLY` area, but `workplace_modes` names `REMOTE` →
      `INCLUDE_REMOTE`. Without this, a profile that explicitly wants remote work
      and also names a city would be given `EXCLUDE_REMOTE` and then filtered down
      to remote-only postings — an empty page produced by two correct filters
      disagreeing.
    - otherwise → `EXCLUDE_REMOTE`. The areas name places and nothing asked for
      remote. An empty `workplace_modes` restricts nothing *within* the areas; it
      does not add a scope the areas did not request. Adding a
      `RemoteOnlySearchArea` is how a user asks for remote work, which is what the
      union is for.

    `remote_countries` collects the countries the remote-only areas named, and
    stays empty as soon as one of them named none — because an area that says
    "remote, anywhere" must not be narrowed by a sibling that says "remote, in CH".
    """
    radii = tuple(
        RadiusFilter(center=area.center,
                     radius=GeoDistance.from_kilometers(area.radius_km),
                     label=area.label)
        for area in areas if area.kind is SearchAreaKind.RADIUS)
    countries = tuple(dict.fromkeys(
        area.country for area in areas if area.kind is SearchAreaKind.COUNTRY))
    remote_areas = tuple(
        area for area in areas if area.kind is SearchAreaKind.REMOTE_ONLY)

    if remote_areas and len(remote_areas) == len(areas):
        policy = RemotePolicy.REMOTE_ONLY
    elif remote_areas or WorkplaceMode.REMOTE in workplace_modes:
        policy = RemotePolicy.INCLUDE_REMOTE
    else:
        policy = RemotePolicy.EXCLUDE_REMOTE

    remote_countries: tuple[CountryCode, ...] = ()
    if policy is not RemotePolicy.EXCLUDE_REMOTE \
            and all(area.country is not None for area in remote_areas) \
            and remote_areas:
        remote_countries = tuple(dict.fromkeys(
            area.country for area in remote_areas if area.country is not None))

    if policy is RemotePolicy.REMOTE_ONLY:
        # §3: no distance, and therefore no viewport either — a REMOTE_ONLY area
        # has no coordinates for a box to contain.
        return GeoSearchQuery(remote_policy=policy,
                              remote_countries=remote_countries,
                              opportunity_types=opportunity_types,
                              workplace_modes=workplace_modes,
                              limit=limit, offset=offset)
    return GeoSearchQuery(radii=radii, countries=countries, bounds=bounds,
                          remote_policy=policy, remote_countries=remote_countries,
                          opportunity_types=opportunity_types,
                          workplace_modes=workplace_modes,
                          limit=limit, offset=offset)


# --- geocoding ---------------------------------------------------------------
#
# The provider-neutral half of Phase 7 (§4-§6). The `Geocoder` port itself lives
# in `backend/app/geo/contracts.py`, beside the adapters that implement it,
# because a Protocol whose methods are `async` is an infrastructure concern; what
# lives here is the vocabulary both sides speak, so no adapter can invent its own
# notion of "found" or "confident".


class GeocodingOutcome(StrEnum):
    """What a geocoding attempt concluded (Phase 7 §6).

    Four members, and the one that carries the phase order's prohibition is
    `AMBIGUOUS`. Two `Lausanne`s — one in Vaud, one in Tennessee — are not a
    result to be resolved by taking the first: §6 forbids turning the first hit
    into truth when several are equally plausible, and a caller that gets
    `AMBIGUOUS` back has to either narrow the query or leave the location
    unresolved, both of which are honest.

    `NOT_FOUND` and `FAILED` are kept apart because they mean opposite things for
    a retry. Nothing found is an answer — retrying the same string tomorrow gives
    the same nothing — while a timeout or a 502 is the absence of an answer, and
    caching it permanently (§23) would turn one provider outage into a permanently
    unresolvable address.
    """

    MATCHED = "MATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    FAILED = "FAILED"

    @property
    def is_answer(self) -> bool:
        """Whether the provider actually answered the question it was asked.

        `FAILED` did not, which is what makes it the one outcome a cache must
        expire.
        """
        return self is not GeocodingOutcome.FAILED


def normalize_geocoding_query(value: str) -> str:
    """The comparison form of a place description — the cache key's text half (§23).

    Case-folded and whitespace-collapsed, and nothing else. Deliberately far less
    aggressive than `normalize_company_name`: an address is not a name, and
    dropping punctuation would fold `Route 9` into `Route9` and `Rue de la Gare,
    5` into a different string than the one the source printed. The only
    normalization applied is the one that is certainly safe — two spellings that
    differ by spacing or case are the same question, and asking a rate-limited
    public geocoder twice for the same question is what §23 exists to prevent.

    Returns `""` for a blank value; callers get a validation error from
    `GeocodingRequest.query` rather than a cache key that matches everything.
    """
    return " ".join(value.split()).casefold()


class GeocodingRequest(DomainModel):
    """One question for a geocoder.

    Free text plus two hints, because that is the intersection of what every
    provider accepts: Nominatim, Google, Mapbox and HERE all take a query string,
    all take a country restriction, and all take a preferred language. Structured
    fields (street, house number) are *not* modelled — the providers disagree
    about them, and a field only some adapters could honour would be a promise the
    port does not keep.

    `country` is a real narrowing rather than a preference: "Lausanne" with
    `country="CH"` has one answer where the bare string has several, which turns
    an `AMBIGUOUS` outcome into a `MATCHED` one for most of a country pack's data.
    """

    query: NonEmptyStr
    country: CountryCode | None = None
    language: LanguageCode | None = None

    @property
    def normalized_query(self) -> str:
        """The cache key's text half — see `normalize_geocoding_query`."""
        return normalize_geocoding_query(self.query)


def geocoding_query_for(location: Location) -> str | None:
    """The text to send a geocoder for this location, or `None` when there is none.

    Deterministic, and the precedence is stated because a different precedence
    would produce different coordinates for the same row:

    1. `raw`, when the source gave one. It is the most specific description
       available — a street and a number survive in it and nowhere else — and the
       parsed fields were, in the normal case, extracted *from* it, so preferring
       raw never loses information the structured fields hold.
    2. otherwise the structured components, coarse to fine, joined with commas:
       postal code and city, then region, then country.

    A location that names only a country returns that country, which will geocode
    to a centroid — and `LocationPrecision.COUNTRY` is what stops that centroid
    being read as an address (§32). A location with nothing but coordinates
    returns `None`: there is nothing to ask, and reverse geocoding is a different
    operation this phase does not need.
    """
    if location.raw:
        return location.raw
    parts = [
        " ".join(part for part in (location.postal_code, location.city) if part),
        location.region or "",
        location.country or "",
    ]
    present = [part for part in parts if part]
    return ", ".join(present) if present else None


class GeocodedPlace(DomainModel):
    """One place a geocoder resolved, with the provenance worth keeping (§6).

    There is no `raw_metadata` bag, and that is the §6 decision rather than an
    omission: "store only useful provenance" taken literally. A provider response
    is a nested document containing bounding boxes, licence text, display names in
    four languages and — depending on the client — the request headers that
    produced it. Persisting it wholesale is how a credential ends up in a shared
    table, which is the failure `backend/app/domain/company.py`'s
    `_raw_carries_no_secrets` had to be written to catch. Here the shape simply
    has nowhere to put one.

    What is kept is what a later reader actually needs: where the place is, how
    precisely, how sure the provider was, what the provider calls it, and the two
    identifiers that let somebody re-ask the same provider the same question —
    `provider_place_id` and `provider_category`, the provider's own word for what
    kind of thing it matched (`house`, `postcode`, `city`).
    """

    point: GeoPoint
    formatted_address: NonEmptyStr
    precision: LocationPrecision
    confidence: GeocodingConfidence
    country: CountryCode | None = None
    region: NonEmptyStr | None = None
    city: NonEmptyStr | None = None
    postal_code: NonEmptyStr | None = None
    provider_place_id: NonEmptyStr | None = None
    provider_category: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _a_resolved_place_states_its_precision(self) -> Self:
        if self.precision is LocationPrecision.UNKNOWN:
            raise ValueError(
                "a GeocodedPlace has coordinates, so it must say how precisely "
                "they locate anything; UNKNOWN is for a location with no point")
        return self

    def to_location(self, *, geocoder: str, geocoded_at: UtcDatetime,
                    raw: str | None = None) -> Location:
        """This place as a `Location`, stamped with who resolved it and when (§7).

        One conversion, used by the enrichment service and by nothing else, so the
        provenance can never be half-filled: `provenance`, `geocoder`,
        `geocoded_at`, `confidence` and `precision` are all set together or the
        model refuses the instance.

        `raw` is carried through from the location being enriched rather than
        replaced by `formatted_address`. §8 is explicit: `Lausanne, VD, CH` must
        survive, because it is the evidence a re-run is judged against, and the
        provider's rendering of the same place is a different string that would
        make the next run ask a different question.
        """
        return Location(
            country=self.country, region=self.region, city=self.city,
            postal_code=self.postal_code, point=self.point,
            raw=raw or self.formatted_address,
            provenance=LocationProvenance.GEOCODED,
            precision=self.precision, confidence=self.confidence,
            geocoder=geocoder, geocoded_at=geocoded_at)


class GeocodingResult(DomainModel):
    """What one geocoder said about one request (§6).

    The outcome and the payload are validated against each other, so an adapter
    cannot return `MATCHED` with nothing matched or `AMBIGUOUS` with one
    candidate. That matters more than the usual "models validate" argument: an
    adapter is where a provider's shape meets ours, it is the code most likely to
    be written against a single example response, and `AMBIGUOUS` with one
    candidate is precisely the bug that would make §6's prohibition unenforced.

    `detail` explains a `NOT_FOUND` or a `FAILED` in a fixed vocabulary composed by
    the adapter — never a forwarded provider message and never a formatted
    exception, which is `backend/app/discovery/failures.py`'s rule for the same
    reason: a URL in an exception message carries its query string, and a query
    string carries the API key.
    """

    request: GeocodingRequest
    provider: NonEmptyStr
    outcome: GeocodingOutcome
    place: GeocodedPlace | None = None
    alternatives: tuple[GeocodedPlace, ...] = ()
    detail: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _the_payload_matches_the_outcome(self) -> Self:
        if self.outcome is GeocodingOutcome.MATCHED:
            if self.place is None:
                raise ValueError("a MATCHED result must carry the place it matched")
            return self
        if self.place is not None:
            raise ValueError(
                f"{self.outcome} carries a place, which contradicts it; only "
                "MATCHED resolves to one location")
        if self.outcome is GeocodingOutcome.AMBIGUOUS:
            if len(self.alternatives) < 2:
                raise ValueError(
                    "AMBIGUOUS means several equally plausible places, so it needs "
                    "at least two alternatives; one candidate is MATCHED and none "
                    "is NOT_FOUND")
            return self
        if self.alternatives:
            raise ValueError(f"{self.outcome} found nothing, so it has no "
                             "alternatives to offer")
        return self

    @property
    def is_usable(self) -> bool:
        """Whether this result can update a location. Only `MATCHED` can."""
        return self.outcome is GeocodingOutcome.MATCHED and self.place is not None
