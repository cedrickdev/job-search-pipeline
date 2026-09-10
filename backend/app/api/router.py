"""The V2 router: one function that assembles the route modules.

A factory rather than a module-level `router` object, for one reason that matters in
the test suite: a router is stateful once mounted, and building a fresh one per
application keeps two `create_app()` calls in one process from sharing anything.

`reject_cross_site_writes` is attached here, to the router, rather than to each
route. That is deliberate — it is the guard that must not be forgettable, and a
router-level dependency applies to `login` and `register` as well, which are the two
routes with no session and therefore no CSRF token yet.
"""
from fastapi import APIRouter, Depends

from backend.app.api import API_V2_PREFIX
from backend.app.api.dependencies import reject_cross_site_writes
from backend.app.api.routes import auth, companies, me, onboarding


def create_v2_router() -> APIRouter:
    """The whole `/api/v2` surface, ready to `include_router`."""
    router = APIRouter(prefix=API_V2_PREFIX,
                       dependencies=[Depends(reject_cross_site_writes)])
    router.include_router(auth.router)
    router.include_router(me.router)
    router.include_router(onboarding.router)
    router.include_router(companies.router)
    router.include_router(companies.discovery_router)
    return router
