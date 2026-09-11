"""Composition: build the geocoder registry once, hand it over.

The only module in `backend.app.geo` that names a provider class, and the reason
`services.geo_enrichment` and `cli.geocode` can be written against the port
without knowing which provider is plugged in.

Nothing here makes a request. Building a provider binds a callable and a piece of
metadata; the configured-Nominatim provider does not even read its file until a
pass runs.
"""
from typing import NamedTuple

from backend.app.geo.caching import CachingGeocoder, InMemoryGeocodingCache
from backend.app.geo.contracts import (
    Geocoder,
    GeocoderSettings,
    GeocodingCacheRepository,
    HttpGet,
)
from backend.app.geo.nominatim import NominatimGeocoder


class GeoDiscovery(NamedTuple):
    """The two objects a process holds for its lifetime.

    Returned together because the enrichment service and the CLI hold the
    geocoder privately: a caller that also needs to answer "which geocoders
    exist?" would otherwise have to build a second, divergent one.
    """

    geocoder: Geocoder
    settings: GeocoderSettings


def build_geocoder(
    *,
    settings: GeocoderSettings,
    http_get: HttpGet,
    api_key: str | None = None,
    repository: GeocodingCacheRepository | None = None,
) -> GeoDiscovery:
    """A ready geocoder stack: cache over delegate, delegate over HTTP.

    A delegation rather than a singleton, so a test can inject a faked delegate
    and a cached repository without touching the production path.

    `repository` defaults to an in-memory dictionary. For production use, pass a
    repository that reads and writes the `geocoding_cache` table.
    """
    delegate = NominatimGeocoder(
        settings=settings, http_get=http_get, api_key=api_key
    )
    if repository is None:
        repository = InMemoryGeocodingCache()
    geocoder = CachingGeocoder(delegate=delegate, repository=repository)
    return GeoDiscovery(geocoder=geocoder, settings=settings)
