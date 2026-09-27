"""`/api/v2/career/strategy-proposals`: the spine's only mutation, gated behind a human.

The "user approves → existing service executes" link, over HTTP (§34-45). A proposal is a typed,
bounded description of an edit to exactly one search or one application policy; it carries the
before → after diff so a surface shows the whole change, and it changes nothing until the user
confirms it. Approval re-validates the confirmed change against the live target and hands it to
the **same** service that already owns that edit — this surface never writes a `SearchProfile` or
an `ApplicationPolicy` itself. A change that loosens a safety brake (widening what the platform
may apply to, raising a cap, lowering the score floor) is refused with
`SENSITIVE_CONFIRMATION_REQUIRED` unless the caller confirms it explicitly, which is the
acceptance rule "never silently expands the user's application policy" made into a status a
surface must handle (§41-43).

The owner is never in the path or the body — it is the account resolved from the session. A
foreign or missing proposal reads as `PROPOSAL_NOT_FOUND` (404) so an id cannot be probed. No
request body carries a `user_id`, a `target_version` or a `status`: the target and its version
precondition are captured from the live target at draft time, and the lifecycle is the service's.
"""
from fastapi import APIRouter, status

from backend.app.api.dependencies import CurrentSession, Now, StrategyProposals
from backend.app.api.schemas import (
    ApproveStrategyChangeRequest,
    ProposeStrategyChangeRequest,
    StrategyChangeExecutionResponse,
    StrategyChangeProposalDetailResponse,
    StrategyChangeProposalListResponse,
    StrategyChangeProposalResponse,
)
from backend.app.career.errors import CareerError, CareerErrorCode
from backend.app.domain.identifiers import StrategyChangeProposalId

router = APIRouter(tags=["v2-strategy-proposals"])


@router.post("/career/strategy-proposals", response_model=StrategyChangeProposalResponse,
             status_code=status.HTTP_201_CREATED)
async def propose_strategy_change(body: ProposeStrategyChangeRequest, current: CurrentSession,
                                  service: StrategyProposals,
                                  instant: Now) -> StrategyChangeProposalResponse:
    """Draft a proposal for a typed change to a search or a policy — writing nothing yet (§37-39).

    201, because it creates a proposal. The typed `change` is validated at the boundary, so a kind
    the platform does not offer is a 422. The live target is read once to capture its version as
    the approval precondition; a target that does not exist for this account is a 409
    (`strategy_target_not_found`). The proposal is born open and touches nothing on the target.
    """
    proposal = await service.propose(
        current.user.id, body.change, summary=body.summary,
        source_recommendation_id=body.source_recommendation_id, now=instant)
    return StrategyChangeProposalResponse.of(proposal)


@router.get("/career/strategy-proposals", response_model=StrategyChangeProposalListResponse)
async def list_pending_proposals(
        current: CurrentSession,
        service: StrategyProposals) -> StrategyChangeProposalListResponse:
    """This account's still-open proposals, most recently updated first — the review queue."""
    proposals = await service.pending(current.user.id)
    return StrategyChangeProposalListResponse.of(proposals)


@router.get("/career/strategy-proposals/history",
            response_model=StrategyChangeProposalListResponse)
async def list_proposal_history(
        current: CurrentSession,
        service: StrategyProposals) -> StrategyChangeProposalListResponse:
    """This account's proposals in every status — a surface to review what was done.

    Declared before `/{proposal_id}` so the literal `history` is never parsed as a proposal id.
    """
    proposals = await service.history(current.user.id)
    return StrategyChangeProposalListResponse.of(proposals)


@router.get("/career/strategy-proposals/{proposal_id}",
            response_model=StrategyChangeProposalDetailResponse)
async def read_proposal(proposal_id: StrategyChangeProposalId, current: CurrentSession,
                        service: StrategyProposals) -> StrategyChangeProposalDetailResponse:
    """One proposal with its execution record, if it has been acted on (§44-45).

    404 (`proposal_not_found`) for "no such proposal" and "not yours" alike, so an id cannot be
    probed. A `PROPOSED`, `DISMISSED` or `EXPIRED` proposal carries a null `execution`; an
    `EXECUTED`/`REJECTED`/`FAILED` one carries the audit of the attempt that closed it — pairing
    the two here avoids a second round-trip and an "execution not found" code.
    """
    proposal = await service.proposal(current.user.id, proposal_id)
    if proposal is None:
        raise CareerError(CareerErrorCode.PROPOSAL_NOT_FOUND, "no such proposal")
    execution = await service.execution(current.user.id, proposal_id)
    return StrategyChangeProposalDetailResponse.of(proposal, execution)


@router.post("/career/strategy-proposals/{proposal_id}/approve",
             response_model=StrategyChangeExecutionResponse)
async def approve_proposal(proposal_id: StrategyChangeProposalId,
                           body: ApproveStrategyChangeRequest, current: CurrentSession,
                           service: StrategyProposals,
                           instant: Now) -> StrategyChangeExecutionResponse:
    """Apply one confirmed proposal through its owning service, returning the audited outcome.

    Idempotent by the proposal-derived execution id: a double-confirm returns the recorded
    execution rather than applying the change twice. A proposal no longer open is a 409
    (`proposal_not_open`); one lapsed is a 409 (`proposal_expired`). A change that loosens a safety
    brake is a 409 (`sensitive_confirmation_required`) unless `confirm_sensitive` is set, and the
    proposal stays open so a second confirmed approval can proceed. The live target is reloaded: a
    vanished one is a 409 (`strategy_target_not_found`) and a drifted version a 409
    (`strategy_proposal_stale`), each recorded as a `REJECTED` audit, so a stale before/after never
    clobbers newer state. Only then is the change applied through the service that owns the edit.
    """
    execution = await service.approve(
        current.user.id, proposal_id, confirm_sensitive=body.confirm_sensitive, now=instant)
    return StrategyChangeExecutionResponse.of(execution)


@router.post("/career/strategy-proposals/{proposal_id}/dismiss",
             response_model=StrategyChangeProposalResponse)
async def dismiss_proposal(proposal_id: StrategyChangeProposalId, current: CurrentSession,
                           service: StrategyProposals,
                           instant: Now) -> StrategyChangeProposalResponse:
    """Decline an open proposal without running it — `PROPOSED` → `DISMISSED`.

    Writes no execution because nothing was attempted; it only advances the status so the proposal
    leaves the open set and cannot later be approved. A foreign or missing id is a 404
    (`proposal_not_found`); one no longer open is a 409 (`proposal_not_open`).
    """
    proposal = await service.dismiss(current.user.id, proposal_id, now=instant)
    return StrategyChangeProposalResponse.of(proposal)
