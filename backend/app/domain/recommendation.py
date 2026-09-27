"""`CareerRecommendation` — an evidence-backed suggestion with zero mutation authority (§26-33).

This is the "recommend" step of the phase's spine — `observe → measure → recommend → user
approves → existing service executes` — and it is built to be the *safest* link in that
chain. A recommendation is a typed observation about the funnel, nothing more: it cannot
change a search profile, relax a policy or touch an application. It says "your Data &
Analytics applications reached an interview 6 times out of 20, your Software Engineering
ones 3 out of 40" and stops. Turning that into a change is a separate, explicit act the
user approves (`backend.app.domain.strategy_change`).

Four rules make the suggestion trustworthy, and the models enforce every one:

- **Evidence or nothing (§62).** A recommendation carries at least one `RecommendationEvidence`
  citing a specific computed metric with its own numbers, so "prioritize data roles" is
  never a hunch — it is a claim a reader can trace back to the `CareerAnalytics` report and
  the `analytics_version` that produced it.
- **No claim from weak data (§24, §62).** Every cited metric must clear
  `MIN_RECOMMENDATION_SAMPLE_SIZE`; a slice of two applications never earns a recommendation,
  and the `confidence` a recommendation reports is a pure function of its weakest evidence's
  sample size, so the uncertainty is visible rather than buried.
- **No causal language (§62).** The kinds are phrased as *suggestions grounded in an observed
  rate*, never as cause and effect: the platform observes that a family converts better, it
  does not claim applying there *causes* offers. `asserts_causation` is offered so the
  wording guard can hold generated prose to the same line.
- **Deterministic core, optional wording (§33).** The engine that produces these is
  deterministic and consumes a `CareerAnalytics` set, never raw rows; a provider may only
  polish the wording, behind the Phase 11 router with a deterministic fallback, and
  `generator_key`/`llm_run_id` record which wrote the prose.

Pure domain values: `backend.app.domain` imports the standard library and Pydantic only
(docs/ARCHITECTURE.md §1). Depends on `analytics` for the metric vocabulary it cites.
"""
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.domain.analytics import DimensionKind, RateKind, TimingKind
from backend.app.domain.base import DomainModel, NonEmptyStr, UtcDatetime
from backend.app.domain.identifiers import (
    CareerRecommendationEvidenceId,
    CareerRecommendationId,
    LLMRunId,
    UserId,
)

# The fewest observations a metric must rest on before it may back a recommendation (§62).
# Below this a rate is noise — one lucky interview in three applications is not a 33% rate —
# so the engine refuses to draw a conclusion from it, and the model refuses to hold evidence
# thinner than this. Five is the spec's floor; it lives here once so the engine, the model
# and the tests share the same threshold.
MIN_RECOMMENDATION_SAMPLE_SIZE = 5

# The sample sizes at which confidence rises. A recommendation always clears the minimum, so
# its confidence is never below LOW; these are where "enough to notice" becomes "enough to
# lean on". Thresholds, not guesses, kept beside the minimum so the whole confidence scale
# is one place to read.
MEDIUM_CONFIDENCE_MIN_SAMPLE = 12
HIGH_CONFIDENCE_MIN_SAMPLE = 30

# Phrases that assert cause and effect. A recommendation observes a rate and suggests a
# focus; it must never claim applying somewhere *causes* an outcome, because observational
# funnel data cannot support causation (§24, §62). The list is deliberately small and
# lower-cased — a wording guard's blunt instrument, not a grammar — matched as substrings so
# "guarantees an interview" and "because it converts" are both caught.
_CAUSAL_PHRASES: tuple[str, ...] = (
    "causes", "caused by", "because", "leads to", "results in", "guarantees",
    "will get you", "ensures", "makes you", "due to",
)


class RecommendationConfidence(StrEnum):
    """How much weight a recommendation's evidence can bear — a visible uncertainty (§24).

    Never a probability of success, only a statement about *sample strength*: `LOW` is
    "enough to notice", `MEDIUM` "enough to lean on", `HIGH` "well-established in your own
    history". Derived from the weakest evidence a recommendation cites, so a comparison
    resting on one thin slice is only as confident as that slice — the uncertainty stays
    where a reader can see it rather than being averaged away.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


def confidence_for_sample(sample_size: int) -> RecommendationConfidence:
    """The confidence a sample of `sample_size` observations earns (§24).

    Pure and monotonic: the thresholds are the only thing that decides, so two recommendations
    with the same weakest sample always report the same confidence. Below
    `MIN_RECOMMENDATION_SAMPLE_SIZE` still returns `LOW` rather than raising — the recommendation
    model is what forbids a too-thin recommendation from existing; this only classifies.
    """
    if sample_size >= HIGH_CONFIDENCE_MIN_SAMPLE:
        return RecommendationConfidence.HIGH
    if sample_size >= MEDIUM_CONFIDENCE_MIN_SAMPLE:
        return RecommendationConfidence.MEDIUM
    return RecommendationConfidence.LOW


def asserts_causation(text: str) -> bool:
    """Whether a piece of prose reads as a causal claim — the guard's blunt check (§62).

    A deterministic substring scan over `_CAUSAL_PHRASES`, offered for the wording guard to
    reject generated text that steps from "this family shows a higher rate" to "applying here
    gets you interviews". Not enforced on the model — legitimate prose occasionally needs a
    word on the list — but pure and reproducible so a guard and a test agree on the verdict.
    """
    haystack = text.casefold()
    return any(phrase in haystack for phrase in _CAUSAL_PHRASES)


class RecommendationKind(StrEnum):
    """The closed set of suggestions the engine can make — each grounded in a metric (§26-31).

    Every member is a *focus* suggestion tied to a funnel dimension, phrased as observation
    plus proposed emphasis, never as cause and effect. `PRIORITIZE_*`/`DEPRIORITIZE_*` follow
    a slice that converts notably better or worse than the rest; `REVIEW_*` flag a pattern
    worth the user's attention without prescribing a direction. A provider that invents a
    kind fails to parse rather than smuggling a new recommendation the engine never reasoned
    about, exactly as every other closed vocabulary in the domain.
    """

    PRIORITIZE_ROLE_FAMILY = "PRIORITIZE_ROLE_FAMILY"
    DEPRIORITIZE_ROLE_FAMILY = "DEPRIORITIZE_ROLE_FAMILY"
    PRIORITIZE_SOURCE = "PRIORITIZE_SOURCE"
    DEPRIORITIZE_SOURCE = "DEPRIORITIZE_SOURCE"
    REVIEW_OPPORTUNITY_TYPE_MIX = "REVIEW_OPPORTUNITY_TYPE_MIX"
    REVIEW_DOCUMENT_STRATEGY = "REVIEW_DOCUMENT_STRATEGY"
    REVIEW_APPLICATION_VOLUME = "REVIEW_APPLICATION_VOLUME"
    REVIEW_INTERVIEW_PREPARATION = "REVIEW_INTERVIEW_PREPARATION"


class RecommendationEvidence(DomainModel):
    """One computed metric a recommendation cites, with its own numbers (§28, §62).

    The auditable heart of "evidence or nothing": each item names exactly one metric — a
    `rate_kind` or a `timing_kind`, never both — the dimension slice it was measured in, and
    the raw counts behind it, so a reader can walk from the recommendation back to the
    `CareerAnalytics` report and check the arithmetic. `detail` is the factual, non-causal
    sentence a surface shows ("Data & Analytics: 6 interviews from 20 applications"); it
    describes the number, it does not explain why. `sample_size` is what the recommendation's
    minimum-sample rule is checked against, so a thin slice can never sneak in as evidence.

    A `None` `dimension` is an overall metric (the whole funnel); a set `dimension` with a
    `None` `dimension_key` is that dimension's unclassified slice — the same honest bucket
    `RoleFamily` keeps, never a catch-all.
    """

    id: CareerRecommendationEvidenceId
    recommendation_id: CareerRecommendationId
    ordinal: Annotated[int, Field(ge=0)]
    dimension: DimensionKind | None = None
    dimension_key: NonEmptyStr | None = None
    rate_kind: RateKind | None = None
    timing_kind: TimingKind | None = None
    numerator: Annotated[int, Field(ge=0)] | None = None
    denominator: Annotated[int, Field(ge=0)] | None = None
    median_days: Annotated[float, Field(ge=0.0)] | None = None
    sample_size: Annotated[int, Field(ge=0)]
    detail: NonEmptyStr

    @model_validator(mode="after")
    def _cites_exactly_one_metric_shape(self) -> Self:
        if (self.rate_kind is None) == (self.timing_kind is None):
            raise ValueError(
                "evidence must cite exactly one metric: a rate_kind or a timing_kind")
        if self.rate_kind is not None:
            if self.numerator is None or self.denominator is None:
                raise ValueError("rate evidence must carry a numerator and a denominator")
            if self.numerator > self.denominator:
                raise ValueError("rate evidence numerator cannot exceed its denominator")
            if self.median_days is not None:
                raise ValueError("rate evidence must not carry median_days")
        else:  # timing evidence
            if self.median_days is None:
                raise ValueError("timing evidence must carry median_days")
            if self.numerator is not None or self.denominator is not None:
                raise ValueError("timing evidence must not carry a numerator or denominator")
        if self.dimension is None and self.dimension_key is not None:
            raise ValueError(
                "an overall metric (no dimension) must not carry a dimension_key")
        return self


class CareerRecommendation(DomainModel):
    """One evidence-backed suggestion from a funnel report — and nothing it can execute (§26-33).

    User-owned like every entity, read `WHERE user_id = ?`. It states its `kind`, a
    `summary` a surface shows, the `analytics_version` of the report it was drawn from (so a
    recommendation is never silently re-read against metrics computed by a newer recipe), and
    the `evidence` that justifies it — at least one item, each clearing
    `MIN_RECOMMENDATION_SAMPLE_SIZE`, which is what makes `confidence` a fact about sample
    strength rather than a flourish. `generator_key` and `llm_run_id` record who wrote the
    prose: a deterministic template, or a provider polishing the wording behind the router
    with a deterministic fallback (§33). The model holds no target profile, no policy and no
    execution — turning a recommendation into a change is `StrategyChangeProposal`'s job,
    which the user approves explicitly, so this object's mutation authority is exactly zero.
    """

    id: CareerRecommendationId
    user_id: UserId
    kind: RecommendationKind
    analytics_version: NonEmptyStr
    summary: NonEmptyStr
    detail: NonEmptyStr | None = None
    evidence: Annotated[tuple[RecommendationEvidence, ...], Field(min_length=1)]
    generator_key: NonEmptyStr | None = None
    llm_run_id: LLMRunId | None = None
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _evidence_is_well_formed_and_strong_enough(self) -> Self:
        ordinals = [item.ordinal for item in self.evidence]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("recommendation evidence must not repeat an ordinal")
        if any(item.recommendation_id != self.id for item in self.evidence):
            raise ValueError("every evidence item must reference its recommendation's id")
        if any(item.sample_size < MIN_RECOMMENDATION_SAMPLE_SIZE for item in self.evidence):
            raise ValueError(
                "every cited metric must clear MIN_RECOMMENDATION_SAMPLE_SIZE: a "
                "recommendation is never drawn from weak observational data")
        return self

    @property
    def min_evidence_sample_size(self) -> int:
        """The weakest sample any cited metric rests on — what governs `confidence`."""
        return min(item.sample_size for item in self.evidence)

    @property
    def confidence(self) -> RecommendationConfidence:
        """Sample-strength confidence, derived from the weakest evidence (never stored)."""
        return confidence_for_sample(self.min_evidence_sample_size)

