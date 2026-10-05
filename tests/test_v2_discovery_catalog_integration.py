"""Synthetic CH search through sweep, durable catalog and owner-scoped map read."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.discovery.orchestrator import DiscoveryOrchestrator
from backend.app.discovery.registry import SourceRegistry
from backend.app.domain.common import Location
from backend.app.domain.discovery_run import DiscoveryRunStatus
from backend.app.domain.geo import GeoSearchQuery, GeoStatus
from backend.app.domain.identifiers import OpportunityId, SearchProfileId, UserId
from backend.app.infrastructure.database.models import (
    ApplicationRow,
    OpportunityRow,
    TaskRunRow,
    UserRow,
)
from backend.app.repositories.discovery_catalog import CatalogOpportunityStore
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyOpportunityRepository,
    SqlAlchemySearchProfileRepository,
)
from backend.app.services.discovery_catalog import DiscoveryCatalogService
from country_packs.contracts import SourceBinding
from country_packs.registry import CountryPackRegistry
from tests.v2_builders import a_search_profile, a_source_record, an_opportunity
from tests.v2_discovery import NOW, FakeSource, a_metadata, a_pack, a_result
from tests.v2_rows import a_user_row


@pytest.mark.asyncio
async def test_synthetic_swiss_sweep_reaches_catalog_map_without_an_application(db_engine):
    user_id = UserId(uuid4())
    other_id = UserId(uuid4())
    search_id = SearchProfileId(uuid4())
    opportunity_id = OpportunityId(uuid4())
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    posting = an_opportunity(
        id=opportunity_id, title="Python internship", company_name="Example AG",
        location=Location(country="CH", city="Suisse romande"),
        source=a_source_record(source_key="synthetic_board", external_id=str(uuid4()),
                               source_url="https://example.ch/jobs/internship"),
    )
    good = FakeSource(a_metadata("synthetic_board"),
                      result=a_result("synthetic_board", posting))
    bad = FakeSource(a_metadata("unavailable_board"), error=TimeoutError("fixture timeout"))
    registry = SourceRegistry()
    registry.register_all((good, bad))
    pack = a_pack("CH", SourceBinding(source_key="synthetic_board"),
                  SourceBinding(source_key="unavailable_board"))
    orchestrator = DiscoveryOrchestrator(
        registry=registry, packs=CountryPackRegistry((pack,)), clock=lambda: NOW)
    try:
        async with factory() as session:
            async with session.begin():
                session.add_all((a_user_row(id=user_id), a_user_row(id=other_id)))
                await session.flush()
                await SqlAlchemySearchProfileRepository(session).upsert(
                    a_search_profile(id=search_id, user_id=user_id))

        service = DiscoveryCatalogService(factory, orchestrator, clock=lambda: NOW)
        run_id = await service.run_profile(user_id, search_id, trigger="MANUAL")
        run = await service.get_run(user_id, run_id)
        assert run is not None and run.status is DiscoveryRunStatus.PARTIAL
        assert run.matched_count == 1
        assert run.warning_codes
        assert any(item.source_key == "unavailable_board" and item.status == "UNAVAILABLE"
                   for item in run.source_health)
        assert await service.get_run(other_id, run_id) is None
        assert await service.list_members(other_id, search_id, limit=10, offset=0) == ()
        assert len(good.requests) == len(bad.requests) == 1

        async with factory() as session:
            geo = await SqlAlchemyOpportunityRepository(session).search_geo(
                GeoSearchQuery(countries=("CH",)), catalog_scope=(user_id, search_id))
            foreign = await SqlAlchemyOpportunityRepository(session).search_geo(
                GeoSearchQuery(countries=("CH",)), catalog_scope=(other_id, search_id))
            applications = (await session.execute(
                select(func.count()).select_from(ApplicationRow)
                .where(ApplicationRow.user_id == user_id))).scalar_one()
            browser_tasks = (await session.execute(
                select(func.count()).select_from(TaskRunRow)
                .where(TaskRunRow.user_id == user_id,
                       TaskRunRow.kind == "APPLICATION_SUBMISSION"))).scalar_one()
            provenance = await CatalogOpportunityStore(session).provenance(opportunity_id)
        assert [item.opportunity.id for item in geo] == [opportunity_id]
        assert geo[0].status is GeoStatus.UNRESOLVED
        assert foreign == ()
        assert applications == 0
        assert browser_tasks == 0
        assert [(record.source_key, record.source_url) for record in provenance] == [
            ("synthetic_board", "https://example.ch/jobs/internship")]
    finally:
        async with factory() as session:
            async with session.begin():
                await session.execute(delete(UserRow).where(UserRow.id.in_((user_id, other_id))))
                await session.execute(delete(OpportunityRow).where(OpportunityRow.id == opportunity_id))
