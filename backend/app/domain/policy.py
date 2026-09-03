"""`ApplicationPolicy` — the user's standing instructions about applying.

Policy is knowledge, not behaviour: nothing in this module submits anything, and
nothing here knows that Playwright, an HTTP form or an email exists. It answers
questions ("may this be submitted without me?", "how many are left today?") that
a Phase 8 application service asks before it calls an adapter
(docs/ARCHITECTURE.md §9, CLAUDE.md: "Playwright is an application/browser
adapter, not the decision engine").

Every default is the cautious one. A policy created with no arguments beyond its
identity is `MANUAL`, requires approval, allows no spontaneous outreach and sets
no thresholds — so a bug that forgets to load a user's real policy cannot produce
an autonomous submission (docs/ENGINEERING_STANDARDS.md §Security).
"""
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.domain.base import DomainModel, NonEmptyStr, Score, UtcDatetime
from backend.app.domain.common import Reason, ReasonImpact
from backend.app.domain.identifiers import ApplicationPolicyId, UserId
from backend.app.domain.matching import MatchDimension, MatchEvaluation
from backend.app.domain.opportunity import OpportunityType


class AutomationMode(StrEnum):
    """How much the platform may do on the user's behalf.

    A deliberate ladder, because "automation on/off" cannot express the mode this
    product actually needs most: prepare everything, submit nothing.

    - `MANUAL` — the platform finds and scores, the user does the rest;
    - `COPY_ASSISTED` — it also prepares materials (CV, message) to copy;
    - `SUPERVISED` — it may fill a form and stop, waiting for approval;
    - `AUTOPILOT` — it may submit, and only within the rest of this policy.
    """

    MANUAL = "MANUAL"
    COPY_ASSISTED = "COPY_ASSISTED"
    SUPERVISED = "SUPERVISED"
    AUTOPILOT = "AUTOPILOT"


class DimensionThreshold(DomainModel):
    """A floor on one match dimension.

    Mirrors `DimensionScore` so a threshold and a score compare directly. Kept as
    a tuple element rather than a mapping field because a mapping would make the
    policy unhashable for no gain.
    """

    dimension: MatchDimension
    minimum: Score


class ApplicationPolicy(DomainModel):
    """One user's rules for turning matches into applications.

    `allowed_opportunity_types` follows the domain-wide convention: empty means
    no restriction. The rate limits use `None` for "no limit" and `0` for "none
    today", which is a real setting — it is how a user pauses without deleting a
    policy.
    """

    id: ApplicationPolicyId
    user_id: UserId
    name: NonEmptyStr
    is_active: bool = True
    mode: AutomationMode = AutomationMode.MANUAL
    require_approval_before_submission: bool = True
    allowed_opportunity_types: tuple[OpportunityType, ...] = ()
    minimum_overall_score: Score | None = None
    dimension_thresholds: tuple[DimensionThreshold, ...] = ()
    allow_incomplete_eligibility: bool = False
    allow_spontaneous_applications: bool = False
    max_applications_per_day: Annotated[int, Field(ge=0)] | None = None
    max_applications_per_week: Annotated[int, Field(ge=0)] | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _brakes_and_limits_are_coherent(self) -> Self:
        """Only `AUTOPILOT` may switch the approval brake off.

        One-directional on purpose: an `AUTOPILOT` policy is still allowed to keep
        the brake on (that is `SUPERVISED` behaviour with autopilot intent), but no
        lesser mode may claim it needs no approval, which would let a
        misconfigured policy submit silently.
        """
        if self.mode is not AutomationMode.AUTOPILOT \
                and not self.require_approval_before_submission:
            raise ValueError(
                f"mode {self.mode} must keep require_approval_before_submission "
                f"set: only AUTOPILOT may submit without approval")
        thresholds = [entry.dimension for entry in self.dimension_thresholds]
        if len(thresholds) != len(set(thresholds)):
            raise ValueError("dimension_thresholds must not repeat a MatchDimension")
        if self.max_applications_per_day is not None \
                and self.max_applications_per_week is not None \
                and self.max_applications_per_day > self.max_applications_per_week:
            raise ValueError("max_applications_per_day must not exceed "
                             "max_applications_per_week")
        if self.updated_at < self.created_at:
            raise ValueError("ApplicationPolicy updated_at must not precede created_at")
        return self
    @property
    def permits_unattended_submission(self) -> bool:
        """Whether the platform may submit without asking first.

        Both switches must agree, and an inactive policy permits nothing.
        """
        return (self.is_active
                and self.mode is AutomationMode.AUTOPILOT
                and not self.require_approval_before_submission)

    @property
    def permits_prepared_materials(self) -> bool:
        """Whether the platform may draft a CV or message for this user."""
        return self.is_active and self.mode is not AutomationMode.MANUAL

    def allows_opportunity_type(self, opportunity_type: OpportunityType | None) -> bool:
        """Empty allow-list means any type.

        An unclassified opportunity (`None`) does *not* pass when a restriction
        exists: unlike discovery, where dropping unknowns hides postings, applying
        to something the platform cannot classify is the risky direction.
        """
        if not self.allowed_opportunity_types:
            return True
        return opportunity_type in self.allowed_opportunity_types

    def unmet_thresholds(self, evaluation: MatchEvaluation) -> tuple[Reason, ...]:
        """Every score floor this evaluation fails, as explainable reasons.

        Returning reasons rather than a bare bool is what lets a decision say
        "below your language floor" instead of "policy rejected". A threshold on a
        dimension the evaluation never computed is unmet: a missing score is not
        evidence of a good one.
        """
        unmet: list[Reason] = []
        if self.minimum_overall_score is not None \
                and evaluation.overall < self.minimum_overall_score:
            unmet.append(Reason(
                code="POLICY_OVERALL_BELOW_MINIMUM",
                detail=(f"overall {evaluation.overall:.2f} is below the policy "
                        f"minimum {self.minimum_overall_score:.2f}"),
                impact=ReasonImpact.NEGATIVE))
        for threshold in self.dimension_thresholds:
            scored = evaluation.score_for(threshold.dimension)
            if scored is None:
                unmet.append(Reason(
                    code="POLICY_DIMENSION_NOT_SCORED",
                    detail=(f"{threshold.dimension} has a policy floor of "
                            f"{threshold.minimum:.2f} but was never scored"),
                    impact=ReasonImpact.NEGATIVE))
            elif scored.score < threshold.minimum:
                unmet.append(Reason(
                    code="POLICY_DIMENSION_BELOW_MINIMUM",
                    detail=(f"{threshold.dimension} {scored.score:.2f} is below the "
                            f"policy minimum {threshold.minimum:.2f}"),
                    impact=ReasonImpact.NEGATIVE))
        return tuple(unmet)

    def remaining_submissions(self, submitted_today: int,
                              submitted_this_week: int) -> int | None:
        """How many more submissions the rate limits allow, or `None` if unlimited.

        Pure arithmetic: the caller owns the clock and the counting, because a
        domain object that read a clock could not be tested deterministically.
        Never negative — an over-budget count returns 0.
        """
        budgets = [limit - used
                   for limit, used in ((self.max_applications_per_day, submitted_today),
                                       (self.max_applications_per_week,
                                        submitted_this_week))
                   if limit is not None]
        if not budgets:
            return None
        return max(0, min(budgets))
