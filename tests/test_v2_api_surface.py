# tests/test_v2_api_surface.py
"""What `/api/v2` publishes, and what V1 still publishes beside it.

Three of the nine properties Phase 4 has to demonstrate are here: that an
unauthenticated caller is refused by every V2 operation except register and login,
that no response model can carry a credential, and that V1 is behaviourally
unchanged. Two more are here because they are surface facts rather than flow facts:
the exact route inventory, and that nothing answering a safe method mutates state.

The inventory tests read `app.openapi()` rather than `app.routes`: FastAPI wraps the
routers it includes, so the router objects are not the list a client sees, and the
document is exactly what `scripts/dump_openapi.py` hands the frontend's type
generator. A route added without a test fails the first assertion in this module,
which is the point of pinning a count that a normal change has no reason to touch.

The counts moved once at Phase 6 — the three company operations — and again at
Phase 7, which adds the three geo reads: postings near a place, employers near a
place, and one saved search run as a geo query. All six are held to the same four
rules the Phase 4 twelve are — under the prefix, authenticated, safe where they read,
and carrying no credential field.
"""
from copy import deepcopy
from datetime import timedelta

import pytest

from backend.app.api import API_V2_PREFIX
from backend.app.domain.identifiers import CompanyId, SearchProfileId
from backend.app.services.authentication import SESSION_TOUCH_INTERVAL
from tests.v2_api import (
    PLACEHOLDER_ID,
    api_harness,
    concrete,
    operations,
    schema_property_names,
)
from tests.v2_builders import a_company, a_search_profile

# The whole V2 surface as of Phase 7, spelled out. Written as a literal on purpose:
# a test that derived it from the application would agree with any change.
V2_OPERATIONS = (
    ("DELETE", "/api/v2/me/search-profiles/{search_profile_id}"),
    ("GET", "/api/v2/auth/session"),
    ("GET", "/api/v2/companies"),
    ("GET", "/api/v2/companies/{company_id}"),
    ("GET", "/api/v2/geo/companies"),
    ("GET", "/api/v2/geo/opportunities"),
    ("GET", "/api/v2/me/profile"),
    ("GET", "/api/v2/me/search-profiles"),
    ("GET", "/api/v2/me/search-profiles/{search_profile_id}/opportunities"),
    ("GET", "/api/v2/onboarding"),
    ("POST", "/api/v2/auth/login"),
    ("POST", "/api/v2/auth/logout"),
    ("POST", "/api/v2/auth/register"),
    ("POST", "/api/v2/company-discovery/run"),
    ("POST", "/api/v2/me/search-profiles"),
    ("POST", "/api/v2/onboarding/complete"),
    ("PUT", "/api/v2/me/profile"),
    ("PUT", "/api/v2/me/search-profiles/{search_profile_id}"),
)

# The two operations a caller reaches without a session, because their purpose is to
# obtain one. Everything else in `V2_OPERATIONS` must answer 401.
PUBLIC_OPERATIONS = frozenset({
    ("POST", "/api/v2/auth/login"),
    ("POST", "/api/v2/auth/register"),
})

# Substrings that must not name a field in any V2 response. `hash` and `digest`
# cover the stored forms, `token` and `csrf` the live ones.
FORBIDDEN_IN_RESPONSES = ("password", "token", "digest", "hash", "secret",
                          "credential", "csrf")

# V1's published surface, as counts. 29 operations over 28 paths — `/api/settings`
# is the one path with two methods. Counts rather than a list of 28 strings: what
# Phase 4 has to prove is that adding the V2 router removed nothing, and a count
# says that without duplicating a V1 inventory that V1's own tests already cover.
V1_OPERATIONS = 29
V1_PATHS = 28


def _get_with_scope(path: str) -> str:
    """A callable URL for one GET in the twice-over sweep.

    The two open geo lists refuse a query with no scope — a full-table scan is not a
    search (§14) — so the sweep gives them the smallest valid one. Every other GET,
    including the saved-search geo read whose scope comes from the profile, is
    callable exactly as its template concretes.
    """
    if path.endswith(("/geo/opportunities", "/geo/companies")):
        return f"{path}?country=CH"
    return path


@pytest.mark.asyncio
async def test_the_v2_surface_is_exactly_the_eighteen_operations_phases_4_6_and_7_define(
        tmp_path):
    """The inventory, and every path scoped under the one prefix.

    The second assertion is the one that matters for the frontend: a V2 route
    mounted outside `/api/v2` would not be proxied by Nuxt, and a V1 path that
    slipped into the prefix would inherit the CSRF dependency and break V1 clients.
    """
    async with api_harness(tmp_path) as api:
        published = operations(api.app, under=API_V2_PREFIX)

        assert published == V2_OPERATIONS
        assert all(path.startswith(f"{API_V2_PREFIX}/") for _, path in published)
        assert len({path for _, path in published}) == 15


@pytest.mark.asyncio
async def test_a_safe_method_is_only_published_where_nothing_is_written(tmp_path):
    """The inventory half of "no GET mutates state".

    Nine `GET`s, all of them reports. `POST /onboarding/complete` exists precisely so
    that the screen displaying progress does not have to be the thing that records
    it, and `POST /company-discovery/run` is the same split for the directory: reading
    it is safe, filling it is a write an operator triggers. The three Phase 7 reads
    join the reports — a map is a view, and viewing it writes nothing.
    """
    async with api_harness(tmp_path) as api:
        published = operations(api.app, under=API_V2_PREFIX)

        assert {path for method, path in published if method == "GET"} == {
            "/api/v2/auth/session",
            "/api/v2/companies",
            "/api/v2/companies/{company_id}",
            "/api/v2/geo/companies",
            "/api/v2/geo/opportunities",
            "/api/v2/me/profile",
            "/api/v2/me/search-profiles",
            "/api/v2/me/search-profiles/{search_profile_id}/opportunities",
            "/api/v2/onboarding"}
        assert not [method for method, _ in published
                    if method in {"HEAD", "OPTIONS", "TRACE", "PATCH"}]


@pytest.mark.asyncio
async def test_calling_every_get_twice_leaves_every_store_identical(tmp_path):
    """The behavioural half, asserted against the stores rather than the bodies.

    A `GET` that wrote something would be a request an attacker's page could make
    cross-site with the browser's cookie attached — `SameSite=Lax` sends the session
    cookie on a top-level navigation, and no CSRF header is required of a safe
    method. So the rule has to hold in the handlers, not only in the route table.

    The company stored under `PLACEHOLDER_ID` is what makes the sweep meaningful for
    the detail route: without it that `GET` would 404 before reaching the service, and
    a handler that wrote on the way to a 200 would never be exercised. The saved search
    stored under the same id does the same job for the geo read on
    `/me/search-profiles/{id}/opportunities` — it has to resolve to a profile this
    account owns, or the sweep would exercise the 404 rather than the 200 path.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await api.finish_onboarding()
        user_id = next(iter(api.users.users))
        await api.companies.upsert(a_company(id=CompanyId(PLACEHOLDER_ID),
                                             locations=()))
        await api.searches.upsert(a_search_profile(
            id=SearchProfileId(PLACEHOLDER_ID), user_id=user_id))
        reads = [_get_with_scope(concrete(path)) for method, path in operations(
            api.app, under=API_V2_PREFIX) if method == "GET"]
        before = deepcopy((api.users.users, api.sessions.sessions,
                           api.profiles.profiles, api.searches.searches,
                           api.companies.companies, api.career_sites.sites,
                           api.discoveries.records, api.postings.opportunities))

        for path in reads:
            for _ in range(2):
                response = await api.client.get(path)
                assert response.status_code == 200, path

        assert (api.users.users, api.sessions.sessions, api.profiles.profiles,
                api.searches.searches, api.companies.companies,
                api.career_sites.sites, api.discoveries.records,
                api.postings.opportunities) == before


@pytest.mark.asyncio
async def test_the_one_write_a_get_performs_is_last_seen_at_and_it_cannot_extend(
        tmp_path):
    """`last_seen_at` moves; `expires_at` does not, which is what makes it safe.

    Recording that a session was used is not a state change in the sense the CSRF
    rules mean: it is idempotent, it concerns the caller's own row, and because the
    lifetime is absolute it cannot lengthen a stolen cookie's usefulness. The test
    exists to hold that last part — a sliding expiry implemented here would look like
    a harmless bookkeeping change.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        issued = next(iter(api.sessions.sessions.values()))
        api.clock.advance(SESSION_TOUCH_INTERVAL + timedelta(seconds=1))

        assert (await api.read("/auth/session")).status_code == 200

        touched = next(iter(api.sessions.sessions.values()))
        assert touched.last_seen_at == api.clock.instant
        assert touched.last_seen_at > issued.last_seen_at
        assert touched.expires_at == issued.expires_at
        assert touched == issued.model_copy(
            update={"last_seen_at": touched.last_seen_at})


@pytest.mark.asyncio
async def test_every_operation_but_register_and_login_refuses_an_anonymous_caller(
        tmp_path):
    """401 from all sixteen, with no body sent and nothing created.

    No payload is needed because FastAPI resolves the session dependency before it
    validates a body, so the refusal happens before the request is read — which is
    also why an unauthenticated write cannot be used to probe the validation rules.
    Sweeping the *published* inventory rather than a hand-written list is what makes
    this catch a future route that forgot the dependency.

    The geo reads are in the sweep, and their 401 is the point Phase 7 has to make:
    an anonymous caller is refused before the query is even parsed, so a missing
    cookie short-circuits a malformed radius rather than leaking which queries are
    well formed.
    """
    async with api_harness(tmp_path) as api:
        protected = [(method, path) for method, path in operations(
            api.app, under=API_V2_PREFIX) if (method, path) not in PUBLIC_OPERATIONS]

        assert len(protected) == 16
        for method, template in protected:
            response = await api.client.request(method, concrete(template))

            assert response.status_code == 401, (method, template)
            assert response.json()["error"] == "not_authenticated", template
            assert "set-cookie" not in response.headers, template

        assert api.users.users == {}
        assert api.sessions.sessions == {}
        assert api.profiles.profiles == {}
        assert api.searches.searches == {}
        assert api.companies.companies == {}
        assert api.discoveries.records == {}


@pytest.mark.asyncio
async def test_no_response_schema_reachable_from_v2_declares_a_credential_field(
        tmp_path):
    """Asserted on the contract, not on one response body that happened to be empty.

    The generated TypeScript client is built from this document, so a credential
    field here would become a typed field the frontend could store, log or render.
    The walk follows `$ref` into the nested models — `SignedInResponse` holds
    `AccountResponse`, which is where a `password_hash` would appear if the response
    models were ever built from the domain object instead of from the API schema.

    The second half is the anti-vacuity check: the register and login *bodies* must
    still declare `password`, which proves the walker finds field names at all.
    """
    async with api_harness(tmp_path) as api:
        responses = schema_property_names(api.app, under=API_V2_PREFIX)
        bodies = schema_property_names(api.app, under=API_V2_PREFIX,
                                       section="requestBody")

        assert len(responses) == len(V2_OPERATIONS)
        for operation, fields in responses.items():
            offending = [field for field in fields
                         if any(word in field.lower()
                                for word in FORBIDDEN_IN_RESPONSES)]
            assert offending == [], f"{operation} exposes {offending}"

        assert "password" in bodies["POST /api/v2/auth/register"]
        assert "password" in bodies["POST /api/v2/auth/login"]
        assert "user_id" not in bodies["PUT /api/v2/me/profile"]
        assert "user_id" not in bodies["POST /api/v2/me/search-profiles"]


@pytest.mark.asyncio
async def test_v1_still_answers_exactly_as_it_did_beside_the_v2_router(tmp_path):
    """No session, no CSRF header, no cross-site check, and V1's own 422 body.

    V1 has no accounts, so every one of its routes has to keep working for a caller
    with no cookie — which is why `current_session` and `reject_cross_site_writes`
    are dependencies of the V2 router and not middleware. Middleware is the obvious
    way to write a CSRF check and it would have broken all 29 of these.

    The 422 is the sharpest of the four: V2 redacts the rejected value, V1 keeps
    FastAPI's `input` key. `install_v2_error_handlers` registers one handler for the
    whole application, so it has to delegate for a V1 path, and this is the assertion
    that it does.
    """
    async with api_harness(tmp_path) as api:
        v1 = [(method, path) for method, path in operations(api.app, under="/api/")
              if not path.startswith(API_V2_PREFIX)]

        assert len(v1) == V1_OPERATIONS
        assert len({path for _, path in v1}) == V1_PATHS

        assert (await api.client.get("/api/overview")).status_code == 200

        write = await api.client.post("/api/jobs/1/status",
                                      json={"status": "approved"})
        assert write.status_code == 409, "a V1 write must not need an X-CSRF-Token"
        assert write.json() == {"detail": {"error": "invalid_transition",
                                          "current": None, "target": "approved"}}
        cross_site = await api.client.post("/api/jobs/1/status",
                                           json={"status": "approved"},
                                           headers={"Sec-Fetch-Site": "cross-site"})
        assert cross_site.status_code == 409, "the V1 surface is not CSRF-checked"

        invalid = await api.client.post("/api/jobs/1/status", json={})
        assert invalid.status_code == 422
        assert invalid.json() == {"detail": [{"type": "missing",
                                             "loc": ["body", "status"],
                                             "msg": "Field required",
                                             "input": {}}]}





