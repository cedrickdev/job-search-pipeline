"""Local reversible opportunity-to-application workflow."""

from fastapi import APIRouter

from backend.app.api.dependencies import CurrentSession, LocalAutopilot, Now
from backend.app.api.schemas import LocalAutopilotResponse
from backend.app.domain.identifiers import OpportunityId

router = APIRouter(tags=["v2-local-autopilot"])


@router.post("/me/opportunities/{opportunity_id}/local-autopilot",
             response_model=LocalAutopilotResponse)
async def run_local_autopilot(
        opportunity_id: OpportunityId,
        current: CurrentSession,
        service: LocalAutopilot,
        instant: Now,
) -> LocalAutopilotResponse:
    """Assess, document and prepare one opportunity without submitting it."""
    report = await service.run(current.user.id, opportunity_id, now=instant)
    return LocalAutopilotResponse.of(report)
