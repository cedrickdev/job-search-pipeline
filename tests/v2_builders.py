# tests/v2_builders.py
"""Shared constructors for the Phase 1 V2 domain tests.

Only what more than one test module needs lives here: a `MatchEvaluation` and an
`EligibilityResult`, which `test_v2_decision.py` and `test_v2_policy.py` both
have to hand to something else. Everything a test is actually asserting on is
built inline in that test — a builder that hides the field under test makes the
test unreadable.

The ids are module constants rather than fresh uuid4s for two reasons: a failure
message points at a value one can grep for, and the "belongs to another user"
tests need a second identity that is obviously different.
"""
from datetime import UTC, datetime
from uuid import UUID

from backend.app.domain.common import Reason, ReasonImpact
from backend.app.domain.eligibility import (
    DeterminationSource,
    EligibilityCheck,
    EligibilityRequirement,
    EligibilityResult,
    EligibilityStatus,
)
from backend.app.domain.identifiers import (
    ApplicationDecisionId,
    ApplicationPolicyId,
    CandidateProfileId,
    CompanyId,
    MatchEvaluationId,
    OpportunityId,
    UserId,
)
from backend.app.domain.matching import DimensionScore, MatchDimension, MatchEvaluation

NOW = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)
LATER = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)

USER = UserId(UUID("00000000-0000-4000-8000-000000000001"))
OTHER_USER = UserId(UUID("00000000-0000-4000-8000-000000000002"))
PROFILE = CandidateProfileId(UUID("00000000-0000-4000-8000-000000000011"))
OTHER_PROFILE = CandidateProfileId(UUID("00000000-0000-4000-8000-000000000012"))
OPPORTUNITY = OpportunityId(UUID("00000000-0000-4000-8000-000000000021"))
OTHER_OPPORTUNITY = OpportunityId(UUID("00000000-0000-4000-8000-000000000022"))
COMPANY = CompanyId(UUID("00000000-0000-4000-8000-000000000031"))
EVALUATION = MatchEvaluationId(UUID("00000000-0000-4000-8000-000000000041"))
POLICY = ApplicationPolicyId(UUID("00000000-0000-4000-8000-000000000051"))
DECISION = ApplicationDecisionId(UUID("00000000-0000-4000-8000-000000000061"))


def a_reason(code="REASON_UNDER_TEST", impact=ReasonImpact.NEUTRAL):
    return Reason(code=code, detail=f"detail behind {code}", impact=impact)


def an_evaluation(**overrides):
    """A high-fit evaluation: one scored dimension, `overall` 0.9."""
    fields = {
        "id": EVALUATION,
        "user_id": USER,
        "candidate_profile_id": PROFILE,
        "opportunity_id": OPPORTUNITY,
        "overall": 0.9,
        "dimensions": (DimensionScore(dimension=MatchDimension.SKILLS_FIT,
                                      score=0.92),),
        "evaluated_at": NOW,
    }
    fields.update(overrides)
    return MatchEvaluation(**fields)


def a_check(requirement=EligibilityRequirement.WORK_AUTHORIZATION,
            status=EligibilityStatus.ELIGIBLE,
            determined_by=DeterminationSource.DETERMINISTIC_RULE):
    """One evaluated gate, with the reason a non-ELIGIBLE verdict must carry."""
    reasons = () if status is EligibilityStatus.ELIGIBLE else (
        a_reason(code=f"{requirement}_{status}", impact=ReasonImpact.NEGATIVE),)
    return EligibilityCheck(requirement=requirement, status=status,
                            determined_by=determined_by, reasons=reasons)


def an_eligibility_result(*checks, **overrides):
    """A result over `checks`, defaulting to a single passing gate."""
    fields = {
        "user_id": USER,
        "candidate_profile_id": PROFILE,
        "opportunity_id": OPPORTUNITY,
        "checks": checks or (a_check(),),
        "determined_at": NOW,
    }
    fields.update(overrides)
    return EligibilityResult(**fields)
