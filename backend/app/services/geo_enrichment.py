"""Geographic enrichment: the only place in Phase 7 that resolves locations (§21).

A pass walks a bounded batch of rows whose `Location` has no coordinates, asks the
`Geocoder` port about each, and writes back the ones it can improve. Three rules
shape everything below, and each is a phase-order requirement rather than a
preference:

**§7 — a stronger coordinate is never replaced.** The decision is
`Location.outranks`: a `MANUAL` or `SOURCE_PROVIDED` point always beats a geocoded
one, and between two geocoded points the confidence decides. So a row that
already carries a point is *skipped*, not re-asked — which is what makes a
second pass over the same batch a no-op (§39's idempotency) rather than a
slow way to degrade data.

**§33 — no candidate location is ever a query.** The store this service reads is
scoped to `opportunities` and `company_locations` by construction: the
`LocationStore` seam has no method that could reach a candidate profile, and the
SQLAlchemy implementation behind it selects from exactly those two tables. A
candidate's home address normalized into a shared cache key would be the leak §33
names, and the way to prevent a leak is to remove the path to it.

**§22 — enrichment happens when somebody runs the command.** Nothing here is
wired to startup, to a scheduler or to a request handler. The CLI builds this
service, hands it a bounded `limit`, and prints the report; a run that crashes
leaves the rows it never reached exactly as it found them, because writes go
through the caller's transaction.

**What a run reports.** `GeoEnrichmentReport` counts four things: locations
attempted, resolved, skipped (already resolved or outranked), and the geocoding
outcomes — so an operator can tell "200 rows, all cached" from "200 rows, the
provider is down" without reading a log. The report names rows by id and table,
never by the address text: an address is candidate-adjacent data, and a report is
a thing that gets pasted into tickets.
"""
from collections.abc import Callable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from backend.app.domain.base import DomainModel, NonEmptyStr, UtcDatetime
from backend.app.domain.common import Location
from backend.app.domain.geo import (
    GeocodingOutcome,
    GeocodingRequest,
    geocoding_query_for,
)
from backend.app.geo.contracts import Geocoder

# How many rows one pass offers to enrich by default. Small on purpose: a
# rate-limited public geocoder answers roughly one question per second, so a
# default in the hundreds is a default that gets a deployment blocked.
DEFAULT_ENRICHMENT_LIMIT = 50


class EnrichedTable(StrEnum):
    """Which table an enriched row came from.

    A member rather than a free string so a report can be grouped without
    trusting its input, and so the store's contract cannot grow a third table
    silently — adding one here is the moment to re-ask whether it should be
    geocoded at all (a third table holding candidate data would be §33's answer:
    no).
    """

    OPPORTUNITIES = "opportunities"
    COMPANY_LOCATIONS = "company_locations"


class LocationRef(DomainModel):
    """Where one enrichable location lives, as the report and store name it."""

    table: EnrichedTable
    row_id: NonEmptyStr
    company_id: NonEmptyStr | None = None


class GeoEnrichmentReport(DomainModel):
    """What one pass did, as counts and outcomes rather than as a log.

    `outcomes` counts every geocoding answer the pass received — including for
    rows it then skipped, because "the provider says NOT_FOUND" is the reason an
    operator re-runs with a country hint rather than a mystery.
    """

    started_at: UtcDatetime
    finished_at: UtcDatetime
    attempted: int
    resolved: int
    skipped: int
    outcomes: dict[GeocodingOutcome, int] = {}
    provider: NonEmptyStr | None = None

    @property
    def unchanged(self) -> int:
        """Rows looked at and left alone — the idempotency evidence (§39)."""
        return self.attempted - self.resolved

    @property
    def is_complete(self) -> bool:
        """Whether every provider call produced a definitive answer.

        `NOT_FOUND` and `AMBIGUOUS` are complete answers: retrying unchanged input
        would repeat them. Only `FAILED` means the provider did not answer and the
        operator should retry after fixing configuration or availability.
        """
        return self.outcomes.get(GeocodingOutcome.FAILED, 0) == 0


@runtime_checkable
class LocationStore(Protocol):
    """The enrichable locations, read and written in domain terms.

    A seam rather than the two repositories, because the pass is over *locations*,
    not over opportunities or companies: a repository-typed service would couple
    enrichment to everything else those repositories can do, and the two-table
    scope §33 requires would become a convention to remember instead of the shape
    of the interface.
    """

    async def list_unresolved(self, *, limit: int) -> \
            Sequence[tuple[LocationRef, Location]]: ...

    async def replace(self, ref: LocationRef, location: Location) -> None: ...


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    from datetime import UTC
    return datetime.now(UTC)


class GeoEnrichmentService:
    """One bounded pass over unresolved locations, through the `Geocoder` port."""

    def __init__(self, *, geocoder: Geocoder, store: LocationStore,
                 clock: Clock = utc_now) -> None:
        self._geocoder = geocoder
        self._store = store
        self._clock = clock

    async def run(self, *, limit: int = DEFAULT_ENRICHMENT_LIMIT,
                  country: str | None = None) -> GeoEnrichmentReport:
        """Enrich up to `limit` unresolved locations; report what happened.

        `country` narrows the geocoding questions, which is the difference
        between `AMBIGUOUS` and `MATCHED` for most of a country pack's data —
        "Lausanne" alone has several answers, "Lausanne" with `country="CH"` has
        one. The CLI passes the pack's code; a `None` asks the provider bare and
        accepts the ambiguity that follows.

        One row's failure is that row's outcome, never the run's exception: the
        geocoder port never raises for a provider problem (§4), so nothing here
        needs a `try` to keep going — and a row whose write the store refuses
        *does* stop the pass, because a storage failure is not a geocoding
        outcome and pretending otherwise would lose rows silently.
        """
        started_at = self._clock()
        batch = await self._store.list_unresolved(limit=limit)
        outcomes: dict[GeocodingOutcome, int] = {}
        resolved = 0
        skipped = 0

        for ref, location in batch:
            query = geocoding_query_for(location)
            if query is None:
                # Nothing to ask: a location with no text and no point cannot be
                # helped by a geocoder, and sending an empty query would be a
                # cache key that matches everything.
                skipped += 1
                continue
            request = GeocodingRequest(query=query, country=country)
            result = await self._geocoder.geocode(request)
            outcomes[result.outcome] = outcomes.get(result.outcome, 0) + 1
            if not result.is_usable or result.place is None:
                skipped += 1
                continue
            place = result.place
            if location.outranks(place.to_location(
                    geocoder=self._geocoder.provider,
                    geocoded_at=self._clock())):
                # §7: the coordinates already there are better than anything this
                # provider could say. Reaching this branch means the row was
                # listed as unresolved and gained a point between `list_unresolved`
                # and now — a concurrent enrichment — and the concurrent winner
                # keeps it.
                skipped += 1
                continue
            await self._store.replace(ref, place.to_location(
                geocoder=self._geocoder.provider, geocoded_at=self._clock(),
                raw=location.raw))
            resolved += 1

        finished_at = self._clock()

        return GeoEnrichmentReport(
            started_at=started_at, finished_at=finished_at, attempted=len(batch),
            resolved=resolved, skipped=skipped, outcomes=outcomes,
            provider=self._geocoder.provider)
