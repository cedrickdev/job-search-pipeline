"""`python -m backend.app.cli.geocode` — enrich stored shared locations."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.settings import DatabaseSettings
from backend.app.geo.bootstrap import GeoDiscovery, build_geocoder
from backend.app.geo.contracts import GeocoderProbe, GeocoderSettings
from backend.app.geo.nominatim import PROVIDER_KEY, nominatim_settings
from backend.app.infrastructure.database.engine import (
    create_async_database_engine,
    create_session_factory,
    session_scope,
)
from backend.app.repositories.sqlalchemy_geocoding_cache import (
    SqlAlchemyGeocodingCacheRepository,
)
from backend.app.repositories.sqlalchemy_location_store import SqlLocationStore
from backend.app.services.geo_enrichment import GeoEnrichmentReport, GeoEnrichmentService

EXIT_OK: Final[int] = 0
EXIT_UNUSABLE: Final[int] = 1
EXIT_INCOMPLETE: Final[int] = 3

USER_AGENT_ENV: Final[str] = "GEOCODER_USER_AGENT"
CONTACT_EMAIL_ENV: Final[str] = "GEOCODER_CONTACT_EMAIL"
API_KEY_NAME_ENV: Final[str] = "GEOCODER_API_KEY"
SUPPORTED_PROVIDERS: Final[tuple[str, ...]] = (PROVIDER_KEY,)


class GeocoderCompositionError(ValueError):
    """The command cannot build the requested provider safely."""


class HttpxGetter:
    """The narrow `HttpGet` port over an identified, bounded HTTPX client."""

    def __init__(self, *, settings: GeocoderSettings,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport

    async def __call__(self, url: str,
                       params: Mapping[str, str]) -> tuple[int, Any]:
        async with httpx.AsyncClient(
            headers={"User-Agent": self._settings.user_agent,
                     "Accept": "application/json"},
            timeout=self._settings.timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.get(url, params=params)
        return response.status_code, response.json()


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {number}")
    return number


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.cli.geocode",
        description="Enrich unresolved opportunity and company locations with "
                    "coordinates. Candidate locations are never queried.",
        epilog=f"Set {USER_AGENT_ENV} to a distinguishing deployment identifier.",
    )
    parser.add_argument(
        "--provider", default=PROVIDER_KEY, metavar="KEY",
        help=f"provider key to use (default {PROVIDER_KEY})",
    )
    parser.add_argument(
        "--country", default="CH", metavar="XX",
        help="ISO 3166-1 alpha-2 hint sent with each query (default CH)",
    )
    parser.add_argument(
        "--limit", type=_positive, default=50, metavar="N",
        help="maximum total rows to enrich (default 50)",
    )
    parser.add_argument(
        "--list-providers", action="store_true",
        help="print provider availability and exit without a request",
    )
    parser.add_argument(
        "--healthcheck", action="store_true",
        help="validate provider configuration without a request",
    )
    return parser.parse_args(argv)


def _format_report(report: GeoEnrichmentReport) -> str:
    lines = [
        f"enrichment started {report.started_at.isoformat()}",
        f"  finished        {report.finished_at.isoformat()}",
        f"  provider        {report.provider or 'none'}",
        f"  locations       {report.attempted}",
        f"  resolved        {report.resolved}",
        f"  skipped         {report.skipped}",
        f"  unchanged       {report.unchanged}",
        f"  complete        {'yes' if report.is_complete else 'no'}",
        "  outcomes:",
    ]
    for outcome, count in report.outcomes.items():
        lines.append(f"    {outcome.value:<15} {count:>4}")
    return "\n".join(lines)


def _format_health(records: Sequence[GeocoderProbe]) -> str:
    if not records:
        return "no provider answered: nothing is registered"
    lines = ["provider health:"]
    for record in records:
        status = "configured" if record.configured else "not configured"
        lines.append(f"  {record.provider:<11} {status:<20}{record.detail or ''}")
    return "\n".join(lines)


def _provider_listing() -> str:
    configured = bool(os.getenv(USER_AGENT_ENV, "").strip())
    status = "configured" if configured else f"not configured ({USER_AGENT_ENV} unset)"
    return f"registered geocoding providers:\n  {PROVIDER_KEY:<11} {status}"


def _settings_for(provider: str) -> GeocoderSettings:
    if provider not in SUPPORTED_PROVIDERS:
        raise GeocoderCompositionError(
            f"unknown geocoding provider {provider!r}; choose one of "
            f"{', '.join(SUPPORTED_PROVIDERS)}"
        )
    user_agent = os.getenv(USER_AGENT_ENV, "").strip()
    if not user_agent:
        raise GeocoderCompositionError(
            f"{USER_AGENT_ENV} environment variable is required"
        )
    contact_email = os.getenv(CONTACT_EMAIL_ENV, "").strip() or None
    api_key_env = os.getenv(API_KEY_NAME_ENV, "").strip() or None
    return nominatim_settings(
        user_agent=user_agent,
        contact_email=contact_email,
        api_key_env=api_key_env,
        api_key_param="api_key" if api_key_env else None,
    )


def _api_key(settings: GeocoderSettings) -> str | None:
    if settings.api_key_env is None:
        return None
    return os.getenv(settings.api_key_env)


def _build_discovery(args: argparse.Namespace) -> GeoDiscovery:
    settings = _settings_for(args.provider)
    return build_geocoder(
        settings=settings,
        http_get=HttpxGetter(settings=settings),
        api_key=_api_key(settings),
    )


async def _run_enrichment(discovery: GeoDiscovery,
                          args: argparse.Namespace) -> GeoEnrichmentReport:
    engine = create_async_database_engine(DatabaseSettings.from_env())
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            return await _enrich_in_session(discovery, args, session)
    finally:
        await engine.dispose()


async def _enrich_in_session(discovery: GeoDiscovery, args: argparse.Namespace,
                             session: AsyncSession) -> GeoEnrichmentReport:
    settings = discovery.settings
    persistent = build_geocoder(
        settings=settings,
        http_get=HttpxGetter(settings=settings),
        api_key=_api_key(settings),
        repository=SqlAlchemyGeocodingCacheRepository(session),
    )
    service = GeoEnrichmentService(
        geocoder=persistent.geocoder,
        store=SqlLocationStore(session),
    )
    return await service.run(limit=args.limit, country=args.country)


def _run(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.list_providers:
        print(_provider_listing())
        return EXIT_OK

    try:
        discovery = _build_discovery(args)
    except (GeocoderCompositionError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE

    try:
        if args.healthcheck:
            probe = asyncio.run(discovery.geocoder.probe())
            print(_format_health([probe]))
            return EXIT_OK if probe.configured else EXIT_INCOMPLETE

        report = asyncio.run(_run_enrichment(discovery, args))
        print(_format_report(report))
        return EXIT_OK if report.is_complete else EXIT_INCOMPLETE
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INCOMPLETE


if __name__ == "__main__":  # pragma: no cover - exercised through _run()
    raise SystemExit(_run())
