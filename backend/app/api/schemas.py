"""Request and response models: the boundary the domain does not cross.

Two rules, and between them they are most of what this module is for.

**Nothing leaves as a domain aggregate.** `User` holds a password hash and
`UserSession` holds two token digests. They are `SecretStr`, so a `model_dump_json`
would render `"**********"` rather than the value — but a response model that has no
field for them cannot leak them even if that changed, and a reader can verify the
guarantee by reading twenty lines instead of auditing every route. `AccountResponse`
is the whole of what an authenticated client learns about its own account.

**Nothing arrives as a domain aggregate either.** A request body is a draft
(`CandidateProfileDraft`, `SearchProfileDraft`) or one of the small models here, and
none of them has a `user_id`, an `id` or a timestamp. The service supplies the owner
from the session, so there is no field for a request to override
(docs/ENGINEERING_STANDARDS.md §Security: user-owned data is authorization-scoped).

The password band is stated here as well as in `backend.app.core.passwords`. Two
checks on purpose: this one is the API's contract and produces a 422 a form can
show, and that one is what a future CLI or importer cannot bypass.

**A discovery record leaves without its `raw` metadata.** `CompanyDiscoveryRecord`
keeps whatever a provider reported about an employer, and Phase 6 §29 draws the line
here: the structured provenance — which provider, which seed, which URL, when, how
confident — is exactly what a company page should show, and the untyped bag behind it
stays on this side of the boundary. `CompanyDiscoveryRecordResponse` therefore has no
field for it, which is a stronger guarantee than a filter somebody has to remember.
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr

from backend.app.companies.contracts import (
    MAX_COMPANIES_PER_PROVIDER,
    CompanyDiscoveryRequest,
    CompanyDiscoveryWarning,
    CompanyDiscoveryWarningCode,
    ProviderKey,
)
from backend.app.core.passwords import MAXIMUM_PASSWORD_LENGTH, MINIMUM_PASSWORD_LENGTH
from backend.app.discovery.contracts import (
    SourceFailureCode,
    SourceHealth,
    SourceHealthStatus,
)
from backend.app.domain.base import CountryCode
from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.company import (
    AtsPlatform,
    CareerSite,
    CareerSiteKind,
    Company,
    CompanyAlias,
    CompanyDiscoveryRecord,
    CompanyIdentityStatus,
    CompanyLocation,
    CompanySeedKind,
    DetectedATS,
    DetectionStatus,
    Evidence,
    SpontaneousApplicationChannel,
    SpontaneousApplicationSupport,
)
from backend.app.domain.identifiers import (
    CandidateProfileId,
    CompanyId,
    SearchProfileId,
    UserId,
)
from backend.app.domain.search import SearchProfile
from backend.app.domain.user import User, UserStatus
from backend.app.repositories.contracts import DEFAULT_LIMIT, CompanyPage
from backend.app.services.company_directory import CompanyDetail
from backend.app.services.company_discovery import (
    CompanyDiscoveryOutcome,
    OpportunityLinkReport,
)
from backend.app.services.onboarding import (
    CandidateProfileDraft,
    OnboardingState,
    SearchProfileDraft,
)

Password = SecretStr


class ApiModel(BaseModel):
    """The base every schema here shares.

    `extra="forbid"` is the half that matters: a request body carrying a field the
    model does not declare is a client that believes something untrue about this
    API, and answering 422 is more useful than silently ignoring it. It is also
    what makes "there is no `user_id` field to override" a checked statement rather
    than an observation about the current field list.
    """

    model_config = ConfigDict(extra="forbid")


class RegisterRequest(ApiModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    email: EmailStr
    password: Password = Field(min_length=MINIMUM_PASSWORD_LENGTH,
                               max_length=MAXIMUM_PASSWORD_LENGTH)


class LoginRequest(ApiModel):
    email: EmailStr
    # No length band on login, deliberately: the band is a policy for *choosing* a
    # password, and applying it here would tell an attacker that a submitted value
    # was too short to be anybody's password — one bit of the search space, given
    # away for nothing.
    password: Password


class AccountResponse(ApiModel):
    """What a client learns about its own account. No credential fields exist."""

    id: UserId
    email: EmailStr
    display_name: str | None
    status: UserStatus
    onboarding_completed_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, user: User) -> "AccountResponse":
        return cls(id=user.id, email=user.email, display_name=user.display_name,
                   status=user.status,
                   onboarding_completed_at=user.onboarding_completed_at,
                   created_at=user.created_at)


class SessionResponse(ApiModel):
    """The current session's window — issued, expires — and nothing identifying it.

    No session id and no digest. The client has the cookie; an id in a body would
    only be useful to something that wanted to name somebody else's session.
    """

    issued_at: datetime
    expires_at: datetime
    last_seen_at: datetime


class SignedInResponse(ApiModel):
    """The reply to register, login and `GET /auth/session`.

    The tokens are absent from this model by construction — they leave in two
    `Set-Cookie` headers, written by `backend.app.api.cookies`, which is the only
    place in the process that touches their raw values.
    """

    account: AccountResponse
    session: SessionResponse


class CandidateProfileResponse(ApiModel):
    """A saved profile, echoed back with the two fields the draft could not carry.

    `evidence` and `claims` are not here either: they have no storage until Phase
    10, and a field that was always `[]` would read as "this candidate has no
    evidence" rather than "this system does not keep any yet".
    """

    id: CandidateProfileId
    user_id: UserId
    updated_at: datetime
    profile: CandidateProfileDraft

    @classmethod
    def of(cls, profile: CandidateProfile) -> "CandidateProfileResponse":
        return cls(
            id=profile.id, user_id=profile.user_id, updated_at=profile.updated_at,
            profile=CandidateProfileDraft(
                display_name=profile.display_name,
                headline=profile.headline,
                base_location=profile.base_location,
                languages=profile.languages,
                work_authorizations=profile.work_authorizations,
                availability=profile.availability))


class SearchProfileResponse(ApiModel):
    """A saved search, echoed back with its id and timestamps."""

    id: SearchProfileId
    user_id: UserId
    created_at: datetime
    updated_at: datetime
    search: SearchProfileDraft

    @classmethod
    def of(cls, profile: SearchProfile) -> "SearchProfileResponse":
        return cls(
            id=profile.id, user_id=profile.user_id, created_at=profile.created_at,
            updated_at=profile.updated_at,
            search=SearchProfileDraft(
                name=profile.name,
                is_active=profile.is_active,
                areas=profile.areas,
                queries=profile.queries,
                title_keywords=profile.title_keywords,
                excluded_keywords=profile.excluded_keywords,
                opportunity_types=profile.opportunity_types,
                contract_types=profile.contract_types,
                workplace_modes=profile.workplace_modes,
                posting_languages=profile.posting_languages,
                workload=profile.workload,
                source_keys=profile.source_keys))


class SearchProfileListResponse(ApiModel):
    """A wrapper, not a bare array.

    A top-level JSON array cannot grow a field, so the first time this needs a
    count or a cursor it would have to become a breaking change. It also keeps
    every V2 response an object, which is one rule for the client to hold.
    """

    search_profiles: tuple[SearchProfileResponse, ...]


class OnboardingStateResponse(ApiModel):
    """Which step the frontend should show, and whether finishing is possible.

    `may_complete` is computed here rather than in the client, so the button's
    enabled state and the server's 409 can never disagree.
    """

    has_profile: bool
    search_profiles: int
    active_search_profiles: int
    completed_at: datetime | None
    is_complete: bool
    may_complete: bool

    @classmethod
    def of(cls, state: OnboardingState) -> "OnboardingStateResponse":
        return cls(has_profile=state.has_profile,
                   search_profiles=state.search_profiles,
                   active_search_profiles=state.active_search_profiles,
                   completed_at=state.completed_at,
                   is_complete=state.is_complete,
                   may_complete=state.may_complete)


class EvidenceResponse(ApiModel):
    """Why the backend believes one thing about a company.

    Carried out to the client because §10 and §12 both rest on it: "this employer is
    on Greenhouse" and "this employer takes spontaneous applications" are only worth
    showing next to what they were concluded from. A code the UI can branch on, a
    sentence a human can read, and — when there is one — the URL that said so.
    """

    code: str
    detail: str
    source_url: str | None
    observed_at: datetime | None

    @classmethod
    def of(cls, evidence: Evidence) -> "EvidenceResponse":
        return cls(code=evidence.code, detail=evidence.detail,
                   source_url=evidence.source_url,
                   observed_at=evidence.observed_at)


class CompanyLocationResponse(ApiModel):
    """One place an employer is, as text.

    No latitude and no longitude, and that is Phase 6's scope rather than an
    oversight: §22 leaves geocoding to Phase 7 and the map to Phase 8, so a
    coordinate pair in this model would be either always null or a promise the
    backend cannot keep. `raw` is what a source wrote when it named no city, which
    is what a list has left to display.
    """

    city: str | None
    region: str | None
    postal_code: str | None
    country: str | None
    raw: str | None
    is_headquarters: bool

    @classmethod
    def of(cls, location: CompanyLocation) -> "CompanyLocationResponse":
        place = location.location
        return cls(city=place.city, region=place.region,
                   postal_code=place.postal_code, country=place.country,
                   raw=place.raw, is_headquarters=location.is_headquarters)


class DetectedAtsResponse(ApiModel):
    """Which platform an employer publishes on, and how sure that is.

    `status` is the whole point (§10): `CONFIRMED` came from the platform's own
    address space, `LIKELY` from configuration a human typed. A client that showed
    the two the same way would be inventing certainty the backend refused to claim.
    """

    platform: AtsPlatform
    organization_id: str | None
    status: DetectionStatus
    detected_by: str
    evidence: tuple[EvidenceResponse, ...]

    @classmethod
    def of(cls, detected: DetectedATS) -> "DetectedAtsResponse":
        return cls(platform=detected.platform,
                   organization_id=detected.organization_id,
                   status=detected.status, detected_by=detected.detected_by,
                   evidence=tuple(EvidenceResponse.of(item)
                                  for item in detected.evidence))


class SpontaneousApplicationResponse(ApiModel):
    """Whether an unsolicited application is possible — including "we do not know".

    Three states, not a boolean (§12), because the third is the common one and
    collapsing it into `false` would turn "nobody has checked" into "this employer
    refuses", which no source ever said. `observed_by` names the provider that
    formed the verdict so an operator can tell who to disbelieve.
    """

    support: SpontaneousApplicationSupport
    url: str | None
    observed_by: str | None
    evidence: tuple[EvidenceResponse, ...]

    @classmethod
    def of(cls,
           channel: SpontaneousApplicationChannel) -> "SpontaneousApplicationResponse":
        return cls(support=channel.support, url=channel.url,
                   observed_by=channel.observed_by,
                   evidence=tuple(EvidenceResponse.of(item)
                                  for item in channel.evidence))


class CompanyResponse(ApiModel):
    """One employer as the API describes it.

    `normalized_name` is included on purpose: it is the value identity resolution
    compares (§3), so a client that wants to explain why two companies stayed
    separate — or a support request asking why they did not merge — has the answer in
    the payload rather than in a backend log.

    `accepts_spontaneous_applications` is the tri-state boolean beside the channel,
    and the two can never disagree because `Company` refuses a row where they do.
    """

    id: CompanyId
    name: str
    normalized_name: str
    website: str | None
    careers_url: str | None
    country: str | None
    identity_status: CompanyIdentityStatus
    detected_ats: DetectedAtsResponse | None
    spontaneous_application: SpontaneousApplicationResponse | None
    accepts_spontaneous_applications: bool | None
    locations: tuple[CompanyLocationResponse, ...]

    @classmethod
    def of(cls, company: Company) -> "CompanyResponse":
        detected = company.detected_ats
        channel = company.spontaneous_application_channel
        return cls(
            id=company.id, name=company.name,
            normalized_name=company.normalized_name, website=company.website,
            careers_url=company.careers_url, country=company.country,
            identity_status=company.identity_status,
            detected_ats=None if detected is None
            else DetectedAtsResponse.of(detected),
            spontaneous_application=None if channel is None
            else SpontaneousApplicationResponse.of(channel),
            accepts_spontaneous_applications=(
                company.accepts_spontaneous_applications),
            locations=tuple(CompanyLocationResponse.of(location)
                            for location in company.locations))


class CompanyListResponse(ApiModel):
    """One page of employers, with the window it came from.

    `total` is what makes the page navigable, and `limit`/`offset` are echoed back
    because the server clamps them — a client that asked for 500 needs to be told it
    received 100, or its "next page" arithmetic silently skips rows (§20).
    """

    companies: tuple[CompanyResponse, ...]
    total: int
    limit: int
    offset: int

    @classmethod
    def of(cls, page: CompanyPage, *, limit: int,
           offset: int) -> "CompanyListResponse":
        return cls(companies=tuple(CompanyResponse.of(company)
                                   for company in page.companies),
                   total=page.total, limit=limit, offset=offset)


class CompanyAliasResponse(ApiModel):
    """A name an employer is also known by, and who called it that.

    `source_key` and the two timestamps are the provenance §4 asks for: an alias is
    an observation by somebody at some time, and one that arrived without those is
    indistinguishable from a guess.
    """

    alias: str
    normalized_alias: str
    source_key: str
    first_seen_at: datetime
    last_seen_at: datetime

    @classmethod
    def of(cls, alias: CompanyAlias) -> "CompanyAliasResponse":
        return cls(alias=alias.alias, normalized_alias=alias.normalized_alias,
                   source_key=alias.source_key,
                   first_seen_at=alias.first_seen_at,
                   last_seen_at=alias.last_seen_at)


class CareerSiteResponse(ApiModel):
    """One place an employer publishes, of possibly several (§11).

    `last_checked_at` is `null` for everything Phase 6 records, and the field exists
    anyway because the distinction it carries is real: a URL that was verified a
    month ago and one that has never been fetched are different facts, and Phase 6
    only ever produces the second.
    """

    url: str
    kind: CareerSiteKind
    platform: AtsPlatform | None
    source_key: str
    verification_status: DetectionStatus
    discovered_at: datetime
    last_checked_at: datetime | None

    @classmethod
    def of(cls, site: CareerSite) -> "CareerSiteResponse":
        return cls(url=site.url, kind=site.kind, platform=site.platform,
                   source_key=site.source_key,
                   verification_status=site.verification_status,
                   discovered_at=site.discovered_at,
                   last_checked_at=site.last_checked_at)


class CompanyDiscoveryRecordResponse(ApiModel):
    """How one provider came to report this employer (§5).

    No `raw`. The stored record keeps whatever the provider handed over, and this
    model deliberately has nowhere to put it: the typed fields answer "discovered via
    what, when, how confident", which is the question a company page asks, and a
    passthrough bag is how a header or a query parameter would eventually reach a
    client (§29).

    `company_name` is the label *that provider* used, which is why it can differ from
    the canonical name — that difference is the provenance, not a defect.
    """

    provider_key: str
    external_id: str
    seed_kind: CompanySeedKind
    company_name: str
    source_url: str | None
    discovered_at: datetime
    confidence: DetectionStatus

    @classmethod
    def of(cls,
           record: CompanyDiscoveryRecord) -> "CompanyDiscoveryRecordResponse":
        return cls(provider_key=record.provider_key,
                   external_id=record.external_id, seed_kind=record.seed_kind,
                   company_name=record.company_name,
                   source_url=record.source_url,
                   discovered_at=record.discovered_at,
                   confidence=record.confidence)


class CompanyDetailResponse(ApiModel):
    """One employer with its aliases, its careers endpoints and its provenance.

    `discovered_by` is the summary of the records below it — the providers that have
    reported this employer, in report order — so a client that only wants the badge
    does not have to fold the list itself.
    """

    company: CompanyResponse
    aliases: tuple[CompanyAliasResponse, ...]
    career_sites: tuple[CareerSiteResponse, ...]
    discoveries: tuple[CompanyDiscoveryRecordResponse, ...]
    discovered_by: tuple[str, ...]

    @classmethod
    def of(cls, detail: CompanyDetail) -> "CompanyDetailResponse":
        return cls(
            company=CompanyResponse.of(detail.company),
            aliases=tuple(CompanyAliasResponse.of(alias)
                          for alias in detail.aliases),
            career_sites=tuple(CareerSiteResponse.of(site)
                               for site in detail.career_sites),
            discoveries=tuple(CompanyDiscoveryRecordResponse.of(record)
                              for record in detail.discoveries),
            discovered_by=detail.provider_keys)


class CompanyDiscoveryRunRequest(ApiModel):
    """What a discovery pass is allowed to do. No seeds.

    A caller may narrow the pass — one country, a named provider, a smaller page —
    and may not add an employer through it (§21): companies are shared facts, so a
    request body that carried a seed would let any authenticated account write into
    every other account's directory. New seeds are configuration, not a request.

    `limit` is `null` by default rather than a number, so the ceiling per provider
    stays stated once, in `CompanyDiscoveryRequest`.
    """

    country: CountryCode | None = None
    provider_keys: tuple[ProviderKey, ...] = ()
    limit: int | None = Field(default=None, gt=0, le=MAX_COMPANIES_PER_PROVIDER)
    # The postings the pass will try to link afterwards (§27). `0` skips that half
    # entirely, and the upper bound is the same one a provider pass gets: a request
    # must not be able to ask for an unbounded walk of the postings table.
    link_limit: int = Field(default=DEFAULT_LIMIT, ge=0,
                            le=MAX_COMPANIES_PER_PROVIDER)

    def to_request(self) -> CompanyDiscoveryRequest:
        """The domain request this body means, validated by the contract itself."""
        if self.limit is None:
            return CompanyDiscoveryRequest(country=self.country,
                                           provider_keys=self.provider_keys)
        return CompanyDiscoveryRequest(country=self.country,
                                       provider_keys=self.provider_keys,
                                       limit=self.limit)


class ProviderHealthResponse(ApiModel):
    """What one company discovery provider last said about itself.

    Reported even when a pass succeeded, because docs/V2_SPECIFICATION.md §22 lists
    hiding failed source health as a non-goal: a client that saw an empty result with
    no health could not tell "no employer is configured" from "the provider that
    knows them could not read its file".

    `reason` and `detail` are the normalized pair §26 requires — a code from a closed
    set and a sentence the adapter wrote. Neither is ever a serialized exception, so
    neither can carry a credential, a token or a query string.
    """

    provider_key: str
    status: SourceHealthStatus
    checked_at: datetime
    latency_ms: int | None
    reason: SourceFailureCode | None
    detail: str | None

    @classmethod
    def of(cls, health: SourceHealth) -> "ProviderHealthResponse":
        return cls(provider_key=health.source_key, status=health.status,
                   checked_at=health.checked_at, latency_ms=health.latency_ms,
                   reason=health.reason, detail=health.detail)


class CompanyDiscoveryWarningResponse(ApiModel):
    """Something a pass could not do, without failing the pass."""

    code: CompanyDiscoveryWarningCode
    detail: str
    provider_key: str | None

    @classmethod
    def of(cls,
           warning: CompanyDiscoveryWarning) -> "CompanyDiscoveryWarningResponse":
        return cls(code=warning.code, detail=warning.detail,
                   provider_key=warning.provider_key)


class OpportunityLinkResponse(ApiModel):
    """What the posting-link half of a pass did (§27).

    The four numbers need not sum to `examined`, and the report says why: a posting
    another pass linked between this one's read and write counts in none of them.
    """

    examined: int
    linked: int
    ambiguous: int
    unresolved: int

    @classmethod
    def of(cls, links: OpportunityLinkReport) -> "OpportunityLinkResponse":
        return cls(examined=links.examined, linked=links.linked,
                   ambiguous=links.ambiguous, unresolved=links.unresolved)


class CompanyDiscoveryRunResponse(ApiModel):
    """The outcome of one pass: what ran, what was written, what stayed ambiguous.

    `ambiguous` is a first-class count rather than an error total (§14): a claim that
    matched two employers was recorded and merged into neither, which is the intended
    behaviour and the number an operator should watch. `created` and `matched`
    together are what makes idempotence visible — a second identical pass reports
    `created: 0`.

    No company bodies. A pass may touch hundreds of employers, and the client that
    wants them has `GET /companies`; returning them here would make one endpoint's
    response size depend on how long since the last run.
    """

    country: str | None
    started_at: datetime
    duration_ms: int
    providers: tuple[str, ...]
    unusable_providers: tuple[str, ...]
    is_complete: bool
    created: int
    matched: int
    ambiguous: int
    company_ids: tuple[CompanyId, ...]
    health: tuple[ProviderHealthResponse, ...]
    warnings: tuple[CompanyDiscoveryWarningResponse, ...]
    links: OpportunityLinkResponse

    @classmethod
    def of(cls, outcome: CompanyDiscoveryOutcome) -> "CompanyDiscoveryRunResponse":
        report = outcome.report
        return cls(
            country=report.country, started_at=report.started_at,
            duration_ms=report.duration_ms, providers=report.provider_keys,
            unusable_providers=report.unusable_provider_keys,
            is_complete=report.is_complete, created=outcome.created,
            matched=outcome.matched, ambiguous=outcome.ambiguous,
            company_ids=outcome.company_ids,
            health=tuple(ProviderHealthResponse.of(health)
                         for health in report.health),
            warnings=tuple(CompanyDiscoveryWarningResponse.of(warning)
                           for warning in report.warnings),
            links=OpportunityLinkResponse.of(outcome.links))
