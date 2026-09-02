"""Analytics (trends) read endpoint."""
from datetime import datetime

from fastapi import APIRouter, Depends

from server import queries
from server.deps import get_conn

router = APIRouter(prefix="/api")


def _parse_now(now: str | None) -> datetime:
    return datetime.fromisoformat(now) if now else datetime.now()


@router.get("/analytics")
def get_analytics(now: str | None = None, days: int = 30, conn=Depends(get_conn)):
    days = max(1, min(days, 365))      # bound the window; zero-fill cost is linear
    return queries.analytics(conn, now=_parse_now(now), days=days)
