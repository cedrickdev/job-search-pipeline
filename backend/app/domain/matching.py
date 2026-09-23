"""Multidimensional match evaluation.

docs/V2_SPECIFICATION.md §9 rejects the single opaque score V1 stores (`scores`,
one 0-100 integer per job): a candidate must be told *why* a posting fits, and a
product decision needs to see which axis is weak. So an evaluation carries a
score per dimension, each with its own reasons, and `overall` is a reported
aggregate rather than the only thing that survives.

Nothing about eligibility appears in this module, and that is deliberate. A
permit that forbids the work is not a low score — averaging it into `overall`
is exactly how "great fit, cannot legally do it" becomes "good enough". That
verdict lives in `backend.app.domain.eligibility`, and
`backend.app.domain.decision.ApplicationDecision` is where the two meet.
"""
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.domain.base import DomainModel, NonEmptyStr, Score, UtcDatetime
from backend.app.domain.common import Reason
from backend.app.domain.identifiers import (
    CandidateProfileId,
    MatchEvaluationId,
    OpportunityId,
    UserId,
)


class MatchDimension(StrEnum):
    """The axes a candidate/opportunity pair is scored on.

    All six are *compatibility* axes: each answers "how well does this fit?" and
    each is legitimately a matter of degree. Anything that answers "is this
    allowed?" belongs to eligibility, not here.
    """

    SKILLS_FIT = "SKILLS_FIT"
    EXPERIENCE_FIT = "EXPERIENCE_FIT"
    EDUCATION_FIT = "EDUCATION_FIT"
    LANGUAGE_FIT = "LANGUAGE_FIT"
    LOCATION_FIT = "LOCATION_FIT"
    SCHEDULE_FIT = "SCHEDULE_FIT"


class DimensionScore(DomainModel):
    """One axis of a match, with the reasons behind it.

    `weight` is relative, not normalized: a service may weigh education at 0 for
    a student job without having to rescale everything else, and
    `MatchEvaluation.weighted_dimension_mean()` normalizes at aggregation time.
    A weight of exactly 0 means "computed, deliberately ignored", which is
    different from omitting the dimension ("not computed").
    """

    dimension: MatchDimension
    score: Score
    weight: Score = 1.0
    reasons: tuple[Reason, ...] = ()


class MatchEvaluation(DomainModel):
    """How one candidate fits one opportunity, per dimension.

    User-scoped: the same posting scores differently for two candidates, so this
    is the user-owned half of the pair (`Opportunity` itself is shared).

    At least one dimension is required. An evaluation holding only `overall`
    would be the opaque score docs/V2_SPECIFICATION.md §9 rules out, so the
    domain refuses to represent one.

    `evaluator_key` is provenance, kept as a plain string for the same reason
    `OpportunitySourceRecord.source_key` is: the domain must stay
    provider-neutral (docs/LLM_PROVIDER_ARCHITECTURE.md §3), and it still has to
    be possible to tell a deterministic score from a model-assisted one when
    auditing later.
    """

    id: MatchEvaluationId
    user_id: UserId
    candidate_profile_id: CandidateProfileId
    opportunity_id: OpportunityId
    overall: Score
    dimensions: Annotated[tuple[DimensionScore, ...], Field(min_length=1)]
    evidence_confidence: Score | None = None
    reasons: tuple[Reason, ...] = ()
    evaluator_key: NonEmptyStr | None = None
    evaluated_at: UtcDatetime

    @model_validator(mode="after")
    def _one_score_per_dimension(self) -> Self:
        seen = [entry.dimension for entry in self.dimensions]
        if len(seen) != len(set(seen)):
            raise ValueError("dimensions must not repeat a MatchDimension")
        return self

    def score_for(self, dimension: MatchDimension) -> DimensionScore | None:
        """The score on one axis, or `None` if it was never computed."""
        for entry in self.dimensions:
            if entry.dimension is dimension:
                return entry
        return None

    def weighted_dimension_mean(self) -> float | None:
        """The default aggregate recipe, offered rather than imposed.

        `overall` is stored, not derived: a matcher may apply its own penalties,
        and a domain object that recomputed the number would quietly disagree
        with the engine that produced it. `None` when every weight is 0 — there
        is no honest average of nothing.
        """
        total_weight = sum(entry.weight for entry in self.dimensions)
        if total_weight <= 0.0:
            return None
        return sum(entry.score * entry.weight
                   for entry in self.dimensions) / total_weight


class MatchClassification(StrEnum):
    """The band a match falls in, named once so no surface invents its own.

    docs/V2_SPECIFICATION.md §9 and the phase order both refuse scattered
    `if score > 0.8` literals: a threshold that lives in five components drifts
    into five different products. The bands live on `MatchProfile` and this enum
    is their vocabulary.

    `UNKNOWN` is not a low score — it is the honest answer when no dimension
    could be evaluated at all, and it must never be rendered as WEAK. "We could
    not assess this" and "this is a poor fit" are different facts.
    """

    EXCELLENT = "EXCELLENT"
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    UNKNOWN = "UNKNOWN"


class DimensionWeight(DomainModel):
    """The relative importance of one axis in a `MatchProfile`.

    Separate from `DimensionScore.weight` on purpose: this is configuration ("how
    much does language matter?"), that is a result ("how much did language count
    in *this* evaluation?"). The engine copies the profile weight onto the score
    it emits, so an audited evaluation records the weights it actually used.
    """

    dimension: MatchDimension
    weight: Score


class MatchProfile(DomainModel):
    """The versioned configuration a deterministic match runs under.

    One place for two things the phase order wants centralized and auditable: the
    per-dimension weights, and the score bands (`classify`). `version` is stamped
    onto every `MatchEvaluation.evaluator_key` the engine produces, so a score
    computed under different weights is never silently compared with a newer one.

    The bands are strictly ordered and validated, so a misconfiguration is a
    construction error rather than a surface that classifies nothing as EXCELLENT.
    """

    version: NonEmptyStr
    weights: Annotated[tuple[DimensionWeight, ...], Field(min_length=1)]
    excellent_min: Score = 0.85
    strong_min: Score = 0.70
    moderate_min: Score = 0.50

    @model_validator(mode="after")
    def _weights_and_bands_are_coherent(self) -> Self:
        seen = [entry.dimension for entry in self.weights]
        if len(seen) != len(set(seen)):
            raise ValueError("a MatchProfile must not weigh a dimension twice")
        if not (self.excellent_min > self.strong_min > self.moderate_min):
            raise ValueError(
                "match bands must be strictly ordered: "
                "excellent_min > strong_min > moderate_min")
        return self

    def weight_for(self, dimension: MatchDimension) -> float:
        """The configured weight of an axis, or 0.0 if the profile omits it.

        0.0 means "this profile does not count that axis", which the engine reads
        as a reason to skip it rather than to score it at zero.
        """
        for entry in self.weights:
            if entry.dimension is dimension:
                return entry.weight
        return 0.0

    def classify(self, overall: float | None) -> MatchClassification:
        """Which band a computed `overall` falls in.

        `None` — no dimension was evaluable — is `UNKNOWN`, never `WEAK`.
        """
        if overall is None:
            return MatchClassification.UNKNOWN
        if overall >= self.excellent_min:
            return MatchClassification.EXCELLENT
        if overall >= self.strong_min:
            return MatchClassification.STRONG
        if overall >= self.moderate_min:
            return MatchClassification.MODERATE
        return MatchClassification.WEAK


# The default weights a deterministic match runs under. They express *importance*,
# not data availability — a dimension nobody can evaluate yet (skills, education)
# still carries its weight, and the engine reports the shortfall as low evidence
# coverage rather than by silently reweighting. Bumping any number is a new
# `version`, so historical evaluations stay comparable only with their own.
DEFAULT_MATCH_PROFILE = MatchProfile(
    version="match-profile/1.0",
    weights=(
        DimensionWeight(dimension=MatchDimension.SKILLS_FIT, weight=0.30),
        DimensionWeight(dimension=MatchDimension.EXPERIENCE_FIT, weight=0.20),
        DimensionWeight(dimension=MatchDimension.LANGUAGE_FIT, weight=0.15),
        DimensionWeight(dimension=MatchDimension.LOCATION_FIT, weight=0.15),
        DimensionWeight(dimension=MatchDimension.EDUCATION_FIT, weight=0.10),
        DimensionWeight(dimension=MatchDimension.SCHEDULE_FIT, weight=0.10),
    ),
)


def to_percent(score: float) -> int:
    """The 0-1 canonical scale rendered on the 0-100 presentation scale.

    The one conversion, so a percentage never appears without passing through it.
    Half-up rounding to an integer: a UI shows "72%", not "71.83%", and two
    surfaces rounding differently would disagree about the same score.
    """
    if not 0.0 <= score <= 1.0:
        raise ValueError("a canonical score is on the closed unit interval [0, 1]")
    return int(score * 100 + 0.5)
