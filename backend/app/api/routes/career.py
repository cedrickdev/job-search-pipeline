"""`/api/v2/career`: the funnel report and the evidence-backed suggestions drawn from it.

The "measure" and "recommend" links of the spine, over HTTP. Analytics is a pure read — it
counts the funnel, the response/interview/offer/acceptance rates, the timings and the
role/source/type breakdowns, all self-describing and stamped with an `analytics_version` — and
it is forbidden to write execution state, which is what keeps a rejection from ever touching an
`Application.state` (§13-25). Recommendations reason only over that typed report, never raw rows,
and every one cites the metric it rests on with its own numbers; generating them *adds*
observations to a write-once store and carries zero authority to change a search or a policy
(§26-33). Turning a suggestion into a change is a separate, human-approved step — see the
strategy-proposal surface.

The owner is never in the path or the body — every figure is the account resolved from the
session. No route returns a `user_id`, and none returns a hiring probability: a rate is a count
over matured applications, a recommendation is a nudge grounded in evidence, never a forecast.
"""
from typing import Annotated

from fastapi import APIRouter, Query, status

from backend.app.api.dependencies import (
    CareerAnalytics,
    CareerRecommendations,
    CurrentSession,
    Now,
)
from backend.app.api.schemas import (
    CareerAnalyticsResponse,
    CareerRecommendationListResponse,
)

router = APIRouter(tags=["v2-career"])

# The maturity horizon a caller may request, bounded so a query cannot ask for a nonsensical or
# absurd window. One day is the smallest horizon that still distinguishes a censored fresh send
# from a resolved one; a year is well past any real hiring cycle. Absent, the service's own
# `DEFAULT_OBSERVATION_HORIZON_DAYS` applies.
HorizonDays = Annotated[int | None, Query(ge=1, le=365)]


@router.get("/career/analytics", response_model=CareerAnalyticsResponse)
async def career_analytics(current: CurrentSession, service: CareerAnalytics, instant: Now,
                           horizon_days: HorizonDays = None) -> CareerAnalyticsResponse:
    """This account's whole funnel report as of now, under one maturity horizon (§13-25).

    A point-in-time read: the funnel, the four named rates, the four timings and the
    role/source/type breakdowns, each self-describing and all over one window. Conversion rates
    read against *matured* applications, so a fresh send that has not heard back is censored, never
    counted as a rejection. `horizon_days` overrides the default maturity window when given.
    """
    report = (await service.report(current.user.id, now=instant)
              if horizon_days is None
              else await service.report(current.user.id, now=instant,
                                        horizon_days=horizon_days))
    return CareerAnalyticsResponse.of(report)


@router.get("/career/recommendations", response_model=CareerRecommendationListResponse)
async def list_recommendations(
        current: CurrentSession,
        service: CareerRecommendations) -> CareerRecommendationListResponse:
    """This account's recommendations, most recently created first — a surface to review.

    A read of the write-once store: past observations, each with the evidence and the
    `analytics_version` that justified it, so a suggestion is never silently re-read against
    metrics computed by a newer recipe.
    """
    recommendations = await service.latest(current.user.id)
    return CareerRecommendationListResponse.of(recommendations)


@router.post("/career/recommendations", response_model=CareerRecommendationListResponse,
             status_code=status.HTTP_201_CREATED)
async def generate_recommendations(current: CurrentSession, service: CareerRecommendations,
                                   instant: Now) -> CareerRecommendationListResponse:
    """Compute the report, derive evidence-backed suggestions from it, persist and return them.

    201, because it adds fresh observations to the write-once store rather than mutating a prior
    set — a re-run is a new snapshot, not an edit. Each suggestion cites the rates it compared,
    every cited slice clears the minimum sample size, and none carries authority to change a search
    or a policy: that is the strategy-proposal step the user approves explicitly (§26-33).
    """
    recommendations = await service.recommend(current.user.id, now=instant)
    return CareerRecommendationListResponse.of(recommendations)
