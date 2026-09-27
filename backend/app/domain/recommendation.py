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
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from json import dumps
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


def _evidence_descriptor(evidence: RecommendationEvidence) -> dict[str, object]:
    """The logical metric one evidence item cites, stripped of id, ordinal and wording (§28).

    Exactly the numbers that make two citations the *same* observation — the dimension slice, the
    metric kind and its raw counts — and nothing that is prose or provenance. This is what the
    fingerprint hashes, so a rephrased `detail` or a re-minted id never forks a recommendation's
    identity while a changed numerator does.
    """
    return {
        "dimension": evidence.dimension.value if evidence.dimension is not None else None,
        "dimension_key": evidence.dimension_key,
        "rate_kind": evidence.rate_kind.value if evidence.rate_kind is not None else None,
        "timing_kind": evidence.timing_kind.value if evidence.timing_kind is not None else None,
        "numerator": evidence.numerator,
        "denominator": evidence.denominator,
        "median_days": evidence.median_days,
        "sample_size": evidence.sample_size,
    }


def compute_recommendation_fingerprint(
        *, user_id: UserId, kind: RecommendationKind, analytics_version: str,
        window_start: datetime | None, window_end: datetime | None,
        observation_horizon_days: int,
        evidence: Iterable[RecommendationEvidence]) -> str:
    """A deterministic identity for a recommendation's logical content — the dedup key (§26-33).

    Two recommendations drawn from the same account, kind, analytics recipe, observation window
    and horizon, resting on the same cited metrics, share this fingerprint and so are one logical
    recommendation: regenerating over unchanged analytics never floods the store with duplicates.
    It hashes only the *evidence and the window it was drawn against*, never the wording —
    `summary`/`detail`, `generator_key`, `llm_run_id`, `analytics_computed_at`, the random id and
    `created_at` are all excluded — so a narrator polishing the prose or a fresh run's clock never
    forks the identity, while a changed metric (a new numerator, a different slice) or a new
    window does, leaving genuinely new evidence free to be a new recommendation.

    The evidence descriptors are serialized to JSON strings and *sorted* before hashing, so the
    fingerprint is independent of the order the engine emitted its citations in and never orders
    heterogeneous values against each other.
    """
    descriptors = sorted(
        dumps(_evidence_descriptor(item), sort_keys=True, separators=(",", ":"))
        for item in evidence)
    payload = {
        "user_id": str(user_id),
        "kind": kind.value,
        "analytics_version": analytics_version,
        "window_start": window_start.isoformat() if window_start is not None else None,
        "window_end": window_end.isoformat() if window_end is not None else None,
        "observation_horizon_days": observation_horizon_days,
        "evidence": descriptors,
    }
    canonical = dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


class CareerRecommendation(DomainModel):
    """One evidence-backed suggestion from a funnel report — and nothing it can execute (§26-33).

    User-owned like every entity, read `WHERE user_id = ?`. It states its `kind`, a
    `summary` a surface shows, the `analytics_version` of the report it was drawn from (so a
    recommendation is never silently re-read against metrics computed by a newer recipe), and
    the `evidence` that justifies it — at least one item, each clearing
    `MIN_RECOMMENDATION_SAMPLE_SIZE`, which is what makes `confidence` a fact about sample
    strength rather than a flourish. It also pins the exact analytics snapshot it rests on —
    `analytics_computed_at`, the `observation_horizon_days` in force, and the observation
    `window_start`/`window_end` (both-or-neither) — so a stored recommendation can always be
    read back against the precise window and horizon that produced it, not merely the recipe
    version. `generator_key` and `llm_run_id` record who wrote the prose: a deterministic
    template, or a provider polishing the wording behind the router with a deterministic
    fallback (§33). The model holds no target profile, no policy and no execution — turning a
    recommendation into a change is `StrategyChangeProposal`'s job, which the user approves
    explicitly, so this object's mutation authority is exactly zero.

    Its `fingerprint` is the identity of that logical content: the store keys on it so
    regenerating over unchanged analytics is idempotent rather than a flood of duplicate rows.
    """

    id: CareerRecommendationId
    user_id: UserId
    kind: RecommendationKind
    analytics_version: NonEmptyStr
    analytics_computed_at: UtcDatetime
    observation_horizon_days: Annotated[int, Field(ge=1)]
    window_start: UtcDatetime | None = None
    window_end: UtcDatetime | None = None
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

    @model_validator(mode="after")
    def _window_is_whole(self) -> Self:
        """The analytics window is both-or-neither and never runs backwards (§14)."""
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError(
                "a recommendation's analytics window is both-or-neither: window_start and "
                "window_end must be set together or both left unset")
        if (self.window_start is not None and self.window_end is not None
                and self.window_end < self.window_start):
            raise ValueError("window_end must not precede window_start")
        return self

    @property
    def fingerprint(self) -> str:
        """The dedup identity of this recommendation's logical content (never stored on the model).

        Delegates to `compute_recommendation_fingerprint` over this recommendation's account,
        kind, analytics version, window, horizon and evidence — the same value the store keys on
        to make regeneration idempotent. Computed, never a field, so it can never drift from the
        content it summarizes.
        """
        return compute_recommendation_fingerprint(
            user_id=self.user_id, kind=self.kind,
            analytics_version=self.analytics_version,
            window_start=self.window_start, window_end=self.window_end,
            observation_horizon_days=self.observation_horizon_days,
            evidence=self.evidence)

    @property
    def min_evidence_sample_size(self) -> int:
        """The weakest sample any cited metric rests on — what governs `confidence`."""
        return min(item.sample_size for item in self.evidence)

    @property
    def confidence(self) -> RecommendationConfidence:
        """Sample-strength confidence, derived from the weakest evidence (never stored)."""
        return confidence_for_sample(self.min_evidence_sample_size)

