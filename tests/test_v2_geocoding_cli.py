"""The Phase 7 geocoding command is truthful, offline-testable, and safe."""
from datetime import UTC, datetime

import httpx
import pytest

from backend.app.cli.geocode import (
    EXIT_INCOMPLETE,
    EXIT_OK,
    EXIT_UNUSABLE,
    GeocoderCompositionError,
    HttpxGetter,
    _format_report,
    _run,
)
from backend.app.domain.common import Location
from backend.app.domain.geo import GeocodingOutcome
from backend.app.geo.nominatim import nominatim_settings
from backend.app.repositories.sqlalchemy_location_store import SqlLocationStore
from backend.app.services.geo_enrichment import EnrichedTable, GeoEnrichmentReport, LocationRef
from tests.v2_builders import LAUSANNE, a_company_location, an_opportunity


NOW = datetime(2026, 9, 11, 12, 30, tzinfo=UTC)


def a_report(
    *,
    attempted: int = 1,
    resolved: int = 0,
    outcomes: dict[GeocodingOutcome, int] | None = None,
) -> GeoEnrichmentReport:
    return GeoEnrichmentReport(
        started_at=NOW,
        finished_at=NOW,
        attempted=attempted,
        resolved=resolved,
        skipped=attempted - resolved,
        outcomes={} if outcomes is None else outcomes,
        provider="nominatim",
    )


def test_listing_providers_is_offline_and_reports_missing_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GEOCODER_USER_AGENT", raising=False)

    assert _run(["--list-providers"]) == EXIT_OK

    captured = capsys.readouterr()
    assert "nominatim" in captured.out
    assert "not configured" in captured.out
    assert captured.err == ""


def test_enrichment_refuses_to_run_without_a_distinguishing_user_agent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GEOCODER_USER_AGENT", raising=False)

    assert _run([]) == EXIT_UNUSABLE

    captured = capsys.readouterr()
    assert "GEOCODER_USER_AGENT" in captured.err
    assert captured.out == ""


def test_an_unknown_provider_is_a_composition_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GEOCODER_USER_AGENT", "job-search-pipeline/test")

    assert _run(["--provider", "unknown"]) == EXIT_UNUSABLE

    captured = capsys.readouterr()
    assert "unknown" in captured.err
    assert "provider" in captured.err


@pytest.mark.asyncio
async def test_http_getter_applies_provider_identity_and_timeout() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    settings = nominatim_settings(
        user_agent="job-search-pipeline/operator@example.test",
        timeout_seconds=7.5,
    )
    getter = HttpxGetter(settings=settings, transport=httpx.MockTransport(answer))

    status, payload = await getter("https://example.test/search", {"q": "Lausanne"})

    assert (status, payload) == (200, [])
    assert seen[0].headers["User-Agent"] == settings.user_agent
    assert seen[0].extensions["timeout"] == {
        "connect": 7.5,
        "read": 7.5,
        "write": 7.5,
        "pool": 7.5,
    }


def test_report_uses_one_unambiguous_utc_suffix() -> None:
    rendered = _format_report(a_report())

    assert "2026-09-11T12:30:00+00:00" in rendered
    assert "+00:00Z" not in rendered


def test_a_definitive_not_found_answer_is_still_a_complete_run() -> None:
    report = a_report(outcomes={GeocodingOutcome.NOT_FOUND: 1})

    assert report.is_complete


def test_a_provider_failure_makes_the_run_incomplete() -> None:
    report = a_report(outcomes={GeocodingOutcome.FAILED: 1})

    assert not report.is_complete
    assert EXIT_INCOMPLETE == 3


class FakeSession:
    def __init__(self, row: object) -> None:
        self.row = row
        self.added: list[object] = []
        self.flushed = False

    async def get(self, row_type: type[object], row_id: object) -> object:
        return self.row

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "row_factory"),
    (
        pytest.param(
            EnrichedTable.OPPORTUNITIES,
            lambda: __import__(
                "backend.app.infrastructure.database.mappers",
                fromlist=["opportunity_to_row"],
            ).opportunity_to_row(an_opportunity()),
            id="opportunity",
        ),
        pytest.param(
            EnrichedTable.COMPANY_LOCATIONS,
            lambda: __import__(
                "backend.app.infrastructure.database.mappers",
                fromlist=["company_location_to_row"],
            ).company_location_to_row(a_company_location()),
            id="company-location",
        ),
    ),
)
async def test_location_store_replaces_each_supported_row_type(
    table: EnrichedTable,
    row_factory: object,
) -> None:
    row = row_factory()
    session = FakeSession(row)
    store = SqlLocationStore(session)
    location = Location(country="CH", city="Lausanne", point=LAUSANNE)

    await store.replace(
        LocationRef(table=table, row_id=str(row.id)),
        location,
    )

    assert row.location_point == LAUSANNE
    assert session.added == [row]
    assert session.flushed


def test_composition_errors_are_safe_to_print() -> None:
    assert str(GeocoderCompositionError("missing configuration")) == \
        "missing configuration"
