# tests/v2_api.py
"""The Phase 4 request-flow harness: the real application, driven over HTTP.

The service tests (`test_v2_authentication.py`, `test_v2_onboarding.py`) cover the
decisions. This covers what a browser actually meets, which is a different set of
facts and is decided by the route layer and by `create_app`'s wiring: the
attributes on a `Set-Cookie`, the 401 for a missing session, the 403 for a missing
`X-CSRF-Token`, the 404 rather than a 403 for somebody else's saved search. A test
that called a service directly would assert none of them.

**No database.** Every service dependency — `authentication_service`,
`onboarding_service`, `company_directory_service`, `company_discovery_service` — is
overridden with one built over `tests/v2_fakes.py`, which replaces the whole
dependency graph below the routes; `database_session` and `session_factory` are then
never resolved. `session_factory` is overridden anyway, with a function that raises:
a future route that reaches for a session instead of a service fails with a sentence
that says so, rather than by opening a socket to PostgreSQL.

**The company provider registry is empty until a test fills it.** The discovery
service is composed once per harness over a registry the test holds, so
`api.providers.register(FakeCompanyProvider(...))` decides what a `POST
/company-discovery/run` will find. An empty registry is a real case rather than a
setup gap — it is what a deployment with nothing configured looks like, and the pass
has to answer with a warning instead of an error.

**The clock is an object.** `Clock` is what `now` resolves to, so a test advances
time by assignment. The expiry and lockout flows are exercised in milliseconds and
without a `sleep`.

**Two cookie policies, both real.** `AuthSettings.for_local_http()` is the default
here because `http.cookiejar` will not send a `Secure` cookie to an `http://`
origin, so a jar-driven flow test needs it. The production policy — `Secure`,
`__Host-`, `SameSite=Lax` — is asserted over `https://testserver`; that the *same*
policy still emits `Secure` over `http://` is itself a test, because cookie
security is a deployment setting and never an inference from the request scheme
(docs/AUTHENTICATION.md §Sessions).

**`tmp_path`, always.** `create_app` bootstraps the V1 SQLite database and resolves
a settings file, so a harness built without a temporary directory would write into
the operator's real `data/`.
"""
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Final, Never
from uuid import UUID

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from backend.app.api import API_V2_PREFIX
from backend.app.api.cookies import COOKIE_PATH
from backend.app.api.dependencies import (
    CSRF_HEADER,
    auth_settings,
    authentication_service,
    company_directory_service,
    company_discovery_service,
    geo_search_service,
    now,
    onboarding_service,
    session_factory,
)
from backend.app.companies.orchestrator import CompanyDiscoveryOrchestrator
from backend.app.companies.registry import CompanyProviderRegistry
from backend.app.core.settings import AuthSettings
from backend.app.services.authentication import AuthenticationService
from backend.app.services.company_directory import CompanyDirectoryService
from backend.app.services.company_discovery import (
    CompanyDiscoveryService,
    CompanyResolutionService,
)
from backend.app.services.geo_search import GeoSearchService
from backend.app.services.onboarding import OnboardingService
from server.app import create_app
from tests.v2_fakes import (
    FakeCandidateProfileRepository,
    FakeCareerSiteRepository,
    FakeCompanyDiscoveryRepository,
    FakeCompanyRepository,
    FakeOpportunityRepository,
    FakeSearchProfileRepository,
    FakeSessionRepository,
    FakeUserRepository,
)

# The instant every flow starts at, and the two addresses they sign in with.
NOW: Final[datetime] = datetime(2026, 4, 1, 8, 0, tzinfo=UTC)
EMAIL: Final[str] = "candidate@example.com"
OTHER_EMAIL: Final[str] = "someone.else@example.com"

# Fixtures rather than secrets: hashed by real Argon2 and never leaving the
# process. Wrapped in `SecretStr` so that passing one into a `password` field is
# not a string literal, which is what ruff's S105/S106 look for — the alternative
# would be a `noqa` on every call site.
PASSWORD: Final[SecretStr] = SecretStr("correct horse battery staple")
WRONG_PASSWORD: Final[SecretStr] = SecretStr("incorrect horse battery staple")
SHORT_PASSWORD: Final[SecretStr] = SecretStr("too short")

# The host every flow runs against, and it has a dot on purpose. `http.cookiejar`
# files a cookie that carried no `Domain` attribute under the *effective* request
# host, and for a dotless name like `testserver` that is `testserver.local` — so
# `cookies.set(name, …, domain="testserver")` would add a second cookie of the same
# name instead of replacing the server's, after which reading it raises
# `CookieConflict` and the planted value is never sent. A dotted host makes the jar
# key predictable, which is what `Harness.plant_cookie` relies on. `.test` is
# reserved for exactly this (RFC 6761) and resolves nowhere.
COOKIE_HOST: Final[str] = "jobsearch.test"
LOCAL_HTTP_BASE_URL: Final[str] = f"http://{COOKIE_HOST}"
HTTPS_BASE_URL: Final[str] = f"https://{COOKIE_HOST}"

# The keys of an OpenAPI path item that are operations, so a path-level
# `parameters` entry cannot be counted as a route.
HTTP_METHODS: Final[frozenset[str]] = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"})

# Stands in for any `{…}` in a route template. It only has to parse: every route
# called with it is refused before the id is looked up.
PLACEHOLDER_ID: Final[UUID] = UUID("00000000-0000-4000-8000-0000000000ff")

# One valid body per draft the API accepts, as JSON rather than as a model: these
# tests are about what a browser sends, and a `model_dump()` would be testing the
# serializer against itself.
PROFILE_BODY: Final[dict[str, Any]] = {
    "display_name": "Candidate",
    "headline": "Backend engineer",
    "base_location": {"city": "Yverdon-les-Bains", "country": "CH"},
    "languages": [{"language": "fr", "level": "NATIVE"}],
}
SEARCH_BODY: Final[dict[str, Any]] = {
    "name": "Backend in Romandie",
    "areas": [{"kind": "COUNTRY", "country": "CH"}],
    "queries": ["backend engineer"],
}


class Clock:
    """The instant `now` resolves to, as something a test can move.

    A class rather than a closure so a test reads `api.clock.advance(...)` and so
    the same object can be handed to the dependency and inspected afterwards.
    """

    def __init__(self, instant: datetime = NOW) -> None:
        self.instant = instant

    def advance(self, delta: timedelta) -> datetime:
        self.instant += delta
        return self.instant

    def __call__(self) -> datetime:
        return self.instant


def credentials(*, email: str = EMAIL, password: SecretStr = PASSWORD,
                **extra: Any) -> dict[str, Any]:
    """A register or login body, with the password unwrapped for JSON."""
    return {"email": email, "password": password.get_secret_value(), **extra}


def _no_database() -> Never:
    """What a V2 route gets here if it asks for a PostgreSQL session."""
    raise AssertionError(
        "a request-flow test resolved the database session factory: a V2 route "
        "must reach persistence through a service dependency, which api_harness "
        "overrides — add the new dependency to the overrides or keep the route "
        "behind a service")


@dataclass(frozen=True, slots=True)
class Harness:
    """One application, the client that keeps its cookies, and the stores below it.

    The stores are exposed because half of what these tests assert is *server-side*:
    that a logout revoked the row, that a refused write left nothing behind, that
    one account's search never entered another's list. A response body cannot show
    any of that.

    `providers` is the one store a test writes *before* the request rather than reads
    after it: a company discovery pass has nothing to find until the test says what
    the providers report.
    """

    client: httpx.AsyncClient
    app: FastAPI
    clock: Clock
    settings: AuthSettings
    users: FakeUserRepository
    sessions: FakeSessionRepository
    profiles: FakeCandidateProfileRepository
    searches: FakeSearchProfileRepository
    postings: FakeOpportunityRepository
    companies: FakeCompanyRepository
    career_sites: FakeCareerSiteRepository
    discoveries: FakeCompanyDiscoveryRepository
    providers: CompanyProviderRegistry

    def url(self, path: str) -> str:
        """A V2 path, prefixed once so no test spells `/api/v2` itself."""
        return f"{API_V2_PREFIX}{path}"

    @property
    def csrf_token(self) -> str | None:
        """The CSRF cookie the client is holding, as a browser script would read it."""
        return self.client.cookies.get(self.settings.csrf_cookie_name)

    @property
    def session_token(self) -> str | None:
        """The session cookie's value.

        Readable here because `http.cookiejar` does not model `HttpOnly`; a script
        in a browser could not do this, which is the whole point of the attribute
        and is asserted from the raw header instead.
        """
        return self.client.cookies.get(self.settings.session_cookie_name)

    def plant_cookie(self, name: str, value: str) -> None:
        """Put a cookie in the jar by hand, replacing the server's if it is there.

        This is the attacker's half of the suite: replaying a token the server has
        revoked, or writing a CSRF cookie from a place that can write cookies but
        cannot read the session's server-side state. The domain and path have to
        match the ones the jar filed the real cookie under or the result is a
        *second* cookie of the same name (see `COOKIE_HOST`) rather than a
        substitution.
        """
        self.client.cookies.set(name, value, domain=COOKIE_HOST, path=COOKIE_PATH)

    async def read(self, path: str, **kwargs: Any) -> httpx.Response:
        """A safe request. No CSRF token, because a safe method needs none."""
        return await self.client.get(self.url(path), **kwargs)

    async def write(self, method: str, path: str, *, json: Any = None,
                    headers: dict[str, str] | None = None) -> httpx.Response:
        """An unsafe request carrying the CSRF header the session was issued with.

        `headers` is merged last, so a test can substitute a wrong token. A test
        that wants *no* header calls `api.client.request` directly — omitting a
        header is not something a merge can express, and the explicit call is
        clearer about what is being withheld.
        """
        merged = {} if self.csrf_token is None else {CSRF_HEADER: self.csrf_token}
        merged.update(headers or {})
        return await self.client.request(method, self.url(path), json=json,
                                         headers=merged)

    async def register(self, *, email: str = EMAIL, password: SecretStr = PASSWORD,
                       display_name: str | None = "Candidate") -> httpx.Response:
        return await self.client.post(
            self.url("/auth/register"),
            json=credentials(email=email, password=password,
                             display_name=display_name))

    async def log_in(self, *, email: str = EMAIL,
                     password: SecretStr = PASSWORD) -> httpx.Response:
        return await self.client.post(
            self.url("/auth/login"), json=credentials(email=email, password=password))

    async def sign_in(self, *, email: str = EMAIL,
                      display_name: str | None = "Candidate") -> httpx.Response:
        """Register, and refuse to continue if that did not work.

        Most tests here need *a* signed-in browser rather than the registration
        itself; asserting the 201 in the helper means a later failure is about the
        thing the test is named after.
        """
        response = await self.register(email=email, display_name=display_name)
        assert response.status_code == 201, response.text
        return response

    async def finish_onboarding(self) -> httpx.Response:
        """Save the profile and the search a signed-in account needs to complete."""
        profile = await self.write("PUT", "/me/profile", json=PROFILE_BODY)
        assert profile.status_code == 200, profile.text
        search = await self.write("POST", "/me/search-profiles", json=SEARCH_BODY)
        assert search.status_code == 201, search.text
        return search


@asynccontextmanager
async def api_harness(tmp_path: Path, *, settings: AuthSettings | None = None,
                      base_url: str = LOCAL_HTTP_BASE_URL,
                      instant: datetime = NOW) -> AsyncIterator[Harness]:
    """The application wired to fresh fakes, with a cookie-keeping client.

    A context manager rather than a fixture: three tests need a different cookie
    policy or a different origin, and a fixture would have to be parameterized to
    give them one. `create_app` is called per harness so the two dependency
    override dictionaries of two tests cannot meet.
    """
    resolved = settings if settings is not None else AuthSettings.for_local_http()
    clock = Clock(instant)
    users = FakeUserRepository()
    sessions = FakeSessionRepository()
    profiles = FakeCandidateProfileRepository()
    searches = FakeSearchProfileRepository()
    postings = FakeOpportunityRepository()
    companies = FakeCompanyRepository(postings)
    # The geo fallback join is cross-store: a posting with no point of its own is
    # placed at its employer's site, so `search_geo` has to reach the company
    # repository. The pair is built together and wired here, once both exist —
    # `FakeOpportunityRepository` late-binds it for exactly this reason.
    postings._companies = companies
    career_sites = FakeCareerSiteRepository()
    discoveries = FakeCompanyDiscoveryRepository()
    providers = CompanyProviderRegistry()
    directory = CompanyDirectoryService(companies, career_sites, discoveries)
    # Composed once, so the registry a test registers a provider into is the one the
    # pass reads. The real dependency rebuilds this per request because it needs that
    # request's session; nothing here holds one.
    discovery = CompanyDiscoveryService(
        CompanyDiscoveryOrchestrator(registry=providers, clock=clock),
        CompanyResolutionService(companies, career_sites, discoveries, postings))
    app = create_app(db_path=tmp_path / "v1.db",
                     settings_path=tmp_path / "settings.json")
    app.dependency_overrides[now] = clock
    app.dependency_overrides[auth_settings] = lambda: resolved
    app.dependency_overrides[authentication_service] = lambda: AuthenticationService(
        users, sessions, resolved)
    app.dependency_overrides[onboarding_service] = lambda: OnboardingService(
        profiles, searches, users)
    app.dependency_overrides[company_directory_service] = lambda: directory
    app.dependency_overrides[company_discovery_service] = lambda: discovery
    app.dependency_overrides[geo_search_service] = lambda: GeoSearchService(
        postings, companies, searches)
    app.dependency_overrides[session_factory] = _no_database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=base_url) as client:
        yield Harness(client=client, app=app, clock=clock, settings=resolved,
                      users=users, sessions=sessions, profiles=profiles,
                      searches=searches, postings=postings, companies=companies,
                      career_sites=career_sites, discoveries=discoveries,
                      providers=providers)


def operations(app: FastAPI, *, under: str) -> tuple[tuple[str, str], ...]:
    """Every `(METHOD, path)` the application publishes under a prefix.

    Read from the OpenAPI document rather than from `app.routes`: FastAPI wraps
    included routers, so the router objects are not the inventory a client sees,
    and the document is exactly what `scripts/dump_openapi.py` hands the frontend's
    type generator. Sorted, so a parametrized test's ids are stable.
    """
    paths: dict[str, dict[str, Any]] = app.openapi()["paths"]
    return tuple(sorted((method.upper(), path)
                        for path, item in paths.items() if path.startswith(under)
                        for method in item if method in HTTP_METHODS))


def concrete(template: str) -> str:
    """A callable path for a route template, with every `{…}` filled in.

    Every V2 path parameter is a UUID today, so one placeholder covers them all. A
    future non-UUID parameter fails here with a 422 instead of the status the test
    is about, which is the reminder to teach this function about it.
    """
    return re.sub(r"\{[^}]+\}", str(PLACEHOLDER_ID), template)


def schema_property_names(app: FastAPI, *, under: str,
                          section: str = "responses") -> dict[str, frozenset[str]]:
    """Every field name reachable from one half of each operation's schemas.

    Keyed by `"METHOD /path"` so a failing assertion names the route rather than
    just the field. `section` is `"responses"` or `"requestBody"`: the same walk
    answers "does any response carry a credential" and "does the register body still
    accept a password", and the second is what keeps the first from passing
    vacuously if this function ever stopped finding anything.

    The walk follows `$ref` into `components.schemas` and recurses through nested
    models, arrays and `anyOf`/`allOf`, because a credential field would not be at
    the top level of a response — it would be inside the account model inside the
    signed-in model.
    """
    document = app.openapi()
    schemas: dict[str, Any] = document.get("components", {}).get("schemas", {})
    found: dict[str, frozenset[str]] = {}
    for path, item in document["paths"].items():
        if not path.startswith(under):
            continue
        for method, operation in item.items():
            if method not in HTTP_METHODS:
                continue
            names: set[str] = set()
            _collect_property_names(operation.get(section), schemas, names, set())
            found[f"{method.upper()} {path}"] = frozenset(names)
    return found


def _collect_property_names(node: Any, schemas: dict[str, Any], names: set[str],
                            seen: set[str]) -> None:
    """Add every `properties` key under `node` to `names`, following references.

    `seen` holds the component names already expanded, so a self-referential schema
    terminates instead of recursing for ever.
    """
    if isinstance(node, list):
        for item in node:
            _collect_property_names(item, schemas, names, seen)
        return
    if not isinstance(node, dict):
        return
    reference = node.get("$ref")
    if isinstance(reference, str):
        component = reference.rsplit("/", 1)[-1]
        if component not in seen:
            seen.add(component)
            _collect_property_names(schemas.get(component), schemas, names, seen)
    properties = node.get("properties")
    if isinstance(properties, dict):
        names.update(properties)
    for key, value in node.items():
        if key != "$ref":
            _collect_property_names(value, schemas, names, seen)


def cookie_attributes(response: httpx.Response, name: str) -> dict[str, Any]:
    """The attributes of one `Set-Cookie` header, keyed as the header spells them.

    Parsed from the raw header because the cookie jar keeps the value and discards
    everything this file cares about: `HttpOnly` is not something a client can read
    back, and `SameSite` is not modelled by `http.cookiejar` at all. Attributes the
    header omits are absent from the result rather than empty, so `"domain" not in
    attributes` is how a test asserts a host-only cookie.
    """
    for header in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        parsed.load(header)
        if name in parsed:
            morsel = parsed[name]
            return {"value": morsel.value,
                    **{key: value for key, value in morsel.items() if value != ""}}
    raise AssertionError(f"no Set-Cookie header for {name!r} in "
                         f"{response.headers.get_list('set-cookie')}")
