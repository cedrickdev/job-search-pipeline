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
from pathlib import Path
from typing import Annotated, Final

from fastapi import Depends, Request
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.api.errors import csrf_failed, not_authenticated
from backend.app.companies.bootstrap import build_company_discovery
from backend.app.companies.providers.manual_seed import (
    PROVIDER_KEY as MANUAL_SEED_PROVIDER,
)
from backend.app.core.settings import (
    AuthSettings,
    DatabaseSettings,
    DocumentSettings,
    LLMSecretSettings,
)
from backend.app.discovery.bootstrap import build_country_packs
from backend.app.documents import (
    DeterministicDocumentGenerator,
    LocalDocumentArtifactStore,
)
from backend.app.documents.guard import CandidateEvidenceGuard
from backend.app.infrastructure.database.engine import (
    create_async_database_engine,
    create_session_factory,
    session_scope,
)
from backend.app.llm.secrets import FernetSecretCipher, SecretCipher
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyCandidateDocumentRepository,
    SqlAlchemyCandidateProfileRepository,
    SqlAlchemyCareerSiteRepository,
    SqlAlchemyCompanyDiscoveryRepository,
    SqlAlchemyCompanyRepository,
    SqlAlchemyEligibilityResultRepository,
    SqlAlchemyLLMConnectionRepository,
    SqlAlchemyMatchEvaluationRepository,
    SqlAlchemyOpportunityRepository,
    SqlAlchemySearchProfileRepository,
    SqlAlchemySessionRepository,
    SqlAlchemyUserRepository,
)
from backend.app.services.assessment import AssessmentService
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
from backend.app.services.documents import DocumentService
from backend.app.services.evidence import CandidateEvidenceService
from backend.app.services.geo_search import GeoSearchService
from backend.app.services.llm_connections import LLMConnectionService
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
DOCUMENT_SETTINGS_ATTRIBUTE: Final[str] = "v2_document_settings"
LLM_KEY_SETTINGS_ATTRIBUTE: Final[str] = "v2_llm_key_settings"
LLM_CIPHER_ATTRIBUTE: Final[str] = "v2_llm_cipher"
SESSION_FACTORY_ATTRIBUTE: Final[str] = "v2_session_factory"
ENGINE_ATTRIBUTE: Final[str] = "v2_engine"
COUNTRY_PACKS_ATTRIBUTE: Final[str] = "v2_country_packs"

# The sentinel a cached `None` cipher is stored as, so "resolved to no cipher" is told
# apart from "not resolved yet" — a deployment with no master key must not re-read the
# environment on every request just because its resolved cipher is falsy.
_NO_CIPHER: Final = "no-cipher"


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


def geo_search_service(
        session: Annotated[AsyncSession, Depends(database_session)],
) -> GeoSearchService:
    """The read side of the geo explorer: three repositories, no clock.

    Composed here like every other service so the request layer wires the
    repositories over PostGIS and the route depends on one geo boundary rather than
    three. The saved-search read needs the profile repository too, so all three are
    handed in — the service never reaches for a session of its own.
    """
    return GeoSearchService(SqlAlchemyOpportunityRepository(session),
                            SqlAlchemyCompanyRepository(session),
                            SqlAlchemySearchProfileRepository(session))


def document_settings(request: Request) -> DocumentSettings:
    """Where rendered document artifacts live, resolved once and cached.

    Cached on `app.state` like the auth and database settings, and for the same
    reason: re-reading the environment per request would let a variable change
    under a running process and split one deployment's artifacts across two roots.
    """
    settings = getattr(request.app.state, DOCUMENT_SETTINGS_ATTRIBUTE, None)
    if isinstance(settings, DocumentSettings):
        return settings
    resolved = DocumentSettings.from_env()
    setattr(request.app.state, DOCUMENT_SETTINGS_ATTRIBUTE, resolved)
    return resolved


def evidence_service(
        session: Annotated[AsyncSession, Depends(database_session)],
) -> CandidateEvidenceService:
    """The write side of the candidate evidence store: one repository, no clock.

    The clock is handed to the methods that write, not the constructor, so a single
    request's timestamps agree — the same convention `onboarding_service` follows.
    """
    return CandidateEvidenceService(SqlAlchemyCandidateProfileRepository(session))


def document_service(
        session: Annotated[AsyncSession, Depends(database_session)],
        settings: Annotated[DocumentSettings, Depends(document_settings)],
) -> DocumentService:
    """The generate-guard-render-store-version workflow, composed for this request.

    Three repositories (the profile it builds from, the posting it targets, the
    documents it versions), the deterministic reference generator, the pure evidence
    guard, and the local artifact store rooted at the configured path. The generator
    and the guard are cheap, stateless value objects built per request rather than
    cached — the store resolves its root once here. No clock in the constructor; the
    route hands `now` to `generate`, so a version's timestamps agree.

    The reference generator is the Phase 10 default: it selects and reorders the
    candidate's own evidence and passes the guard by construction. A model-backed
    generator (Phase 11) would be swapped in here without the route or the service
    changing (docs/LLM_PROVIDER_ARCHITECTURE.md §3).
    """
    return DocumentService(
        SqlAlchemyCandidateProfileRepository(session),
        SqlAlchemyOpportunityRepository(session),
        SqlAlchemyCandidateDocumentRepository(session),
        DeterministicDocumentGenerator(),
        CandidateEvidenceGuard(),
        LocalDocumentArtifactStore(Path(settings.artifact_root)))


def llm_secret_settings(request: Request) -> LLMSecretSettings:
    """The master key for encrypting stored LLM credentials, resolved once and cached.

    Cached on `app.state` like the other settings, and for the same reason: re-reading
    `JOBSEARCH_LLM_SECRET_KEY` per request would let the key change under a running
    process and split a deployment's credentials across two ciphers, so a value stored
    a moment ago could no longer decrypt.
    """
    settings = getattr(request.app.state, LLM_KEY_SETTINGS_ATTRIBUTE, None)
    if isinstance(settings, LLMSecretSettings):
        return settings
    resolved = LLMSecretSettings.from_env()
    setattr(request.app.state, LLM_KEY_SETTINGS_ATTRIBUTE, resolved)
    return resolved


def llm_cipher(
        settings: Annotated[LLMSecretSettings, Depends(llm_secret_settings)],
        request: Request,
) -> SecretCipher | None:
    """The Fernet cipher, or `None` when the deployment configured no master key.

    Built once and cached on `app.state`: a `Fernet` compiles its key, and a
    deployment that manages API credentials makes that cost once per process rather
    than per request. `None` is a legitimate resolved value — a CLI-only deployment
    encrypts nothing — so it is cached under a sentinel to tell "resolved to no cipher"
    apart from "not resolved yet", and the connection service raises a clear
    `LLMSecretKeyUnavailable` only if a credential must actually be stored without one.
    """
    cached = getattr(request.app.state, LLM_CIPHER_ATTRIBUTE, None)
    if isinstance(cached, FernetSecretCipher):
        return cached
    if cached == _NO_CIPHER:
        return None
    resolved: SecretCipher | None = (
        FernetSecretCipher(settings.master_key) if settings.master_key else None)
    setattr(request.app.state, LLM_CIPHER_ATTRIBUTE,
            resolved if resolved is not None else _NO_CIPHER)
    return resolved


def llm_connection_service(
        session: Annotated[AsyncSession, Depends(database_session)],
        cipher: Annotated[SecretCipher | None, Depends(llm_cipher)],
) -> LLMConnectionService:
    """The write side of the LLM settings surface: one repository and the cipher.

    The cipher is what encrypts a submitted credential before it is stored; a CLI-only
    deployment passes `None` and the service refuses only the write that would need a
    key it cannot make. No clock in the constructor — the route hands `now` to each
    write, so a single request's `created_at`/`updated_at` agree. No `http_transport`:
    a healthcheck opens a real client in production, and a test overrides this whole
    dependency to inject a `MockTransport`-backed service rather than reach through it.
    """
    return LLMConnectionService(
        SqlAlchemyLLMConnectionRepository(session), cipher=cipher)


def assessment_service(
        session: Annotated[AsyncSession, Depends(database_session)],
        packs: Annotated[CountryPackRegistry, Depends(country_packs)],
) -> AssessmentService:
    """The match-and-eligibility orchestrator, composed for this request.

    Four repositories and the pack registry: the profile it scores, the posting it
    scores against, and the two verdict stores it writes to — kept apart because the
    two axes are separate records, not two fields of one. The country packs are the
    cached, read-only registry every other service shares; the engines read from a
    pack but never write one. No clock in the constructor — the route hands `now` to
    `evaluate`, so a single request's timestamps agree.
    """
    return AssessmentService(
        SqlAlchemyCandidateProfileRepository(session),
        SqlAlchemyOpportunityRepository(session),
        SqlAlchemyMatchEvaluationRepository(session),
        SqlAlchemyEligibilityResultRepository(session),
        packs)


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
GeoSearch = Annotated[GeoSearchService, Depends(geo_search_service)]
Assessment = Annotated[AssessmentService, Depends(assessment_service)]
Evidence = Annotated[CandidateEvidenceService, Depends(evidence_service)]
Documents = Annotated[DocumentService, Depends(document_service)]
LLMConnections = Annotated[LLMConnectionService, Depends(llm_connection_service)]
