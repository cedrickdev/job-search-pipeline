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
