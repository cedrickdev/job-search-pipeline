"""Nominatim: the one concrete geocoder Phase 7 ships (§5).

Chosen because it proves the abstraction under the hardest constraint. Nominatim
is a *public* service with a published usage policy — one request per second, a
distinguishing User-Agent, no bulk downloads — so an adapter for it has to carry
configuration for a contact address and a rate limit, which is exactly what a
commercial provider's adapter would need for an API key. An adapter written
against an unlimited paid API would have proved less.

Nothing here is Nominatim-specific above the parsing line. The shape of the
adapter — build parameters, call the injected `HttpGet`, classify, map precision,
return a `GeocodingResult` — is what a Google, Mapbox or HERE adapter would be,
and the three functions that differ are grouped below the fold.

**Three decisions worth stating.**

*Ambiguity is detected, not resolved (§6).* Nominatim is asked for several results
and, when two of them are equally plausible, the answer is `AMBIGUOUS` with both
candidates rather than the first one. "Equally plausible" is defined precisely
below and is deliberately narrow: two places of the same `addresstype` in
different administrative areas.

*Precision comes from `addresstype`, not from the coordinate's decimal places
(§32).* A city centroid is reported to seven decimals like everything else;
believing that would be the exact mistake §32 names.

*Confidence is three buckets from `importance`, and the mapping says why.*
Nominatim's `importance` is a Wikipedia-derived popularity score, not a
probability, so it is used only for ordering within a bucket that
`addresstype` already decided.
"""
from collections.abc import Mapping, Sequence
from typing import Any, Final

from backend.app.domain.base import CountryCode
from backend.app.domain.common import (
    GeocodingConfidence,
    GeoPoint,
    LocationPrecision,
)
from backend.app.domain.geo import (
    GeocodedPlace,
    GeocodingOutcome,
    GeocodingRequest,
    GeocodingResult,
)
from backend.app.geo import failures
from backend.app.geo.contracts import (
    GeocoderProbe,
    GeocoderSettings,
    HttpGet,
)

# The public instance. Overridable through `GeocoderSettings.base_url` precisely
# because a deployment doing more than occasional lookups is expected to run its
# own — which is what the usage policy asks for, and what makes this adapter
# usable at volume without violating it.
NOMINATIM_BASE_URL: Final[str] = "https://nominatim.openstreetmap.org"

PROVIDER_KEY: Final[str] = "nominatim"

_SEARCH_PATH: Final[str] = "/search"
_OK: Final[int] = 200

# `addresstype` → how precisely the coordinates locate anything (§32). Nominatim
# reports the kind of object it matched, which is the only honest source for this:
# a `city` result is a centroid whatever its decimal places suggest.
#
# Unlisted types fall back to `CITY` rather than `EXACT_ADDRESS`, and the
# direction is the conservative one — overstating precision is the §32 failure,
# understating it only widens a disc a UI draws.
_PRECISION_BY_TYPE: Final[dict[str, LocationPrecision]] = {
    "building": LocationPrecision.EXACT_ADDRESS,
    "house": LocationPrecision.EXACT_ADDRESS,
    "amenity": LocationPrecision.EXACT_ADDRESS,
    "office": LocationPrecision.EXACT_ADDRESS,
    "place": LocationPrecision.EXACT_ADDRESS,
    "postcode": LocationPrecision.POSTAL_CODE,
    "city": LocationPrecision.CITY,
    "town": LocationPrecision.CITY,
    "village": LocationPrecision.CITY,
    "municipality": LocationPrecision.CITY,
    "suburb": LocationPrecision.CITY,
    "district": LocationPrecision.CITY,
    "county": LocationPrecision.REGION,
    "state": LocationPrecision.REGION,
    "province": LocationPrecision.REGION,
    "region": LocationPrecision.REGION,
    "country": LocationPrecision.COUNTRY,
}

# How precisely a result locates something → how much to trust the coordinates.
# The mapping is the point of the three-bucket scale: a street address is a
# building, a postcode is a neighbourhood and a country is a continent-sized
# guess, and §7's "do not let a weaker answer replace a stronger one" needs those
# to be comparable.
_CONFIDENCE_BY_PRECISION: Final[dict[LocationPrecision, GeocodingConfidence]] = {
    LocationPrecision.EXACT_ADDRESS: GeocodingConfidence.HIGH,
    LocationPrecision.POSTAL_CODE: GeocodingConfidence.MEDIUM,
    LocationPrecision.CITY: GeocodingConfidence.MEDIUM,
    LocationPrecision.REGION: GeocodingConfidence.LOW,
    LocationPrecision.COUNTRY: GeocodingConfidence.LOW,
}

# The `address` keys Nominatim uses for a settlement, in the order a display would
# prefer them. Several exist because an OSM place is tagged as exactly one of
# them and which one depends on population.
_CITY_KEYS: Final[tuple[str, ...]] = (
    "city", "town", "village", "municipality", "suburb")

_REGION_KEYS: Final[tuple[str, ...]] = ("state", "region", "county")


def nominatim_settings(*, user_agent: str, contact_email: str | None = None,
                       base_url: str = NOMINATIM_BASE_URL,
                       **overrides: Any) -> GeocoderSettings:
    """`GeocoderSettings` for Nominatim, with its two policy fields required.

    A named constructor rather than a default instance, because there is no
    correct default for `user_agent`: the usage policy asks for a string that
    identifies *this* deployment, and a shared constant would identify the
    project instead — which is how one misbehaving installation gets every
    installation blocked.
    """
    return GeocoderSettings(provider=PROVIDER_KEY, base_url=base_url,
                            user_agent=user_agent, contact_email=contact_email,
                            **overrides)


class NominatimGeocoder:
    """A `Geocoder` over Nominatim's `/search` endpoint.

    Satisfies the Protocol structurally; nothing here inherits from it, matching
    how `LocalCompanyProvider` relates to `CompanyDiscoveryProvider`.

    Holds no HTTP client. `http_get` is injected, which is what makes every §38
    test — success, ambiguous, not found, timeout, provider error, malformed
    response — a three-line fake instead of a mocked third-party library, and what
    makes it impossible for this class to contact a real service in a test run.
    """

    def __init__(self, *, settings: GeocoderSettings, http_get: HttpGet,
                 api_key: str | None = None) -> None:
        if settings.provider != PROVIDER_KEY:
            raise ValueError(
                f"NominatimGeocoder is {PROVIDER_KEY!r}; settings name "
                f"{settings.provider!r}, and the provider key is written into "
                "every coordinate's provenance")
        self._settings = settings
        self._http_get = http_get
        # Read once by the caller from `settings.api_key_env` and handed over, so
        # this class never touches `os.environ`. Nominatim needs none; the
        # parameter exists because the *port* has to work for providers that do,
        # and an adapter that could not accept one would not have proved that.
        self._api_key = api_key

    @property
    def provider(self) -> str:
        return self._settings.provider

    @property
    def settings(self) -> GeocoderSettings:
        return self._settings

    async def probe(self) -> GeocoderProbe:
        """Whether this geocoder can be called at all, without calling it.

        No request is made: a probe that hit the network would be a request the
        usage policy counts, run every time a CLI starts. What is checked is the
        one thing that is wrong in practice — a configured API key variable whose
        value never arrived.
        """
        if self._settings.api_key_env is not None and not self._api_key:
            return GeocoderProbe(
                provider=self.provider, configured=False,
                detail=f"{self._settings.api_key_env} is named in the geocoder "
                       "settings but is unset in this environment")
        return GeocoderProbe(provider=self.provider, configured=True)

    async def geocode(self, request: GeocodingRequest) -> GeocodingResult:
        """Ask Nominatim, and return a result whatever happens.

        The `except Exception` is intentional and is the contract: §21's bounded
        enrichment pass processes a batch, and one address that provokes an
        unanticipated error must be a `FAILED` row rather than the end of the run.
        `failures.code_for_exception` classifies by type; nothing from `str(exc)`
        reaches the stored detail.
        """
        if self._settings.api_key_env is not None and not self._api_key:
            return failures.failed(
                request, provider=self.provider,
                code=failures.GeocodingFailureCode.GEOCODER_MISCONFIGURED,
                env_var_names=self._env_var_names)
        url = f"{self._settings.base_url.rstrip('/')}{_SEARCH_PATH}"
        try:
            status, payload = await self._http_get(url, self._params(request))
        except Exception as exc:
            return failures.failed(request, provider=self.provider,
                                   code=failures.code_for_exception(exc),
                                   env_var_names=self._env_var_names)
        if status != _OK:
            return failures.failed(request, provider=self.provider,
                                   code=failures.code_for_status(status),
                                   status=status,
                                   env_var_names=self._env_var_names)
        return self._read(request, payload)

    # -- the provider-shaped half -------------------------------------------
    @property
    def _env_var_names(self) -> tuple[str, ...]:
        """The variable names `redact_secrets` should blank values of."""
        return () if self._settings.api_key_env is None \
            else (self._settings.api_key_env,)

    def _params(self, request: GeocodingRequest) -> Mapping[str, str]:
        """The query string for one lookup.

        `addressdetails=1` is what makes the structured fields available, and
        `format=jsonv2` is what makes `addresstype` present — the field §32's
        precision mapping depends on. Without both, this adapter would have to
        infer precision from the shape of the display name.
        """
        params: dict[str, str] = {
            "q": request.query,
            "format": "jsonv2",
            "addressdetails": "1",
            "limit": str(self._settings.max_results),
        }
        if request.country is not None:
            params["countrycodes"] = request.country.lower()
        if request.language is not None:
            params["accept-language"] = request.language
        if self._settings.contact_email is not None:
            # Nominatim's policy asks bulk users to be reachable. A contact
            # address, not a credential — see `GeocoderSettings.contact_email`.
            params["email"] = self._settings.contact_email
        if self._api_key is not None and self._settings.api_key_param is not None:
            params[self._settings.api_key_param] = self._api_key
        return params

    def _read(self, request: GeocodingRequest, payload: Any) -> GeocodingResult:
        """One 200 response, as a result.

        A payload that is not a list of objects is `GEOCODER_MALFORMED_RESPONSE`
        rather than an exception: an HTML error page served with status 200 is a
        thing public endpoints do behind a proxy, and it must not look like a
        crash in the enrichment pass.
        """
        if not isinstance(payload, list):
            return failures.failed(
                request, provider=self.provider,
                code=failures.GeocodingFailureCode.GEOCODER_MALFORMED_RESPONSE,
                env_var_names=self._env_var_names)
        places: list[GeocodedPlace] = []
        for entry in payload:
            place = _place_from(entry)
            if place is not None:
                places.append(place)
        if not places:
            # An empty list is an answer; a list whose every entry was unreadable
            # is not. The two are told apart by whether the provider sent
            # anything, which is the only evidence available.
            if payload:
                return failures.failed(
                    request, provider=self.provider,
                    code=failures.GeocodingFailureCode.GEOCODER_MALFORMED_RESPONSE,
                    env_var_names=self._env_var_names)
            return failures.not_found(request, provider=self.provider)
        contenders = _equally_plausible(places)
        if len(contenders) > 1:
            return GeocodingResult(
                request=request, provider=self.provider,
                outcome=GeocodingOutcome.AMBIGUOUS,
                alternatives=tuple(contenders),
                detail=f"{len(contenders)} places match this description equally "
                       "well; narrowing the query or naming a country would "
                       "resolve it")
        return GeocodingResult(request=request, provider=self.provider,
                               outcome=GeocodingOutcome.MATCHED, place=places[0])


def _equally_plausible(places: Sequence[GeocodedPlace]) -> tuple[GeocodedPlace, ...]:
    """The candidates that are genuinely indistinguishable, in order (§6).

    Nominatim returns results ordered by its own relevance, so "several results"
    is not ambiguity — a search for a street returns the street, then the
    district, then the city, and the first is plainly the answer. Ambiguity is
    narrower and is defined here as: **the leading results share a precision and
    disagree about where they are.**

    Two `city` results in different countries are the `Lausanne, VD` versus
    `Lausanne, TN` case §6 names, and returning the first would be exactly the
    prohibited behaviour. A `building` followed by a `city` is not ambiguous, and
    treating it as such would make almost every address unresolvable.

    "Disagree about where they are" is compared on country and region rather than
    on coordinates: two entries for the same city hall a hundred metres apart are
    one place described twice, and a distance threshold here would be a second,
    Python-side geographic calculation — which §2 and §15 are about avoiding.
    """
    best = places[0]
    contenders = [place for place in places
                  if place.precision is best.precision
                  and (place.country, place.region) != (best.country, best.region)]
    return (best, *contenders) if contenders else (best,)


def _place_from(entry: Any) -> GeocodedPlace | None:
    """One Nominatim entry as a `GeocodedPlace`, or `None` if it cannot be read.

    Returns `None` rather than raising, so one malformed entry in an otherwise
    good response costs that entry and not the lookup. The caller distinguishes
    "the provider sent nothing" from "the provider sent things this adapter could
    not read", which are different failures.
    """
    if not isinstance(entry, dict):
        return None
    point = _point_from(entry)
    display = entry.get("display_name")
    if point is None or not isinstance(display, str) or not display.strip():
        return None
    address = entry.get("address")
    address = address if isinstance(address, dict) else {}
    precision = _PRECISION_BY_TYPE.get(str(entry.get("addresstype", "")),
                                       LocationPrecision.CITY)
    place_id = entry.get("place_id")
    return GeocodedPlace(
        point=point,
        formatted_address=display.strip(),
        precision=precision,
        confidence=_CONFIDENCE_BY_PRECISION[precision],
        country=_country_from(address),
        region=_first_of(address, _REGION_KEYS),
        city=_first_of(address, _CITY_KEYS),
        postal_code=_first_of(address, ("postcode",)),
        provider_place_id=None if place_id is None else str(place_id),
        provider_category=_text(entry.get("addresstype")))


def _point_from(entry: Mapping[str, Any]) -> GeoPoint | None:
    """`lat`/`lon` as a validated point, or `None`.

    Nominatim sends them as strings. `GeoPoint` enforces the −90..90 and
    −180..180 bounds §1 asks for, so a provider that sent nonsense produces an
    unreadable entry rather than a row PostGIS will later reject.
    """
    try:
        latitude = float(entry["lat"])
        longitude = float(entry["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        return GeoPoint(latitude=latitude, longitude=longitude)
    except ValueError:
        return None


def _country_from(address: Mapping[str, Any]) -> CountryCode | None:
    """The ISO-3166-1 alpha-2 code, upper-cased. Nominatim sends it lower-case."""
    code = address.get("country_code")
    if not isinstance(code, str) or len(code.strip()) != 2:
        return None
    return code.strip().upper()


def _first_of(address: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    """The first of `keys` present as non-empty text."""
    for key in keys:
        value = _text(address.get(key))
        if value is not None:
            return value
    return None


def _text(value: Any) -> str | None:
    """A non-empty trimmed string, or `None`. Never raises on an unexpected type."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None
