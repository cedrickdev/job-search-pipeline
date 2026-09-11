"""A `Geocoder` that remembers, so a rate-limited provider is asked once (§23).

A decorator, not a base class: `CachingGeocoder` holds another `Geocoder` and
satisfies the same Protocol, so everything downstream — the enrichment service,
the CLI — is written against one port and cannot tell whether an answer came from
the table or the network.

**What is cached, and for how long.** Everything, and forever, *except* failures.
An address does not move: re-asking a provider for coordinates it already gave is
spending a rate limit on a question with a known answer. A `FAILED` result is the
opposite — it is not an answer at all, and §23 is explicit that a transient outage
must not be cached permanently — so it is stored with an expiry and re-asked after
it. `NOT_FOUND` is *not* a failure and does not expire, because "there is no such
place" is a real answer that tomorrow will repeat.

**The key is `(provider, country_hint, normalized_query)`.** All three, because
each changes the answer: two providers disagree, `"Lausanne"` and `"Lausanne"`
with `country="CH"` are different questions, and the normalization is what makes
`" Lausanne  "` and `"lausanne"` the same one. `normalize_geocoding_query` is
deliberately gentle about punctuation — see its docstring — so `Route 9` is not
folded into `Route9`.

**No candidate data reaches this table (§33).** That is a property of who calls
the enrichment pass, not of this class, and it is stated in
`backend.app.services.geo_enrichment`: the pass geocodes opportunities and company
locations, and a candidate profile's address is never a query it builds.
"""
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

from backend.app.domain.geo import (
    GeocodingOutcome,
    GeocodingRequest,
    GeocodingResult,
)
from backend.app.geo.contracts import (
    Geocoder,
    GeocoderProbe,
    GeocodingCacheEntry,
    GeocodingCacheRepository,
)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


# How long a `FAILED` answer is remembered before the provider is asked again.
# Long enough that a 200-address batch retried immediately does not hammer a
# provider that is already struggling, short enough that a fifteen-minute outage
# does not cost a day of enrichment.
FAILURE_RETRY_AFTER: Final[timedelta] = timedelta(hours=1)


class CachingGeocoder:
    """Consult the cache, ask the delegate, remember what it said.

    Satisfies `Geocoder` structurally. `provider` is the delegate's key, not a
    composed one, because the cache is not a provider: a coordinate resolved
    through this class was resolved by Nominatim, and `Location.geocoder` has to
    say so or the provenance §7 asks for would name a wrapper.
    """

    def __init__(self, *, delegate: Geocoder,
                 repository: GeocodingCacheRepository,
                 clock: Clock = utc_now,
                 failure_retry_after: timedelta = FAILURE_RETRY_AFTER) -> None:
        self._delegate = delegate
        self._repository = repository
        self._clock = clock
        self._failure_retry_after = failure_retry_after

    @property
    def provider(self) -> str:
        return self._delegate.provider

    async def probe(self) -> GeocoderProbe:
        """The delegate's probe. A cache is always configured; a provider may not be."""
        return await self._delegate.probe()

    async def geocode(self, request: GeocodingRequest) -> GeocodingResult:
        """The remembered answer if there is a fresh one, otherwise a fresh call.

        The result is rebuilt around the *incoming* request rather than returned
        as it was stored. They are equal on everything the key covers, and can
        differ on `language` — which changes how a provider renders a name but not
        where the place is. Returning the stored request would make a caller's
        result disagree with what it asked, for no benefit.
        """
        now = self._clock()
        key = request.normalized_query
        cached = await self._repository.get(self.provider, request.country, key)
        if cached is not None and cached.is_fresh_at(now):
            return cached.result.model_copy(update={"request": request})

        result = await self._delegate.geocode(request)
        await self._remember(result, now=now)
        return result

    async def _remember(self, result: GeocodingResult, *, now: datetime) -> None:
        """Store one answer under its normalized key.

        Writing through a `GeocodingCacheEntry` rather than straight to the
        repository is what keeps the expiry rule in one place: the model refuses
        an answer with a lifetime and a failure without one, so a repository
        cannot be handed an entry that contradicts §23.
        """
        expires_at = (now + self._failure_retry_after
                      if result.outcome is GeocodingOutcome.FAILED else None)
        await self._repository.put(GeocodingCacheEntry(
            provider=self.provider,
            country_hint=result.request.country,
            normalized_query=result.request.normalized_query,
            result=result,
            checked_at=now,
            expires_at=expires_at))


class InMemoryGeocodingCache:
    """A `GeocodingCacheRepository` backed by a dictionary.

    For tests and for a CLI run with no database. Not a fallback the production
    path can silently reach: `bootstrap` takes the repository explicitly, so a
    deployment that meant to cache in PostgreSQL and got this one would have had
    to write the class name.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], GeocodingCacheEntry] = {}

    async def get(self, provider: str, country_hint: str | None,
                  normalized_query: str) -> GeocodingCacheEntry | None:
        return self._entries.get((provider, country_hint or "", normalized_query))

    async def put(self, entry: GeocodingCacheEntry) -> None:
        self._entries[(entry.provider, entry.country_hint or "",
                       entry.normalized_query)] = entry

    def __len__(self) -> int:
        return len(self._entries)
