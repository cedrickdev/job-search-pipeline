"""The read side of the Phase 7 geo explorer: three searches, one service.

The two open reads take a `GeoSearchQuery` the API layer has already built and
validated and hand it to the matching repository — the query object is the whole
contract (docs/V2_SPECIFICATION.md §14), so there is nothing left to decide here.
Wrapping them in a service anyway keeps the routes depending on one geo boundary
rather than reaching into three repositories, which is the pattern every other V2
route already follows.

The saved-search read is the one with a decision. It loads the profile *scoped to
its owner*, refuses a search that is not this account's with the same 404 as one
that does not exist (§Security: ids must not be enumerable), and derives the
executable query from the saved areas through the single `geo_query_for_areas`
mapping — so the one-area API request and the multi-area saved profile stay the
same shape and §27's "areas combine as OR, without duplicates" is one
implementation, not two.
"""
from backend.app.domain.common import GeoBounds
from backend.app.domain.geo import (
    DEFAULT_GEO_LIMIT,
    GeoSearchQuery,
    geo_query_for_areas,
)
from backend.app.domain.identifiers import SearchProfileId, UserId
from backend.app.repositories.contracts import (
    CompanyGeoResult,
    CompanyRepository,
    OpportunityGeoResult,
    OpportunityRepository,
    SearchProfileRepository,
)
from backend.app.services.onboarding import SearchProfileNotFound


class GeoSearchService:
    """Geographic reads over postings, employers and saved searches.

    The three repositories are handed in rather than reached for, so the request
    layer wires them (over PostGIS in production, over the fakes in a flow test) and
    this class never learns which. It holds no clock and writes nothing: every
    method is a read.
    """

    def __init__(self, opportunities: OpportunityRepository,
                 companies: CompanyRepository,
                 searches: SearchProfileRepository) -> None:
        self._opportunities = opportunities
        self._companies = companies
        self._searches = searches

    async def opportunities(
            self, query: GeoSearchQuery) -> tuple[OpportunityGeoResult, ...]:
        """Postings admitted by one geographic/remote query, closest first."""
        return await self._opportunities.search_geo(query)

    async def companies(
            self, query: GeoSearchQuery) -> tuple[CompanyGeoResult, ...]:
        """Employers with a site in scope, each once, at their nearest match."""
        return await self._companies.search_geo(query)

    async def opportunities_for_search_profile(
            self, user_id: UserId, search_profile_id: SearchProfileId, *,
            bounds: GeoBounds | None = None, limit: int = DEFAULT_GEO_LIMIT,
            offset: int = 0) -> tuple[OpportunityGeoResult, ...]:
        """Run one of a user's saved searches as a geo query.

        The scope is the profile's areas; `bounds` is the optional viewport Phase 8
        sends when the map pans, narrowing the union without widening it. A profile
        that is not this user's comes back `None` from the owner-scoped `get` and
        becomes `SearchProfileNotFound` — the same answer as a missing id.
        """
        profile = await self._searches.get(user_id, search_profile_id)
        if profile is None:
            raise SearchProfileNotFound(str(search_profile_id))
        query = geo_query_for_areas(
            profile.areas, opportunity_types=profile.opportunity_types,
            workplace_modes=profile.workplace_modes, bounds=bounds,
            limit=limit, offset=offset)
        return await self._opportunities.search_geo(query)
