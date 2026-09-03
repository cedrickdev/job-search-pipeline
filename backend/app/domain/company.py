"""`Company` and `CompanyLocation` — employers as first-class entities.

V1 only ever knew a company as a string on a job row, which is why it cannot
answer "which employers near me have no posting today but accept spontaneous
applications" — the question docs/V2_SPECIFICATION.md §6 makes a product feature.
Phase 1 introduces the entities; Phase 6 fills them from a discovery engine and
Phase 7 gives the locations real coordinates.
"""
from typing import Self

from pydantic import model_validator

from backend.app.domain.base import DomainModel, HttpUrlStr, NonEmptyStr
from backend.app.domain.common import Location
from backend.app.domain.identifiers import CompanyId, CompanyLocationId


class CompanyLocation(DomainModel):
    """One physical site of a company.

    Its own entity rather than a field on `Company` because the map plots sites,
    not legal entities: a retail chain with forty branches is one employer and
    forty markers (docs/V2_SPECIFICATION.md §8). `location.point` is where
    PostGIS geometry attaches in Phase 2.
    """

    id: CompanyLocationId
    company_id: CompanyId
    location: Location
    is_headquarters: bool = False


class Company(DomainModel):
    """A canonical employer identity.

    `accepts_spontaneous_applications` is three-valued on purpose: `None` means
    nobody has looked yet, which must not be confused with `False` ("we looked,
    there is no channel"). An application strategy that treats unknown as
    refused would silently skip half the market.
    """

    id: CompanyId
    name: NonEmptyStr
    website: HttpUrlStr | None = None
    careers_url: HttpUrlStr | None = None
    locations: tuple[CompanyLocation, ...] = ()
    accepts_spontaneous_applications: bool | None = None

    @model_validator(mode="after")
    def _locations_belong_here(self) -> Self:
        """A location carried by a company must point back at that company.

        Cheap to check, and it catches the copy-paste that would otherwise put a
        competitor's branch on this employer's card.
        """
        stray = [loc.id for loc in self.locations if loc.company_id != self.id]
        if stray:
            raise ValueError(f"locations belong to another company: {stray}")
        if sum(1 for loc in self.locations if loc.is_headquarters) > 1:
            raise ValueError("a company has at most one headquarters location")
        return self
