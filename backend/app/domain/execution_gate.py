"""The deterministic gate that decides whether an application may be submitted.

This is the single load-bearing safety mechanism of the whole engine (§1, §4). No
match score, no model proposal and no earlier `ApplicationDecision` submits
anything on its own — every submission passes through `ApplicationExecutionGate`
*at submission time*, and the gate re-reads the policy, the eligibility verdict, the
match thresholds, the rate budget and the adapter's own safety ceiling, then returns
one typed `ExecutionAuthorization`. The regression §5 exists for exactly this: a run
authorized under an `AUTOPILOT` policy at 09:00 is `BLOCKED` at 11:05 if the policy
became `MANUAL` in between, because the gate reads the policy as it is *now*, not as
it was when the decision was made.

The gate is pure. It reads a clock through nothing — the caller counts the day's and
week's submissions against the current time and passes the counts in — so the same
inputs always yield the same authorization, which is what makes §99-119's regressions
possible to write. Nothing here submits, prepares or touches an adapter; it only
decides, and it composes every reason it decided on so the answer is auditable
(§13, §41).
"""
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from backend.app.domain.application_channel import (
    AdapterSafetyLevel,
    HumanRequiredReason,
)
from backend.app.domain.base import DomainModel
from backend.app.domain.common import Reason, ReasonImpact
from backend.app.domain.decision import ApplicationDecision
from backend.app.domain.eligibility import EligibilityResult, EligibilityStatus
from backend.app.domain.matching import MatchEvaluation
from backend.app.domain.opportunity import OpportunityType
from backend.app.domain.policy import ApplicationPolicy, AutomationMode


class ExecutionOutcome(StrEnum):
    """What the gate authorizes, from most to least autonomous.

    A ladder, so combining several constraints is "take the least autonomous", and
    a UI can rank them. `severity` makes that ordering explicit — the string values
    do not compare.

    - `PERMITTED` — the platform may submit unattended;
    - `REQUIRES_APPROVAL` — it may prepare and fill, but a human must approve the
      actual submission (§52-59);
    - `REQUIRES_HUMAN` — something can only be resolved by a person: a review-gated
      eligibility verdict, a CAPTCHA, a sensitive question, an unknown required field
      (§19-27);
    - `BLOCKED` — it must not proceed at all: ineligible, over budget, a policy that
      forbids submission, a match below the user's floor.
    """

    PERMITTED = "PERMITTED"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    REQUIRES_HUMAN = "REQUIRES_HUMAN"
    BLOCKED = "BLOCKED"

    @property
    def severity(self) -> int:
        """Position on the ladder; the least autonomous of several outcomes wins."""
        return _OUTCOME_SEVERITY[self]


_OUTCOME_SEVERITY: dict[ExecutionOutcome, int] = {
    ExecutionOutcome.PERMITTED: 0,
    ExecutionOutcome.REQUIRES_APPROVAL: 1,
    ExecutionOutcome.REQUIRES_HUMAN: 2,
    ExecutionOutcome.BLOCKED: 3,
}


class ExecutionAuthorization(DomainModel):
    """The gate's verdict: an outcome, every reason behind it, and any human needs.

    `reasons` is required and non-empty — a `PERMITTED` authorization still says why
    it permitted, so the audit trail (§41) never has to infer intent from silence,
    and a refusal always names the gate that closed (§13). `human_required_reasons`
    is the typed subset a UI turns into an actionable checklist ("solve the CAPTCHA",
    "answer this question"); it is only populated when `outcome` is `REQUIRES_HUMAN`.
    """

    outcome: ExecutionOutcome
    reasons: tuple[Reason, ...] = Field(min_length=1)
    human_required_reasons: tuple[HumanRequiredReason, ...] = ()

    @model_validator(mode="after")
    def _human_reasons_belong_to_a_human_outcome(self) -> Self:
        if self.human_required_reasons \
                and self.outcome is not ExecutionOutcome.REQUIRES_HUMAN:
            raise ValueError(
                "human_required_reasons may only accompany a REQUIRES_HUMAN outcome")
        return self

    @property
    def permits_submission(self) -> bool:
        return self.outcome is ExecutionOutcome.PERMITTED

    @property
    def needs_approval(self) -> bool:
        return self.outcome is ExecutionOutcome.REQUIRES_APPROVAL

    @property
    def needs_human(self) -> bool:
        return self.outcome is ExecutionOutcome.REQUIRES_HUMAN

    @property
    def is_blocked(self) -> bool:
        return self.outcome is ExecutionOutcome.BLOCKED


def _reason(code: str, detail: str,
            impact: ReasonImpact = ReasonImpact.NEGATIVE) -> Reason:
    return Reason(code=code, detail=detail, impact=impact)


class ApplicationExecutionGate:
    """Evaluates whether one prepared application may be submitted, and how.

    Stateless and deterministic: `evaluate` is the whole surface. It gathers a list
    of `(outcome, reason)` findings from independent checks, and the authorization's
    outcome is the least autonomous — the most restrictive — of them, with `PERMITTED`
    as the floor when nothing restricts. Every finding's reason is kept, so the result
    explains *all* the constraints that applied, not just the winning one.
    """

    @classmethod
    def evaluate(
        cls,
        *,
        decision: ApplicationDecision,
        policy: ApplicationPolicy,
        adapter_safety: AdapterSafetyLevel,
        opportunity_type: OpportunityType | None = None,
        eligibility: EligibilityResult | None = None,
        match: MatchEvaluation | None = None,
        submitted_today: int = 0,
        submitted_this_week: int = 0,
        human_required_reasons: tuple[HumanRequiredReason, ...] = (),
    ) -> ExecutionAuthorization:
        """Return the one authorization that governs this submission attempt.

        Every argument is read as it is *now* (§5): `policy` is the freshly loaded
        policy, `submitted_today`/`submitted_this_week` are counted by the caller
        against the current clock, and `human_required_reasons` are whatever
        preparation surfaced (a CAPTCHA, a sensitive question). The gate itself holds
        no clock and no state, so the same inputs always produce the same verdict.
        """
        findings: list[tuple[ExecutionOutcome, Reason]] = []
        findings.extend(cls._check_decision(decision))
        findings.extend(cls._check_policy_mode(policy))
        findings.extend(cls._check_opportunity_type(policy, opportunity_type))
        findings.extend(cls._check_eligibility(eligibility))
        findings.extend(cls._check_incomplete_eligibility(policy, eligibility))
        findings.extend(cls._check_match(policy, match))
        findings.extend(cls._check_rate_limit(policy, submitted_today,
                                              submitted_this_week))
        human = cls._collect_human_reasons(eligibility, human_required_reasons)
        findings.extend(cls._check_adapter_ceiling(adapter_safety))
        findings.extend(cls._check_approval_brake(policy))

        for reason in human:
            findings.append((ExecutionOutcome.REQUIRES_HUMAN,
                            cls._human_reason(reason)))

        return cls._decide(findings, human)

    # -- individual checks ---------------------------------------------------

    @staticmethod
    def _check_decision(
            decision: ApplicationDecision) -> list[tuple[ExecutionOutcome, Reason]]:
        if not decision.is_submission:
            return [(ExecutionOutcome.BLOCKED, _reason(
                "DECISION_NOT_SUBMITTABLE",
                f"a {decision.kind} decision does not submit an application"))]
        return []

    @staticmethod
    def _check_policy_mode(
            policy: ApplicationPolicy) -> list[tuple[ExecutionOutcome, Reason]]:
        if not policy.is_active:
            return [(ExecutionOutcome.BLOCKED, _reason(
                "POLICY_INACTIVE", "the application policy is not active"))]
        if policy.mode in (AutomationMode.MANUAL, AutomationMode.COPY_ASSISTED):
            return [(ExecutionOutcome.BLOCKED, _reason(
                "POLICY_MODE_FORBIDS_SUBMISSION",
                f"policy mode {policy.mode} does not let the platform submit; the "
                f"candidate applies themselves"))]
        return []

    @staticmethod
    def _check_opportunity_type(
        policy: ApplicationPolicy, opportunity_type: OpportunityType | None,
    ) -> list[tuple[ExecutionOutcome, Reason]]:
        if not policy.allows_opportunity_type(opportunity_type):
            named = opportunity_type or "an unclassified opportunity"
            return [(ExecutionOutcome.BLOCKED, _reason(
                "POLICY_OPPORTUNITY_TYPE_NOT_ALLOWED",
                f"the policy does not allow applying to {named}"))]
        return []

    @staticmethod
    def _check_eligibility(
        eligibility: EligibilityResult | None,
    ) -> list[tuple[ExecutionOutcome, Reason]]:
        # Absence is treated as unresolved, not as a pass: without a verdict the
        # platform cannot claim the application is allowed, so a human must establish
        # it before anything is submitted.
        if eligibility is None:
            return [(ExecutionOutcome.REQUIRES_HUMAN, _reason(
                "ELIGIBILITY_NOT_EVALUATED",
                "eligibility has not been evaluated for this pair"))]
        status = eligibility.status
        if status is EligibilityStatus.INELIGIBLE:
            failed = ", ".join(str(check.requirement)
                               for check in eligibility.failed_checks())
            return [(ExecutionOutcome.BLOCKED, _reason(
                "ELIGIBILITY_INELIGIBLE",
                f"eligibility is INELIGIBLE ({failed})"))]
        if status is EligibilityStatus.REVIEW_REQUIRED:
            return [(ExecutionOutcome.REQUIRES_HUMAN, _reason(
                "ELIGIBILITY_REVIEW_REQUIRED",
                "an eligibility gate needs human confirmation before it counts"))]
        # INCOMPLETE is handled by the caller's policy flag (§6); it is not decided
        # here, because whether an unresolved gate may be submitted through is a
        # policy setting, and `_check_incomplete_eligibility` reads that flag.
        return []

    @classmethod
    def _check_match(
        cls, policy: ApplicationPolicy, match: MatchEvaluation | None,
    ) -> list[tuple[ExecutionOutcome, Reason]]:
        wants_thresholds = (policy.minimum_overall_score is not None
                           or bool(policy.dimension_thresholds))
        if match is None:
            if wants_thresholds:
                return [(ExecutionOutcome.REQUIRES_HUMAN, _reason(
                    "POLICY_MATCH_NOT_EVALUATED",
                    "the policy sets match floors but this pair was never scored"))]
            return []
        return [(ExecutionOutcome.BLOCKED, unmet)
                for unmet in policy.unmet_thresholds(match)]

    @staticmethod
    def _check_rate_limit(
        policy: ApplicationPolicy, submitted_today: int, submitted_this_week: int,
    ) -> list[tuple[ExecutionOutcome, Reason]]:
        remaining = policy.remaining_submissions(submitted_today, submitted_this_week)
        if remaining is not None and remaining <= 0:
            return [(ExecutionOutcome.BLOCKED, _reason(
                "POLICY_RATE_LIMIT_EXHAUSTED",
                "the policy's application rate limit is exhausted"))]
        return []

    @staticmethod
    def _check_incomplete_eligibility(
        policy: ApplicationPolicy, eligibility: EligibilityResult | None,
    ) -> list[tuple[ExecutionOutcome, Reason]]:
        if eligibility is None:
            return []
        if eligibility.status is EligibilityStatus.INCOMPLETE \
                and not policy.allow_incomplete_eligibility:
            return [(ExecutionOutcome.BLOCKED, _reason(
                "ELIGIBILITY_INCOMPLETE_NOT_ALLOWED",
                "eligibility is INCOMPLETE and the policy does not allow submitting "
                "through unresolved gates"))]
        return []

    @staticmethod
    def _check_adapter_ceiling(
        adapter_safety: AdapterSafetyLevel,
    ) -> list[tuple[ExecutionOutcome, Reason]]:
        """The adapter's one-directional autonomy ceiling (§59).

        An adapter can only *lower* autonomy: it caps the outcome, never raises it.
        `UNSUPPORTED` and `MANUAL_ONLY` cap at a human hand-off, `SUPPORTED_WITH_REVIEW`
        at approval, and `FULLY_SUPPORTED` adds no ceiling of its own.
        """
        if adapter_safety is AdapterSafetyLevel.UNSUPPORTED:
            return [(ExecutionOutcome.REQUIRES_HUMAN, _reason(
                "ADAPTER_UNSUPPORTED",
                "no adapter can drive this channel; a human must apply"))]
        if adapter_safety is AdapterSafetyLevel.MANUAL_ONLY:
            return [(ExecutionOutcome.REQUIRES_HUMAN, _reason(
                "ADAPTER_MANUAL_ONLY",
                "the adapter can prepare but not submit; a human submits"))]
        if adapter_safety is AdapterSafetyLevel.SUPPORTED_WITH_REVIEW:
            return [(ExecutionOutcome.REQUIRES_APPROVAL, _reason(
                "ADAPTER_REQUIRES_REVIEW",
                "the adapter requires a human to approve the submission",
                impact=ReasonImpact.NEUTRAL))]
        return []

    @staticmethod
    def _check_approval_brake(
        policy: ApplicationPolicy,
    ) -> list[tuple[ExecutionOutcome, Reason]]:
        if policy.permits_unattended_submission:
            return [(ExecutionOutcome.PERMITTED, _reason(
                "POLICY_PERMITS_UNATTENDED",
                "the policy authorizes unattended submission",
                impact=ReasonImpact.POSITIVE))]
        return [(ExecutionOutcome.REQUIRES_APPROVAL, _reason(
            "POLICY_REQUIRES_APPROVAL",
            "the policy requires a human to approve before submission",
            impact=ReasonImpact.NEUTRAL))]

    # -- combination ---------------------------------------------------------

    @staticmethod
    def _collect_human_reasons(
        eligibility: EligibilityResult | None,
        human_required_reasons: tuple[HumanRequiredReason, ...],
    ) -> tuple[HumanRequiredReason, ...]:
        # Deduplicate while preserving the caller's order, so the checklist a UI
        # renders is stable and a repeated reason does not appear twice.
        seen: dict[HumanRequiredReason, None] = {}
        for reason in human_required_reasons:
            seen.setdefault(reason, None)
        return tuple(seen)

    @staticmethod
    def _human_reason(reason: HumanRequiredReason) -> Reason:
        return _reason("HUMAN_REVIEW_REQUIRED",
                       f"a human must resolve {reason} before submission")

    @classmethod
    def _decide(
        cls,
        findings: list[tuple[ExecutionOutcome, Reason]],
        human: tuple[HumanRequiredReason, ...],
    ) -> ExecutionAuthorization:
        if not findings:
            findings = [(ExecutionOutcome.PERMITTED, _reason(
                "NO_CONSTRAINT",
                "no policy, eligibility, match or adapter constraint applied",
                impact=ReasonImpact.POSITIVE))]
        outcome = max((found for found, _ in findings), key=lambda o: o.severity)
        reasons = tuple(reason for _, reason in findings)
        # The human checklist is only meaningful on a human outcome; on a stricter
        # BLOCKED outcome the run stops for a harder reason and the checklist would
        # be misleading, so it is dropped.
        attached = human if outcome is ExecutionOutcome.REQUIRES_HUMAN else ()
        return ExecutionAuthorization(
            outcome=outcome, reasons=reasons, human_required_reasons=attached)
