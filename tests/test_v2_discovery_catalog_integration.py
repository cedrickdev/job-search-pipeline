"""Synthetic CH search through sweep, durable catalog and owner-scoped map read."""

from uuid import uuid4
from datetime import date

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.discovery.orchestrator import DiscoveryOrchestrator
from backend.app.discovery.ats_board_catalog import ReviewedBoard
from backend.app.discovery import bootstrap as discovery_bootstrap
from backend.app.discovery.registry import SourceRegistry
from backend.app.domain.common import Location
from backend.app.domain.discovery_run import DiscoveryRunStatus
from backend.app.domain.geo import GeoSearchQuery, GeoStatus
from backend.app.domain.identifiers import OpportunityId, SearchProfileId, UserId
from backend.app.domain.opportunity import WorkplaceMode
from backend.app.infrastructure.database.models import (
    ApplicationRow,
    OpportunityRow,
    OpportunitySourceRecordRow,
    SearchProfileRow,
    TaskRunRow,
    MorningPreparationRow,
    UserRow,
)
from backend.app.repositories.discovery_catalog import CatalogOpportunityStore
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyCompanyRepository,
    SqlAlchemyOpportunityRepository,
    SqlAlchemySearchProfileRepository,
)
from backend.app.services.discovery_catalog import DiscoveryCatalogService
from backend.app.services.discovery_source_access import SourceAccessPolicy
from backend.app.services.geo_search import GeoSearchService
from country_packs.contracts import SourceBinding
from country_packs.registry import CountryPackRegistry
from tests.v2_builders import a_search_profile, a_source_record, an_opportunity
from tests.v2_discovery import NOW, FakeSource, a_metadata, a_pack, a_result
from tests.v2_rows import a_user_row


@pytest.mark.asyncio
async def test_reviewed_ats_partial_run_persists_only_swiss_offer_without_browser_work(
        db_engine, monkeypatch):
    owner = UserId(uuid4())
    other = UserId(uuid4())
    search_id = SearchProfileId(uuid4())
    posting_id = str(uuid4().int % 10**12)
    url = f"https://boards.greenhouse.io/example/jobs/{posting_id}"
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    def greenhouse_fetch(token, company):
        return [{"source": "greenhouse", "company": company,
                 "title": "Software engineer", "url": url,
                 "location": "Zürich, Switzerland", "description": "Engineering."},
                {"source": "greenhouse", "company": company,
                 "title": "Software engineer", "url": url,
                 "location": "Zürich, Switzerland", "description": "Engineering."},
                {"source": "greenhouse", "company": company,
                 "title": "Software engineer", "url": url + "-foreign",
                 "location": "Paris, France", "description": "Engineering."}]

    def lever_fetch(token, company):
        raise TimeoutError("fixture Lever timeout")

    original_sources = discovery_bootstrap.ATS_SOURCES
    monkeypatch.setattr(discovery_bootstrap, "ATS_SOURCES", tuple(
        (metadata, greenhouse_fetch if metadata.source_key == "greenhouse"
         else lever_fetch if metadata.source_key == "lever" else fetch)
        for metadata, fetch in original_sources))

    class Budget:
        async def reserve(self, source_key, board_token, swiss_date, limit):
            return True

    def board(source_key):
        return ReviewedBoard(
            source_key=source_key, token="fixture-board", company="Example SA",  # noqa: S106
            country="CH", provenance_url="https://example.ch/jobs",
            conditions_url="https://example.ch/terms", access_decision="approved",
            reviewed_at=date(2026, 10, 12), request_budget_per_day=5)

    discovery = discovery_bootstrap.build_discovery(
        boards={"greenhouse": (board("greenhouse"),),
                "lever": (board("lever"),)},
        budget=Budget(), clock=lambda: NOW)
    policy = SourceAccessPolicy(
        configured_source_keys=("greenhouse", "lever"),
        allowed_source_keys=("greenhouse", "lever"),
        reviewed_ats_source_keys=("greenhouse", "lever"))
    try:
        async with factory.begin() as session:
            session.add_all((a_user_row(id=owner), a_user_row(id=other)))
            await session.flush()
            await SqlAlchemySearchProfileRepository(session).upsert(
                a_search_profile(id=search_id, user_id=owner,
                                 source_keys=("greenhouse", "lever"),
                                 queries=("engineer",)))
        service = DiscoveryCatalogService(
            factory, discovery.orchestrator, clock=lambda: NOW,
            source_policy=policy, prepare_discovered=True)
        run_id = await service.run_profile(owner, search_id, trigger="MORNING")
        assert await service.run_profile(owner, search_id, trigger="MORNING",
                                         run_id=run_id) == run_id
        run = await service.get_run(owner, run_id)
        assert run is not None and run.status is DiscoveryRunStatus.PARTIAL
        assert {health.source_key for health in run.source_health} == {
            "greenhouse", "lever"}
        assert {health.source_key: health.status for health in run.source_health} == {
            "greenhouse": "HEALTHY", "lever": "UNAVAILABLE"}
        assert run.matched_count == 1
        assert await service.get_run(other, run_id) is None
        assert await service.list_members(other, search_id, limit=10, offset=0) == ()
        async with factory() as session:
            rows = (await session.scalars(select(OpportunityRow).join(
                OpportunitySourceRecordRow,
                OpportunitySourceRecordRow.opportunity_id == OpportunityRow.id).where(
                OpportunitySourceRecordRow.source_url == url))).all()
            sources = (await session.scalars(select(OpportunitySourceRecordRow).where(
                OpportunitySourceRecordRow.source_url == url))).all()
            browser_count = (await session.execute(
                select(func.count()).select_from(TaskRunRow).where(
                    TaskRunRow.kind == "APPLICATION_SUBMISSION",
                    TaskRunRow.user_id == owner))).scalar_one()
            matching_count = (await session.execute(
                select(func.count()).select_from(TaskRunRow).where(
                    TaskRunRow.kind == "MATCHING", TaskRunRow.user_id == owner))).scalar_one()
            checkpoints = (await session.scalars(select(MorningPreparationRow).where(
                MorningPreparationRow.user_id == owner,
                MorningPreparationRow.discovery_run_id == run_id))).all()
        assert len(rows) == 1
        assert rows[0].location_country == "CH"
        assert [(source.source_key, source.external_id) for source in sources] == [
            ("greenhouse", posting_id)]
        assert browser_count == 0
        assert matching_count == 1
        assert len(checkpoints) == 1 and checkpoints[0].stage == "DISCOVERED"
    finally:
        async with factory.begin() as session:
            await session.execute(delete(UserRow).where(UserRow.id.in_((owner, other))))
            await session.execute(delete(OpportunityRow).where(
                OpportunityRow.id.in_(select(
                    OpportunitySourceRecordRow.opportunity_id).where(
                    OpportunitySourceRecordRow.source_url == url))))


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
        # Third-party sources often do not classify workplace mode. A Swiss
        # catalog entry with this unknown value must remain visible when the
        # saved search simply excludes explicitly remote opportunities.
        workplace_mode=None,
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
                    a_search_profile(id=search_id, user_id=user_id, queries=()))

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

        # A later preference change must not make already-collected, unclassified
        # rows disappear from this search's own catalog.
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    update(SearchProfileRow)
                    .where(SearchProfileRow.id == search_id)
                    .values(workplace_modes=[WorkplaceMode.ON_SITE.value]))

        async with factory() as session:
            geo = await SqlAlchemyOpportunityRepository(session).search_geo(
                GeoSearchQuery(countries=("CH",)), catalog_scope=(user_id, search_id))
            saved_catalog = await GeoSearchService(
                SqlAlchemyOpportunityRepository(session),
                SqlAlchemyCompanyRepository(session),
                SqlAlchemySearchProfileRepository(session),
            ).opportunities_for_search_profile(
                user_id, search_id, catalog=True,
            )
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
        # A catalog already admitted this unclassified posting for an ON_SITE
        # search. The map must not erase it by applying that filter a second time.
        assert [item.opportunity.id for item in saved_catalog] == [opportunity_id]
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
