"""The geocoding cache table through an `AsyncSession`.

The only repository contract implemented outside `sqlalchemy_repositories.py`,
and the reason is who reads it: the cache is an implementation detail of
`CachingGeocoder` and of nothing else. Keeping it beside the contract it serves —
rather than in the general repository module — is what stops an application
service from reading the cache directly, which is exactly the coupling that turns
a cache into a second source of truth.

The row↔model conversion also lives here rather than in `mappers.py`, for a
layering reason worth writing down: `mappers.py` converts rows into *domain*
models, while a `GeocodingCacheEntry` belongs to the geo package's contract. This
module is the adapter *for* that contract, so it is the one place allowed to know
both sides.
"""
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.domain.common import GeoPoint
from backend.app.domain.geo import (
    GeocodedPlace,
    GeocodingOutcome,
    GeocodingRequest,
    GeocodingResult,
)
from backend.app.domain.identifiers import geocoding_cache_entry_id
from backend.app.infrastructure.database.models import GeocodingCacheRow

if TYPE_CHECKING:  # pragma: no cover - compile-time conformance, never executed
    from backend.app.geo.contracts import GeocodingCacheEntry


class SqlAlchemyGeocodingCacheRepository:
    """`GeocodingCacheRepository` over an `AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, provider: str, country_hint: str | None,
                  normalized_query: str) -> "GeocodingCacheEntry | None":
        """The remembered answer, or `None` when absent or expired.

        An expired `FAILED` row is treated as absent rather than returned, so the
        decorator re-asks the provider for exactly such an entry and the freshness
        rule lives in one place. The comparison is UTC — both columns are
        `TIMESTAMPTZ`, and the domain refuses naive datetimes at the model
        boundary.
        """
        identifier = geocoding_cache_entry_id(provider, country_hint or "",
                                             normalized_query)
        row = await self._session.get(GeocodingCacheRow, identifier)
        if row is None:
            return None
        entry = _entry_from_row(row)
        if entry.expires_at is not None and entry.expires_at <= datetime.now(UTC):
            return None
        return entry

    async def put(self, entry: "GeocodingCacheEntry") -> None:
        """Upsert one remembered answer under its derived key (§23).

        The primary key is uuid5 over `(provider, country_hint, normalized_query)`,
        so a re-asked question *updates* the existing row instead of inserting a
        second one. The idempotency is in the key itself: no prior read decides
        insert-vs-update, and two processes answering the same question converge
        on one row — the later `checked_at` wins, and the table stays
        one-row-per-question.
        """
        identifier = geocoding_cache_entry_id(
            entry.provider, entry.country_hint or "", entry.normalized_query)
        row = await self._session.get(GeocodingCacheRow, identifier)
        if row is None:
            self._session.add(GeocodingCacheRow(
                id=identifier,
                provider=entry.provider,
                country_hint=entry.country_hint or "",
                normalized_query=entry.normalized_query,
                outcome=entry.result.outcome,
                place=_document(entry.result.place, default={}),
                alternatives=_documents(entry.result.alternatives),
                detail=entry.result.detail,
                checked_at=entry.checked_at,
                expires_at=entry.expires_at))
        else:
            row.outcome = entry.result.outcome
            row.place = _document(entry.result.place, default={})
            row.alternatives = _documents(entry.result.alternatives)
            row.detail = entry.result.detail
            row.checked_at = entry.checked_at
            row.expires_at = entry.expires_at
        await self._session.flush()

    async def evict_expired(self, now: datetime, *, limit: int = 100) -> int:
        """Delete the failures that outlived their retry window. Returns the count.

        Expired failures are already invisible to `get`, so this is housekeeping
        for table size rather than correctness — and it is a bounded statement a
        CLI may call, not a scheduled job, because §22 forbids adding a background
        worker for it.
        """
        result = await self._session.execute(
            select(GeocodingCacheRow.id)
            .where(GeocodingCacheRow.outcome.is_(GeocodingOutcome.FAILED),
                   GeocodingCacheRow.expires_at.is_not(None),
                   GeocodingCacheRow.expires_at <= now)
            .limit(limit))
        expired = list(result.scalars())
        if expired:
            await self._session.execute(
                delete(GeocodingCacheRow).where(GeocodingCacheRow.id.in_(expired)))
        return len(expired)


def _document(place: GeocodedPlace | None, *, default: Any) -> Any:
    """A `GeocodedPlace` as the JSONB document the column stores.

    `model_dump` renders enums as their values and the point as a two-float
    object, which is what `jsonb` demands. Reading back goes through
    `GeocodedPlace`'s own validators, so a document a future field invalidates is
    refused loudly rather than silently ignored.
    """
    return place.model_dump() if place is not None else default


def _documents(places: tuple[GeocodedPlace, ...]) -> Any:
    """The alternatives payload, as JSONB documents."""
    return [place.model_dump() for place in places]


def _entry_from_row(row: GeocodingCacheRow) -> "GeocodingCacheEntry":
    """The row as the entry the contract speaks.

    The request is rebuilt from the *key parts*, not from a stored copy: the
    normalized query is the key's text half, and `GeocodingRequest`'s
    normalization is idempotent, so the rebuilt request normalizes to exactly the
    value it was filed under — which is the property
    `GeocodingCacheEntry._the_entry_is_filed_under_what_it_answers` checks.
    """
    from backend.app.geo.contracts import GeocodingCacheEntry

    request = GeocodingRequest(query=row.normalized_query,
                               country=row.country_hint or None)
    return GeocodingCacheEntry(
        provider=row.provider,
        country_hint=row.country_hint or None,
        normalized_query=row.normalized_query,
        result=GeocodingResult(
            request=request,
            provider=row.provider,
            outcome=row.outcome,
            place=_place_from(row.place),
            alternatives=tuple(
                place for place in (_place_from(document)
                                    for document in row.alternatives)
                if place is not None),
            detail=row.detail),
        checked_at=row.checked_at,
        expires_at=row.expires_at)


def _place_from(document: Any) -> GeocodedPlace | None:
    """One JSONB document as a `GeocodedPlace`, or `None` if it cannot be read.

    Returns `None` rather than raising so one unreadable document costs one
    alternative and not the whole entry — the same politeness the Nominatim
    adapter extends to a malformed entry in an otherwise good response.
    """
    if not isinstance(document, dict):
        return None
    point = document.get("point")
    if isinstance(point, dict):
        document = {**document, "point": GeoPoint(**point)}
    try:
        return GeocodedPlace.model_validate(document)
    except ValueError:
        return None
