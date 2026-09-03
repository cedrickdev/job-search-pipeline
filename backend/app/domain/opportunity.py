"""`Opportunity` — the V2 central abstraction, replacing V1's `Job`.

The rename is not cosmetic. V1's `jobs` table can only describe a vacancy at a
company; the product has to describe a student job, an internship, an
apprenticeship, a graduate programme and a spontaneous-application target as
first-class citizens of the same funnel (docs/V2_SPECIFICATION.md §1, §7).

Nothing country-specific appears here. The universal model carries categories
(`APPRENTICESHIP`, `WORK_STUDY`); the local words a board actually prints stay in
the source record's `raw` snapshot until a Country Pack translates them
(docs/V2_SPECIFICATION.md §5, Phase 5).
"""
from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from backend.app.domain.base import (
    DomainModel,
    HttpUrlStr,
    LanguageCode,
    NonEmptyStr,
    UtcDatetime,
)
from backend.app.domain.common import (
    LanguageRequirement,
    Location,
    SalaryRange,
    WorkloadRange,
)
from backend.app.domain.identifiers import CompanyId, OpportunityId


class OpportunityType(StrEnum):
    """What kind of engagement is on offer.

    One enum, no country terminology: the Swiss/French "alternance" and the
    German "duales Studium" are both `WORK_STUDY`, and the mapping from those
    words lives in a Country Pack, not here. `None` on an `Opportunity` means
    "not classified yet", which is honest and different from a catch-all member
    that would quietly absorb every failed classification.
    """

    FULL_TIME = "FULL_TIME"
    PART_TIME = "PART_TIME"
    STUDENT_JOB = "STUDENT_JOB"
    INTERNSHIP = "INTERNSHIP"
    APPRENTICESHIP = "APPRENTICESHIP"
    WORK_STUDY = "WORK_STUDY"
    GRADUATE = "GRADUATE"
    TEMPORARY = "TEMPORARY"
    FREELANCE = "FREELANCE"


class WorkplaceMode(StrEnum):
    """Where the work happens. Orthogonal to `OpportunityType`."""

    ON_SITE = "ON_SITE"
    HYBRID = "HYBRID"
    REMOTE = "REMOTE"


class ContractType(StrEnum):
    """The legal basis of the engagement, in country-neutral terms.

    Separate from `OpportunityType` because the two vary independently: an
    internship can be fixed-term or an agency placement, and a part-time role
    can be open-ended. Local contract names map onto these members in a Country
    Pack.
    """

    PERMANENT = "PERMANENT"
    FIXED_TERM = "FIXED_TERM"
    TEMPORARY_AGENCY = "TEMPORARY_AGENCY"
    SERVICE_CONTRACT = "SERVICE_CONTRACT"


class OpportunitySourceRecord(DomainModel):
    """Where an opportunity came from, and what the source literally said.

    docs/ARCHITECTURE.md §3 makes this its own entity, and
    docs/LLM_PROVIDER_ARCHITECTURE.md §11 explains why `raw` exists: normalization
    is lossy and revisable, so the un-normalized fields have to survive for
    traceability and for re-running a better parser later.

    `source_key` is a plain string, not an enum: sources are plugins registered
    at runtime (docs/ARCHITECTURE.md §7), so adding a board must not require
    editing the domain.

    `raw` holds public posting metadata only. Credentials, cookies and session
    tokens belong nowhere near a domain object
    (docs/ENGINEERING_STANDARDS.md §Security).
    """

    source_key: NonEmptyStr
    external_id: NonEmptyStr | None = None
    source_url: HttpUrlStr | None = None
    fetched_at: UtcDatetime
    raw: Mapping[str, str] = Field(default_factory=dict)


class Opportunity(DomainModel):
    """A normalized professional opportunity (docs/V2_SPECIFICATION.md §7).

    Not user-owned, and therefore carries no `user_id`: a posting is a shared
    fact about the world. What a *particular* candidate thinks of it lives in
    `MatchEvaluation` and `ApplicationDecision`, which are user-scoped
    (docs/ARCHITECTURE.md §5).

    Almost every descriptive field is optional because discovery is incremental:
    a search index gives a title, a company and a URL, and a detail fetch fills
    the rest. Only identity, provenance, the employer name, the title and the
    discovery instant are non-negotiable — an opportunity that cannot say who is
    hiring, for what, or where it was found is not a fact worth storing.

    `company_name` sits beside an optional `company_id` on purpose: at discovery
    time the employer is a string, and Phase 6 canonicalizes it into a `Company`
    without losing what the posting claimed.
    """

    id: OpportunityId
    source: OpportunitySourceRecord
    company_name: NonEmptyStr
    company_id: CompanyId | None = None
    title: NonEmptyStr
    description: NonEmptyStr | None = None
    opportunity_type: OpportunityType | None = None
    contract_type: ContractType | None = None
    workplace_mode: WorkplaceMode | None = None
    workload: WorkloadRange | None = None
    salary: SalaryRange | None = None
    location: Location | None = None
    # The language the posting is written in — a fact about the text. Distinct
    # from `language_requirements`, which is a demand on the candidate. Treating
    # the two as one is how a French-language posting becomes a fabricated
    # "French required" requirement.
    posting_language: LanguageCode | None = None
    language_requirements: tuple[LanguageRequirement, ...] = ()
    posted_at: date | None = None
    discovered_at: UtcDatetime
    application_url: HttpUrlStr | None = None
    # V1's `dedup_hash` carries through unchanged so Phase 2 can import without
    # re-deriving identity. The domain declares the field; computing it is a
    # discovery-service concern.
    dedup_fingerprint: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _one_requirement_per_language(self) -> Self:
        languages = [r.language for r in self.language_requirements]
        if len(languages) != len(set(languages)):
            raise ValueError("language_requirements must not repeat a language")
        return self

    @property
    def is_remote(self) -> bool:
        return self.workplace_mode is WorkplaceMode.REMOTE


