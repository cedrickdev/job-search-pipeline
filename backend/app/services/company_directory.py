"""The company directory: the read side of Phase 6, paginated and shared.

Two questions, and the split between them is §20's:

- **"Which employers do we know?"** — a filtered, explicitly paginated page. Never an
  unbounded list: the table grows with every sweep, and an endpoint that returned all
  of it would be a slow query today and an outage later.
- **"What do we know about this one?"** — the company with its aliases, its careers
  endpoints and its provenance, assembled in one place so a route does not have to
  know which repository holds what.

**No `user_id` anywhere** (§21). A company is a fact about the world, so two
authenticated users asking the same question get the same answer and the directory
takes no owner to scope by. Authentication still applies — the endpoints require a
session — but authorization has nothing to filter here, and adding an owner column to
make the read "look" scoped would be a lie about who the data belongs to.

**What is returned is not what is exposed.** `CompanyDetail` carries the domain
records whole, `raw` provenance metadata included, because the service is inside the
backend boundary. Choosing the safe subset is the response model's job (§29), and
keeping that decision in one visible place — `ApiModel` with `extra="forbid"` — is
what stops a field added to a domain record from appearing in a payload nobody
reviewed.
"""
from typing import Final

from backend.app.domain.base import DomainModel
from backend.app.domain.company import (
    CareerSite,
    Company,
    CompanyAlias,
    CompanyDiscoveryRecord,
)
from backend.app.domain.identifiers import CompanyId
from backend.app.repositories.contracts import (
    CareerSiteRepository,
    CompanyDiscoveryRepository,
    CompanyFilter,
    CompanyPage,
    CompanyRepository,
)

# The largest page the directory will build, whatever a caller asks for. The route
# validates its own query parameter and returns 422 above this; the clamp here is the
# backstop for every other caller, because "explicit pagination" (§20) has to hold for
# a CLI and a test as well as for HTTP.
MAX_PAGE_SIZE: Final = 100

# What a caller gets when it names no page size.
DEFAULT_PAGE_SIZE: Final = 20


class CompanyDetail(DomainModel):
    """One employer and everything Phase 6 stores about it.

    Assembled from four reads rather than one join, and deliberately: the careers
    endpoints and the provenance records are not aggregate children of `Company` —
    they are written and read independently, precisely so one provider cannot delete
    another's findings — so loading them together here is a *view*, not a relationship
    the domain claims.
    """

    company: Company
    aliases: tuple[CompanyAlias, ...] = ()
    career_sites: tuple[CareerSite, ...] = ()
    discoveries: tuple[CompanyDiscoveryRecord, ...] = ()

    @property
    def provider_keys(self) -> tuple[str, ...]:
        """Which providers have reported this employer, in report order.

        The short answer to §5's "how did we discover this company?", for a caller
        that wants the summary rather than every sighting.
        """
        return tuple({record.provider_key: None for record in self.discoveries})


class CompanyDirectoryService:
    """Reads. Nothing in this class writes, and it holds no clock.

    Separate from `CompanyDiscoveryService` because the two have opposite properties:
    a discovery pass is a bounded write that an operator triggers, and this is a query
    every authenticated request may make. Sharing one class would mean every reader
    holding the orchestrator and every writer holding the pagination rules.
    """

    def __init__(self, companies: CompanyRepository, sites: CareerSiteRepository,
                 records: CompanyDiscoveryRepository) -> None:
        self._companies = companies
        self._sites = sites
        self._records = records

    async def search(self, filters: CompanyFilter | None = None, *,
                     limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> CompanyPage:
        """One page of employers matching `filters`, plus the total that matched.

        The total is what makes the page navigable, and it is counted under exactly
        the same predicates as the page — one shared `_filters` in the repository — so
        a caller cannot be told there are forty results and then find thirty.

        A company with no active opportunity is returned like any other: that is §28's
        acceptance criterion, and `has_opportunities` is a filter a caller may apply
        rather than a condition the directory imposes.
        """
        return await self._companies.search(
            filters or CompanyFilter(), limit=_page_size(limit),
            offset=max(0, offset))

    async def get(self, company_id: CompanyId) -> CompanyDetail | None:
        """One employer with its aliases, endpoints and provenance, or `None`.

        `None` rather than an exception: "no such company" is an ordinary answer for a
        UUID a caller typed, and the route turns it into a 404. The three companion
        reads only happen once the company exists, so a wrong id costs one query.
        """
        company = await self._companies.get(company_id)
        if company is None:
            return None
        return CompanyDetail(
            company=company,
            aliases=await self._companies.aliases(company_id),
            career_sites=await self._sites.list_for_company(company_id),
            discoveries=await self._records.list_for_company(company_id))


def _page_size(limit: int) -> int:
    """A page size inside the bounds, whatever was asked for.

    Clamped rather than refused because this is the backstop and not the validation:
    the HTTP layer rejects an out-of-range parameter with a 422 that names it, and a
    programmatic caller passing 10_000 should get a page rather than a traceback from
    a repository three layers down.
    """
    return min(max(1, limit), MAX_PAGE_SIZE)
