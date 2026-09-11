"""SQLAlchemy store for the two location collections enrichment may change.

The store intentionally cannot reach candidate profiles: candidate-owned addresses
must never become shared geocoding-cache keys. It updates only opportunities and
company locations, preserving the rest of each row exactly as stored.
"""
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.domain.common import Location
from backend.app.infrastructure.database.mappers import apply_location_to_row
from backend.app.infrastructure.database.models import CompanyLocationRow, OpportunityRow
from backend.app.services.geo_enrichment import EnrichedTable, LocationRef


class SqlLocationStore:
    """`LocationStore` over opportunities and company locations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_unresolved(
        self, *, limit: int
    ) -> Sequence[tuple[LocationRef, Location]]:
        rows: list[tuple[LocationRef, Location]] = []
        opportunity_rows = await self._session.execute(
            select(OpportunityRow).where(
                OpportunityRow.location_point.is_(None),
                OpportunityRow.location_country.is_not(None),
            ).limit(limit)
        )
        for opportunity_row in opportunity_rows.scalars():
            rows.append((
                LocationRef(
                    table=EnrichedTable.OPPORTUNITIES,
                    row_id=str(opportunity_row.id),
                    company_id=(str(opportunity_row.company_id)
                                if opportunity_row.company_id is not None else None),
                ),
                self._location_from(opportunity_row),
            ))

        remaining = limit - len(rows)
        if remaining > 0:
            location_rows = await self._session.execute(
                select(CompanyLocationRow).where(
                    CompanyLocationRow.location_point.is_(None),
                    CompanyLocationRow.location_country.is_not(None),
                ).limit(remaining)
            )
            for company_location_row in location_rows.scalars():
                rows.append((
                    LocationRef(
                        table=EnrichedTable.COMPANY_LOCATIONS,
                        row_id=str(company_location_row.id),
                        company_id=str(company_location_row.company_id),
                    ),
                    self._location_from(company_location_row),
                ))
        return tuple(rows)

    async def replace(self, ref: LocationRef, location: Location) -> None:
        if ref.table is EnrichedTable.OPPORTUNITIES:
            opportunity_row = await self._session.get(
                OpportunityRow, UUID(ref.row_id)
            )
            if opportunity_row is not None:
                apply_location_to_row(opportunity_row, location)
                self._session.add(opportunity_row)
                await self._session.flush()
            return

        company_location_row = await self._session.get(
            CompanyLocationRow, UUID(ref.row_id)
        )
        if company_location_row is not None:
            apply_location_to_row(company_location_row, location)
            self._session.add(company_location_row)
            await self._session.flush()

    @staticmethod
    def _location_from(row: OpportunityRow | CompanyLocationRow) -> Location:
        return Location(
            country=row.location_country,
            region=row.location_region,
            city=row.location_city,
            postal_code=row.location_postal_code,
            raw=row.location_raw,
        )
