"""Jobs list (table) + board grouping."""
from fastapi import APIRouter, Depends, HTTPException

from dashboard import queries as dq
from server import queries
from server.deps import get_conn

router = APIRouter(prefix="/api")


@router.get("/jobs")
def get_jobs(view: str = "table", status: str | None = None,
             q: str | None = None, sort: str = "recent",
             source: str | None = None, conn=Depends(get_conn)):
    if view == "board":
        return {"board": dq.board(conn)}
    return {"items": queries.jobs_list(conn, status=status, q=q, sort=sort,
                                       source=source)}


@router.get("/jobs/{job_id}")
def get_job(job_id: int, conn=Depends(get_conn)):
    detail = queries.job_detail(conn, job_id)
    if detail is None:
        raise HTTPException(404, "job not found")
    return detail


# Spec §4.1/§5.4 list a dedicated fit endpoint. Reuses queries.job_detail so the
# serialized fit shape (and §8.3 null semantics) cannot drift from the drawer's
# `fit` key. Path is nested under /jobs (see deviations appendix) rather than the
# spec's /api/prep/{job_id}/fit, matching the rest of this plan's prep nesting.
@router.get("/jobs/{job_id}/fit")
def get_job_fit(job_id: int, conn=Depends(get_conn)):
    detail = queries.job_detail(conn, job_id)
    if detail is None:
        raise HTTPException(404, "job not found")
    return detail["fit"]            # {"available": False} or the full report
