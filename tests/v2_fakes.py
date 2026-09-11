# tests/v2_fakes.py
"""In-memory repositories, so the Phase 4 services can be tested without a socket.

`backend.app.services` takes repository `Protocol`s and the current instant as
arguments precisely to make this possible: the lockout window, session expiry,
CSRF check and onboarding gate are decisions the service makes, and a test that
needed PostgreSQL to observe them would be testing the driver as well.

Two properties are deliberate rather than incidental, because a fake that lacks
them proves the wrong thing:

**Stored objects are copied on the way in and out.** The real repositories go
through SQLAlchemy rows, so a caller cannot mutate the store by holding on to
what it saved. A dict of shared references would let a test pass because two
names point at one object — and the same code would fail against Postgres.

**Every read is scoped by `user_id` exactly where the contract says it is.** These
fakes are what the cross-user isolation tests run against, so a `get` that
ignored its `user_id` argument would quietly make those tests vacuous.

The Phase 6 company repositories are here for the same reason and with one extra
rule: **an upsert merges the way the real one does**. Keeping the earliest
`first_seen_at` and the latest `last_checked_at` is not an implementation detail of
SQLAlchemy — it is what makes a repeated discovery pass idempotent (§23) — so a fake
that simply overwrote would let a service test pass against behaviour PostgreSQL
does not have.
"""
import math
from collections.abc import Sequence
from datetime import datetime

from pydantic import SecretStr

from backend.app.core.tokens import digests_match
from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.common import GeoPoint
from backend.app.domain.company import (
    AtsPlatform,
    CareerSite,
    Company,
    CompanyAlias,
    CompanyDiscoveryRecord,
    CompanyLocation,
    normalize_company_name,
)
from backend.app.domain.geo import (
    GeoSearchQuery,
    GeoStatus,
    RemotePolicy,
    remote_scope_of,
)
from backend.app.domain.identifiers import (
    CandidateProfileId,
    CareerSiteId,
    CompanyAliasId,
    CompanyDiscoveryRecordId,
    CompanyId,
    OpportunityId,
    SearchProfileId,
    UserId,
    UserSessionId,
)
from backend.app.domain.opportunity import Opportunity
from backend.app.domain.search import SearchProfile
from backend.app.domain.user import User, UserSession, normalize_email
from backend.app.repositories.contracts import (
    DEFAULT_LIMIT,
    CandidateProfileRepository,
    CareerSiteRepository,
    CompanyCandidate,
    CompanyDiscoveryRepository,
    CompanyFilter,
    CompanyGeoResult,
    CompanyPage,
    CompanyRepository,
    MatchedRadius,
    OpportunityGeoResult,
    OpportunityNearby,
    OpportunityRepository,
    SearchProfileRepository,
    SessionRepository,
    UserRepository,
)

# The mean Earth radius WGS84 uses, in metres. The production queries measure on
# PostGIS's spheroid and these fakes measure on a sphere, which differ by about
# 0.5% — irrelevant for "is this site inside the radius?" and stated here so nobody
# compares a number from a fake against a number from the database.
EARTH_RADIUS_METERS = 6_371_008.8


def _implements_contracts() -> tuple[
        UserRepository, SessionRepository, CandidateProfileRepository,
        SearchProfileRepository, CompanyRepository, CareerSiteRepository,
        CompanyDiscoveryRepository, OpportunityRepository]:
    """Structural conformance, the same guard `sqlalchemy_repositories` carries.

    A fake whose signature drifted from the `Protocol` would still run — Python
    does not care — and the service tests would go on passing against an interface
    the production repositories no longer have. This return type is the check.
    `mypy` does not cover `tests/` by configuration, so run it explicitly:
    `mypy tests/v2_fakes.py`.
    """
    return (FakeUserRepository(), FakeSessionRepository(),
            FakeCandidateProfileRepository(), FakeSearchProfileRepository(),
            FakeCompanyRepository(), FakeCareerSiteRepository(),
            FakeCompanyDiscoveryRepository(), FakeOpportunityRepository())


def _meters_between(one: GeoPoint, other: GeoPoint) -> float:
    """Great-circle distance, so `list_near` answers with geography and not a guess."""
    first, second = math.radians(one.latitude), math.radians(other.latitude)
    delta_latitude = second - first
    delta_longitude = math.radians(other.longitude - one.longitude)
    chord = (math.sin(delta_latitude / 2) ** 2
             + math.cos(first) * math.cos(second) * math.sin(delta_longitude / 2) ** 2)
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(chord))



class FakeUserRepository:
    """Accounts, keyed by id, with the email index the login flow needs."""

    def __init__(self) -> None:
        self.users: dict[UserId, User] = {}

    async def get(self, user_id: UserId) -> User | None:
        return self.users.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        # `normalize_email`, not `.lower()`: the real repository applies the
        # domain's rule so a lookup cannot drift from the write, and a fake that
        # lower-cased on its own would hide a drift rather than reproduce it.
        wanted = normalize_email(email)
        return next((user for user in self.users.values() if user.email == wanted),
                    None)

    async def upsert(self, user: User) -> User:
        stored = user.model_copy(deep=True)
        self.users[stored.id] = stored
        return stored


class FakeSessionRepository:
    """Sessions, and the revocation and expiry operations, over one dict.

    `get_by_digest` compares in constant time through `digests_match` for the same
    reason the production path does — not because a test can be attacked, but
    because using the digest as a dict key would let a fake accept a token the real
    lookup rejects.
    """

    def __init__(self) -> None:
        self.sessions: dict[UserSessionId, UserSession] = {}

    async def get_by_digest(self, token_digest: SecretStr) -> UserSession | None:
        for session in self.sessions.values():
            if digests_match(token_digest, session.token_digest):
                return session.model_copy(deep=True)
        return None

    async def upsert(self, session: UserSession) -> UserSession:
        stored = session.model_copy(deep=True)
        self.sessions[stored.id] = stored
        return stored

    async def revoke(self, user_id: UserId, session_id: UserSessionId,
                     revoked_at: datetime) -> bool:
        session = self.sessions.get(session_id)
        if session is None or session.user_id != user_id \
                or session.revoked_at is not None:
            return False
        self.sessions[session_id] = session.model_copy(
            update={"revoked_at": revoked_at})
        return True

    async def revoke_all_for_user(self, user_id: UserId,
                                  revoked_at: datetime) -> int:
        live = [session for session in self.sessions.values()
                if session.user_id == user_id and session.revoked_at is None]
        for session in live:
            self.sessions[session.id] = session.model_copy(
                update={"revoked_at": revoked_at})
        return len(live)

    async def delete_expired(self, as_of: datetime, *,
                             limit: int = DEFAULT_LIMIT) -> int:
        doomed = sorted((session for session in self.sessions.values()
                         if session.expires_at < as_of),
                        key=lambda session: session.expires_at)[:limit]
        for session in doomed:
            del self.sessions[session.id]
        return len(doomed)


class FakeCandidateProfileRepository:
    """Profiles, with the Phase 10 refusal the real repository performs.

    The refusal is reproduced rather than skipped because `OnboardingService`
    relies on it: `save_profile` leaves `evidence` and `claims` empty, and a fake
    that accepted them would let a future change start dropping them silently.
    """

    def __init__(self) -> None:
        self.profiles: dict[CandidateProfileId, CandidateProfile] = {}

    async def get(self, user_id: UserId,
                  profile_id: CandidateProfileId) -> CandidateProfile | None:
        profile = self.profiles.get(profile_id)
        if profile is None or profile.user_id != user_id:
            return None
        return profile.model_copy(deep=True)

    async def get_default(self, user_id: UserId) -> CandidateProfile | None:
        return next(iter(await self.list_for_user(user_id)), None)

    async def upsert(self, profile: CandidateProfile) -> CandidateProfile:
        if profile.evidence or profile.claims:
            raise ValueError(
                "candidate evidence and claims have no V2 persistence yet (Phase 10)")
        stored = profile.model_copy(deep=True)
        self.profiles[stored.id] = stored
        return stored

    async def list_for_user(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT
                            ) -> tuple[CandidateProfile, ...]:
        # Insertion order, which is oldest-first and therefore what the real
        # query's `ORDER BY created_at, id` produces. `CandidateProfile` has no
        # `created_at` of its own — the column is a server default — so the dict's
        # order is the only thing a fake can honestly sort by. It is also stable
        # across a re-upsert, which is what `get_default` depends on.
        return tuple(profile.model_copy(deep=True)
                     for profile in self.profiles.values()
                     if profile.user_id == user_id)[:limit]


class FakeSearchProfileRepository:
    """Saved searches, with the `active_only` filter and the scoped delete."""

    def __init__(self) -> None:
        self.searches: dict[SearchProfileId, SearchProfile] = {}

    async def get(self, user_id: UserId,
                  search_profile_id: SearchProfileId) -> SearchProfile | None:
        search = self.searches.get(search_profile_id)
        if search is None or search.user_id != user_id:
            return None
        return search.model_copy(deep=True)

    async def upsert(self, profile: SearchProfile) -> SearchProfile:
        stored = profile.model_copy(deep=True)
        self.searches[stored.id] = stored
        return stored

    async def delete(self, user_id: UserId,
                     search_profile_id: SearchProfileId) -> bool:
        search = self.searches.get(search_profile_id)
        if search is None or search.user_id != user_id:
            return False
        del self.searches[search_profile_id]
        return True

    async def list_for_user(self, user_id: UserId, *, active_only: bool = False,
                            limit: int = DEFAULT_LIMIT) -> tuple[SearchProfile, ...]:
        mine = [search.model_copy(deep=True) for search in self.searches.values()
                if search.user_id == user_id
                and (search.is_active or not active_only)]
        # Newest first, like the real query.
        mine.sort(key=lambda search: (search.created_at, search.id), reverse=True)
        return tuple(mine[:limit])


class FakeOpportunityRepository:
    """Postings, with the seed query and the one-column link the resolver uses.

    Nothing here is user-scoped, and that is the contract rather than an omission:
    a posting is a shared fact (§21).

    `companies` is the store `search_geo` falls back to when a posting has no
    coordinates of its own (§9, §31). It is late-bound rather than a constructor
    argument because the company repository is built *with* this one — the API
    harness wires the pair together after both exist — and `None` means "no
    fallback source", which is the honest answer for the resolver tests that
    never set it.
    """

    def __init__(self, companies: "FakeCompanyRepository | None" = None) -> None:
        self.opportunities: dict[OpportunityId, Opportunity] = {}
        self._companies = companies

    async def get(self, opportunity_id: OpportunityId) -> Opportunity | None:
        stored = self.opportunities.get(opportunity_id)
        return None if stored is None else stored.model_copy(deep=True)

    async def get_by_source(self, source_key: str,
                            external_id: str) -> Opportunity | None:
        return next((posting.model_copy(deep=True)
                     for posting in self.opportunities.values()
                     if posting.source.source_key == source_key
                     and posting.source.external_id == external_id), None)

    async def get_by_fingerprint(self, fingerprint: str) -> Opportunity | None:
        return next((posting.model_copy(deep=True)
                     for posting in self.opportunities.values()
                     if posting.dedup_fingerprint == fingerprint), None)

    async def upsert(self, opportunity: Opportunity) -> Opportunity:
        stored = opportunity.model_copy(deep=True)
        self.opportunities[stored.id] = stored
        return stored

    async def list_recent(self, *,
                          limit: int = DEFAULT_LIMIT) -> tuple[Opportunity, ...]:
        newest = sorted(self.opportunities.values(),
                        key=lambda posting: str(posting.id))
        newest.sort(key=lambda posting: posting.discovered_at, reverse=True)
        return tuple(posting.model_copy(deep=True) for posting in newest[:limit])

    async def list_near(self, center: GeoPoint, radius_meters: float, *,
                        limit: int = DEFAULT_LIMIT) -> tuple[OpportunityNearby, ...]:
        nearby = []
        for posting in self.opportunities.values():
            point = posting.location.point if posting.location else None
            if point is None:
                continue
            meters = _meters_between(center, point)
            if meters <= radius_meters:
                nearby.append(OpportunityNearby(posting.model_copy(deep=True), meters))
        nearby.sort(key=lambda found: (found.distance_meters, found.opportunity.id))
        return tuple(nearby[:limit])

    async def search_geo(
            self, query: GeoSearchQuery) -> tuple[OpportunityGeoResult, ...]:
        """The typed Phase 7 query, mirroring `SqlAlchemyOpportunityRepository`.

        Every branch here has a twin in the SQL: the per-radius `own point OR
        (unresolved AND employer site)` union, the country fallback through the
        nearest site, the explicit remote policy, and the
        RESOLVED/COMPANY_FALLBACK/UNRESOLVED status a null distance would
        otherwise hide (§12). A fake that took a shortcut on any of them would let
        a service test pass against behaviour PostGIS does not have.
        """
        found: list[OpportunityGeoResult] = []
        for posting in self.opportunities.values():
            matched = self._match_opportunity(posting, query)
            if matched is not None:
                found.append(matched)
        # The SQL's `ORDER BY`, built least-significant key first so the stable
        # sort composes: id, then discovered_at DESC, then — with centres — the
        # result distance NULLS LAST.
        found.sort(key=lambda result: str(result.opportunity.id))
        found.sort(key=lambda result: result.opportunity.discovered_at, reverse=True)
        if query.radii:
            found.sort(key=lambda result: (
                result.distance_meters is None,
                result.distance_meters if result.distance_meters is not None else 0.0))
        return tuple(found[query.offset:query.offset + query.limit])

    def _match_opportunity(self, posting: Opportunity,
                           query: GeoSearchQuery) -> OpportunityGeoResult | None:
        own_point = posting.location.point if posting.location else None
        own_country = posting.location.country if posting.location else None
        is_remote = posting.is_remote

        site = self._fallback_site(posting.company_id, query)
        site_point = site.location.point if site is not None else None
        site_country = site.location.country if site is not None else None

        own_matches = [
            own_point is not None
            and _meters_between(radius.center, own_point) <= radius.radius.meters
            for radius in query.radii]
        site_matches = [
            site_point is not None
            and _meters_between(radius.center, site_point) <= radius.radius.meters
            for radius in query.radii]
        # Per radius: the posting's own point, or — only when it has none — its
        # employer's nearest site. The SQL is `opp_match OR (~resolved AND site)`.
        radius_hits = [own or (own_point is None and site_hit)
                       for own, site_hit in zip(own_matches, site_matches, strict=True)]

        geographic = any(radius_hits)
        if query.countries:
            geographic = geographic or own_country in query.countries or (
                own_country is None and site_country in query.countries)

        if query.remote_policy is RemotePolicy.REMOTE_ONLY:
            if not is_remote:
                return None
        elif query.remote_policy is RemotePolicy.INCLUDE_REMOTE:
            if not (geographic or is_remote):
                return None
        elif not geographic or is_remote:      # EXCLUDE_REMOTE: geographic, not remote
            return None

        if query.remote_countries and is_remote \
                and own_country not in query.remote_countries:
            return None
        if query.opportunity_types \
                and posting.opportunity_type not in query.opportunity_types:
            return None
        if query.workplace_modes \
                and posting.workplace_mode not in query.workplace_modes:
            return None
        if query.bounds is not None:
            inside = (own_point is not None and query.bounds.contains(own_point)) or (
                own_point is None and site_point is not None
                and query.bounds.contains(site_point))
            if not inside:
                return None

        has_own_point = own_point is not None
        fallback = not has_own_point and site is not None and site_point is not None
        company_location = (site.model_copy(deep=True)
                            if fallback and site is not None else None)
        opportunity = posting.model_copy(deep=True)
        result_location = (company_location.location if company_location is not None
                           else opportunity.location)

        distance: float | None = None
        if query.radii:
            if own_point is not None:
                distance = min(_meters_between(radius.center, own_point)
                               for radius in query.radii)
            elif site_point is not None:
                distance = min(_meters_between(radius.center, site_point)
                               for radius in query.radii)
        # The distance and the matched radii report the point that was actually
        # used, so a fallback row's `matched_radii` names the site's branch.
        selected = own_matches if has_own_point else site_matches
        matched_radii = tuple(MatchedRadius(index, query.radii[index].label)
                              for index, hit in enumerate(selected) if hit)

        return OpportunityGeoResult(
            opportunity=opportunity,
            location=result_location,
            distance_meters=distance,
            status=(GeoStatus.REMOTE if is_remote else
                    GeoStatus.RESOLVED if has_own_point else
                    GeoStatus.COMPANY_FALLBACK if fallback else
                    GeoStatus.UNRESOLVED),
            remote_scope=remote_scope_of(opportunity.workplace_mode,
                                         opportunity.location),
            matched_radii=matched_radii,
            company_location=company_location)

    def _fallback_site(self, company_id: CompanyId | None,
                       query: GeoSearchQuery) -> CompanyLocation | None:
        """The employer site that stands in for a posting with no point of its own.

        Mirrors the correlated `nearest_site` subquery: the closest site to a
        search centre when the query has radii, otherwise the headquarters, ties
        broken by id. Chosen among *all* the employer's sites, exactly like the
        subquery — the radius predicate is then applied to whichever site this
        returns, not used to select it.
        """
        if company_id is None or self._companies is None:
            return None
        company = self._companies.companies.get(company_id)
        if company is None or not company.locations:
            return None
        if query.radii:
            return min(company.locations,
                       key=lambda site: self._site_rank(site, query))
        return min(company.locations,
                   key=lambda site: (not site.is_headquarters, str(site.id)))

    def _site_rank(self, site: CompanyLocation,
                   query: GeoSearchQuery) -> tuple[bool, float, str]:
        point = site.location.point
        if point is None:
            return (True, 0.0, str(site.id))      # NULL distance sorts last
        nearest = min(_meters_between(radius.center, point) for radius in query.radii)
        return (False, nearest, str(site.id))

    async def list_unlinked(self, *,
                            limit: int = DEFAULT_LIMIT) -> tuple[Opportunity, ...]:
        # `company_name != ""` is the real query's second predicate, reproduced
        # because it is what stops an unresolvable posting heading every pass
        # forever. Oldest first, for the same reason: a bounded pass has to make
        # progress through the backlog.
        backlog = sorted((posting for posting in self.opportunities.values()
                          if posting.company_id is None and posting.company_name),
                         key=lambda posting: (posting.discovered_at, posting.id))
        return tuple(posting.model_copy(deep=True) for posting in backlog[:limit])

    async def link_company(self, opportunity_id: OpportunityId,
                           company_id: CompanyId) -> bool:
        posting = self.opportunities.get(opportunity_id)
        if posting is None or posting.company_id == company_id:
            return False
        # One field, exactly as the real `UPDATE` writes one column: §13's rule that
        # the posting keeps the employer name the board published is structural here
        # too, not a convention this fake could forget.
        self.opportunities[opportunity_id] = posting.model_copy(
            update={"company_id": company_id})
        return True


class FakeCompanyRepository:
    """Employers and their labels, with the two lookups identity resolution needs.

    `postings` is optional and exists for one filter: `has_opportunities` is an
    `EXISTS` over another table in SQL, so a fake that ignored it would answer §28's
    "employers with no opportunity" question with every employer.
    """

    def __init__(self, postings: "FakeOpportunityRepository | None" = None) -> None:
        self.companies: dict[CompanyId, Company] = {}
        self.company_aliases: dict[CompanyAliasId, CompanyAlias] = {}
        self._postings = postings

    async def get(self, company_id: CompanyId) -> Company | None:
        stored = self.companies.get(company_id)
        return None if stored is None else stored.model_copy(deep=True)

    async def upsert(self, company: Company) -> Company:
        stored = company.model_copy(deep=True)
        self.companies[stored.id] = stored
        return stored

    async def list_near(self, center: GeoPoint, radius_meters: float, *,
                        limit: int = DEFAULT_LIMIT) -> tuple[Company, ...]:
        found = [company for company in self._ordered()
                 if any(site.location.point is not None
                        and _meters_between(center, site.location.point)
                        <= radius_meters
                        for site in company.locations)]
        return tuple(company.model_copy(deep=True) for company in found[:limit])

    async def search_geo(self, query: GeoSearchQuery) -> tuple[CompanyGeoResult, ...]:
        """Each employer once, at its closest matching site — the SQL's `row_number`.

        REMOTE_ONLY returns nothing, exactly as the real query does: an employer is
        a place, and "remote only" is a question about postings, not companies.
        """
        if query.remote_policy is RemotePolicy.REMOTE_ONLY:
            return ()
        found: list[CompanyGeoResult] = []
        for company in self._ordered():
            best = self._best_site(company, query)
            if best is None:
                continue
            site, distance, hits = best
            found.append(CompanyGeoResult(
                company=company.model_copy(deep=True),
                location=site.model_copy(deep=True),
                distance_meters=distance,
                status=(GeoStatus.RESOLVED if site.location.point is not None
                        else GeoStatus.UNRESOLVED),
                matched_radii=tuple(MatchedRadius(index, query.radii[index].label)
                                    for index, hit in enumerate(hits) if hit)))
        # `_ordered()` already sorts by name then id; this stable distance sort
        # keeps that order among equal distances — the SQL's `distance NULLS LAST,
        # name, id`.
        if query.radii:
            found.sort(key=lambda result: (
                result.distance_meters is None,
                result.distance_meters if result.distance_meters is not None else 0.0))
        return tuple(found[query.offset:query.offset + query.limit])

    def _best_site(
            self, company: Company, query: GeoSearchQuery
    ) -> tuple[CompanyLocation, float | None, list[bool]] | None:
        candidates: list[tuple[CompanyLocation, float | None, list[bool]]] = []
        for site in company.locations:
            point = site.location.point
            hits = [point is not None
                    and _meters_between(radius.center, point) <= radius.radius.meters
                    for radius in query.radii]
            matched = any(hits) or (
                bool(query.countries) and site.location.country in query.countries)
            if not matched:
                continue
            if query.bounds is not None and (
                    point is None or not query.bounds.contains(point)):
                continue
            distance = (min(_meters_between(radius.center, point)
                            for radius in query.radii)
                        if query.radii and point is not None else None)
            candidates.append((site, distance, hits))
        if not candidates:
            return None
        if query.radii:
            return min(candidates, key=lambda item: (
                item[1] is None, item[1] if item[1] is not None else 0.0,
                str(item[0].id)))
        return min(candidates,
                   key=lambda item: (not item[0].is_headquarters, str(item[0].id)))

    async def find_candidates(
            self, *, name_forms: Sequence[str] = (), domain: str | None = None,
            ats_platform: AtsPlatform | None = None,
            ats_organization_id: str | None = None,
            limit: int = DEFAULT_LIMIT) -> tuple[CompanyCandidate, ...]:
        forms = {form for form in name_forms if form}
        organization = ats_platform is not None and bool(ats_organization_id)
        # No evidence means no shortlist, never "the first hundred employers": a
        # caller holding nothing would otherwise be handed candidates nothing
        # connects to its claim.
        if not forms and not domain and not organization:
            return ()
        found = []
        for company in self._ordered():
            labels = await self.aliases(company.id)
            matched = (
                (bool(forms) and company.normalized_name in forms)
                or any(alias.normalized_alias in forms for alias in labels)
                or (bool(domain) and company.normalized_domain == domain)
                or (organization and company.detected_ats is not None
                    and company.detected_ats.platform is ats_platform
                    and company.detected_ats.organization_id == ats_organization_id))
            if matched:
                found.append(CompanyCandidate(company.model_copy(deep=True), labels))
        return tuple(found[:limit])

    async def search(self, filters: CompanyFilter, *, limit: int = DEFAULT_LIMIT,
                     offset: int = 0) -> CompanyPage:
        matched = [company for company in self._ordered()
                   if await self._matches(company, filters)]
        page = matched[offset:offset + limit]
        return CompanyPage(
            companies=tuple(company.model_copy(deep=True) for company in page),
            total=len(matched))

    async def aliases(self, company_id: CompanyId) -> tuple[CompanyAlias, ...]:
        mine = sorted((alias for alias in self.company_aliases.values()
                       if alias.company_id == company_id),
                      key=lambda alias: (alias.first_seen_at, alias.id))
        return tuple(alias.model_copy(deep=True) for alias in mine)

    async def upsert_alias(self, alias: CompanyAlias) -> CompanyAlias:
        existing = self.company_aliases.get(alias.id)
        if existing is not None:
            # The union of the two windows, like the real upsert: rediscovering a
            # label must not make it look newly found (§23).
            alias = alias.model_copy(update={
                "first_seen_at": min(alias.first_seen_at, existing.first_seen_at),
                "last_seen_at": max(alias.last_seen_at, existing.last_seen_at)})
        stored = alias.model_copy(deep=True)
        self.company_aliases[stored.id] = stored
        return stored

    def _ordered(self) -> list[Company]:
        """By name then id, which is the order the real query pages through."""
        return sorted(self.companies.values(),
                      key=lambda company: (company.name, str(company.id)))

    async def _matches(self, company: Company, filters: CompanyFilter) -> bool:
        if filters.text is not None:
            key = normalize_company_name(filters.text)
            labels = await self.aliases(company.id)
            if not key or not (key in company.normalized_name
                               or any(key in alias.normalized_alias
                                      for alias in labels)):
                return False
        if filters.country is not None and company.country != filters.country:
            return False
        platform = company.detected_ats.platform if company.detected_ats else None
        if filters.ats_platform is not None and platform is not filters.ats_platform:
            return False
        channel = company.spontaneous_application_channel
        support = channel.support if channel else None
        if filters.spontaneous_support is not None \
                and support is not filters.spontaneous_support:
            return False
        if filters.has_opportunities is not None:
            linked = any(posting.company_id == company.id for posting
                         in (self._postings.opportunities.values()
                             if self._postings else ()))
            if linked is not filters.has_opportunities:
                return False
        return True


class FakeCareerSiteRepository:
    """Careers endpoints, keyed like their rows and merged like their upsert."""

    def __init__(self) -> None:
        self.sites: dict[CareerSiteId, CareerSite] = {}

    async def list_for_company(self, company_id: CompanyId) -> tuple[CareerSite, ...]:
        mine = sorted((site for site in self.sites.values()
                       if site.company_id == company_id),
                      key=lambda site: (site.discovered_at, site.id))
        return tuple(site.model_copy(deep=True) for site in mine)

    async def upsert(self, site: CareerSite) -> CareerSite:
        existing = self.sites.get(site.id)
        if existing is not None:
            checked = [instant for instant
                       in (site.last_checked_at, existing.last_checked_at)
                       if instant is not None]
            site = site.model_copy(update={
                "discovered_at": min(site.discovered_at, existing.discovered_at),
                "last_checked_at": max(checked) if checked else None})
        stored = site.model_copy(deep=True)
        self.sites[stored.id] = stored
        return stored


class FakeCompanyDiscoveryRepository:
    """Sightings, which are appended and refreshed and never deleted."""

    def __init__(self) -> None:
        self.records: dict[CompanyDiscoveryRecordId, CompanyDiscoveryRecord] = {}

    async def get_by_external(self, provider_key: str,
                              external_id: str) -> CompanyDiscoveryRecord | None:
        return next((record.model_copy(deep=True)
                     for record in self.records.values()
                     if record.provider_key == provider_key
                     and record.external_id == external_id), None)

    async def upsert(self, record: CompanyDiscoveryRecord) -> CompanyDiscoveryRecord:
        existing = self.records.get(record.id)
        if existing is not None:
            record = record.model_copy(update={
                "discovered_at": min(record.discovered_at, existing.discovered_at)})
        stored = record.model_copy(deep=True)
        self.records[stored.id] = stored
        return stored

    async def list_for_company(
            self, company_id: CompanyId, *,
            limit: int = DEFAULT_LIMIT) -> tuple[CompanyDiscoveryRecord, ...]:
        mine = sorted((record for record in self.records.values()
                       if record.company_id == company_id),
                      key=lambda record: str(record.id))
        # Most recent first, ties broken by id *ascending* — the two halves of the
        # real `ORDER BY`, which a single reversed sort would get backwards.
        mine.sort(key=lambda record: record.discovered_at, reverse=True)
        return tuple(record.model_copy(deep=True) for record in mine[:limit])
