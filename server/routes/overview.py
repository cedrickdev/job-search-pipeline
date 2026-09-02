"""Overview (command-center landing) read endpoint."""
from datetime import datetime

from fastapi import APIRouter, Depends

from server import queries
from server.deps import get_conn

router = APIRouter(prefix="/api")


def _parse_now(now: str | None) -> datetime:
    return datetime.fromisoformat(now) if now else datetime.now()


@router.get("/overview")
def get_overview(now: str | None = None, conn=Depends(get_conn)):
    return queries.overview(conn, now=_parse_now(now))
