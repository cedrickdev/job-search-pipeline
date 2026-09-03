# tests/test_v2_persistence_geo.py
"""The geographic primitives Phase 7 will build the map on, proved on PostGIS.

Phase 2 owes storage and two predicates, not the explorer: a point that survives
a round trip exactly, a radius query in metres, and a distance the database
computed. Everything here is asserted against a real `geography(Point,4326)`
column, because none of it is true of the Python objects alone — a coordinate pair
swapped on the way in and swapped again on the way out would round-trip perfectly
and put every marker in the wrong place.

The four text-form tests need no database: `geo_point_to_ewkt` is the wire format,
and its longitude-first ordering is the single most consequential detail in this
file.
"""
from uuid import UUID

import pytest
from sqlalchemy import text

from backend.app.domain.common import GeoPoint, Location
from backend.app.domain.identifiers import CompanyId, CompanyLocationId, OpportunityId
from backend.app.infrastructure.database.types import (
    WGS84_SRID,
    ewkt_to_geo_point,
    geo_point_to_ewkt,
)
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyCompanyRepository,
    SqlAlchemyOpportunityRepository,
)
from tests.v2_builders import (
    COMPANY,
    COMPANY_LOCATION,
    GENEVA,
    LAUSANNE,
    OPPORTUNITY,
    ZURICH,
    a_company,
    a_company_location,
    a_source_record,
    an_opportunity,
)

# 17 significant digits: more than a double can distinguish, which is the point.
# `repr` produces the shortest string that round-trips exactly, and PostGIS 3.1+
# does the same on the way out, so this value must come back bit-for-bit.
PRECISE = GeoPoint(latitude=46.51972222222222, longitude=6.632222222222222)

def test_the_text_form_puts_longitude_first():
    """WKT is `(x y)`, and x is longitude. The classic PostGIS bug, pinned.

    A swapped pair stays inside the valid range for most European coordinates, so
    nothing fails — the marker simply appears in another country.
    """
    assert geo_point_to_ewkt(GeoPoint(latitude=46.5, longitude=6.6)) == \
        "SRID=4326;POINT(6.6 46.5)"


def test_a_point_round_trips_through_its_text_form_exactly():
    assert ewkt_to_geo_point(geo_point_to_ewkt(PRECISE)) == PRECISE
    assert ewkt_to_geo_point(geo_point_to_ewkt(PRECISE)).latitude == PRECISE.latitude


def test_another_projection_is_refused_rather_than_reinterpreted():
    """A Swiss LV95 coordinate is not a WGS84 one, and must not be read as one.

    Silently accepting SRID 2056 would put a point 2.6 million degrees from where
    it belongs; refusing it is what keeps one SRID an invariant of the column.
    """
    with pytest.raises(ValueError, match=f"expected SRID {WGS84_SRID}"):
        ewkt_to_geo_point("SRID=2056;POINT(2537000 1152000)")


def test_something_that_is_not_a_point_is_refused():
    """The column stores points; a polygon coming back would mean the type changed."""
    with pytest.raises(ValueError, match="not a WGS84 EWKT point"):
        ewkt_to_geo_point("SRID=4326;POLYGON((6.6 46.5, 6.7 46.5, 6.7 46.6, 6.6 46.5))")


@pytest.mark.asyncio
async def test_a_stored_point_comes_back_bit_for_bit(db_session):
    """No precision is lost in the column, the driver or the text form.

    Float equality on purpose: `pytest.approx` would hide exactly the rounding this
    test exists to rule out, since a geocoded coordinate must compare equal to
    itself after a write.
    """
    repository = SqlAlchemyOpportunityRepository(db_session)
    stored = await repository.upsert(
        an_opportunity(location=Location(city="Lausanne", point=PRECISE)))
    assert stored.location is not None
    assert stored.location.point == PRECISE

    read_back = await repository.get(OPPORTUNITY)
    assert read_back is not None
    assert read_back.location is not None
    assert read_back.location.point.latitude == PRECISE.latitude
    assert read_back.location.point.longitude == PRECISE.longitude


@pytest.mark.asyncio
async def test_postgis_reads_the_coordinates_the_way_a_map_will(db_session):
    """Asks the database which number is the latitude.

    The round-trip test cannot catch a swap, because the same mistake on the way
    in and on the way out cancels out. `ST_Y` is latitude and `ST_X` is longitude
    by definition, so this is the assertion that the stored geometry means what
    every other PostGIS client — and Phase 7's map tiles — will read.
    """
    await SqlAlchemyOpportunityRepository(db_session).upsert(
        an_opportunity(location=Location(city="Lausanne", point=LAUSANNE)))
    result = await db_session.execute(text(
        "SELECT ST_Y(location_point::geometry), ST_X(location_point::geometry), "
        "ST_SRID(location_point) FROM opportunities WHERE id = :id"),
        {"id": str(OPPORTUNITY)})
    latitude, longitude, srid = result.one()
    assert (latitude, longitude) == (LAUSANNE.latitude, LAUSANNE.longitude)
    assert srid == WGS84_SRID


def a_posting_at(index: int, point: GeoPoint | None, city: str):
    """Posting `index`, located at `point` — or nowhere, when `point` is None."""
    return an_opportunity(
        id=OpportunityId(UUID(f"00000000-0000-4000-8000-0000000002{index:02d}")),
        source=a_source_record(external_id=f"posting-{index}"),
        dedup_fingerprint=f"fingerprint-{index}",
        title=f"Posting in {city}",
        location=Location(city=city) if point is None
        else Location(city=city, point=point))


@pytest.mark.asyncio
async def test_a_radius_query_orders_by_the_distance_the_database_computed(db_session):
    """The Phase 7 primitive: what is within 100 km of here, closest first.

    The distance is the one `ST_DWithin` used to decide the row matched, not a
    number recomputed in Python — so a result can never be listed as 101 km away
    by a query for everything within 100 km.
    """
    repository = SqlAlchemyOpportunityRepository(db_session)
    for index, (point, city) in enumerate(
            ((ZURICH, "Zurich"), (GENEVA, "Geneve"), (LAUSANNE, "Lausanne")), start=1):
        await repository.upsert(a_posting_at(index, point, city))

    nearby = await repository.list_near(LAUSANNE, 100_000)
    assert [found.opportunity.title for found in nearby] == [
        "Posting in Lausanne", "Posting in Geneve"]
    assert nearby[0].distance_meters == pytest.approx(0.0, abs=1.0)
    # ~51 km by road-free great-circle distance; the bounds are wide enough to
    # survive a PROJ update and narrow enough to fail on a degree-based answer.
    assert 45_000 < nearby[1].distance_meters < 60_000


@pytest.mark.asyncio
async def test_a_posting_nobody_could_place_on_a_map_is_never_nearby(db_session):
    """No point, no marker — and `ST_DWithin` returning NULL is what achieves it.

    `WHERE` keeps only TRUE, so an unlocated posting drops out of every radius
    query without a second predicate. Worth pinning: the alternative reading —
    treating "unknown" as "here" — would put a remote posting at the centre of the
    map for every search.
    """
    repository = SqlAlchemyOpportunityRepository(db_session)
    await repository.upsert(a_posting_at(1, LAUSANNE, "Lausanne"))
    await repository.upsert(a_posting_at(2, None, "Suisse romande"))

    nearby = await repository.list_near(LAUSANNE, 1_000_000)
    assert [found.opportunity.title for found in nearby] == ["Posting in Lausanne"]
    # Still stored, still in the feed: it is a posting, only not a located one.
    assert len(await repository.list_recent()) == 2


@pytest.mark.asyncio
async def test_the_radius_is_metres_because_the_column_is_a_geography(db_session):
    """The reason the column is `geography` and not `geometry`.

    Zurich is ~173 km from Lausanne. On a `geometry(Point,4326)` column the same
    query would compare degrees, `100_000` would cover the planet, and this test
    would find Zurich inside a 100 km radius.
    """
    repository = SqlAlchemyOpportunityRepository(db_session)
    await repository.upsert(a_posting_at(1, ZURICH, "Zurich"))

    assert await repository.list_near(LAUSANNE, 100_000) == ()
    reached = await repository.list_near(LAUSANNE, 200_000)
    assert [found.opportunity.title for found in reached] == ["Posting in Zurich"]
    assert 160_000 < reached[0].distance_meters < 185_000


@pytest.mark.asyncio
async def test_the_edge_of_the_radius_is_the_distance_the_database_reports(db_session):
    """`ST_DWithin` is inclusive, and it agrees with `ST_Distance` to the metre.

    Asked of the database rather than assumed: the two functions must use the same
    spheroid, or a result would be listed at exactly the radius by one call and
    filtered out by the next with the same argument.
    """
    repository = SqlAlchemyOpportunityRepository(db_session)
    await repository.upsert(a_posting_at(1, GENEVA, "Geneve"))
    exact = (await repository.list_near(LAUSANNE, 1_000_000))[0].distance_meters

    assert len(await repository.list_near(LAUSANNE, exact)) == 1
    assert await repository.list_near(LAUSANNE, exact - 1.0) == ()


@pytest.mark.asyncio
async def test_the_nearest_postings_stop_at_the_limit(db_session):
    """`limit` cuts the far end, not an arbitrary end.

    The ordering is applied before the cap, so a capped radius query is the top N
    nearest — which is what a map viewport asks for, and what makes an uncapped
    query unnecessary.
    """
    repository = SqlAlchemyOpportunityRepository(db_session)
    for index, (point, city) in enumerate(
            ((ZURICH, "Zurich"), (GENEVA, "Geneve"), (LAUSANNE, "Lausanne")), start=1):
        await repository.upsert(a_posting_at(index, point, city))

    nearby = await repository.list_near(LAUSANNE, 1_000_000, limit=2)
    assert [found.opportunity.title for found in nearby] == [
        "Posting in Lausanne", "Posting in Geneve"]


SECOND_SITE = CompanyLocationId(UUID("00000000-0000-4000-8000-000000000036"))
DISTANT_COMPANY = CompanyId(UUID("00000000-0000-4000-8000-000000000033"))
DISTANT_SITE = CompanyLocationId(UUID("00000000-0000-4000-8000-000000000037"))


@pytest.mark.asyncio
async def test_a_company_with_two_nearby_sites_is_returned_once(db_session):
    """The `IN (subquery)` in `CompanyRepository.list_near`, and why it is not a join.

    A retail chain with four branches in the radius is one employer to write to.
    Joining `company_locations` would return it four times, and the caller would
    have to deduplicate an entity it did not know was duplicated.
    """
    repository = SqlAlchemyCompanyRepository(db_session)
    await repository.upsert(a_company(
        a_company_location(),
        a_company_location(id=SECOND_SITE,
                           location=Location(country="CH", city="Geneve",
                                             point=GENEVA),
                           is_headquarters=False)))

    found = await repository.list_near(LAUSANNE, 100_000)
    assert [company.id for company in found] == [COMPANY]
    assert len(found[0].locations) == 2


@pytest.mark.asyncio
async def test_a_company_is_found_by_any_of_its_sites_not_only_its_head_office(db_session):
    """Where the work is decides who is nearby, and a branch is a workplace.

    A group headquartered in Zurich with an office in Lausanne must answer the
    Phase 6 question "who could I write to here?" — so the predicate is over the
    sites, and `is_headquarters` plays no part in it.
    """
    repository = SqlAlchemyCompanyRepository(db_session)
    await repository.upsert(a_company(
        a_company_location(location=Location(country="CH", city="Zurich",
                                             point=ZURICH)),
        a_company_location(id=SECOND_SITE,
                           location=Location(country="CH", city="Lausanne",
                                             point=LAUSANNE),
                           is_headquarters=False)))
    await repository.upsert(a_company(
        a_company_location(id=DISTANT_SITE, company_id=DISTANT_COMPANY,
                           location=Location(country="CH", city="Zurich",
                                             point=ZURICH)),
        id=DISTANT_COMPANY, name="Faraway AG"))

    assert [company.name for company in
            await repository.list_near(LAUSANNE, 100_000)] == ["Fixture SA"]
    assert {company.name for company in
            await repository.list_near(ZURICH, 100_000)} == {"Fixture SA", "Faraway AG"}


@pytest.mark.asyncio
async def test_the_radius_predicate_can_use_the_gist_index(db_session):
    """Why `within_radius` is `ST_DWithin` and not `ST_Distance(...) <= r`.

    The two are equivalent in result and not in cost: only the first can be
    answered by `ix_opportunities_location_point`. Three rows are far too few for
    the planner to prefer an index, so `enable_seqscan` is turned off for this
    transaction to ask a different question — *can* the index serve this
    predicate — which is the property that stops being true if the predicate form
    or the index method is changed.
    """
    repository = SqlAlchemyOpportunityRepository(db_session)
    for index, (point, city) in enumerate(
            ((ZURICH, "Zurich"), (GENEVA, "Geneve"), (LAUSANNE, "Lausanne")), start=1):
        await repository.upsert(a_posting_at(index, point, city))
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))

    plan = await db_session.execute(text(
        "EXPLAIN SELECT id FROM opportunities "
        "WHERE ST_DWithin(location_point, "
        "ST_GeogFromText('SRID=4326;POINT(6.6323 46.5197)'), 100000)"))
    assert "ix_opportunities_location_point" in "\n".join(
        line for (line,) in plan.all())
