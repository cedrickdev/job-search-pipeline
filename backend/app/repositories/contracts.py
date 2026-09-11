"""What the application needs from storage, in domain terms.

Three rules shape every signature here:

**No transaction control.** There is no `commit`, no `rollback` and no `begin`. A
repository that commits decides on its caller's behalf that a unit of work is
over, which is how an import ends up half-applied; the boundary is
`backend.app.infrastructure.database.engine.session_scope`.

**User-scoped reads take the user first.** `list_for_user(user_id, …)` cannot be
called without saying whose data is wanted, so an authorization filter is
impossible to forget in Phase 4 (docs/ENGINEERING_STANDARDS.md §Security:
authorization enforced server-side). `Opportunity` and `Company` are shared facts
and deliberately have no such parameter, and the two authentication lookups that
*establish* identity — `get_by_email` and `get_by_digest` — cannot have one.

**Writes are upserts.** Every persisted entity has an id the domain produced —
uuid5 for anything derived from V1, uuid4 otherwise — so "create" and "update"
are the same operation from the caller's point of view, and retrying a failed
import is safe by construction.

Small on purpose: a method nobody calls yet is a guess about Phase 12 that would
have to be unguessed. Phase 6 added the company-side contracts, and split them the
way §16 asks — one for employers and their aliases, one for careers endpoints, one
for discovery provenance — rather than growing `CompanyRepository` into the object
every company service would have to be handed whole.
"""
from collections.abc import Sequence
from datetime import datetime
from typing import NamedTuple, Protocol, runtime_checkable

from pydantic import SecretStr

from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.common import GeoPoint, Location
from backend.app.domain.company import (
    AtsPlatform,
    CareerSite,
    Company,
    CompanyAlias,
    CompanyDiscoveryRecord,
    CompanyLocation,
    SpontaneousApplicationSupport,
)
from backend.app.domain.geo import GeoSearchQuery, GeoStatus, RemoteScope
from backend.app.domain.identifiers import (
    CandidateProfileId,
    CompanyId,
    MatchEvaluationId,
    OpportunityId,
    SearchProfileId,
    UserId,
    UserSessionId,
)
from backend.app.domain.matching import MatchEvaluation
from backend.app.domain.opportunity import Opportunity
from backend.app.domain.search import SearchProfile
from backend.app.domain.user import User, UserSession

# Every list method is capped. An uncapped query is fine against the empty
# development database and is an outage against a real one, and the caller that
# wants everything can say so explicitly.
DEFAULT_LIMIT = 100


class OpportunityNearby(NamedTuple):
    """An opportunity and how far it is from the point that was searched.

    The distance is computed by the database, not recomputed in Python: it comes
    from the same spheroid calculation that decided the row matched, so a result
    can never be shown as 101 km away by a query for everything within 100 km.
    """

    opportunity: Opportunity
    distance_meters: float


class MatchedRadius(NamedTuple):
    """One radius branch that admitted a geographic result."""

    radius_index: int
    label: str | None


class OpportunityGeoResult(NamedTuple):
    """One posting in a geo page, including why and where it matched."""

    opportunity: Opportunity
    location: Location | None
    distance_meters: float | None
    status: GeoStatus
    remote_scope: RemoteScope | None
    matched_radii: tuple[MatchedRadius, ...] = ()
    company_location: CompanyLocation | None = None


class CompanyGeoResult(NamedTuple):
    """One employer in a geo page, represented by its best matching site."""

    company: Company
    location: CompanyLocation
    distance_meters: float | None
    status: GeoStatus
    matched_radii: tuple[MatchedRadius, ...] = ()


class CompanyCandidate(NamedTuple):
    """A company that *might* be the one a caller is holding evidence about.

    Carries the aliases with it because the comparison needs them: an employer
    stored as `Logitech` with a confirmed alias `Logitech Europe S.A.` is a match on
    a strong signal, and a shortlist that omitted the aliases would force the
    resolver back to name similarity — which §2 forbids as a merge reason.
    """

    company: Company
    aliases: tuple[CompanyAlias, ...]


class CompanyFilter(NamedTuple):
    """The company-directory query, as the values §20 lists.

    Every field defaults to `None`, meaning "do not restrict on this". `None` and
    "the user asked for the unknown ones" are different questions, which is why
    `spontaneous_support` is an enum member rather than a boolean: `UNKNOWN` is a
    filterable answer here, not the absence of one.
    """

    text: str | None = None
    country: str | None = None
    ats_platform: AtsPlatform | None = None
    spontaneous_support: SpontaneousApplicationSupport | None = None
    has_opportunities: bool | None = None


class CompanyPage(NamedTuple):
    """One page of companies plus how many the filter matched in total.

    The total is what lets a caller say "showing 1–20 of 143" without asking for
    143 rows, and §20's "do not return unbounded company lists" is the reason it is
    a count rather than the rest of the list.
    """

    companies: tuple[Company, ...]
    total: int


@runtime_checkable
class CompanyRepository(Protocol):
    """Employers, with their sites, their aliases and the lookups identity needs.

    Nothing here takes a `user_id`, and that is a decision rather than an omission
    (§21): a company is a shared fact, and a per-user column on the table everyone
    queries would be the first step towards one employer directory per account.
    """

    async def get(self, company_id: CompanyId) -> Company | None:
        """The company and all of its locations, or `None`."""
        ...

    async def upsert(self, company: Company) -> Company:
        """Write the company and reconcile its locations.

        Returns what is now stored, which is how a caller learns that a location
        it did not include has been deleted.

        Aliases, careers endpoints and discovery records are *not* touched: each has
        its own upsert, because a provider that knows one careers endpoint must not
        erase what another provider found.
        """
        ...

    async def list_near(self, center: GeoPoint, radius_meters: float, *,
                        limit: int = DEFAULT_LIMIT) -> tuple[Company, ...]:
        """Companies with at least one site within `radius_meters` of `center`.

        The Phase 6 spontaneous-application question ("who is nearby") in its
        minimal form. Phase 7 builds the explorer on top of it.
        """
        ...

    async def search_geo(self, query: GeoSearchQuery) -> tuple[CompanyGeoResult, ...]:
        """Companies matching a bounded geographic query, once per employer."""
        ...

    async def find_candidates(
            self, *, name_forms: Sequence[str] = (), domain: str | None = None,
            ats_platform: AtsPlatform | None = None,
            ats_organization_id: str | None = None,
            limit: int = DEFAULT_LIMIT) -> tuple[CompanyCandidate, ...]:
        """The shortlist `companies.resolution.resolve` then judges (§2, §13).

        Every argument is a *comparison key* the caller derived — the output of
        `resolution.name_lookup_keys` and `strong_lookup_keys` — and the query is a
        union of exact matches over the indexed columns: normalized name, normalized
        domain, ATS organization, normalized alias. No fuzzy matching, because there
        is none to do: §2 forbids merging on name similarity, so a `LIKE` here would
        widen a shortlist that a comparison must then be careful to reject.

        Deciding is emphatically not this method's job. It returns everything
        comparable, including candidates that will turn out to be `DISTINCT`, and
        `resolve` weighs them. A repository that filtered would be making the
        identity decision where no evidence is visible.
        """
        ...

    async def search(self, filters: CompanyFilter, *, limit: int = DEFAULT_LIMIT,
                     offset: int = 0) -> CompanyPage:
        """One page of the company directory, plus how many matched (§20).

        Ordered by name and then id, so a page is stable across requests: an
        `ORDER BY` that ties would let the same company appear on two pages and
        another appear on none.
        """
        ...

    async def aliases(self, company_id: CompanyId) -> tuple[CompanyAlias, ...]:
        """Every label recorded for this employer, oldest sighting first."""
        ...

    async def upsert_alias(self, alias: CompanyAlias) -> CompanyAlias:
        """Record a label, or move an existing one's `last_seen_at` forward (§4, §23).

        The idempotent half of Phase 6's alias handling, and the reason it is a
        repository method rather than a mapper concern: keeping the *earliest*
        `first_seen_at` requires seeing what is already stored, so a caller that
        rediscovers `LOGITECH` on every sweep gets one row whose window grows rather
        than a second row claiming today as the first sighting.
        """
        ...


@runtime_checkable
class CareerSiteRepository(Protocol):
    """Careers endpoints, one row per URL an employer publishes (§11).

    Separate from `CompanyRepository` because the write patterns differ: a company
    is written whole by whoever resolved it, while a careers endpoint is discovered
    one at a time by providers that each know about one. A single `upsert` on the
    company would make the second provider's find erase the first one's.
    """

    async def list_for_company(self, company_id: CompanyId) -> tuple[CareerSite, ...]:
        """Every endpoint recorded for this employer, oldest discovery first."""
        ...

    async def upsert(self, site: CareerSite) -> CareerSite:
        """Write the endpoint, or refresh what is known about one already stored.

        Idempotent per `(company_id, url)` and, like `upsert_alias`, it keeps the
        *earliest* `discovered_at` and the *latest* `last_checked_at`: rediscovering
        a board must not make it look newly found, and a pass that did not check it
        must not erase the instant one that did recorded.

        Everything else comes from the argument. Whether a rediscovery may change a
        `CONFIRMED` verification back to `LIKELY` is an evidence question (§10), and
        the service that weighed the evidence is the only layer that can answer it.
        """
        ...


@runtime_checkable
class CompanyDiscoveryRepository(Protocol):
    """How each company came to be known — the provenance §5 asks for.

    Records are append-and-refresh, never deleted with the company they point at:
    `company_id` is nullable so an ambiguous seed can be recorded unlinked (§14) and
    a later explicit merge (§24) does not destroy the evidence that revealed the
    duplication.
    """

    async def get_by_external(self, provider_key: str,
                              external_id: str) -> CompanyDiscoveryRecord | None:
        """What this provider already recorded under this identifier, or `None`.

        The idempotency lookup for a discovery pass, and the reason §23's
        "re-running creates no duplicates" is a property of the identifier rather
        than of a heuristic: `(provider_key, external_id)` is unique in the schema.
        """
        ...

    async def upsert(self, record: CompanyDiscoveryRecord) -> CompanyDiscoveryRecord:
        """Write the sighting, keeping the first `discovered_at` it ever had."""
        ...

    async def list_for_company(
            self, company_id: CompanyId, *,
            limit: int = DEFAULT_LIMIT) -> tuple[CompanyDiscoveryRecord, ...]:
        """Every sighting linked to this employer, most recent first.

        What the company detail endpoint turns into "discovered via" (§29) — after
        the API layer drops `raw`, which stays behind the backend boundary.
        """
        ...


@runtime_checkable
class OpportunityRepository(Protocol):
    """Postings — shared facts, so nothing here is user-scoped."""

    async def get(self, opportunity_id: OpportunityId) -> Opportunity | None: ...

    async def get_by_source(self, source_key: str,
                            external_id: str) -> Opportunity | None:
        """The posting a source already published under this identifier.

        The idempotency lookup for any importer or discovery run: it answers
        "have I seen this one before?" without depending on how the id was
        derived.
        """
        ...

    async def get_by_fingerprint(self, fingerprint: str) -> Opportunity | None:
        """The posting carrying this dedup fingerprint, whatever source found it."""
        ...

    async def upsert(self, opportunity: Opportunity) -> Opportunity: ...

    async def list_recent(self, *,
                          limit: int = DEFAULT_LIMIT) -> tuple[Opportunity, ...]:
        """Newest discoveries first — the shape of the feed."""
        ...

    async def list_near(self, center: GeoPoint, radius_meters: float, *,
                        limit: int = DEFAULT_LIMIT) -> tuple[OpportunityNearby, ...]:
        """Postings within `radius_meters` of `center`, closest first."""
        ...

    async def search_geo(
            self, query: GeoSearchQuery) -> tuple[OpportunityGeoResult, ...]:
        """Postings admitted by one typed geographic/remote query."""
        ...

    async def list_unlinked(self, *,
                            limit: int = DEFAULT_LIMIT) -> tuple[Opportunity, ...]:
        """Postings that name an employer but are not linked to one yet (§27).

        The seed query for opportunity-derived company discovery, oldest first so
        repeated bounded passes work through the backlog instead of re-reading the
        same page. A posting whose `company_name` is empty is not returned: there is
        nothing to resolve, and returning it would guarantee an unresolvable seed on
        every pass forever.
        """
        ...

    async def link_company(self, opportunity_id: OpportunityId,
                           company_id: CompanyId) -> bool:
        """Point one posting at a resolved employer; `True` if that changed a row.

        Deliberately not `upsert`. This writes `company_id` and nothing else, so
        §13's rule — "the posting's original company name remains provenance" — is
        structural rather than remembered: there is no argument here through which
        `company_name` could be overwritten. Idempotent, and returns `False` when
        the posting was already linked to this company.
        """
        ...


@runtime_checkable
class MatchEvaluationRepository(Protocol):
    """Evaluations — user-owned, so every method names the owner.

    `user_id` is a parameter and not an ambient value on purpose. A repository
    that read the current user from a context variable would let a service forget
    the filter and still compile; here the filter is part of the call.
    """

    async def get(self, user_id: UserId,
                  evaluation_id: MatchEvaluationId) -> MatchEvaluation | None:
        """The evaluation, or `None` — including when it belongs to somebody else.

        Not found and not yours are deliberately indistinguishable: a caller that
        could tell them apart could enumerate another user's rows by id
        (docs/ENGINEERING_STANDARDS.md §Security: no cross-user data leakage).
        """
        ...

    async def get_for_pair(self, user_id: UserId,
                           candidate_profile_id: CandidateProfileId,
                           opportunity_id: OpportunityId) -> MatchEvaluation | None:
        """This profile's verdict on this posting, if it has been evaluated."""
        ...

    async def upsert(self, evaluation: MatchEvaluation) -> MatchEvaluation:
        """Write the evaluation and reconcile its dimension scores.

        The owner comes from `evaluation.user_id`, so there is no signature in
        which the row's owner and the caller's intent can disagree.
        """
        ...

    async def list_for_user(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT) -> tuple[MatchEvaluation, ...]:
        """This user's evaluations, most recently evaluated first."""
        ...


# Phase 4. The four contracts below are what authentication and onboarding need,
# and nothing more. Two of them break the "user first" convention, both for the
# same reason: `UserRepository.get_by_email` and `SessionRepository.get_by_digest`
# are how a caller *establishes* which user it is talking about, so there is no
# `user_id` to pass yet. Every other method here takes one.


@runtime_checkable
class UserRepository(Protocol):
    """Accounts, looked up by the two things a login flow has: an id or an email."""

    async def get(self, user_id: UserId) -> User | None: ...

    async def get_by_email(self, email: str) -> User | None:
        """The account registered under this address, or `None`.

        The caller passes the address as the user typed it; the implementation
        normalizes it the same way `EmailAddress` does, so a login with
        `Ada@Example.com` finds the account stored as `ada@example.com` rather
        than reporting no such user.
        """
        ...

    async def upsert(self, user: User) -> User:
        """Write the account.

        Also the update path for the lockout counters and `last_login_at`, which
        is why there is no separate `record_failed_login`: those are fields of the
        aggregate, and a second write path would be a second place to forget
        `updated_at`.
        """
        ...


@runtime_checkable
class SessionRepository(Protocol):
    """Server-side sessions: issue one, find one by its digest, revoke them.

    Every method speaks in digests. The raw token never reaches this layer, so
    there is no signature here that could accidentally store one
    (docs/AUTHENTICATION.md §Sessions).
    """

    async def get_by_digest(self, token_digest: SecretStr) -> UserSession | None:
        """The session whose stored digest matches, whatever its state.

        Expired and revoked sessions are returned rather than filtered out: the
        caller holds the clock (`UserSession.is_usable`), and a repository that
        applied its own `now()` would make expiry untestable and disagree with the
        service the moment the two clocks differed by a millisecond.
        """
        ...

    async def upsert(self, session: UserSession) -> UserSession:
        """Write the session — the issue path and the `last_seen_at` touch."""
        ...

    async def revoke(self, user_id: UserId, session_id: UserSessionId,
                     revoked_at: datetime) -> bool:
        """Revoke one of this user's sessions; `True` if it was live until now.

        Scoped by `user_id` so a logout cannot revoke somebody else's session by
        id, and idempotent: revoking an already-revoked session returns `False`
        and leaves the original instant intact.
        """
        ...

    async def revoke_all_for_user(self, user_id: UserId,
                                  revoked_at: datetime) -> int:
        """Revoke every live session this user has; returns how many were live.

        The "log me out everywhere" primitive, and what a future password change
        must call. Phase 4 uses it to make a `DISABLED` account's sessions stop.
        """
        ...

    async def delete_expired(self, as_of: datetime, *, limit: int = DEFAULT_LIMIT) -> int:
        """Delete sessions that expired before `as_of`; returns how many.

        Capped like every other bulk method. Nothing schedules this in Phase 4 —
        it is the housekeeping a worker will call, and it exists now so the table
        does not grow without a stated way to prune it.
        """
        ...


@runtime_checkable
class CandidateProfileRepository(Protocol):
    """Candidate profiles — user-owned, so `user_id` comes first everywhere."""

    async def get(self, user_id: UserId,
                  profile_id: CandidateProfileId) -> CandidateProfile | None:
        """The profile, or `None` — including when it belongs to somebody else.

        Not found and not yours are indistinguishable here for the same reason as
        in `MatchEvaluationRepository.get`.
        """
        ...

    async def get_default(self, user_id: UserId) -> CandidateProfile | None:
        """The profile onboarding created for this account, if it exists.

        A lookup by owner rather than by the derived id, so it keeps working the
        day an account has a second profile — at which point this becomes "the
        first one", not "the only one".
        """
        ...

    async def upsert(self, profile: CandidateProfile) -> CandidateProfile:
        """Write the profile and reconcile its languages, permits and slots.

        Raises `ValueError` if the profile carries evidence or claims: there is
        nowhere to put them until Phase 10, and writing the profile without them
        would silently break the claim-to-evidence link on the way back out.
        """
        ...

    async def list_for_user(
            self, user_id: UserId, *,
            limit: int = DEFAULT_LIMIT) -> tuple[CandidateProfile, ...]:
        """This user's profiles, oldest first — the order they were created in."""
        ...


@runtime_checkable
class SearchProfileRepository(Protocol):
    """Saved searches — user-owned, and the unit discovery will iterate over."""

    async def get(self, user_id: UserId,
                  search_profile_id: SearchProfileId) -> SearchProfile | None: ...

    async def upsert(self, profile: SearchProfile) -> SearchProfile:
        """Write the search and reconcile its areas."""
        ...

    async def delete(self, user_id: UserId, search_profile_id: SearchProfileId) -> bool:
        """Delete one of this user's searches; `True` if a row was removed.

        The only hard delete in this module, and it is right: a saved search is
        the user's own list, and an "archived" search they cannot remove is a bug
        report waiting to happen. Its areas go with it by `ON DELETE CASCADE`.
        """
        ...

    async def list_for_user(self, user_id: UserId, *, active_only: bool = False,
                            limit: int = DEFAULT_LIMIT) -> tuple[SearchProfile, ...]:
        """This user's searches, newest first.

        `active_only` is what onboarding completion checks and what a discovery
        run will want; it is a parameter rather than a separate method so the
        index on `(user_id, is_active)` serves one query shape.
        """
        ...



