"""In-app run triggers + status. The RunManager lives on app.state. Discovery is
the deterministic sweep; full is the agentic pipeline. A busy manager maps to 409
so the UI can say 'a run is already in progress'. trigger() returns promptly, so
a successful POST is 202 Accepted (the work continues in the background)."""
from fastapi import APIRouter, HTTPException, Request, Response

from server.runs import RunBusyError

router = APIRouter(prefix="/api")


async def _trigger(request: Request, kind: str) -> Response:
    try:
        await request.app.state.run_manager.trigger(kind)
    except RunBusyError:
        raise HTTPException(status_code=409, detail="a run is already in progress")
    return Response(status_code=202)


@router.post("/runs/discover")
async def run_discover(request: Request):
    return await _trigger(request, "discovery")


@router.post("/runs/full")
async def run_full(request: Request):
    return await _trigger(request, "full")


@router.get("/runs/status")
def run_status(request: Request):
    return request.app.state.run_manager.status()
