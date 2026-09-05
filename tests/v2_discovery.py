# tests/v2_discovery.py
"""Shared constructors and fakes for the Phase 5 discovery tests.

Everything here exists so that a discovery test needs no socket, no YAML file and
no V1 module. `OpportunitySource` is a Protocol with three members, which is what
makes that cheap: `FakeSource` is thirty lines and the orchestrator cannot tell it
from a wrapped job board — the property §3 asks for, exercised rather than
asserted.

The one thing deliberately *not* faked is the Swiss pack. The registry and
orchestration tests build their own minimal `CountryPack` so a failure names the
field under test, but `test_v2_country_packs.py` loads the real
`country_packs/ch/` files: a fake pack cannot prove that the pack an operator
edits is loadable, and that is one of §18's requirements.
"""
from datetime import UTC, datetime

from backend.app.discovery.capabilities import SourceCapability
from backend.app.discovery.contracts import (
    DiscoveryMetrics,
    DiscoveryRequest,
    DiscoveryResult,
    SourceHealth,
    SourceHealthStatus,
    SourceMetadata,
    SourceType,
)
from backend.app.domain.identifiers import new_search_profile_id, new_user_id
from backend.app.domain.search import CountrySearchArea, SearchProfile
from country_packs.contracts import (
    CountryPack,
    PackMetadata,
    SourceBinding,
)

NOW = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


def frozen_clock(instant: datetime = NOW):
    """A `Clock` that never moves, so a report's timestamps are assertable."""
    return lambda: instant


def a_metadata(source_key: str = "fake_board", **overrides) -> SourceMetadata:
    """Metadata for a source that claims a keyword search and nothing else.

    Minimal on purpose: every test that cares about a capability passes it, so a
    test asserting on `LOCATION_SEARCH` reads as being about `LOCATION_SEARCH`.
    """
    fields = {
        "source_key": source_key,
        "display_name": f"{source_key} (fake)",
        "source_type": SourceType.JOB_BOARD,
        "capabilities": frozenset({SourceCapability.KEYWORD_SEARCH}),
    }
    fields.update(overrides)
    return SourceMetadata(**fields)


def a_request(**overrides) -> DiscoveryRequest:
    fields = {"country": "CH", "keywords": ("developpeur",), "locations": ("Lausanne",)}
    fields.update(overrides)
    return DiscoveryRequest(**fields)


def a_search_profile(**overrides) -> SearchProfile:
    """A profile that reaches Switzerland and nowhere else."""
    fields = {
        "id": new_search_profile_id(),
        "user_id": new_user_id(),
        "name": "profile under test",
        "areas": (CountrySearchArea(country="CH", label="Lausanne"),),
        "queries": ("developpeur",),
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return SearchProfile(**fields)


def a_pack(country: str = "CH", *bindings: SourceBinding, **overrides) -> CountryPack:
    """A pack with just enough metadata to be valid, and the bindings given."""
    fields = {
        "metadata": PackMetadata(
            country=country,
            display_name=f"{country} (test)",
            default_locale="fr-CH",
            locales=("fr-CH",),
            currency="CHF",
            languages=("fr",),
            timezone="Europe/Zurich",
            full_time_weekly_hours=42.0),
        "sources": bindings,
    }
    fields.update(overrides)
    return CountryPack(**fields)


def a_health(source_key: str = "fake_board",
             status: SourceHealthStatus = SourceHealthStatus.HEALTHY,
             **overrides) -> SourceHealth:
    fields = {"source_key": source_key, "status": status, "checked_at": NOW}
    if status is not SourceHealthStatus.HEALTHY:
        fields["reason"] = "SOURCE_UNAVAILABLE"
        fields["detail"] = "the source did not answer"
    fields.update(overrides)
    return SourceHealth(**fields)


def a_result(source_key: str = "fake_board", *opportunities, **overrides
             ) -> DiscoveryResult:
    """A result whose metrics agree with its payload, as the model requires.

    Written as a helper because `DiscoveryResult` refuses a mismatch: a test that
    had to keep `opportunities_returned` in step by hand would eventually assert on
    a hand-maintained number instead of on behaviour.
    """
    fields = {
        "health": a_health(source_key),
        "opportunities": opportunities,
        "metrics": DiscoveryMetrics(requests_made=1,
                                    postings_seen=len(opportunities),
                                    opportunities_returned=len(opportunities)),
    }
    fields.update(overrides)
    return DiscoveryResult(**fields)


class FakeSource:
    """An `OpportunitySource` that answers from a script instead of a network.

    Three behaviours, which are the three the orchestrator has to tell apart: it
    returns a result, it raises (the contract violation §14 says must be contained),
    or it blocks until released (how a concurrency bound is observed without
    sleeping). `requests` records what it was asked, which is how the
    `SearchProfile` → `DiscoveryRequest` mapping is checked end to end.
    """

    def __init__(self, metadata: SourceMetadata | None = None, *,
                 result: DiscoveryResult | None = None,
                 error: BaseException | None = None,
                 health: SourceHealth | None = None,
                 on_discover=None) -> None:
        self._metadata = metadata if metadata is not None else a_metadata()
        self._result = result
        self._error = error
        self._health = health
        self._on_discover = on_discover
        self.requests: list[DiscoveryRequest] = []
        self.healthchecks = 0

    @property
    def metadata(self) -> SourceMetadata:
        return self._metadata

    async def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        self.requests.append(request)
        if self._on_discover is not None:
            await self._on_discover(self)
        if self._error is not None:
            raise self._error
        if self._result is not None:
            return self._result
        return a_result(self._metadata.source_key)

    async def healthcheck(self) -> SourceHealth:
        self.healthchecks += 1
        if self._error is not None:
            raise self._error
        return self._health if self._health is not None \
            else a_health(self._metadata.source_key)
