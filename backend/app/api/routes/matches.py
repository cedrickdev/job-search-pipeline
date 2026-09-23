"""`/api/v2`: the match score and the eligibility verdict for a pair.

Three endpoints, and the owner is never in the path. The candidate is the
account's own profile, resolved from the session, so a request cannot ask for
someone else's profile to be scored — the same authorization model `me.py` uses,
for the same reason (docs/ENGINEERING_STANDARDS.md §Security).

`POST /matches/evaluate` runs both engines and stores both verdicts; it is a
`POST` because it writes, and idempotent because the two verdicts are keyed on the
pair. `GET /opportunities/{id}/match` is the pure read of one pair, and
`GET /matches` lists what this account has assessed. The two reads answer 404 for
"not evaluated", "no profile yet" and "not this account's" alike — the service
returns `None` for all three so none can be told apart from another by id.

The response carries both axes side by side and never collapses them: an
INELIGIBLE verdict does not lower the match score, and a 92%-match/blocked pair is
exactly as representable here as a 61%-match/eligible one. That separation is the
whole point of the phase, so the wire format keeps `match` and `eligibility` as
two independent objects.
"""
from fastapi import APIRouter, status

from backend.app.api.dependencies import Assessment, CurrentSession, Now
from backend.app.api.errors import ApiError
from backend.app.api.schemas import (
    AssessmentListResponse,
    AssessmentResponse,
    EvaluateMatchRequest,
)
from backend.app.domain.identifiers import OpportunityId

router = APIRouter(tags=["v2-matches"])


@router.post("/matches/evaluate", response_model=AssessmentResponse)
async def evaluate_match(body: EvaluateMatchRequest, current: CurrentSession,
                         service: Assessment, instant: Now) -> AssessmentResponse:
    """Assess one posting for this account's profile, on both axes.

    Runs the deterministic match engine and the eligibility engine independently
    and stores each verdict in its own record. Not a 201: the pair's verdicts are
    keyed on `(profile, opportunity)`, so a re-evaluation replaces the previous
    answer rather than creating a resource, and `200` with the fresh assessment is
    the honest status.

    404 when the account has no profile yet (go to onboarding) or when no posting
    is stored under the id — two distinct error codes, because the frontend acts on
    them differently.
    """
    assessment = await service.evaluate(current.user.id, body.opportunity_id,
                                         now=instant)
    return AssessmentResponse.of(assessment)


@router.get("/opportunities/{opportunity_id}/match",
            response_model=AssessmentResponse)
async def read_match(opportunity_id: OpportunityId, current: CurrentSession,
                     service: Assessment) -> AssessmentResponse:
    """The stored assessment for one pair, or 404 if it has not been evaluated.

    A pure read — it never runs an engine. The 404 covers "not evaluated yet", "no
    profile yet" and "not this account's posting-run" without distinguishing them:
    the service answers `None` for all three, so a caller cannot learn that another
    user has assessed a posting by asking about it (§Security: ids must not be
    enumerable).
    """
    assessment = await service.assessment_for(current.user.id, opportunity_id)
    if assessment is None:
        raise _not_assessed()
    return AssessmentResponse.of(assessment)


@router.get("/matches", response_model=AssessmentListResponse)
async def list_matches(current: CurrentSession,
                       service: Assessment) -> AssessmentListResponse:
    """This account's assessed pairs, most recently determined first.

    Chronological, not ranked: both axes travel untouched and an INELIGIBLE pair is
    never pushed down by pretending its match is low. A UI that wants to rank by fit
    or filter by eligibility has both numbers and does so itself
    (docs/MATCHING_ELIGIBILITY.md §Ranking).
    """
    assessments = await service.list_assessments(current.user.id)
    return AssessmentListResponse.of(assessments)


def _not_assessed() -> ApiError:
    """404 for a pair this account has not evaluated.

    One code for the three indistinguishable reasons a read can come back empty, so
    a client shows "not scored yet — evaluate it" rather than trying to tell them
    apart.
    """
    return ApiError(status.HTTP_404_NOT_FOUND, "match_not_evaluated",
                    "this opportunity has not been assessed for your profile yet")
