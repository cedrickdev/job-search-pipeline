"""`/api/v2/geo/*` and the saved-search geo read: the Phase 7 map explorer.

Three GETs, and they are the first V2 reads to answer a *place* rather than an id.
Two are open over the shared corpus — postings near a point, employers near a point —
and one runs a saved search the session owns. All three are behind authentication:
the geo surface exposes the whole shared corpus, and a public map is a Phase-8
decision nobody has made (docs/V2_SPECIFICATION.md §14).

**The query is a model, not a fistful of `Query(...)` parameters.** `GeoSearchParams`
validates the whole query string in one place and builds the domain `GeoSearchQuery`
once, so the contradictions the domain forbids — a `REMOTE_ONLY` search beside a
radius, a search with no scope — are a 422 where the request was parsed rather than an
empty page nobody can explain. A route reads the built query through `.query`; it never
re-derives it.

**The saved-search route does not check ownership itself.** It hands the id and the
session user to the service, whose repository scopes the load, and a search belonging to
another account comes back absent and turns into the same 404 as one that never existed
(§Security: ids must not be enumerable) — the identical arrangement `me.py` uses for the
profile routes, so the rule lives in one place.
"""
from typing import Annotated

from fastapi import APIRouter, Query

from backend.app.api.dependencies import CurrentSession, GeoSearch
from backend.app.api.schemas import (
    CompanyGeoResponse,
    GeoSavedSearchParams,
    GeoSearchParams,
    OpportunityGeoResponse,
)
from backend.app.domain.identifiers import SearchProfileId

router = APIRouter(tags=["v2-geo"])


@router.get("/geo/opportunities", response_model=OpportunityGeoResponse)
async def search_opportunities(
        params: Annotated[GeoSearchParams, Query()],
        current: CurrentSession, service: GeoSearch) -> OpportunityGeoResponse:
    """Postings admitted by one geographic/remote query, closest first.

    The window echoed in the body is the one the query validated, so a client pages
    against the same `limit`/`offset` it sent (§17). A posting with no point of its
    own is placed at its employer's nearest site and flagged `COMPANY_FALLBACK`, so a
    map never silently drops it or draws the weaker pin like a geocoded one (§12).
    """
    query = params.query
    results = await service.opportunities(query)
    return OpportunityGeoResponse.of(results, limit=query.limit, offset=query.offset)


@router.get("/geo/companies", response_model=CompanyGeoResponse)
async def search_companies(
        params: Annotated[GeoSearchParams, Query()],
        current: CurrentSession, service: GeoSearch) -> CompanyGeoResponse:
    """Employers with a site in scope, each once, at their nearest match.

    A `remote=only` search is valid but empty by construction — an employer is a
    place, and "remote only" is a question about postings — so the route answers 200
    with no companies rather than inventing one.
    """
    query = params.query
    results = await service.companies(query)
    return CompanyGeoResponse.of(results, limit=query.limit, offset=query.offset)


@router.get("/me/search-profiles/{search_profile_id}/opportunities",
            response_model=OpportunityGeoResponse)
async def search_saved_profile(
        search_profile_id: SearchProfileId, params: Annotated[GeoSavedSearchParams,
                                                              Query()],
        current: CurrentSession, service: GeoSearch) -> OpportunityGeoResponse:
    """Run one of this account's saved searches as a geo query.

    The scope is the profile's saved areas; the only query parameters are the optional
    map `bounds` Phase 8 sends when the user pans, and the page window. A profile that
    is not this account's is the same 404 as one that does not exist — the service
    loads it scoped by the session user and never says which of the two it was.
    """
    results = await service.opportunities_for_search_profile(
        current.user.id, search_profile_id, bounds=params.bounds_box,
        limit=params.limit, offset=params.offset)
    return OpportunityGeoResponse.of(results, limit=params.limit, offset=params.offset)
