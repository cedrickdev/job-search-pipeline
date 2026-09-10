"""Per-request wiring: the engine, the clock, the session cookie, the CSRF guard.

Five decisions here are worth the paragraphs, because each one is the sort of thing
a reader would otherwise be tempted to simplify:

**The engine is created lazily and cached on `app.state`.** `create_app` must boot
with no database — 1045 V1 tests and `scripts/dump_openapi.py` construct the
application without PostgreSQL running, and a connection attempt in the factory
would break all of them. The first `/api/v2` request that needs a session builds
the engine; every later one reuses it.

**The clock is sampled once per request.** `now()` is a dependency, so every
timestamp a single request writes agrees: the session's `issued_at`, the profile's
`updated_at` and the onboarding stamp cannot land a few microseconds apart and
imply an ordering that did not happen.

**Authentication reads the session cookie and nothing else.** No `Authorization`
header, no query parameter, no body field. A second accepted channel is a second
thing to get wrong, and a token in a URL ends up in access logs and `Referer`
headers.

**The CSRF check is part of `current_session`, not a separate opt-in dependency.**
Any authenticated route that uses an unsafe method is checked, so protection cannot
be forgotten by omitting a `Depends`. `Sec-Fetch-Site` is rejected separately, at
router level, so it also covers the two unauthenticated `POST`s
(docs/AUTHENTICATION.md §CSRF).

**Every service is a dependency, including the composed ones.** Company discovery
needs three repositories, a provider registry and an orchestrator, and it is assembled
here rather than reached for inside a route: this is the only module a request-flow
test has to override to run the whole V2 surface without PostgreSQL, and a route that
built its own service would take that property away.
"""
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import Depends, Request
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.api.errors import csrf_failed, not_authenticated
from backend.app.companies.bootstrap import build_company_discovery
from backend.app.companies.providers.manual_seed import (
    PROVIDER_KEY as MANUAL_SEED_PROVIDER,
)
from backend.app.core.settings import AuthSettings, DatabaseSettings
from backend.app.discovery.bootstrap import build_country_packs
from backend.app.infrastructure.database.engine import (
    create_async_database_engine,
    create_session_factory,
    session_scope,
)
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyCandidateProfileRepository,
    SqlAlchemyCareerSiteRepository,
    SqlAlchemyCompanyDiscoveryRepository,
    SqlAlchemyCompanyRepository,
    SqlAlchemyOpportunityRepository,
    SqlAlchemySearchProfileRepository,
    SqlAlchemySessionRepository,
    SqlAlchemyUserRepository,
)
from backend.app.services.authentication import (
    AuthenticatedSession,
    AuthenticationService,
    csrf_token_matches,
)
from backend.app.services.company_directory import CompanyDirectoryService
from backend.app.services.company_discovery import (
    CompanyDiscoveryService,
    CompanyResolutionService,
)
from backend.app.services.onboarding import OnboardingService
from country_packs.registry import CountryPackRegistry

# The header the client copies the CSRF cookie into. Named here rather than
# inline so `frontend/app/utils/api-client.ts` and the tests refer to one spelling.
CSRF_HEADER: Final[str] = "X-CSRF-Token"

# Methods that cannot change server state and therefore need no CSRF token.
# `OPTIONS` is included because a preflight carries no credentials to protect.
SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})

# Set by `create_app`, read here. Attribute names rather than a module global so
# two applications in one process (which is what the test suite is) cannot share
# an engine.
AUTH_SETTINGS_ATTRIBUTE: Final[str] = "v2_auth_settings"
DATABASE_SETTINGS_ATTRIBUTE: Final[str] = "v2_database_settings"
SESSION_FACTORY_ATTRIBUTE: Final[str] = "v2_session_factory"
ENGINE_ATTRIBUTE: Final[str] = "v2_engine"
COUNTRY_PACKS_ATTRIBUTE: Final[str] = "v2_country_packs"


def now() -> datetime:
    """The instant this request happened, in UTC.

    A dependency rather than a call inside each service so that overriding it is
    how a test freezes time, and so that one request cannot disagree with itself
    about what "now" was.
    """
    return datetime.now(UTC)


def auth_settings(request: Request) -> AuthSettings:
    """The cookie and lockout policy for this deployment.

    Resolved by `create_app` and stored on the application, never re-read from the
    environment per request: a deployment that changed `JOBSEARCH_AUTH_COOKIE_SECURE`
    under a running process would otherwise start issuing cookies the browser
    already holding one cannot match.
    """
    settings = getattr(request.app.state, AUTH_SETTINGS_ATTRIBUTE, None)
    if isinstance(settings, AuthSettings):
        return settings
    resolved = AuthSettings.from_env()
    setattr(request.app.state, AUTH_SETTINGS_ATTRIBUTE, resolved)
    return resolved


def session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """The async session factory, built on first use and cached on the app.

    No connection is opened here — `create_async_engine` is lazy — so this stays
    safe for an application that will never receive a V2 request. The failure a
    caller sees when PostgreSQL is genuinely absent comes from the first statement
    and is mapped to 503 by `install_v2_error_handlers`.
    """
    existing = getattr(request.app.state, SESSION_FACTORY_ATTRIBUTE, None)
    if isinstance(existing, async_sessionmaker):
        return existing
    settings = getattr(request.app.state, DATABASE_SETTINGS_ATTRIBUTE, None)
    if not isinstance(settings, DatabaseSettings):
        settings = DatabaseSettings.from_env()
        setattr(request.app.state, DATABASE_SETTINGS_ATTRIBUTE, settings)
    engine = create_async_database_engine(settings)
    factory = create_session_factory(engine)
    # The engine is kept as well as the factory: `_lifespan` disposes it on
    # shutdown, and a factory alone gives nothing to dispose.
    setattr(request.app.state, ENGINE_ATTRIBUTE, engine)
    setattr(request.app.state, SESSION_FACTORY_ATTRIBUTE, factory)
    return factory


async def database_session(
        factory: Annotated[async_sessionmaker[AsyncSession], Depends(session_factory)],
) -> AsyncIterator[AsyncSession]:
    """One unit of work per request, committed on success and rolled back on error.

    The commit is here rather than in a route because a route that committed would
    be deciding for a future route that the work ends there. It also means the
    `last_seen_at` touch a read performs is committed by the same boundary as a
    write — one transaction per request, whatever the method.
    """
    async with session_scope(factory) as session:
        yield session


def authentication_service(
        session: Annotated[AsyncSession, Depends(database_session)],
        settings: Annotated[AuthSettings, Depends(auth_settings)],
) -> AuthenticationService:
    return AuthenticationService(SqlAlchemyUserRepository(session),
                                 SqlAlchemySessionRepository(session), settings)


def onboarding_service(
        session: Annotated[AsyncSession, Depends(database_session)],
) -> OnboardingService:
    return OnboardingService(SqlAlchemyCandidateProfileRepository(session),
                             SqlAlchemySearchProfileRepository(session),
                             SqlAlchemyUserRepository(session))


def country_packs(request: Request) -> CountryPackRegistry:
    """The country packs, loaded once per application and cached like the engine.

    A pack is YAML on disk, so re-reading it per request would put three file reads
    in front of every company query for configuration that cannot change while the
    process runs. Cached on `app.state` rather than in a module global for the same
    reason the engine is: two applications in one process — which is what the test
    suite is — must not share one.
    """
    existing = getattr(request.app.state, COUNTRY_PACKS_ATTRIBUTE, None)
    if isinstance(existing, CountryPackRegistry):
        return existing
    loaded = build_country_packs()
    setattr(request.app.state, COUNTRY_PACKS_ATTRIBUTE, loaded)
    return loaded


def company_directory_service(
        session: Annotated[AsyncSession, Depends(database_session)],
) -> CompanyDirectoryService:
    """The read side of the company directory. No clock, no orchestrator."""
    return CompanyDirectoryService(SqlAlchemyCompanyRepository(session),
                                   SqlAlchemyCareerSiteRepository(session),
                                   SqlAlchemyCompanyDiscoveryRepository(session))


def company_discovery_service(
        session: Annotated[AsyncSession, Depends(database_session)],
        packs: Annotated[CountryPackRegistry, Depends(country_packs)],
        instant: Annotated[datetime, Depends(now)],
) -> CompanyDiscoveryService:
    """A discovery pass, composed for this request.

    Built per request rather than once per process because the providers and the
    resolution service both need this request's session, and a cached composition
    would hold a session that closed at the end of the last one.

    Three decisions are made here and nowhere else, which is what §17 and §18 ask
    for — the orchestrator names no provider and the services take what they are
    given:

    - **the clock is this request's instant**, so a provider's `discovered_at` and
      the row the resolution service writes for it cannot disagree about when the
      pass happened;
    - **the posting lister is `list_recent`**, bound as a callable so the
      posting-derived provider still cannot reach the rest of the repository;
    - **manual seeds count as confirmed aliases** (§2): an operator writing "LOGITECH
      is Logitech" is the manually confirmed alias identity resolution may trust,
      while a job board's spelling of the same name is only an observation.

    `manual_seeds` is empty: Phase 6 has no operator seed file, and the provider is
    registered anyway so a status page reports `NOTHING_CONFIGURED` rather than
    silence.
    """
    opportunities = SqlAlchemyOpportunityRepository(session)
    composition = build_company_discovery(
        opportunities=lambda limit: opportunities.list_recent(limit=limit),
        clock=lambda: instant)
    return CompanyDiscoveryService(
        composition.orchestrator,
        CompanyResolutionService(
            SqlAlchemyCompanyRepository(session),
            SqlAlchemyCareerSiteRepository(session),
            SqlAlchemyCompanyDiscoveryRepository(session),
            opportunities,
            packs=packs,
            confirmed_alias_sources=frozenset({MANUAL_SEED_PROVIDER})))


async def current_session(
        request: Request,
        service: Annotated[AuthenticationService, Depends(authentication_service)],
        settings: Annotated[AuthSettings, Depends(auth_settings)],
        instant: Annotated[datetime, Depends(now)],
) -> AuthenticatedSession:
    """Who is making this request — and, if it is unsafe, that it may.

    Raises 401 when there is no usable session, and 403 when an unsafe method
    arrives without a matching `X-CSRF-Token`. The order matters: the CSRF check
    needs the session's stored digest, so it can only happen after the cookie has
    resolved to a session.
    """
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        raise not_authenticated()
    authenticated = await service.authenticate(token=SecretStr(raw), now=instant)
    if authenticated is None:
        raise not_authenticated()
    if request.method.upper() not in SAFE_METHODS and not csrf_token_matches(
            authenticated.session, request.headers.get(CSRF_HEADER)):
        raise csrf_failed()
    return authenticated


def reject_cross_site_writes(request: Request) -> None:
    """Refuse an unsafe request the browser itself calls cross-site.

    Applied to the whole V2 router, so it covers `login` and `register` too — the
    two routes that have no session yet and therefore no CSRF token to check. This
    is what closes login CSRF, where an attacker's page signs a victim into the
    attacker's account.

    A missing header is allowed through: `Sec-Fetch-Site` is sent by current
    browsers and by nothing else, so requiring it would reject `curl`, the test
    suite and any server-to-server client. The double-submit check is what protects
    an authenticated write; this is the cheap outer layer
    (docs/AUTHENTICATION.md §CSRF).
    """
    if request.method.upper() in SAFE_METHODS:
        return
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        raise csrf_failed()


CurrentSession = Annotated[AuthenticatedSession, Depends(current_session)]
Now = Annotated[datetime, Depends(now)]
Auth = Annotated[AuthSettings, Depends(auth_settings)]
Authentication = Annotated[AuthenticationService, Depends(authentication_service)]
Onboarding = Annotated[OnboardingService, Depends(onboarding_service)]
Companies = Annotated[CompanyDirectoryService, Depends(company_directory_service)]
CompanyDiscovery = Annotated[CompanyDiscoveryService,
                             Depends(company_discovery_service)]
