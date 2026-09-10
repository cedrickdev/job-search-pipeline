# tests/test_v2_api_companies.py
"""`/api/v2/companies` and `/api/v2/company-discovery/run` over HTTP.

The service suites decide what a claim becomes; this one covers what a browser
meets, which is a different set of facts and is decided by the route signatures and
by the response models. Four properties carry the file:

**Authentication is required and authorization has nothing to scope** (§21). A
company is a shared fact with no `user_id`, so two accounts must get the same answer
— and the anonymous caller must be refused *before* a provider is asked, which is
something only a request-flow test can show.

**Pagination is bounded at the signature** (§20). `limit` and `offset` carry their
range in the declaration, so an out-of-band value is a 422 naming the parameter
rather than a clamp the client cannot see. The window is echoed back because the
server is allowed to return fewer rows than were asked for.

**The provenance that leaves the backend is the typed subset** (§29). A discovery
record keeps whatever the provider handed over; the response model has nowhere to put
it. The test for that asserts on the bytes on the wire, not on the model's field list.

**A pass is a bounded write that reports its own failures** (§23, §26). The same
`POST` twice creates once, a provider that raised is in `health` instead of in a 500,
and a deployment with nothing configured gets a warning rather than an error.

The providers are fakes registered into the harness's registry, so nothing here
touches a socket or a YAML file — `api.providers` is the composition §18 keeps out of
the orchestrator, and a test fills it the way `bootstrap` does.
"""
from uuid import UUID

import pytest

from backend.app.companies.contracts import CompanyDiscoveryWarningCode
from backend.app.domain.company import (
    AtsPlatform,
    DetectionStatus,
    Evidence,
    SpontaneousApplicationSupport,
)
from backend.app.domain.identifiers import CompanyId
from tests.v2_api import EMAIL, OTHER_EMAIL, api_harness
from tests.v2_builders import COMPANY, a_company, an_opportunity
from tests.v2_companies import (
    FakeCompanyProvider,
    a_career_site,
    a_discovered_company,
    a_discovery_record,
    a_provider_metadata,
    a_provider_result,
    a_seed,
    an_alias,
)

PROVIDER = "fake_provider"
OTHER_PROVIDER = "other_provider"

# Three employers with ids a failure message can be grepped for, named so that the
# order the directory pages through — by name, then id — is the alphabet.
ACME = CompanyId(UUID("00000000-0000-4000-8000-0000000000a1"))
BETA = CompanyId(UUID("00000000-0000-4000-8000-0000000000a2"))
CEDAR = CompanyId(UUID("00000000-0000-4000-8000-0000000000a3"))

# An id that parses and matches nothing, for the 404 that is the only reason this
# route can refuse a read (§21).
UNKNOWN_COMPANY = "00000000-0000-4000-8000-0000000000ff"


def an_employer(company_id: CompanyId, name: str, **overrides):
    """One stored employer, with no site: `a_company`'s default belongs to `COMPANY`.

    `Company._locations_belong_here` refuses a location pointing at another company,
    which is exactly the copy-paste this helper exists to avoid.
    """
    return a_company(id=company_id, name=name, locations=(), **overrides)


def a_claim(name: str = "Acme SA", **overrides):
    """One employer a provider is reporting, keyed on its name like a real seed."""
    return a_discovered_company(seed=a_seed(name), **overrides)


def a_provider(*claims, provider_key: str = PROVIDER,
               error: BaseException | None = None) -> FakeCompanyProvider:
    """A registered provider answering from a script (§30: no live network call)."""
    return FakeCompanyProvider(
        a_provider_metadata(provider_key), error=error,
        result=a_provider_result(provider_key, *claims))


# --- §20, §21: who may read the directory --------------------------------------


@pytest.mark.asyncio
async def test_all_three_company_operations_refuse_a_caller_with_no_session(tmp_path):
    """401 from each, and the refused pass never reaches the provider.

    The inventory sweep in `test_v2_api_surface.py` asserts the status; this asserts
    what did *not* happen behind it. A session dependency that resolved after the
    body — or a route that ran the pass and then checked — would still answer 401
    while having asked every provider, which is a denial-of-service shaped hole in a
    write that fans out to configured sources.
    """
    async with api_harness(tmp_path) as api:
        provider = a_provider(a_claim())
        api.providers.register(provider)

        for method, path in (("GET", "/companies"),
                             ("GET", f"/companies/{UNKNOWN_COMPANY}"),
                             ("POST", "/company-discovery/run")):
            response = await api.client.request(method, api.url(path))

            assert response.status_code == 401, path
            assert response.json()["error"] == "not_authenticated", path

        assert provider.requests == []
        assert provider.healthchecks == 0
        assert api.companies.companies == {}
        assert api.discoveries.records == {}


@pytest.mark.asyncio
async def test_a_pass_without_the_csrf_header_is_refused_before_it_discovers(tmp_path):
    """403, because a discovery pass is an unsafe method like any other.

    `POST /company-discovery/run` takes no owner and writes shared rows, which is
    precisely why it needs the double-submit check: without it, an attacker's page
    could make an authenticated browser trigger passes against every configured
    provider.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        provider = a_provider(a_claim())
        api.providers.register(provider)

        response = await api.client.request("POST", api.url("/company-discovery/run"))

        assert response.status_code == 403
        assert response.json()["error"] == "csrf_failed"
        assert provider.requests == []
        assert api.companies.companies == {}


@pytest.mark.asyncio
async def test_two_accounts_read_the_same_employer_because_a_company_has_no_owner(
        tmp_path):
    """§21 over HTTP: the same bytes for both, and no owner field to ask about.

    The stronger half is the last assertion. There is no `user_id` on the company
    table and no owner parameter on either route, so "B sees what A sees" is not a
    leak — it is the contract. What would be a defect is a response *carrying* an
    account id, because that is how a shared fact starts being treated as a row
    somebody owns.
    """
    async with api_harness(tmp_path) as api:
        await api.companies.upsert(an_employer(ACME, "Acme SA"))
        await api.sign_in(email=EMAIL)
        mine = await api.read("/companies")
        detail = await api.read(f"/companies/{ACME}")
        assert (await api.write("POST", "/auth/logout")).status_code == 204

        await api.sign_in(email=OTHER_EMAIL, display_name="Someone else")
        theirs = await api.read("/companies")
        their_detail = await api.read(f"/companies/{ACME}")

        assert mine.status_code == theirs.status_code == 200
        assert theirs.json() == mine.json()
        assert their_detail.json() == detail.json()
        assert "user_id" not in theirs.text
        assert len(api.users.users) == 2


# --- §28: an employer with no opportunity is still an employer ------------------


@pytest.mark.asyncio
async def test_an_employer_with_no_opportunity_is_listed_like_any_other(tmp_path):
    """The main Phase 6 acceptance criterion, asked over HTTP.

    The directory does not require an active posting, and `has_opportunities` is a
    filter a client may apply rather than a condition the route imposes — which is
    what makes "0 open roles, spontaneous application possible" a reachable screen.
    Both directions are asserted, because a filter that ignored its argument would
    pass the interesting half by accident.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await api.companies.upsert(an_employer(ACME, "Acme SA"))
        await api.companies.upsert(an_employer(BETA, "Beta SA"))
        await api.postings.upsert(an_opportunity(company_id=ACME))

        everyone = (await api.read("/companies")).json()
        quiet = (await api.read("/companies?has_opportunities=false")).json()
        hiring = (await api.read("/companies?has_opportunities=true")).json()

        assert [company["name"] for company in everyone["companies"]] == ["Acme SA",
                                                                         "Beta SA"]
        assert everyone["total"] == 2
        assert [company["id"] for company in quiet["companies"]] == [str(BETA)]
        assert [company["id"] for company in hiring["companies"]] == [str(ACME)]


@pytest.mark.asyncio
async def test_the_window_is_echoed_back_and_one_outside_the_bounds_is_refused(
        tmp_path):
    """`limit`/`offset` in the body, and a 422 naming the parameter outside the range.

    The echo is what makes the next page computable: the server may return fewer
    companies than the client asked for, and a client doing `offset += len(companies)`
    against a silently clamped page would skip rows. The refusals are the signature's
    — `MAX_PAGE_SIZE` is declared with `le`, so an unbounded list is not something a
    caller can ask for in the first place (§20).
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        for company_id, name in ((ACME, "Acme SA"), (BETA, "Beta SA"),
                                 (CEDAR, "Cedar SA")):
            await api.companies.upsert(an_employer(company_id, name))

        first = (await api.read("/companies?limit=2")).json()
        last = (await api.read("/companies?limit=2&offset=2")).json()

        assert [company["name"] for company in first["companies"]] == ["Acme SA",
                                                                      "Beta SA"]
        assert (first["total"], first["limit"], first["offset"]) == (3, 2, 0)
        assert [company["name"] for company in last["companies"]] == ["Cedar SA"]
        assert (last["total"], last["limit"], last["offset"]) == (3, 2, 2)

        for query, parameter in (("limit=101", "limit"), ("limit=0", "limit"),
                                 ("offset=-1", "offset")):
            refused = await api.read(f"/companies?{query}")

            assert refused.status_code == 422, query
            assert refused.json()["error"] == "validation_failed"
            assert [error["loc"] for error in refused.json()["errors"]] == [
                ["query", parameter]]


@pytest.mark.asyncio
async def test_a_filter_value_the_signature_does_not_recognize_is_refused(tmp_path):
    """A misspelled filter is a 422, never a silently unfiltered list.

    Each of these would otherwise return *every* employer, which is the failure mode
    worth a test: a client that asked for Swiss Greenhouse employers and received the
    whole directory has no way to tell that its question was dropped.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await api.companies.upsert(an_employer(ACME, "Acme SA", country="CH"))

        for query in ("country=ch", "country=CHE", "text=",
                      "ats_platform=WORKDAY", "spontaneous_support=MAYBE"):
            refused = await api.read(f"/companies?{query}")

            assert refused.status_code == 422, query
            assert refused.json()["error"] == "validation_failed", query

        accepted = await api.read("/companies?country=CH&ats_platform=GREENHOUSE")

        assert accepted.status_code == 200
        # The country matches and the platform does not, so the page is empty rather
        # than a 404: "no employer matches" is a list, not a missing resource.
        assert accepted.json() == {"companies": [], "total": 0, "limit": 20,
                                   "offset": 0}


# --- §29: what one employer's detail is allowed to say -------------------------


@pytest.mark.asyncio
async def test_an_unknown_company_is_a_404_and_an_unparsable_id_is_a_422(tmp_path):
    """The two refusals a detail route can make, and they are different failures.

    404 means "nothing is stored under that id" and is the *only* reason this route
    refuses a signed-in caller (§21) — there is no owner to hide behind the same
    status. 422 means the path never named an id at all, which is a client bug rather
    than a missing employer.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        missing = await api.read(f"/companies/{UNKNOWN_COMPANY}")
        malformed = await api.read("/companies/not-a-uuid")

        assert missing.status_code == 404
        assert missing.json() == {"error": "company_not_found",
                                  "detail": "no company is stored under that id"}
        assert malformed.status_code == 422
        assert malformed.json()["error"] == "validation_failed"


@pytest.mark.asyncio
async def test_the_detail_says_how_we_found_them_and_keeps_the_raw_payload_inside(
        tmp_path):
    """Aliases, careers endpoints and provenance — and no provider passthrough.

    The stored record carries the provider's own metadata, and the assertion here is
    on the response *text*: `CompanyDiscoveryRecordResponse` has no `raw` field, so a
    future model that added one to be helpful would fail this test rather than start
    shipping a provider's bag of strings to a browser (§29).

    The last assertion is the anti-vacuity check — the record really does hold the
    payload, so the absence above is redaction at the boundary and not an empty
    fixture.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await api.companies.upsert(a_company())
        await api.companies.upsert_alias(an_alias(COMPANY, "LOGITECH"))
        await api.career_sites.upsert(a_career_site(COMPANY))
        await api.discoveries.upsert(a_discovery_record(
            PROVIDER, "fixture sa", company_id=COMPANY,
            raw={"board": "greenhouse-internal-4821", "headcount": "300"}))

        response = await api.read(f"/companies/{COMPANY}")

        assert response.status_code == 200
        body = response.json()
        assert body["company"]["name"] == "Fixture SA"
        assert body["company"]["normalized_name"] == "fixture sa"
        assert [location["city"] for location in body["company"]["locations"]] == [
            "Lausanne"]
        assert [alias["alias"] for alias in body["aliases"]] == ["LOGITECH"]
        assert body["aliases"][0]["source_key"] == "test_provider"
        assert [site["url"] for site in body["career_sites"]] == [
            "https://example.test/jobs"]
        # Null for everything Phase 6 writes: nothing in this phase fetches a URL, so
        # a timestamp here would claim a check nobody performed (§11).
        assert body["career_sites"][0]["last_checked_at"] is None
        assert body["discovered_by"] == [PROVIDER]
        assert body["discoveries"][0]["external_id"] == "fixture sa"
        assert "raw" not in body["discoveries"][0]
        assert "greenhouse-internal-4821" not in response.text

        assert next(iter(api.discoveries.records.values())).raw["headcount"] == "300"


# --- §17, §23, §26: the pass as an endpoint ------------------------------------


@pytest.mark.asyncio
async def test_a_pass_persists_what_a_provider_reported_and_the_second_creates_nothing(
        tmp_path):
    """200 twice, `created: 1` then `created: 0` — idempotence made visible (§23).

    200 rather than 201 for exactly this reason: the second call is the common one in
    a deployment that runs discovery on a schedule, and "201 Created" would be a lie
    about it. The counts are what an operator watches, so they are asserted rather
    than the company list, which `GET /companies` owns.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        provider = a_provider(a_claim("Acme SA", website="https://acme.test"))
        api.providers.register(provider)

        first = await api.write("POST", "/company-discovery/run")
        second = await api.write("POST", "/company-discovery/run")

        assert first.status_code == 200
        created = first.json()
        assert (created["created"], created["matched"], created["ambiguous"]) == (
            1, 0, 0)
        assert created["providers"] == [PROVIDER]
        assert created["unusable_providers"] == []
        assert created["is_complete"] is True
        assert created["links"] == {"examined": 0, "linked": 0, "ambiguous": 0,
                                   "unresolved": 0}

        assert second.status_code == 200
        again = second.json()
        assert (again["created"], again["matched"]) == (0, 1)
        assert again["company_ids"] == created["company_ids"]
        assert len(provider.requests) == 2

        listed = (await api.read("/companies")).json()
        assert listed["total"] == 1
        assert listed["companies"][0]["name"] == "Acme SA"
        assert listed["companies"][0]["website"] == "https://acme.test"


@pytest.mark.asyncio
async def test_a_provider_that_raised_is_reported_in_health_and_never_in_the_body(
        tmp_path):
    """One broken provider does not cancel the others, and its exception stays home.

    Both halves are §26. Failure isolation is why the healthy provider's employer is
    in the database afterwards; the normalized code and sentence are why the response
    cannot carry what the exception held — an adapter's error message is the one place
    a credential or a query string plausibly reaches a client.
    """
    leak = "hunter2"
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        api.providers.register(a_provider(a_claim("Acme SA")))
        api.providers.register(a_provider(
            provider_key=OTHER_PROVIDER,
            error=RuntimeError(f"GET /boards?credential={leak} failed")))

        response = await api.write("POST", "/company-discovery/run")

        assert response.status_code == 200
        body = response.json()
        assert body["created"] == 1
        assert body["unusable_providers"] == [OTHER_PROVIDER]
        assert body["is_complete"] is False
        broken = next(health for health in body["health"]
                      if health["provider_key"] == OTHER_PROVIDER)
        assert broken["status"] != "HEALTHY"
        assert broken["reason"] is not None
        assert leak not in response.text
        assert [company["name"] for company in
                (await api.read("/companies")).json()["companies"]] == ["Acme SA"]


@pytest.mark.asyncio
async def test_a_deployment_with_nothing_configured_gets_a_warning_not_an_error(
        tmp_path):
    """An empty registry is a real state, and the body has to say which one it is.

    `config/companies.yaml` ships empty, so this is what a fresh deployment sees. A
    200 with `created: 0` and no warning would be indistinguishable from "every
    provider ran and the world has no employers", which is the failure
    docs/V2_SPECIFICATION.md §22 calls hiding source health.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        response = await api.write("POST", "/company-discovery/run")

        assert response.status_code == 200
        body = response.json()
        assert (body["created"], body["matched"], body["ambiguous"]) == (0, 0, 0)
        assert body["providers"] == []
        assert body["is_complete"] is False
        assert [warning["code"] for warning in body["warnings"]] == [
            CompanyDiscoveryWarningCode.NO_PROVIDER_SELECTED]
        assert body["warnings"][0]["provider_key"] is None


@pytest.mark.asyncio
async def test_the_run_body_may_narrow_a_pass_and_may_not_add_an_employer(tmp_path):
    """`provider_keys` selects; a seed field is a 422 (§21).

    A request body that carried a seed would let any authenticated account write into
    every other account's directory, because companies are shared. `extra="forbid"`
    is what makes "there is no field to put one in" a checked statement, and the
    bounded numbers are what stop a caller asking for an unbounded walk.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        chosen = a_provider(a_claim("Acme SA"))
        ignored = a_provider(a_claim("Beta SA"), provider_key=OTHER_PROVIDER)
        api.providers.register(chosen)
        api.providers.register(ignored)

        narrowed = await api.write("POST", "/company-discovery/run",
                                   json={"provider_keys": [PROVIDER]})

        assert narrowed.status_code == 200
        assert narrowed.json()["providers"] == [PROVIDER]
        assert narrowed.json()["created"] == 1
        assert len(chosen.requests) == 1
        assert ignored.requests == []

        for body in ({"seeds": [{"name": "Injected SA"}]},
                     {"company": {"name": "Injected SA"}},
                     {"limit": 0}, {"link_limit": -1}, {"country": "ch"}):
            refused = await api.write("POST", "/company-discovery/run", json=body)

            assert refused.status_code == 422, body
            assert refused.json()["error"] == "validation_failed", body

        assert [company["name"] for company in
                (await api.read("/companies")).json()["companies"]] == ["Acme SA"]


@pytest.mark.asyncio
async def test_a_pass_links_a_stored_posting_and_leaves_the_published_name_alone(
        tmp_path):
    """§27 end to end, and `link_limit: 0` proving the two halves are separable.

    The posting resolves on the ATS organization in its apply URL rather than on its
    company string: `ACME` and `Acme SA` are only similar, and §2 forbids merging on
    that. The evidence is `jobs.lever.co/acme`, which names the employer inside
    Lever's address space.

    `company_name` is asserted unchanged afterwards because it is provenance — what
    the board published — and the resolved `company_id` is an addition beside it, not
    a correction of it (§13).
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        api.providers.register(a_provider(a_claim(
            "Acme SA", website="https://acme.test",
            ats_platform=AtsPlatform.LEVER, ats_organization_id="acme",
            ats_status=DetectionStatus.CONFIRMED,
            ats_evidence=(Evidence(code="BOARD_URL",
                                   detail="the configured board names this token"),),
            spontaneous_application=SpontaneousApplicationSupport.UNKNOWN)))
        await api.postings.upsert(an_opportunity(
            company_name="ACME", company_id=None,
            application_url="https://jobs.lever.co/acme/123"))

        discovery_only = await api.write("POST", "/company-discovery/run",
                                         json={"link_limit": 0})

        assert discovery_only.json()["created"] == 1
        assert discovery_only.json()["links"] == {"examined": 0, "linked": 0,
                                                 "ambiguous": 0, "unresolved": 0}
        assert next(iter(api.postings.opportunities.values())).company_id is None

        linked = await api.write("POST", "/company-discovery/run")

        assert linked.status_code == 200
        assert linked.json()["links"]["examined"] == 1
        assert linked.json()["links"]["linked"] == 1
        posting = next(iter(api.postings.opportunities.values()))
        assert str(posting.company_id) == linked.json()["company_ids"][0]
        assert posting.company_name == "ACME"

        hiring = (await api.read("/companies?has_opportunities=true")).json()
        assert [company["name"] for company in hiring["companies"]] == ["Acme SA"]
        assert hiring["companies"][0]["detected_ats"]["status"] == "CONFIRMED"
        assert hiring["companies"][0]["detected_ats"]["organization_id"] == "acme"
