"""Employers as first-class entities: identity, aliases, careers endpoints, ATS.

V1 only ever knew a company as a string on a job row, which is why it cannot
answer "which employers near me have no posting today but accept spontaneous
applications" — the question docs/V2_SPECIFICATION.md §6 makes a product feature.
Phase 1 introduced `Company` and `CompanyLocation`; Phase 6 gives them the
identity, provenance and detection vocabulary a discovery engine needs, and Phase
7 gives the locations real coordinates.

Three decisions shape what Phase 6 added, and each is a constraint on what the
rest of the system can accidentally do.

**Normalization is a pure function of the stored value, not a stored decision.**
`normalized_name` and `normalized_domain` are properties, not fields, so no code
path can write a normalized form that disagrees with the name or website beside
it. Persistence still gets columns — the mapper reads the properties — but the
column is a projection of the row rather than a second source of truth. The
normalization here removes no words: `Acme Switzerland` normalizes to
`acme switzerland`, because dropping a meaningful word is a country-aware
judgement that belongs to `backend.app.companies.identity` and produces a
*comparison key* that is never stored.

**A verdict carries its evidence or it does not exist.**
`accepts_spontaneous_applications` stays three-valued for the reason Phase 1 gave
it, and Phase 6 adds the rule that a non-`None` answer must say what it rests on.
"No active jobs" is not evidence of a spontaneous-application channel, and the
invariant is what stops that inference being one plausible line of code away.
`DetectedATS` obeys the same rule.

**Provenance is an entity, not a comment.** `CompanyAlias`, `CareerSite` and
`CompanyDiscoveryRecord` each record who told us and when. They are deliberately
*not* children of the `Company` aggregate: a provider that knows one careers
endpoint must not erase the endpoints other providers found, so each is written
by its own idempotent upsert keyed by a derived id
(`backend.app.domain.identifiers`).
"""
import re
import unicodedata
from enum import StrEnum
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from backend.app.domain.base import (
    CountryCode,
    DomainModel,
    HttpUrlStr,
    NonEmptyStr,
    ReasonCode,
    UtcDatetime,
)
from backend.app.domain.common import Location
from backend.app.domain.identifiers import (
    CareerSiteId,
    CompanyAliasId,
    CompanyDiscoveryRecordId,
    CompanyId,
    CompanyLocationId,
)

# Who told us. The same shape as `discovery.contracts.SourceKey` and deliberately
# not an import of it: the domain must not depend on the discovery package, and a
# provenance key is a plain identity here whether it names an opportunity source
# (`jobup`) or a company discovery provider (`configured_ats`).
ProvenanceKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)]

# A normalized domain: lower case, no scheme, no `www.`, no port, no trailing dot.
# The pattern is what makes "the website normalized to a domain" checkable rather
# than assumed — a value with a slash or an upper-case letter in it never reaches
# the comparison that treats domain equality as strong evidence (§2).
NormalizedDomain = Annotated[str, Field(pattern=r"^[a-z0-9-]+(\.[a-z0-9-]+)+$",
                                        max_length=253)]

# Keys a raw discovery payload may never carry (§5: no credentials, no sensitive
# response headers). Matched as substrings of the lower-cased key, because the
# leak arrives as `Authorization`, `X-Api-Key` or `set-cookie` depending on whose
# client produced the dict, and listing exact spellings would miss the next one.
_FORBIDDEN_RAW_KEYS = (
    "authorization", "cookie", "api_key", "apikey", "api-key", "token",
    "password", "passwd", "secret", "credential", "private_key", "session",
    "signature", "bearer",
)

# Punctuation that joins a word rather than separating it. Deleted outright, so
# `S.A.` collapses to `sa` and `L'Oreal` to `loreal` — the CH pack's suffix list
# spells its entries `sa` and `sarl` precisely because it expects this. Turning
# these into spaces instead would produce `s a`, which no suffix in any pack
# matches, and the legal-form comparison would silently never fire.
_JOINING_PUNCTUATION = re.compile(r"[.'’ʼ´]", flags=re.UNICODE)
# Everything else non-word becomes a space: `Acme & Co`, `Acme/Co` and `Acme-Co`
# are two words in every source that prints them.
_SEPARATING_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def _fold_accents(value: str) -> str:
    """NFKD, then drop the combining marks: `Sàrl` and `Sarl` compare equal.

    Chosen over NFKC-only because the sources spell the same Swiss employer both
    ways in the same week, and a normalization that kept the accent would file
    them as two companies.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalize_company_name(value: str) -> str:
    """The deterministic comparison form of an employer name (§3).

    Unicode normalization, accent folding, case folding, punctuation handling and
    whitespace collapse — in that order, and nothing else. In particular **no word
    is ever removed**: `Acme Switzerland` becomes `acme switzerland`, not `acme`.
    Legal-form handling is country-aware, needs a Country Pack, and lives in
    `backend.app.companies.identity`, where its output is used as evidence and
    never stored.

    Punctuation is treated two ways, and the split is what makes the pack's suffix
    list usable: a dot or an apostrophe is deleted (`Logitech Europe S.A.` →
    `logitech europe sa`), everything else becomes a space (`Acme & Co` → `acme
    co`).

    `casefold` rather than `lower` for the German `ß`, which `lower` leaves alone
    and which the Swiss commercial register does print.

    Returns `""` for a value that is nothing but punctuation. Callers that need a
    non-empty result — every model in this module — get a validation error from
    the field type instead of a silently empty key.
    """
    folded = _fold_accents(_JOINING_PUNCTUATION.sub("", value)).casefold()
    return _WHITESPACE.sub(" ", _SEPARATING_PUNCTUATION.sub(" ", folded)).strip()


def normalize_domain(value: str) -> str:
    """The deterministic comparison form of a website or careers URL (§3).

    Host only: scheme, credentials, port, path, query and fragment are dropped,
    `www.` is dropped, and a trailing dot goes with it. Domain equality is the
    strongest non-manual identity signal Phase 6 has (§2), so it has to be
    computed one way — `https://WWW.Logitech.com/fr/`, `http://logitech.com` and
    `logitech.com.` all have to reduce to `logitech.com` or the signal is noise.

    Accepts a bare host as well as a URL, because a seed configuration writes
    `logitech.com` and `urlsplit` would read that as a path. Returns `""` when
    nothing host-shaped is left, for the same reason as `normalize_company_name`.
    """
    candidate = value.strip()
    split = urlsplit(candidate if "//" in candidate else f"//{candidate}")
    host = (split.hostname or "").casefold().rstrip(".")
    return host.removeprefix("www.")


class CompanyIdentityStatus(StrEnum):
    """How much we trust that this row is one real employer (§1).

    Three members because three is what Phase 6 can justify. `SEEDED` is a name
    somebody handed us — a posting's `company_name`, a line in
    `config/companies.yaml` — with nothing corroborating it. `PROVISIONAL` means a
    second independent signal agreed (a website, an ATS organization). `VERIFIED`
    is reserved for a human confirmation or a redirect we followed ourselves; no
    Phase 6 code path sets it automatically, which is the point.
    """

    SEEDED = "SEEDED"
    PROVISIONAL = "PROVISIONAL"
    VERIFIED = "VERIFIED"


class DetectionStatus(StrEnum):
    """How firmly a detection holds — for an ATS, and for a careers endpoint (§10).

    Detection is not verification. A redirect from `acme.test/jobs` to
    `boards.greenhouse.io/acme` is `CONFIRMED`: the organization identifier came
    out of the URL a server sent us. An HTML page that merely contains the word
    "Greenhouse" is `LIKELY` at best, and a configured organization id nobody has
    fetched is `LIKELY` too. `UNKNOWN` is what a detector returns when it
    concluded nothing; it never reaches a stored `DetectedATS`.
    """

    CONFIRMED = "CONFIRMED"
    LIKELY = "LIKELY"
    UNKNOWN = "UNKNOWN"


class AtsPlatform(StrEnum):
    """The applicant tracking systems this deployment can already read (§9).

    Exactly the three V1 has adapters for. Adding a fourth means adding a source
    plugin, so the enum grows with the capability rather than ahead of it — a
    member nobody can fetch would be a promise the platform does not keep.
    """

    GREENHOUSE = "GREENHOUSE"
    LEVER = "LEVER"
    ASHBY = "ASHBY"


class CareerSiteKind(StrEnum):
    """What one careers endpoint of a company actually is (§11).

    A company has several and they are not interchangeable: the corporate page is
    where a human starts, the ATS board is what a source plugin can fetch, and the
    spontaneous-application form is the only one that answers §12's question.
    Keeping them apart is why `CareerSite` exists as a record instead of the single
    `Company.careers_url` field Phase 1 had.
    """

    CAREERS_PAGE = "CAREERS_PAGE"
    ATS_BOARD = "ATS_BOARD"
    SPONTANEOUS_APPLICATION = "SPONTANEOUS_APPLICATION"


class CompanySeedKind(StrEnum):
    """Where a company came from, before anything was resolved (§7).

    Recorded on the discovery record, so "how did we learn this employer exists?"
    has an answer that survives canonicalization. A seed is not a company: it is a
    claim that one might exist, and the resolution step decides.
    """

    OPPORTUNITY = "OPPORTUNITY"
    CONFIGURED = "CONFIGURED"
    ATS_ORGANIZATION = "ATS_ORGANIZATION"
    WEBSITE = "WEBSITE"
    MANUAL = "MANUAL"


class SpontaneousApplicationSupport(StrEnum):
    """Whether unsolicited applications are possible (§12).

    Three-valued, and the third member is the one that carries the phase order's
    prohibition: `UNKNOWN` means nobody has looked. Reading it as `NOT_SUPPORTED`
    would skip every employer we simply have not examined, and inferring
    `SUPPORTED` from "this company has no active postings" is invalid — a company
    with nothing open is the *normal* case, not evidence of a channel.
    """

    SUPPORTED = "SUPPORTED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNKNOWN = "UNKNOWN"


class Evidence(DomainModel):
    """One observation behind a detection or a verdict.

    Deliberately a narrower shape than `common.Reason`: a `Reason` explains a
    score to a candidate and points at `CandidateEvidence`, while this points at a
    URL somebody can open. `source_url` is optional because a configured seed has
    no URL to show — the configuration file is the evidence, and `detail` says so.

    `code` reuses `ReasonCode`'s `^[A-Z][A-Z0-9_]*$` shape so a dashboard can group
    detections the way it groups everything else.
    """

    code: ReasonCode
    detail: NonEmptyStr
    source_url: HttpUrlStr | None = None
    observed_at: UtcDatetime | None = None


class DetectedATS(DomainModel):
    """Which applicant tracking system an employer publishes through (§9, §10).

    A value object embedded in `Company` rather than an entity: an employer has one
    canonical ATS, and the several *endpoints* that come with it are `CareerSite`
    records. `organization_id` is the identifier the platform itself uses —
    `boards.greenhouse.io/{organization_id}` — because that is the only thing that
    lets a source plugin actually fetch the board, and it doubles as strong
    identity evidence (§2).

    `status` may not be `UNKNOWN`: a detection that concluded nothing is `None` on
    the company, not a row asserting ignorance. And a detection without evidence is
    refused outright, which is what keeps §10's distinction honest.
    """

    platform: AtsPlatform
    organization_id: NonEmptyStr | None = None
    status: DetectionStatus = DetectionStatus.LIKELY
    detected_by: ProvenanceKey
    evidence: tuple[Evidence, ...] = ()

    @model_validator(mode="after")
    def _a_detection_shows_its_work(self) -> Self:
        if self.status is DetectionStatus.UNKNOWN:
            raise ValueError(
                "a DetectedATS states a platform, so its status cannot be UNKNOWN; "
                "leave Company.detected_ats as None instead")
        if not self.evidence:
            raise ValueError("a detected ATS must carry at least one piece of "
                             "evidence (§10: detection is not verification)")
        if self.status is DetectionStatus.CONFIRMED and self.organization_id is None:
            raise ValueError(
                "a CONFIRMED ATS detection must name the organization identifier; "
                "without it nothing can fetch the board, so the claim is untestable")
        return self


class SpontaneousApplicationChannel(DomainModel):
    """The answer to "can this employer be approached unsolicited?" plus its basis.

    Modelled together because the phase order forbids the verdict without the
    basis (§12). `UNKNOWN` is the only value allowed to carry no evidence — it is
    the absence of an observation, and demanding evidence for it would mean
    inventing some.
    """

    support: SpontaneousApplicationSupport = SpontaneousApplicationSupport.UNKNOWN
    url: HttpUrlStr | None = None
    observed_by: ProvenanceKey | None = None
    evidence: tuple[Evidence, ...] = ()

    @model_validator(mode="after")
    def _a_verdict_shows_its_work(self) -> Self:
        decided = self.support is not SpontaneousApplicationSupport.UNKNOWN
        if decided and not self.evidence:
            raise ValueError(
                f"{self.support} is a claim about an employer, so it must carry "
                "evidence; use UNKNOWN when nobody has looked")
        if decided and self.observed_by is None:
            raise ValueError(f"{self.support} must say which provider observed it")
        if self.support is SpontaneousApplicationSupport.NOT_SUPPORTED \
                and self.url is not None:
            raise ValueError("NOT_SUPPORTED with a URL contradicts itself; if the "
                             "form exists the support is SUPPORTED")
        return self

    @property
    def is_decided(self) -> bool:
        return self.support is not SpontaneousApplicationSupport.UNKNOWN


class CompanyAlias(DomainModel):
    """Another label the same employer is published under (§4).

    Its own entity because an alias has provenance: `LOGITECH` came from a job
    board's own spelling, `Logitech Europe S.A.` from a commercial register, and
    knowing which is which is what lets an operator judge a doubtful merge later.
    Storing them as a bare list of strings on the company would lose that, and
    would invite the code that overwrites the canonical name every time a source
    spells it differently — which §4 forbids.

    `normalized_alias` is derived from `alias`, never passed: it is the key the id
    is built from, so a caller that could set it independently could create two
    rows for one claim.
    """

    id: CompanyAliasId
    company_id: CompanyId
    alias: NonEmptyStr
    source_key: ProvenanceKey
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime

    @model_validator(mode="after")
    def _the_alias_says_something_and_the_dates_agree(self) -> Self:
        if not normalize_company_name(self.alias):
            raise ValueError("an alias must contain at least one letter or digit "
                             f"once normalized: {self.alias!r}")
        if self.last_seen_at < self.first_seen_at:
            raise ValueError("last_seen_at precedes first_seen_at")
        return self

    @property
    def normalized_alias(self) -> str:
        """The comparison form, and the key `company_alias_id` is derived from."""
        return normalize_company_name(self.alias)


class CareerSite(DomainModel):
    """One careers endpoint of one company, with how firmly we believe in it (§11).

    Several per company is the normal case, which is why this is a record and not
    a field. `verification_status` is `DetectionStatus` — the same vocabulary as an
    ATS detection, because the question is the same one: did a server tell us this,
    or did we infer it?

    `last_checked_at` is nullable and Phase 6 leaves it `None` for everything it
    writes: nothing in this phase fetches a URL (§9 rules out crawling), so a
    timestamp here would claim a check that never happened. The column exists for
    the phase that does fetch.
    """

    id: CareerSiteId
    company_id: CompanyId
    url: HttpUrlStr
    kind: CareerSiteKind = CareerSiteKind.CAREERS_PAGE
    platform: AtsPlatform | None = None
    source_key: ProvenanceKey
    verification_status: DetectionStatus = DetectionStatus.LIKELY
    discovered_at: UtcDatetime
    last_checked_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _an_ats_board_names_its_platform(self) -> Self:
        if self.kind is CareerSiteKind.ATS_BOARD and self.platform is None:
            raise ValueError("an ATS_BOARD career site must name its platform; "
                             "without it no source plugin can read the board")
        if self.last_checked_at is not None \
                and self.last_checked_at < self.discovered_at:
            raise ValueError("last_checked_at precedes discovered_at")
        return self


class CompanyDiscoveryRecord(DomainModel):
    """How one provider came to tell us about one company (§5).

    The provenance trail, one row per (provider, external identifier) pair. It
    points *at* a company rather than belonging to it: resolution may re-point a
    sighting from a provisional company to the confirmed one, and that is still the
    same sighting. `company_id` is therefore nullable — a seed the resolver refused
    to attach (§14: `AMBIGUOUS` leaves it unlinked) is a discovery we want to keep,
    because losing it means rediscovering and re-refusing it every sweep.

    `raw` is a flat string map, not free-form JSON, and `_raw_carries_no_secrets`
    refuses the keys a credential arrives under. The phase order states the rule
    (§5, §26); this validator is what makes it hold for payloads nobody reviewed.
    """

    id: CompanyDiscoveryRecordId
    provider_key: ProvenanceKey
    external_id: NonEmptyStr
    seed_kind: CompanySeedKind
    company_id: CompanyId | None = None
    company_name: NonEmptyStr
    source_url: HttpUrlStr | None = None
    discovered_at: UtcDatetime
    confidence: DetectionStatus = DetectionStatus.LIKELY
    raw: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _raw_carries_no_secrets(self) -> Self:
        """No credential-shaped key in the payload we persist and later display.

        Refuses rather than redacts, deliberately: a provider that hands us a
        header bag has a bug in the adapter, and silently dropping the key would
        let the next payload shape through unnoticed.
        """
        offending = sorted(key for key in self.raw
                           if any(forbidden in key.casefold()
                                  for forbidden in _FORBIDDEN_RAW_KEYS))
        if offending:
            raise ValueError(
                f"raw discovery metadata must not carry {offending}: §5 forbids "
                "credentials and sensitive headers in stored provenance")
        return self


class CompanyLocation(DomainModel):
    """One physical site of a company.

    Its own entity rather than a field on `Company` because the map plots sites,
    not legal entities: a retail chain with forty branches is one employer and
    forty markers (docs/V2_SPECIFICATION.md §8). `location.point` is where
    PostGIS geometry attaches in Phase 2.
    """

    id: CompanyLocationId
    company_id: CompanyId
    location: Location
    is_headquarters: bool = False


class Company(DomainModel):
    """A canonical employer identity.

    `accepts_spontaneous_applications` is three-valued on purpose: `None` means
    nobody has looked yet, which must not be confused with `False` ("we looked,
    there is no channel"). An application strategy that treats unknown as
    refused would silently skip half the market.

    Phase 6 kept that field — it is what Phase 1 tests and what the API has always
    exposed — and put the evidence beside it in `spontaneous_application_channel`.
    `_the_channel_and_the_flag_agree` is what stops the two disagreeing, so a
    reader may use either and get the same answer.

    `careers_url` likewise stays as the *preferred* endpoint (§11 permits it) while
    the full set lives in `CareerSite` records that are not carried here: they are
    written and read independently, precisely so one provider cannot delete
    another's findings.
    """

    id: CompanyId
    name: NonEmptyStr
    website: HttpUrlStr | None = None
    careers_url: HttpUrlStr | None = None
    country: CountryCode | None = None
    identity_status: CompanyIdentityStatus = CompanyIdentityStatus.SEEDED
    detected_ats: DetectedATS | None = None
    spontaneous_application_channel: SpontaneousApplicationChannel | None = None
    locations: tuple[CompanyLocation, ...] = ()
    accepts_spontaneous_applications: bool | None = None

    @model_validator(mode="after")
    def _locations_belong_here(self) -> Self:
        """A location carried by a company must point back at that company.

        Cheap to check, and it catches the copy-paste that would otherwise put a
        competitor's branch on this employer's card.
        """
        stray = [loc.id for loc in self.locations if loc.company_id != self.id]
        if stray:
            raise ValueError(f"locations belong to another company: {stray}")
        if sum(1 for loc in self.locations if loc.is_headquarters) > 1:
            raise ValueError("a company has at most one headquarters location")
        return self

    @model_validator(mode="after")
    def _the_channel_and_the_flag_agree(self) -> Self:
        """One employer, one answer about spontaneous applications.

        Both representations are legitimate — the boolean is the API's and Phase
        1's, the channel is the evidence-backed one §12 asks for — but a row where
        they contradict each other would make the answer depend on which field the
        reader happened to look at.
        """
        if self.spontaneous_application_channel is None:
            return self
        support = self.spontaneous_application_channel.support
        expected = {
            SpontaneousApplicationSupport.SUPPORTED: True,
            SpontaneousApplicationSupport.NOT_SUPPORTED: False,
            SpontaneousApplicationSupport.UNKNOWN: None,
        }[support]
        if self.accepts_spontaneous_applications != expected:
            raise ValueError(
                "accepts_spontaneous_applications="
                f"{self.accepts_spontaneous_applications} contradicts "
                f"spontaneous_application_channel.support={support}")
        return self

    @model_validator(mode="after")
    def _a_name_has_a_comparison_form(self) -> Self:
        """A name made only of punctuation cannot be canonicalized, so it is refused.

        `NonEmptyStr` accepts `"—"`; identity resolution cannot. Catching it here
        means the failure names the company instead of surfacing as an empty
        `normalized_name` column that matches every other broken row.
        """
        if not normalize_company_name(self.name):
            raise ValueError("a company name must contain at least one letter or "
                             f"digit once normalized: {self.name!r}")
        return self

    @property
    def normalized_name(self) -> str:
        """The stored comparison form of `name` (§3).

        A property rather than a field so it cannot drift from the name it
        describes. No word is removed — see the module docstring, and
        `backend.app.companies.identity` for the legal-form key that is computed on
        demand and never stored.
        """
        return normalize_company_name(self.name)

    @property
    def normalized_domain(self) -> str | None:
        """`website`'s host, or the careers URL's when there is no website.

        Falling back to `careers_url` matters more than it looks: a company
        discovered from an ATS board often has no corporate site on file, and its
        careers host is then the only domain evidence available. A
        `boards.greenhouse.io` host is a real host and is returned as one — the
        rule that a shared ATS host proves nothing about identity belongs to the
        comparison in `companies.identity`, not to this accessor.
        """
        for candidate in (self.website, self.careers_url):
            if candidate is None:
                continue
            host = normalize_domain(candidate)
            if host:
                return host
        return None
