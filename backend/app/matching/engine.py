"""The deterministic match engine.

docs/V2_SPECIFICATION.md §9 wants a match reported per dimension, with reasons,
and CLAUDE.md forbids the LLM and the embedding: this engine is arithmetic over
structured data, and nothing else. It reads a `CandidateProfile`, an
`Opportunity` and — only for the one conversion that needs it — a `CountryPack`,
and produces a `MatchEvaluation` on the canonical 0-1 scale.

Two rules from the phase order shape every choice here:

- *A dimension nobody can evaluate is UNKNOWN, never zero.* A missing skill
  inventory is not a skills score of 0; it is the absence of a skills score. So a
  dimension the data cannot support is **omitted** from the evaluation rather than
  scored low, and `overall` is the weighted mean over the dimensions that *were*
  evaluated. `evidence_confidence` then reports how much of the profile's total
  weight those dimensions covered — the separate "how much could we even assess?"
  axis, kept apart from "how good is what we assessed?".
- *Eligibility is not a dimension.* A permit cap, a work-authorization refusal and
  a hard language floor are gates, and they live in `backend.app.eligibility`.
  Nothing in this module can lower `overall` on their account.

Today three dimensions are evaluable — LANGUAGE_FIT, LOCATION_FIT and
SCHEDULE_FIT — because those are the fields the model actually populates. SKILLS,
EXPERIENCE and EDUCATION have no structured data behind them yet (Phase 10), so
the engine omits them and the shortfall shows up as low evidence coverage rather
than as invented scores.
"""
from datetime import datetime

from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.common import LanguageLevel, Reason, ReasonImpact, WorkloadRange
from backend.app.domain.identifiers import match_evaluation_id
from backend.app.domain.matching import (
    DEFAULT_MATCH_PROFILE,
    DimensionScore,
    MatchDimension,
    MatchEvaluation,
    MatchProfile,
)
from backend.app.domain.opportunity import Opportunity
from country_packs.contracts import CountryPack

# Stamped onto every evaluation's `evaluator_key`, joined with the profile version
# so a score is never silently compared across a change to either the algorithm or
# the weights. Bumping the arithmetic here is a bump to this key.
MATCH_ENGINE_KEY = "deterministic-match/1"

# The distance between the lowest and highest language levels, used to grade a
# shortfall smoothly. Derived from the enum so it tracks a level being added.
_LANGUAGE_LEVEL_SPAN = (max(level.rank for level in LanguageLevel)
                        - min(level.rank for level in LanguageLevel))

# The full-time week to measure a schedule gap against when no pack supplies one.
_FALLBACK_FULL_TIME_HOURS = 40.0


def evaluate_match(profile: CandidateProfile, opportunity: Opportunity, *,
                   pack: CountryPack | None, now: datetime,
                   match_profile: MatchProfile = DEFAULT_MATCH_PROFILE
                   ) -> MatchEvaluation | None:
    """Score one candidate against one opportunity, or `None` if nothing is scorable.

    `None` — not a zero-dimension evaluation — is the answer when no dimension can
    be evaluated: `MatchEvaluation` requires at least one dimension precisely so an
    empty match cannot masquerade as a computed one, and the caller renders the
    absence as the UNKNOWN classification.
    """
    dimensions = [
        dimension for dimension in (
            _language_fit(profile, opportunity,
                          match_profile.weight_for(MatchDimension.LANGUAGE_FIT)),
            _location_fit(profile, opportunity,
                          match_profile.weight_for(MatchDimension.LOCATION_FIT)),
            _schedule_fit(profile, opportunity, pack,
                          match_profile.weight_for(MatchDimension.SCHEDULE_FIT)),
        ) if dimension is not None
    ]
    if not dimensions:
        return None

    total_weight = sum(entry.weight for entry in dimensions)
    if total_weight > 0.0:
        overall = sum(entry.score * entry.weight
                      for entry in dimensions) / total_weight
    else:
        # Every evaluated dimension was configured to weight 0. There is no honest
        # weighted mean, so fall back to the plain one rather than divide by zero.
        overall = sum(entry.score for entry in dimensions) / len(dimensions)

    profile_weight = sum(weight.weight for weight in match_profile.weights)
    covered = sum(match_profile.weight_for(entry.dimension) for entry in dimensions)
    confidence = covered / profile_weight if profile_weight > 0.0 else None

    return MatchEvaluation(
        id=match_evaluation_id(profile.id, opportunity.id),
        user_id=profile.user_id,
        candidate_profile_id=profile.id,
        opportunity_id=opportunity.id,
        overall=overall,
        dimensions=tuple(dimensions),
        evidence_confidence=confidence,
        evaluator_key=f"{MATCH_ENGINE_KEY}+{match_profile.version}",
        evaluated_at=now,
    )


def _language_fit(profile: CandidateProfile, opportunity: Opportunity,
                  weight: float) -> DimensionScore | None:
    """How well the candidate's languages meet the posting's, as a degree.

    Scored only over requirements for languages the candidate has actually
    declared: a language they never listed is UNKNOWN, not a zero, so it is left
    out of the mean and noted with a neutral reason rather than dragging the score
    down. The eligibility engine treats that same silence as INCOMPLETE — the two
    axes look at the same fact and refuse to guess in the same direction. Required
    languages count double the nice-to-haves, matching `LanguageRequirement`'s own
    distinction.
    """
    requirements = opportunity.language_requirements
    if not requirements or not profile.languages:
        return None
    declared = {proficiency.language: proficiency.level
                for proficiency in profile.languages}
    weighted: list[tuple[float, float]] = []
    reasons: list[Reason] = []
    for requirement in requirements:
        importance = 1.0 if requirement.required else 0.5
        language = requirement.language.upper()
        level = declared.get(requirement.language)
        if level is None:
            reasons.append(Reason(
                code="LANGUAGE_NOT_DECLARED",
                detail=f"{language} {requirement.minimum_level} is not among the "
                       f"candidate's declared languages",
                impact=ReasonImpact.NEUTRAL))
            continue
        if level.meets(requirement.minimum_level):
            weighted.append((1.0, importance))
            reasons.append(Reason(
                code="LANGUAGE_MEETS_MINIMUM",
                detail=f"candidate {language} {level} meets the required "
                       f"{requirement.minimum_level}",
                impact=ReasonImpact.POSITIVE))
        else:
            deficit = requirement.minimum_level.rank - level.rank
            fit = max(0.0, 1.0 - deficit / _LANGUAGE_LEVEL_SPAN)
            weighted.append((fit, importance))
            reasons.append(Reason(
                code="LANGUAGE_BELOW_MINIMUM",
                detail=f"candidate {language} {level} is below the required "
                       f"{requirement.minimum_level}",
                impact=ReasonImpact.NEGATIVE))
    if not weighted:
        return None
    total = sum(importance for _, importance in weighted)
    score = sum(fit * importance for fit, importance in weighted) / total
    return DimensionScore(dimension=MatchDimension.LANGUAGE_FIT, score=score,
                          weight=weight, reasons=tuple(reasons))


def _location_fit(profile: CandidateProfile, opportunity: Opportunity,
                  weight: float) -> DimensionScore | None:
    """A categorical location fit — same city, region, country or not.

    Deliberately *not* a distance. Phase 7 §1 makes PostGIS the single authority on
    how far apart two points are, and CLAUDE.md forbids recomputing Haversine
    outside it; a pure engine with no database has no business inventing a second
    answer. So this reads the administrative fields only, and a remote posting
    short-circuits to a full fit because it does not constrain where anyone lives.
    Location is a preference here, never a gate — reachability, if it ever becomes
    a gate, is the geo layer's to decide.
    """
    if opportunity.is_remote:
        return DimensionScore(
            dimension=MatchDimension.LOCATION_FIT, score=1.0, weight=weight,
            reasons=(Reason(code="LOCATION_REMOTE",
                            detail="a remote posting does not constrain where the "
                                   "candidate is based",
                            impact=ReasonImpact.POSITIVE),))
    posting = opportunity.location
    base = profile.base_location
    if posting is None or base is None or not posting.country or not base.country:
        return None
    if posting.country == base.country:
        if posting.city and base.city and posting.city.casefold() == base.city.casefold():
            return _location_score(1.0, "LOCATION_SAME_CITY",
                                   f"both are in {base.city}",
                                   ReasonImpact.POSITIVE, weight)
        if posting.region and base.region \
                and posting.region.casefold() == base.region.casefold():
            return _location_score(0.8, "LOCATION_SAME_REGION",
                                   f"both are in {base.region}",
                                   ReasonImpact.POSITIVE, weight)
        return _location_score(0.6, "LOCATION_SAME_COUNTRY",
                               f"both are in {base.country}",
                               ReasonImpact.NEUTRAL, weight)
    return _location_score(0.2, "LOCATION_DIFFERENT_COUNTRY",
                           f"the posting is in {posting.country}, the candidate in "
                           f"{base.country}",
                           ReasonImpact.NEGATIVE, weight)


def _location_score(score: float, code: str, detail: str, impact: ReasonImpact,
                    weight: float) -> DimensionScore:
    return DimensionScore(dimension=MatchDimension.LOCATION_FIT, score=score,
                          weight=weight,
                          reasons=(Reason(code=code, detail=detail, impact=impact),))


def _schedule_fit(profile: CandidateProfile, opportunity: Opportunity,
                  pack: CountryPack | None, weight: float) -> DimensionScore | None:
    """Whether the posting's hours and the candidate's availability can agree.

    A preference, not the permit cap: this asks "does the candidate *want* these
    hours?", while the eligibility engine asks "is the candidate *allowed* them?".
    Collapsing the two is the mistake `Availability`'s own docstring warns against.
    Any overlap between the two hour bands scores a full fit — a mutually workable
    number exists — and disjoint bands are graded by the gap between them, measured
    against the local full-time week.
    """
    workload = opportunity.workload
    availability = profile.availability
    if workload is None or availability is None:
        return None
    if availability.min_weekly_hours is None and availability.max_weekly_hours is None:
        return None
    job_low, job_high = _workload_hours(workload, pack)
    if job_low is None and job_high is None:
        return None
    candidate_low = availability.min_weekly_hours or 0.0
    candidate_high = availability.max_weekly_hours or 168.0
    posting_low = job_low if job_low is not None else 0.0
    posting_high = job_high if job_high is not None else 168.0
    overlap = min(candidate_high, posting_high) - max(candidate_low, posting_low)
    if overlap >= 0.0:
        return DimensionScore(
            dimension=MatchDimension.SCHEDULE_FIT, score=1.0, weight=weight,
            reasons=(Reason(code="SCHEDULE_OVERLAP",
                            detail="the posting's hours and the candidate's "
                                   "availability overlap",
                            impact=ReasonImpact.POSITIVE),))
    gap = -overlap
    reference = _full_time_hours(pack)
    score = max(0.0, 1.0 - gap / reference)
    return DimensionScore(
        dimension=MatchDimension.SCHEDULE_FIT, score=score, weight=weight,
        reasons=(Reason(code="SCHEDULE_MISMATCH",
                        detail=f"the posting's hours and the candidate's "
                               f"availability are about {gap:.0f}h/week apart",
                        impact=ReasonImpact.NEGATIVE),))


def _workload_hours(workload: WorkloadRange,
                    pack: CountryPack | None) -> tuple[float | None, float | None]:
    """A workload as a (low, high) weekly-hours band.

    Hours are used as given. A percentage band is converted through the *only*
    sanctioned percent-to-hours conversion, `PackMetadata.weekly_hours_for_percent`
    — and only when a pack is present, because the conversion needs the local
    full-time week (`WorkloadRange`'s docstring says why the domain refuses to
    guess it). No pack and only a percentage means the band is unknown: `(None,
    None)`.
    """
    if workload.min_weekly_hours is not None or workload.max_weekly_hours is not None:
        return workload.min_weekly_hours, workload.max_weekly_hours
    if pack is None or (workload.min_percent is None and workload.max_percent is None):
        return None, None
    low = (pack.metadata.weekly_hours_for_percent(workload.min_percent)
           if workload.min_percent is not None else None)
    high = (pack.metadata.weekly_hours_for_percent(workload.max_percent)
            if workload.max_percent is not None else None)
    return low, high


def _full_time_hours(pack: CountryPack | None) -> float:
    """The local full-time week, or a neutral fallback when no pack is known."""
    if pack is None:
        return _FALLBACK_FULL_TIME_HOURS
    return pack.metadata.weekly_hours_for_percent(100)
