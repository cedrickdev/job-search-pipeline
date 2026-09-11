"""Phase 7 geographic repository searches, proved against PostGIS."""
from uuid import UUID

import pytest

from backend.app.domain.common import GeoBounds, GeoDistance, GeoPoint, Location
from backend.app.domain.geo import GeoSearchQuery, GeoStatus, RadiusFilter
from backend.app.domain.identifiers import (
    CompanyId,
    CompanyLocationId,
    OpportunityId,
)
from backend.app.domain.opportunity import OpportunityType, WorkplaceMode
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyCompanyRepository,
    SqlAlchemyOpportunityRepository,
)
from tests.v2_builders import (
    GENEVA,
    LAUSANNE,
    ZURICH,
    a_company,
    a_company_location,
    a_source_record,
    an_opportunity,
)


def a_geo_posting(index: int, point: GeoPoint | None, city: str, **overrides):
    fields = {
        "id": OpportunityId(UUID(f"00000000-0000-4000-8000-0000000003{index:02d}")),
        "source": a_source_record(external_id=f"geo-posting-{index}"),
        "dedup_fingerprint": f"geo-fingerprint-{index}",
        "title": f"Posting in {city}",
        "location": Location(country="CH", city=city) if point is None
                    else Location(country="CH", city=city, point=point),
    }
    fields.update(overrides)
    return an_opportunity(**fields)


@pytest.mark.asyncio
async def test_geo_search_uses_postgis_for_radius_membership_distance_and_order(
    db_session,
):
    repository = SqlAlchemyOpportunityRepository(db_session)
    for index, (point, city) in enumerate(
        ((ZURICH, "Zurich"), (GENEVA, "Geneve"), (LAUSANNE, "Lausanne")),
        start=1,
    ):
        await repository.upsert(a_geo_posting(index, point, city))

    found = await repository.search_geo(GeoSearchQuery(radii=(
        RadiusFilter(
            center=LAUSANNE,
            radius=GeoDistance.from_kilometers(100),
            label="Lake Geneva",
        ),
    )))

    assert [result.opportunity.title for result in found] == [
        "Posting in Lausanne",
        "Posting in Geneve",
    ]
    assert found[0].status is GeoStatus.RESOLVED
    assert found[0].distance_meters == pytest.approx(0.0, abs=1.0)
    assert 45_000 < found[1].distance_meters < 60_000
    assert [(match.radius_index, match.label) for match in found[0].matched_radii] == [
        (0, "Lake Geneva")
    ]


@pytest.mark.asyncio
async def test_country_search_returns_unresolved_rows_without_inventing_distance(
    db_session,
):
    repository = SqlAlchemyOpportunityRepository(db_session)
    await repository.upsert(a_geo_posting(1, LAUSANNE, "Lausanne"))
    await repository.upsert(a_geo_posting(2, None, "Suisse romande"))
    await repository.upsert(a_geo_posting(
        3,
        None,
        "Lyon",
        location=Location(country="FR", city="Lyon"),
    ))

    found = await repository.search_geo(GeoSearchQuery(countries=("CH",)))

    assert [result.opportunity.title for result in found] == [
        "Posting in Lausanne",
        "Posting in Suisse romande",
    ]
    assert found[0].status is GeoStatus.RESOLVED
    assert found[1].status is GeoStatus.UNRESOLVED
    assert [result.distance_meters for result in found] == [None, None]


@pytest.mark.asyncio
async def test_remote_policy_is_explicit_and_hybrid_stays_geographically_anchored(
    db_session,
):
    repository = SqlAlchemyOpportunityRepository(db_session)
    await repository.upsert(a_geo_posting(
        1, LAUSANNE, "Lausanne", workplace_mode=WorkplaceMode.ON_SITE))
    await repository.upsert(a_geo_posting(
        2, LAUSANNE, "Hybrid Lausanne", workplace_mode=WorkplaceMode.HYBRID))
    await repository.upsert(a_geo_posting(
        3, None, "Remote CH", workplace_mode=WorkplaceMode.REMOTE))

    exclude = await repository.search_geo(GeoSearchQuery(countries=("CH",)))
    include = await repository.search_geo(GeoSearchQuery(
        countries=("CH",),
        remote_policy="INCLUDE_REMOTE",
        remote_countries=("CH",),
    ))
    remote_only = await repository.search_geo(GeoSearchQuery(
        remote_policy="REMOTE_ONLY",
        remote_countries=("CH",),
    ))

    assert {result.opportunity.title for result in exclude} == {
        "Posting in Lausanne", "Posting in Hybrid Lausanne"}
    assert {result.opportunity.title for result in include} == {
        "Posting in Lausanne", "Posting in Hybrid Lausanne", "Posting in Remote CH"}
    assert [result.opportunity.title for result in remote_only] == [
        "Posting in Remote CH"
    ]
    assert remote_only[0].status is GeoStatus.REMOTE


@pytest.mark.asyncio
async def test_unresolved_opportunity_can_match_through_a_company_location(db_session):
    company_id = CompanyId(UUID("00000000-0000-4000-8000-000000000401"))
    site_id = CompanyLocationId(UUID("00000000-0000-4000-8000-000000000402"))
    company_repository = SqlAlchemyCompanyRepository(db_session)
    await company_repository.upsert(a_company(
        a_company_location(
            id=site_id,
            company_id=company_id,
            location=Location(country="CH", city="Lausanne", point=LAUSANNE),
        ),
        id=company_id,
        name="Fallback SA",
    ))
    repository = SqlAlchemyOpportunityRepository(db_session)
    await repository.upsert(a_geo_posting(
        1,
        None,
        "Unknown office",
        company_id=company_id,
        workplace_mode=WorkplaceMode.ON_SITE,
    ))

    found = await repository.search_geo(GeoSearchQuery(radii=(
        RadiusFilter(center=LAUSANNE, radius=GeoDistance.from_kilometers(10)),
    )))

    assert [result.opportunity.title for result in found] == [
        "Posting in Unknown office"
    ]
    assert found[0].status is GeoStatus.COMPANY_FALLBACK
    assert found[0].location == found[0].company_location.location
    assert found[0].distance_meters == pytest.approx(0.0, abs=1.0)


@pytest.mark.asyncio
async def test_geo_search_combines_areas_without_duplicates_and_applies_filters(
    db_session,
):
    repository = SqlAlchemyOpportunityRepository(db_session)
    await repository.upsert(a_geo_posting(
        1, LAUSANNE, "Lausanne", opportunity_type=OpportunityType.INTERNSHIP,
        workplace_mode=WorkplaceMode.HYBRID))
    await repository.upsert(a_geo_posting(
        2, GENEVA, "Geneve", opportunity_type=OpportunityType.FULL_TIME,
        workplace_mode=WorkplaceMode.ON_SITE))
    await repository.upsert(a_geo_posting(
        3, ZURICH, "Zurich", opportunity_type=OpportunityType.INTERNSHIP,
        workplace_mode=WorkplaceMode.HYBRID))

    found = await repository.search_geo(GeoSearchQuery(
        radii=(
            RadiusFilter(center=LAUSANNE,
                         radius=GeoDistance.from_kilometers(10), label="Home"),
            RadiusFilter(center=GENEVA,
                         radius=GeoDistance.from_kilometers(100), label="West"),
        ),
        bounds=GeoBounds(north=47.0, south=46.0, west=5.5, east=7.0),
        opportunity_types=(OpportunityType.INTERNSHIP,),
        workplace_modes=(WorkplaceMode.HYBRID,),
        limit=1,
        offset=0,
    ))

    assert [result.opportunity.title for result in found] == [
        "Posting in Lausanne"
    ]
    assert [(match.radius_index, match.label) for match in found[0].matched_radii] == [
        (0, "Home"), (1, "West")
    ]


@pytest.mark.asyncio
async def test_company_geo_search_uses_nearest_matching_site_once(db_session):
    company_id = CompanyId(UUID("00000000-0000-4000-8000-000000000411"))
    repository = SqlAlchemyCompanyRepository(db_session)
    await repository.upsert(a_company(
        a_company_location(
            id=CompanyLocationId(UUID("00000000-0000-4000-8000-000000000412")),
            company_id=company_id,
            location=Location(country="CH", city="Lausanne", point=LAUSANNE),
        ),
        a_company_location(
            id=CompanyLocationId(UUID("00000000-0000-4000-8000-000000000413")),
            company_id=company_id,
            location=Location(country="CH", city="Geneve", point=GENEVA),
            is_headquarters=False,
        ),
        id=company_id,
        name="Two Sites SA",
    ))

    found = await repository.search_geo(GeoSearchQuery(radii=(
        RadiusFilter(center=LAUSANNE, radius=GeoDistance.from_kilometers(100)),
    )))

    assert [result.company.name for result in found] == ["Two Sites SA"]
    assert found[0].location.location.city == "Lausanne"
    assert found[0].distance_meters == pytest.approx(0.0, abs=1.0)
