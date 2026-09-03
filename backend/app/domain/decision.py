"""`ApplicationDecision` — where fit, legality and the user's rules meet.

This is the one place the two halves of the model come together: a
`MatchEvaluation` says how well the pair fits, an `EligibilityResult` says
whether it is allowed, and the decision records what the platform intends to do
about it. Keeping them as three objects is what makes the Phase 1 target case
representable — technical fit high, eligibility failed, decision do-not-apply —
instead of one number that has already lost the argument.

A decision is a *record of intent*, not an action. Nothing here submits, and the
model carries no adapter, credential or session detail
(docs/ARCHITECTURE.md §9). Phase 8 reads a decision, checks the policy again at
execution time, and only then calls an adapter.
"""
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.domain.base import DomainModel, NonEmptyStr, Score, UtcDatetime
from backend.app.domain.common import Reason
from backend.app.domain.eligibility import EligibilityResult
from backend.app.domain.identifiers import (
    ApplicationDecisionId,
    ApplicationPolicyId,
    CandidateProfileId,
    CompanyId,
    OpportunityId,
    UserId,
)
from backend.app.domain.matching import MatchEvaluation


class ApplicationDecisionKind(StrEnum):
    """What the platform intends to do about one opportunity.

    Ordered from least to most committing:

    - `SKIP` — not worth pursuing, with reasons the user can inspect;
    - `SAVE` — keep it for the user to look at, do nothing else;
    - `PREPARE` — draft materials so the user can submit quickly;
    - `REQUIRE_REVIEW` — something is unresolved and a human must answer it;
    - `AUTO_APPLY` — submit through the posting's own channel;
    - `APPLY_AND_OUTREACH` — submit *and* contact a person about it;
    - `SPONTANEOUS_APPLICATION` — approach an employer that has no posting
      (docs/V2_SPECIFICATION.md §6), which is why a decision can name a company
      without naming an opportunity.
    """

    SKIP = "SKIP"
    SAVE = "SAVE"
    PREPARE = "PREPARE"
    REQUIRE_REVIEW = "REQUIRE_REVIEW"
    AUTO_APPLY = "AUTO_APPLY"
    APPLY_AND_OUTREACH = "APPLY_AND_OUTREACH"
    SPONTANEOUS_APPLICATION = "SPONTANEOUS_APPLICATION"


# The kinds that put something in front of an employer. Phase 8 gates real
# submissions on this set, so it is public and named once rather than re-listed
# at every call site.
SUBMITTING_DECISION_KINDS: frozenset[ApplicationDecisionKind] = frozenset({
    ApplicationDecisionKind.AUTO_APPLY,
    ApplicationDecisionKind.APPLY_AND_OUTREACH,
    ApplicationDecisionKind.SPONTANEOUS_APPLICATION,
})


class ApplicationDecision(DomainModel):
    """One decision about one target, with everything needed to defend it.

    `reasons` is required and non-empty. docs/V2_SPECIFICATION.md §13 asks that a
    decision explain itself, and the failure this prevents is concrete: a `SKIP`
    with no reason is indistinguishable from a crash, and an `AUTO_APPLY` with no
    reason cannot be audited after the fact.

    `match` and `eligibility` are optional because an early decision (`SAVE` on a
    freshly discovered posting) legitimately precedes scoring. When present, they
    must describe *this* pair — the validator refuses a decision that pairs one
    candidate's fit with another's eligibility.
    """

    id: ApplicationDecisionId
    user_id: UserId
    candidate_profile_id: CandidateProfileId
    opportunity_id: OpportunityId | None = None
    company_id: CompanyId | None = None
    policy_id: ApplicationPolicyId | None = None
    kind: ApplicationDecisionKind
    reasons: Annotated[tuple[Reason, ...], Field(min_length=1)]
    confidence: Score | None = None
    requires_human_review: bool = False
    match: MatchEvaluation | None = None
    eligibility: EligibilityResult | None = None
    decided_by: NonEmptyStr | None = None
    decided_at: UtcDatetime

    @property
    def is_submission(self) -> bool:
        """Whether acting on this decision would contact an employer."""
        return self.kind in SUBMITTING_DECISION_KINDS

    @model_validator(mode="after")
    def _target_matches_the_kind(self) -> Self:
        """A decision must name what it is about.

        Every kind but `SPONTANEOUS_APPLICATION` is about a posting and needs an
        `opportunity_id`. A spontaneous application is the mirror image: there is
        no posting, so it needs a `company_id` instead.
        """
        if self.kind is ApplicationDecisionKind.SPONTANEOUS_APPLICATION:
            if self.company_id is None:
                raise ValueError(
                    "a SPONTANEOUS_APPLICATION must name the company it targets")
        elif self.opportunity_id is None:
            raise ValueError(f"a {self.kind} decision must name an opportunity_id")
        return self

    @model_validator(mode="after")
    def _evidence_describes_this_pair(self) -> Self:
        if self.match is not None:
            if self.match.user_id != self.user_id \
                    or self.match.candidate_profile_id != self.candidate_profile_id:
                raise ValueError("match belongs to a different candidate")
            if self.opportunity_id is not None \
                    and self.match.opportunity_id != self.opportunity_id:
                raise ValueError("match describes a different opportunity")
        if self.eligibility is not None:
            if self.eligibility.user_id != self.user_id \
                    or self.eligibility.candidate_profile_id \
                    != self.candidate_profile_id:
                raise ValueError("eligibility belongs to a different candidate")
            if self.opportunity_id is not None \
                    and self.eligibility.opportunity_id != self.opportunity_id:
                raise ValueError("eligibility describes a different opportunity")
        return self
    @model_validator(mode="after")
    def _nothing_submits_past_a_closed_gate(self) -> Self:
        """The invariant the whole eligibility/compatibility split exists for.

        A definite `INELIGIBLE` verdict, or a decision that has already been
        flagged for a human, cannot coexist with a submitting kind. `INCOMPLETE`
        is deliberately *not* refused here: whether an unresolved gate may be
        submitted through is a policy setting
        (`ApplicationPolicy.allow_incomplete_eligibility`), and hard-coding it
        would make that setting dead. `REQUIRE_REVIEW` must agree with its own
        flag, or the two fields would contradict each other.
        """
        if self.kind is ApplicationDecisionKind.REQUIRE_REVIEW \
                and not self.requires_human_review:
            raise ValueError(
                "a REQUIRE_REVIEW decision must set requires_human_review")
        if not self.is_submission:
            return self
        if self.requires_human_review:
            raise ValueError(
                f"{self.kind} cannot also require human review: decide "
                f"REQUIRE_REVIEW instead")
        if self.eligibility is not None and self.eligibility.is_blocking:
            failed = ", ".join(str(check.requirement)
                               for check in self.eligibility.failed_checks())
            raise ValueError(
                f"{self.kind} is refused: eligibility is INELIGIBLE ({failed})")
        return self
