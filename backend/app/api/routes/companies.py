"""`/api/v2/companies` and `/api/v2/company-discovery/run`: the employer directory.

Three operations, and the split between the two routers here is the split §20 draws:
reading the directory is something every authenticated request may do, and running a
discovery pass is a bounded write. One prefix per shape, so a deployment that wanted
to rate-limit or disable the write half has a router to attach that to rather than a
path pattern to match.

**Companies are shared, so no route takes an owner.** There is no `user_id` on the
table (§21) and therefore nothing here filters by session — authentication is required
because the directory is not public, and authorization has nothing to scope. Two
accounts asking the same question get the same answer, which is the point: an employer
is a fact about the world and not a row belonging to whoever discovered it first.

**Pagination is explicit and bounded at the signature.** `limit` and `offset` are
declared with their range, so an out-of-band value is a 422 that names the parameter
rather than a clamp the client cannot see. `CompanyDirectoryService` clamps as well,
for every non-HTTP caller.

**The pass takes no seeds.** `POST /company-discovery/run` accepts a country, a
provider selection and two ceilings — nothing that could add an employer. Adding one
is configuration, because a request body that carried a seed would let any account
write into every other account's directory.
"""
from uuid import UUID

from fastapi import APIRouter, Query, status

from backend.app.api.dependencies import Companies, CompanyDiscovery, CurrentSession, Now
from backend.app.api.errors import company_not_found
from backend.app.api.schemas import (
    CompanyDetailResponse,
    CompanyDiscoveryRunRequest,
    CompanyDiscoveryRunResponse,
    CompanyListResponse,
)
from backend.app.domain.company import (
    AtsPlatform,
    SpontaneousApplicationSupport,
)
from backend.app.domain.identifiers import CompanyId
from backend.app.repositories.contracts import CompanyFilter
from backend.app.services.company_directory import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(prefix="/companies", tags=["v2-companies"])
discovery_router = APIRouter(prefix="/company-discovery", tags=["v2-companies"])


@router.get("", response_model=CompanyListResponse)
async def list_companies(
    current: CurrentSession,
    service: Companies,
    text: str | None = Query(default=None, min_length=1, max_length=200),
    country: str | None = Query(default=None, pattern=r"^[A-Z]{2}$"),
    ats_platform: AtsPlatform | None = None,
    spontaneous_support: SpontaneousApplicationSupport | None = None,
    has_opportunities: bool | None = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> CompanyListResponse:
    """One page of employers, filtered by the five values §20 names.

    An employer with no active opportunity is in this list like any other — that is
    acceptance criterion §28, and `has_opportunities` is a filter a client may apply
    rather than a condition the directory imposes.

    `spontaneous_support` is an enum and not a boolean because `UNKNOWN` is an answer
    here (§12): "show me the employers nobody has checked" is a real question, and a
    boolean parameter could not ask it.
    """
    page = await service.search(
        CompanyFilter(text=text, country=country, ats_platform=ats_platform,
                      spontaneous_support=spontaneous_support,
                      has_opportunities=has_opportunities),
        limit=limit, offset=offset)
    return CompanyListResponse.of(page, limit=limit, offset=offset)


@router.get("/{company_id}", response_model=CompanyDetailResponse)
async def read_company(company_id: UUID, current: CurrentSession,
                       service: Companies) -> CompanyDetailResponse:
    """One employer with its aliases, its careers endpoints and its provenance.

    404 for an id nothing is stored under, and that is the only reason it can happen:
    there is no owner to hide behind the same status (§21). The provenance in the
    response is the typed subset — `raw` provider metadata never leaves the backend
    (§29).
    """
    detail = await service.get(CompanyId(company_id))
    if detail is None:
        raise company_not_found()
    return CompanyDetailResponse.of(detail)


@discovery_router.post("/run", response_model=CompanyDiscoveryRunResponse,
                       status_code=status.HTTP_200_OK)
async def run_discovery(current: CurrentSession, service: CompanyDiscovery,
                        instant: Now,
                        body: CompanyDiscoveryRunRequest | None = None,
                        ) -> CompanyDiscoveryRunResponse:
    """Ask every eligible provider, persist what they said, link the postings.

    200 rather than 201: a pass is idempotent by design (§23), so a second identical
    call creates nothing and "201 Created" would be a lie about the common case. What
    happened is in the body — `created`, `matched`, `ambiguous` and the link counts.

    A provider that failed is reported in `health`, never raised (§26): one unreadable
    source must not cancel the other providers' findings, and hiding the failure would
    make an empty result indistinguishable from an empty configuration.

    The body is optional so a plain `POST` runs the default pass.
    """
    settings = body or CompanyDiscoveryRunRequest()
    outcome = await service.run(settings.to_request(), now=instant,
                                link_limit=settings.link_limit)
    return CompanyDiscoveryRunResponse.of(outcome)
