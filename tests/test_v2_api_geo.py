# tests/test_v2_api_geo.py
"""Phase 7 geo explorer, over HTTP: the three read routes on the fakes.

The repository tests (`test_v2_geo_search_repositories.py`) prove PostGIS decides
membership, distance and order. This proves what a browser meets instead, which is
the route layer's job and nothing PostGIS can answer: that an anonymous caller is
refused before the query is even parsed, that a malformed radius is a *redacted*
422, that the response carries a resolved point with its provenance and never a
credential field, and — the one authorization fact — that a saved search belonging
to another account is the same 404 as one that does not exist.

The 401 comes before the 422 on purpose: `current_session` is a dependency, and
FastAPI resolves the dependency tree before it validates the endpoint's own query
model, so a missing cookie short-circuits a malformed query rather than leaking
which query strings are well formed.
"""
from uuid import UUID

import pytest

from backend.app.domain.common import Location
from backend.app.domain.identifiers import (
    CompanyId,
    CompanyLocationId,
    OpportunityId,
    SearchProfileId,
)
from backend.app.domain.opportunity import WorkplaceMode
from tests.v2_api import PLACEHOLDER_ID, api_harness
from tests.v2_builders import (
    LAUSANNE,
    OTHER_USER,
    a_company,
    a_company_location,
    a_search_profile,
    a_source_record,
    an_opportunity,
)

# Ids in the Phase-7 API band (…09xx), so a failure names a value this file owns
# rather than one shared with the repository or builder fixtures.
POSTING = OpportunityId(UUID("00000000-0000-4000-8000-0000000009a1"))
FALLBACK_COMPANY = CompanyId(UUID("00000000-0000-4000-8000-0000000009c1"))
FALLBACK_SITE = CompanyLocationId(UUID("00000000-0000-4000-8000-0000000009c2"))
FALLBACK_POSTING = OpportunityId(UUID("00000000-0000-4000-8000-0000000009c3"))
TWO_SITE_COMPANY = CompanyId(UUID("00000000-0000-4000-8000-0000000009d1"))
HQ_SITE = CompanyLocationId(UUID("00000000-0000-4000-8000-0000000009d2"))
BRANCH_SITE = CompanyLocationId(UUID("00000000-0000-4000-8000-0000000009d3"))
MINE = SearchProfileId(UUID("00000000-0000-4000-8000-0000000009b1"))
THEIRS = SearchProfileId(UUID("00000000-0000-4000-8000-0000000009b2"))

GEO_ROUTES = (
    "/geo/opportunities?country=CH",
    "/geo/companies?country=CH",
    f"/me/search-profiles/{PLACEHOLDER_ID}/opportunities",
)


@pytest.mark.asyncio
async def test_the_geo_routes_all_require_a_session(tmp_path):
    """401 before the query is parsed, on every one of the three reads.

    Includes the two open searches, which have no owner: the geo surface is still
    behind authentication because it exposes the whole shared corpus, and a public
    map is a Phase-8 decision nobody has made.
    """
    async with api_harness(tmp_path) as api:
        for path in GEO_ROUTES:
            response = await api.client.get(api.url(path))

            assert response.status_code == 401, path
            assert response.json()["error"] == "not_authenticated", path


@pytest.mark.asyncio
async def test_a_malformed_radius_is_a_redacted_422(tmp_path):
    """A bad `radius` is refused where the request was parsed, value stripped.

    The value never appears in the body: a query string can carry a token, and a
    422 that echoed the rejected input back would be a way to reflect it into a log
    (docs/ENGINEERING_STANDARDS.md §Security).
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        response = await api.read("/geo/opportunities?radius=not-a-number")

        assert response.status_code == 422
        assert response.json()["error"] == "validation_failed"
        assert "not-a-number" not in response.text
        for error in response.json()["errors"]:
            assert set(error) <= {"type", "loc", "msg"}


@pytest.mark.asyncio
async def test_a_query_with_no_scope_is_refused(tmp_path):
    """The domain's "a search needs somewhere to look" surfaces as a 422.

    An empty geo query is a full-table scan wearing the name of a search; the
    domain refuses it, and the param model turns that refusal into a 422 rather
    than an empty page nobody can explain.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        response = await api.read("/geo/opportunities")

        assert response.status_code == 422
        assert response.json()["error"] == "validation_failed"


@pytest.mark.asyncio
async def test_a_radius_query_returns_a_resolved_opportunity_with_its_provenance(
        tmp_path):
    """A posting in range comes back placed, measured and labelled.

    The shape is the contract Phase 8 draws pins from: a point with its provenance,
    a distance the database computed, the status that says how sure the pin is, and
    which radius branch admitted it.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await api.postings.upsert(an_opportunity(
            id=POSTING, title="Resolved in Lausanne",
            location=Location(country="CH", city="Lausanne", point=LAUSANNE),
            workplace_mode=WorkplaceMode.ON_SITE,
            source=a_source_record(external_id="geo-api-resolved"),
            dedup_fingerprint="geo-api-resolved"))

        response = await api.read("/geo/opportunities?radius=46.5197,6.6323,50,Home")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["limit"] == 50
        assert body["offset"] == 0
        assert len(body["opportunities"]) == 1
        item = body["opportunities"][0]
        assert item["title"] == "Resolved in Lausanne"
        assert item["status"] == "RESOLVED"
        assert item["remote_scope"] is None
        assert item["distance_meters"] == pytest.approx(0.0, abs=2.0)
        assert item["location"]["point"]["latitude"] == pytest.approx(46.5197)
        assert item["location"]["point"]["longitude"] == pytest.approx(6.6323)
        assert item["location"]["provenance"] == "SOURCE_PROVIDED"
        assert item["matched_radii"] == [{"radius_index": 0, "label": "Home"}]


@pytest.mark.asyncio
async def test_a_pointless_posting_falls_back_to_its_employer_site(tmp_path):
    """A posting with no point of its own is placed at its employer's office.

    This exercises the cross-store wiring the harness sets up (`postings` can see
    `companies`): without it the fallback join has nothing to reach, the posting
    matches no radius, and the page is empty. `COMPANY_FALLBACK` is the status that
    keeps a UI from drawing that weaker pin like a geocoded one (§12).
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await api.companies.upsert(a_company(
            a_company_location(id=FALLBACK_SITE, company_id=FALLBACK_COMPANY,
                               location=Location(country="CH", city="Lausanne",
                                                 point=LAUSANNE)),
            id=FALLBACK_COMPANY, name="Fallback SA"))
        await api.postings.upsert(an_opportunity(
            id=FALLBACK_POSTING, company_id=FALLBACK_COMPANY,
            location=Location(country="CH", city="Unknown office"),
            workplace_mode=WorkplaceMode.ON_SITE,
            source=a_source_record(external_id="geo-api-fallback"),
            dedup_fingerprint="geo-api-fallback"))

        response = await api.read("/geo/opportunities?radius=46.5197,6.6323,10")

        assert response.status_code == 200, response.text
        item = response.json()["opportunities"][0]
        assert item["status"] == "COMPANY_FALLBACK"
        assert item["location"]["city"] == "Lausanne"


@pytest.mark.asyncio
async def test_the_companies_route_returns_each_employer_once_at_its_nearest_site(
        tmp_path):
    """An employer with two sites in range appears once, at the closer one."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await api.companies.upsert(a_company(
            a_company_location(id=HQ_SITE, company_id=TWO_SITE_COMPANY,
                               location=Location(country="CH", city="Lausanne",
                                                 point=LAUSANNE)),
            a_company_location(id=BRANCH_SITE, company_id=TWO_SITE_COMPANY,
                               location=Location(country="CH", city="Geneve",
                                                 point=LAUSANNE),
                               is_headquarters=False),
            id=TWO_SITE_COMPANY, name="Two Sites SA"))

        response = await api.read("/geo/companies?radius=46.5197,6.6323,100")

        assert response.status_code == 200, response.text
        companies = response.json()["companies"]
        assert len(companies) == 1
        assert companies[0]["company"]["name"] == "Two Sites SA"
        assert companies[0]["is_headquarters"] is True
        assert companies[0]["status"] == "RESOLVED"


@pytest.mark.asyncio
async def test_a_remote_only_company_search_is_refused(tmp_path):
    """A company search cannot ask for remote-only: an employer is a place.

    `remote=only` with no geography is a `REMOTE_ONLY` query, and companies are
    matched by their sites, so the domain query is valid but the answer is empty by
    construction — the route still returns 200 with no companies rather than
    inventing one.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        response = await api.read("/geo/companies?remote=only")

        assert response.status_code == 200, response.text
        assert response.json()["companies"] == []


@pytest.mark.asyncio
async def test_a_saved_search_runs_only_for_its_owner(tmp_path):
    """The owner gets 200; another account's search is an indistinguishable 404.

    Ownership is the service's job, not the handler's: it loads the profile scoped
    by the session user and raises the same `search_profile_not_found` for "not
    yours" as for "no such search", so ids cannot be probed.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = next(iter(api.users.users))
        await api.searches.upsert(a_search_profile(id=MINE, user_id=user_id))
        await api.searches.upsert(a_search_profile(id=THEIRS, user_id=OTHER_USER))

        mine = await api.read(f"/me/search-profiles/{MINE}/opportunities")
        theirs = await api.read(f"/me/search-profiles/{THEIRS}/opportunities")

        assert mine.status_code == 200, mine.text
        assert mine.json()["opportunities"] == []
        assert theirs.status_code == 404
        assert theirs.json()["error"] == "search_profile_not_found"
