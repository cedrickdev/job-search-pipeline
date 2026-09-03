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
    """Three values, because two would lie.

    `INCOMPLETE` is the important one: it means the platform does not yet know,
    and it must not be collapsed into either neighbour. Reading unknown as
    eligible produces applications that waste everyone's time; reading it as
    ineligible silently hides opportunities. It pairs with the
    `ELIGIBILITY_INCOMPLETE` reason code docs/ENGINEERING_STANDARDS.md
    §Observability asks for.
    """

    ELIGIBLE = "ELIGIBLE"
    INCOMPLETE = "INCOMPLETE"
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


class EligibilityCheck(DomainModel):
    """One gate, evaluated.

    Two invariants, both from the phase order rather than from taste:

    - a verdict that is not `ELIGIBLE` must carry at least one `Reason`. An
      unexplained rejection is exactly what docs/V2_SPECIFICATION.md §13 forbids,
      and the candidate is entitled to know which gate closed;
    - an `LLM_EXTRACTION` check may only be `INCOMPLETE`. A model can flag that a
      posting seems to demand a licence; the licence question is then settled by a
      rule or a human, never by the model.
    """

    requirement: EligibilityRequirement
    status: EligibilityStatus
    determined_by: DeterminationSource
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
        return self
_STATUS_SEVERITY: dict[EligibilityStatus, int] = {
    EligibilityStatus.ELIGIBLE: 0,
    EligibilityStatus.INCOMPLETE: 1,
    EligibilityStatus.INELIGIBLE: 2,
}


class EligibilityResult(DomainModel):
    """Every gate for one candidate/opportunity pair, and the verdict they imply.

    `status` is a property, not a field: a stored aggregate can drift out of step
    with the checks it summarizes, and a result that says ELIGIBLE while holding a
    failed check is worse than no result at all. Phase 2 may persist the derived
    value as a generated column; the domain keeps one source of truth.

    At least one check is required. An empty result would have to be read as
    vacuously eligible, which means a service that crashed before evaluating
    anything would look like a clean pass.

    Unlike `MatchEvaluation.dimensions`, repeated requirements are allowed: two
    languages produce two `LANGUAGE_MINIMUM` checks, and both belong in the
    audit trail.
    """

    user_id: UserId
    candidate_profile_id: CandidateProfileId
    opportunity_id: OpportunityId
    checks: Annotated[tuple[EligibilityCheck, ...], Field(min_length=1)]
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

        `INCOMPLETE` is not blocking — it routes to human review, which is what
        `ApplicationDecision.REQUIRE_REVIEW` exists for.
        """
        return self.status is EligibilityStatus.INELIGIBLE

    def failed_checks(self) -> tuple[EligibilityCheck, ...]:
        """The gates that closed, for a UI that has to explain the refusal."""
        return tuple(check for check in self.checks
                     if check.status is EligibilityStatus.INELIGIBLE)

    def unresolved_checks(self) -> tuple[EligibilityCheck, ...]:
        """The gates still unknown, i.e. the questions a human should answer."""
        return tuple(check for check in self.checks
                     if check.status is EligibilityStatus.INCOMPLETE)
