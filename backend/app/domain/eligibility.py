"""Eligibility — the binary half of "should this application happen?".

The Phase 1 order is explicit: *separate eligibility from compatibility*, so that
a pair can report technical fit high, eligibility failed, decision do-not-apply.
This module is the second of those three, and it is intentionally not a score.

The line between this module and `matching` is whether the question admits
degrees. "How well do these skills match?" is a matter of degree and belongs to
`MatchDimension`. "Does this person hold a permit that allows 20h of paid work a
week?" is yes, no, or not-yet-known — a gate. Turning a gate into a 0-1 number is
how an illegal application becomes a 0.85.

Determinism is enforced structurally: `DeterminationSource.LLM_EXTRACTION` may
only ever accompany `EligibilityStatus.INCOMPLETE`. A model may read a posting
and propose that a requirement exists, but it may not be what decides the
verdict (CLAUDE.md; docs/LLM_PROVIDER_ARCHITECTURE.md §8).
"""
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.domain.base import DomainModel, NonEmptyStr, UtcDatetime
from backend.app.domain.common import Reason
from backend.app.domain.identifiers import (
    CandidateProfileId,
    EligibilityResultId,
    EvidenceId,
    OpportunityId,
    UserId,
)


class EligibilityRequirement(StrEnum):
    """The gates an opportunity can put in front of a candidate.

    Every member is binary by nature. Nothing here is "how good is the match" —
    that is `MatchDimension`. Country-specific rules (which permit allows what,
    what the local minimum working age is) are Country Pack knowledge (Phase 5);
    this enum only names the *kind* of gate so the verdict has a stable code.
    """

    WORK_AUTHORIZATION = "WORK_AUTHORIZATION"
    PERMIT_HOURS_CAP = "PERMIT_HOURS_CAP"
    MINIMUM_AGE = "MINIMUM_AGE"
    LANGUAGE_MINIMUM = "LANGUAGE_MINIMUM"
    EDUCATION_LEVEL = "EDUCATION_LEVEL"
    CERTIFICATION = "CERTIFICATION"
    DRIVING_LICENCE = "DRIVING_LICENCE"
    AVAILABILITY_WINDOW = "AVAILABILITY_WINDOW"
    LOCATION_REACHABLE = "LOCATION_REACHABLE"


class EligibilityStatus(StrEnum):
    """Four values, because fewer would lie.

    Two of them are the honest middle the phase order insists on, and they are
    not interchangeable:

    - `INCOMPLETE` means the platform does not yet know — evidence is missing.
      Reading it as eligible produces applications that waste everyone's time;
      reading it as ineligible silently hides opportunities. It pairs with the
      `ELIGIBILITY_INCOMPLETE` reason code docs/ENGINEERING_STANDARDS.md
      §Observability asks for.
    - `REVIEW_REQUIRED` means the platform found something a human must confirm
      before it counts against the candidate — most importantly an eligibility
      rule that is operator-maintained rather than legally verified (see
      `RuleAuthority`). It is *not* a refusal: it is the platform refusing to
      refuse on an unverified basis.

    A value that is not `ELIGIBLE` never submits an application on its own;
    `INELIGIBLE` blocks and the two middles route to human review
    (`is_blocking`).
    """

    ELIGIBLE = "ELIGIBLE"
    INCOMPLETE = "INCOMPLETE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    INELIGIBLE = "INELIGIBLE"


class DeterminationSource(StrEnum):
    """Who decided a check, which is an audit fact, not a detail.

    `LLM_EXTRACTION` is deliberately the weakest: see the module docstring and
    the `EligibilityCheck` validator below.
    """

    DETERMINISTIC_RULE = "DETERMINISTIC_RULE"
    COUNTRY_PACK_RULE = "COUNTRY_PACK_RULE"
    CANDIDATE_DECLARATION = "CANDIDATE_DECLARATION"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    LLM_EXTRACTION = "LLM_EXTRACTION"


class RuleAuthority(StrEnum):
    """How much a rule behind a check is entitled to close a gate.

    The distinction has legal teeth. A Country Pack's permit table, its minimum
    working age and its language thresholds are *operator-maintained
    configuration*, not law this repository asserts — docs/COUNTRY_PACKS.md
    §Eligibility and the `PermitRule` docstring say so in the same words. A wrong
    number in a YAML file must never become an automatic "you may not apply".

    So authority is carried on the check and enforced by the domain: only a
    `VERIFIED` rule may produce `INELIGIBLE`. Anything less can inform, can lower
    a match score elsewhere, and can raise `REVIEW_REQUIRED` so a human looks —
    but it cannot refuse on its own. The four levels, weakest last:

    - `VERIFIED` — reviewed against the actual legal source and signed off; the
      only authority permitted to block.
    - `SOURCE_DECLARED` — the opportunity or employer stated the requirement
      itself (e.g. a posting that says "EU work permit required"). Strong, but a
      posting is not the law and can be wrong, so it informs and reviews.
    - `OPERATOR_CONFIG` — a value an operator maintains in a Country Pack. The
      default for pack-supplied rules, and deliberately not blocking.
    - `UNKNOWN` — no provenance stated; treated as the weakest.
    """

    VERIFIED = "VERIFIED"
    SOURCE_DECLARED = "SOURCE_DECLARED"
    OPERATOR_CONFIG = "OPERATOR_CONFIG"
    UNKNOWN = "UNKNOWN"


class EligibilityCheck(DomainModel):
    """One gate, evaluated.

    Three invariants, each from the phase order rather than from taste:

    - a verdict that is not `ELIGIBLE` must carry at least one `Reason`. An
      unexplained rejection is exactly what docs/V2_SPECIFICATION.md §13 forbids,
      and the candidate is entitled to know which gate closed;
    - an `LLM_EXTRACTION` check may only be `INCOMPLETE`. A model can flag that a
      posting seems to demand a licence; the licence question is then settled by a
      rule or a human, never by the model;
    - a `COUNTRY_PACK_RULE` may only reach `INELIGIBLE` when its `authority` is
      `VERIFIED`. Operator-maintained pack data (the Swiss student-permit hours
      cap is the canonical example) cannot refuse an application on its own — it
      raises `REVIEW_REQUIRED` instead. This makes the legal-safety rule of
      docs/COUNTRY_PACKS.md §Eligibility *structurally impossible* to violate:
      the model cannot be constructed the wrong way round.
    """

    requirement: EligibilityRequirement
    status: EligibilityStatus
    determined_by: DeterminationSource
    authority: RuleAuthority = RuleAuthority.UNKNOWN
    detail: NonEmptyStr | None = None
    reasons: tuple[Reason, ...] = ()
    evidence_ids: tuple[EvidenceId, ...] = ()

    @model_validator(mode="after")
    def _verdict_is_accountable(self) -> Self:
        if self.status is not EligibilityStatus.ELIGIBLE and not self.reasons:
            raise ValueError(
                f"{self.requirement} is {self.status} and must carry at least one "
                f"reason")
        if self.determined_by is DeterminationSource.LLM_EXTRACTION \
                and self.status is not EligibilityStatus.INCOMPLETE:
            raise ValueError(
                "an LLM_EXTRACTION check may only be INCOMPLETE: a model must not "
                "be the source of truth for a deterministic eligibility rule")
        if self.determined_by is DeterminationSource.COUNTRY_PACK_RULE \
                and self.status is EligibilityStatus.INELIGIBLE \
                and self.authority is not RuleAuthority.VERIFIED:
            raise ValueError(
                "a COUNTRY_PACK_RULE may only be INELIGIBLE when its authority is "
                "VERIFIED; operator-maintained pack data raises REVIEW_REQUIRED, "
                "it does not refuse an application on its own")
        return self


_STATUS_SEVERITY: dict[EligibilityStatus, int] = {
    EligibilityStatus.ELIGIBLE: 0,
    EligibilityStatus.INCOMPLETE: 1,
    EligibilityStatus.REVIEW_REQUIRED: 2,
    EligibilityStatus.INELIGIBLE: 3,
}


class EligibilityResult(DomainModel):
    """Every gate for one candidate/opportunity pair, and the verdict they imply.

    `status` is a property, not a field: a stored aggregate can drift out of step
    with the checks it summarizes, and a result that says ELIGIBLE while holding a
    failed check is worse than no result at all. Phase 9 persists a denormalized
    copy of the derived value so a list can rank and filter by it without loading
    every check, but the property here stays the one source of truth the mapper
    reads *from*.

    At least one check is required. An empty result would have to be read as
    vacuously eligible, which means a service that crashed before evaluating
    anything would look like a clean pass.

    Unlike `MatchEvaluation.dimensions`, repeated requirements are allowed: two
    languages produce two `LANGUAGE_MINIMUM` checks, and both belong in the
    audit trail.

    `policy_version` is provenance: the eligibility engine and policy that
    produced this verdict, kept as a plain string for the same provider-neutral
    reason `MatchEvaluation.evaluator_key` is, so a re-evaluation under a changed
    policy is auditable.
    """

    id: EligibilityResultId
    user_id: UserId
    candidate_profile_id: CandidateProfileId
    opportunity_id: OpportunityId
    checks: Annotated[tuple[EligibilityCheck, ...], Field(min_length=1)]
    policy_version: NonEmptyStr | None = None
    determined_at: UtcDatetime

    @property
    def status(self) -> EligibilityStatus:
        """Worst-of aggregation: one closed gate closes the result."""
        return max(
            (check.status for check in self.checks),
            key=lambda status: _STATUS_SEVERITY[status],
        )

    @property
    def is_blocking(self) -> bool:
        """True only for a definite refusal.

        Neither `INCOMPLETE` nor `REVIEW_REQUIRED` blocks — both route to human
        review, which is what `ApplicationDecision.REQUIRE_REVIEW` exists for.
        Only `INELIGIBLE`, which the domain permits only on a `VERIFIED` rule or a
        candidate's own declaration, is a refusal.
        """
        return self.status is EligibilityStatus.INELIGIBLE

    def failed_checks(self) -> tuple[EligibilityCheck, ...]:
        """The gates that closed, for a UI that has to explain the refusal."""
        return tuple(check for check in self.checks
                     if check.status is EligibilityStatus.INELIGIBLE)

    def review_checks(self) -> tuple[EligibilityCheck, ...]:
        """The gates a human must confirm before they could count against anyone.

        Distinct from `unresolved_checks`: a review gate found something (an
        unverified pack rule, a posting-declared requirement) and is asking for
        sign-off, whereas an unresolved gate is simply missing evidence.
        """
        return tuple(check for check in self.checks
                     if check.status is EligibilityStatus.REVIEW_REQUIRED)

    def unresolved_checks(self) -> tuple[EligibilityCheck, ...]:
        """The gates still unknown, i.e. the questions more evidence would answer."""
        return tuple(check for check in self.checks
                     if check.status is EligibilityStatus.INCOMPLETE)
