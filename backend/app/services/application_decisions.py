"""ApplicationDecisionService: turn a scored pair into a deterministic intent (§30-32).

A decision is *intent*, not action, and this service is the one deterministic place
that forms it. Given a (candidate, opportunity) pair's match, eligibility and the
user's policy, it decides what the platform means to do — skip, save, prepare, ask a
human, or apply — with reasons, and never anything else. It reads no clock beyond the
`now` handed to it and consults no model: the same inputs always yield the same
decision, which is what lets §99-119 pin the behaviour down.

The order of the checks is the safety order. A definite ineligibility or a match
below the user's floor ends it at `SKIP` before any intent to apply forms; an
unresolved or review-gated eligibility routes to `REQUIRE_REVIEW`; and only a pair
that clears all of that reaches the policy-mode mapping, where `MANUAL` saves,
`COPY_ASSISTED` prepares, and the two autonomous modes intend to apply. The
*authority* to actually submit is still the execution gate's alone (§4) — this
service only records what the platform would like to do, and the gate re-decides
whether it may.
"""
from datetime import datetime

from backend.app.domain.common import Reason, ReasonImpact
from backend.app.domain.decision import ApplicationDecision, ApplicationDecisionKind
from backend.app.domain.eligibility import EligibilityResult, EligibilityStatus
from backend.app.domain.identifiers import (
    OpportunityId,
    UserId,
    new_application_decision_id,
)
from backend.app.domain.matching import MatchEvaluation
from backend.app.domain.policy import ApplicationPolicy, AutomationMode
from backend.app.repositories.contracts import (
    ApplicationDecisionRepository,
    ApplicationPolicyRepository,
    CandidateProfileRepository,
    EligibilityResultRepository,
    MatchEvaluationRepository,
    OpportunityRepository,
)
from backend.app.services.assessment import (
    CandidateProfileNotFound,
    OpportunityNotFound,
)

# How each automation mode expresses "this pair is worth acting on". The floor is
# `SAVE` (find and keep, do nothing) and the ceiling is `AUTO_APPLY` (intend to
# submit, gate permitting); nothing here bypasses the gate, it only names intent.
_MODE_INTENT: dict[AutomationMode, ApplicationDecisionKind] = {
    AutomationMode.MANUAL: ApplicationDecisionKind.SAVE,
    AutomationMode.COPY_ASSISTED: ApplicationDecisionKind.PREPARE,
    AutomationMode.SUPERVISED: ApplicationDecisionKind.AUTO_APPLY,
    AutomationMode.AUTOPILOT: ApplicationDecisionKind.AUTO_APPLY,
}


class ApplicationDecisionService:
    """Forms and stores the deterministic decision for a (candidate, opportunity) pair.

    Every collaborator is injected: the repositories that load the pair's profile,
    opportunity, match, eligibility and policy, and the decision store the result is
    upserted into. The decision id is derived from nothing random when re-deciding —
    a fresh decision uses a new id, so re-running the matcher records a new intent
    rather than mutating the last, and the audit reads the history.
    """

    def __init__(self, profiles: CandidateProfileRepository,
                 opportunities: OpportunityRepository,
                 matches: MatchEvaluationRepository,
                 eligibilities: EligibilityResultRepository,
                 policies: ApplicationPolicyRepository,
                 decisions: ApplicationDecisionRepository) -> None:
        self._profiles = profiles
        self._opportunities = opportunities
        self._matches = matches
        self._eligibilities = eligibilities
        self._policies = policies
        self._decisions = decisions

    async def decide(self, user_id: UserId, opportunity_id: OpportunityId, *,
                     now: datetime) -> ApplicationDecision:
        """Form, persist and return the decision for one pair.

        Loads the pair's evidence — the profile and opportunity must exist; the
        match, eligibility and policy may not yet, and their absence is handled
        conservatively (an unscored pair is saved, not applied to). The composed
        decision is upserted so the intent is durable and auditable.
        """
        profile = await self._profiles.get_default(user_id)
        if profile is None:
            raise CandidateProfileNotFound(str(user_id))
        opportunity = await self._opportunities.get(opportunity_id)
        if opportunity is None:
            raise OpportunityNotFound(str(opportunity_id))

        match = await self._matches.get_for_pair(user_id, profile.id, opportunity_id)
        eligibility = await self._eligibilities.get_for_pair(
            user_id, profile.id, opportunity_id)
        policy = await self._policies.get_default(user_id)

        kind, requires_review, reasons = _classify(policy, eligibility, match)
        decision = ApplicationDecision(
            id=new_application_decision_id(),
            user_id=user_id,
            candidate_profile_id=profile.id,
            opportunity_id=opportunity_id,
            policy_id=policy.id if policy is not None else None,
            kind=kind,
            reasons=reasons,
            confidence=match.overall if match is not None else None,
            requires_human_review=requires_review,
            match=match,
            eligibility=eligibility,
            decided_by="application-decision-service/1",
            decided_at=now,
        )
        return await self._decisions.upsert(decision)


def _classify(
    policy: ApplicationPolicy | None,
    eligibility: EligibilityResult | None,
    match: MatchEvaluation | None,
) -> tuple[ApplicationDecisionKind, bool, tuple[Reason, ...]]:
    """The whole deterministic decision, as (kind, requires_human_review, reasons).

    Pure: it reads only its arguments, so it is the unit the regressions exercise
    directly. The checks run worst-first, and each returns as soon as it fires, so a
    definite refusal never falls through to an intent to apply.
    """
    # 1. A definite ineligibility ends it: SKIP, with the gates that closed.
    if eligibility is not None and eligibility.is_blocking:
        return (ApplicationDecisionKind.SKIP, False,
                _blocking_reasons(eligibility))

    # 2. A match below the user's floors ends it too: not worth pursuing.
    if policy is not None and match is not None:
        unmet = policy.unmet_thresholds(match)
        if unmet:
            return (ApplicationDecisionKind.SKIP, False, unmet)

    # 3. An unresolved or review-gated eligibility routes to a human.
    if eligibility is not None and eligibility.status is EligibilityStatus.REVIEW_REQUIRED:
        return (ApplicationDecisionKind.REQUIRE_REVIEW, True,
                _review_reasons(eligibility))
    if eligibility is not None \
            and eligibility.status is EligibilityStatus.INCOMPLETE \
            and (policy is None or not policy.allow_incomplete_eligibility):
        return (ApplicationDecisionKind.REQUIRE_REVIEW, True,
                _incomplete_reasons(eligibility))

    # 4. No policy at all is the cautious default: save, decide nothing autonomous.
    if policy is None or not policy.is_active:
        return (ApplicationDecisionKind.SAVE, False, (_reason(
            "NO_ACTIVE_POLICY",
            "no active application policy, so the opportunity is saved for review"),))

    # 5. A pair nobody scored cannot be applied to on merit: save it.
    if match is None:
        return (ApplicationDecisionKind.SAVE, False, (_reason(
            "MATCH_NOT_EVALUATED",
            "the pair has not been scored yet, so it is saved for review"),))

    # 6. Cleared: the policy mode expresses how far to go.
    kind = _MODE_INTENT[policy.mode]
    return (kind, False, (_reason(
        "MEETS_POLICY",
        f"overall {match.overall:.2f} meets the policy and eligibility is clear",
        impact=ReasonImpact.POSITIVE),))


def _reason(code: str, detail: str,
            impact: ReasonImpact = ReasonImpact.NEUTRAL) -> Reason:
    return Reason(code=code, detail=detail, impact=impact)


def _blocking_reasons(eligibility: EligibilityResult) -> tuple[Reason, ...]:
    reasons = tuple(r for check in eligibility.failed_checks() for r in check.reasons)
    return reasons or (_reason("ELIGIBILITY_INELIGIBLE",
                               "eligibility is INELIGIBLE", ReasonImpact.NEGATIVE),)


def _review_reasons(eligibility: EligibilityResult) -> tuple[Reason, ...]:
    reasons = tuple(r for check in eligibility.review_checks() for r in check.reasons)
    return reasons or (_reason("ELIGIBILITY_REVIEW_REQUIRED",
                               "an eligibility gate needs human confirmation"),)


def _incomplete_reasons(eligibility: EligibilityResult) -> tuple[Reason, ...]:
    reasons = tuple(r for check in eligibility.unresolved_checks() for r in check.reasons)
    return reasons or (_reason("ELIGIBILITY_INCOMPLETE",
                               "eligibility is INCOMPLETE and needs more evidence"),)
