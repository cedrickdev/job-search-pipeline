"""The `Geocoder` port, its configuration, and the HTTP seam beneath it.

Three things live here, and the reason they live together is that they are the
whole of what "provider-neutral" means in Phase 7:

**The port (§4).** One method, `geocode`, taking a `GeocodingRequest` and
returning a `GeocodingResult`. No `reverse_geocode`: §4 permits one "only if
genuinely required", and nothing in this phase asks a coordinate what its address
is — the enrichment pass runs the other way, and an unused method on a port is a
promise every future adapter has to keep for no caller.

**The settings (§5).** `provider`, `base_url`, `api_key_env`, `timeout_seconds`,
`user_agent`, and nothing that could hold a secret. `api_key_env` is an
environment variable *name*, constrained by a pattern no real credential matches,
which is the same trick `discovery.contracts.EnvVarName` plays and for the same
reason: it makes "do not commit credentials" a type error rather than a review
comment.

**The HTTP seam (§38).** `HttpGet` is a two-argument async callable, and it is the
only way an adapter in this package reaches a network. A test injects a function
returning a canned payload and the adapter is exercised end to end — parsing,
classification, precision mapping — with no live service, no monkeypatching of a
third-party client, and no `respx`-shaped dependency on httpx's internals.

A geocoder **never raises for a provider problem**. A timeout is
`GeocodingOutcome.FAILED` with a composed `detail`, exactly as a source failure is
`SourceHealth` rather than an exception in Phase 5: the enrichment pass processes
a batch, and one unreachable provider must not abandon the other 199 addresses.
"""
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any, Final, Protocol, Self, runtime_checkable

from pydantic import Field, model_validator

from backend.app.discovery.contracts import EnvVarName
from backend.app.domain.base import (
    CountryCode,
    DomainModel,
    HttpUrlStr,
    NonEmptyStr,
    UtcDatetime,
)
from backend.app.domain.company import ProvenanceKey
from backend.app.domain.geo import (
    GeocodingOutcome,
    GeocodingRequest,
    GeocodingResult,
)

# How long an adapter waits for one geocoding call. Ten seconds is generous for a
# single address lookup and short enough that a batch of 200 cannot hang a CLI for
# an hour: the enrichment pass is bounded (§21), and an unbounded timeout would
# make that bound meaningless.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0

# The ceiling on candidates an adapter asks a provider for. Enough to detect
# ambiguity — §6 needs at least two to say `AMBIGUOUS` — and small enough that the
# response stays a payload rather than a download.
DEFAULT_MAX_RESULTS: Final[int] = 5

# What one HTTP GET returns to an adapter: the status code and the decoded JSON
# body. Deliberately not a response object — an adapter that could read headers
# would be an adapter that could log them, and §6's "store only useful provenance"
# is easier to keep when the provider's headers are not in scope at all.
HttpResponse = tuple[int, Any]

# `(url, params) -> (status, json)`. The narrowest thing that can express every
# geocoding call this phase makes, which is what makes it a seam a fake can fill
# in three lines rather than a client a fake has to impersonate.
HttpGet = Callable[[str, Mapping[str, str]], Awaitable[HttpResponse]]


class GeocoderSettings(DomainModel):
    """How to reach one geocoding provider (§5).

    Frozen like every `DomainModel`, and carrying **no credential value**. The API
    key is named by its environment variable, and `EnvVarName`'s pattern
    (`^[A-Z][A-Z0-9_]*$`, at most 64 characters) is what makes putting the key
    itself here a validation error instead of a leak — a real token has lower-case
    letters, dashes or dots in it and simply will not fit.

    `user_agent` is required rather than optional, and that is a usage-policy
    decision, not a style one: the OpenStreetMap Nominatim terms require a
    distinguishing User-Agent naming the application, and a default of
    `python-httpx/0.27` is the one that gets a deployment blocked. Making the
    field mandatory means a deployment has to have read something before it can
    make a call.
    """

    provider: ProvenanceKey
    base_url: HttpUrlStr
    user_agent: NonEmptyStr
    api_key_env: EnvVarName | None = None
    api_key_param: NonEmptyStr | None = None
    timeout_seconds: Annotated[float, Field(gt=0.0, le=120.0)] = \
        DEFAULT_TIMEOUT_SECONDS
    max_results: Annotated[int, Field(ge=2, le=50)] = DEFAULT_MAX_RESULTS
    # The email an operator is reachable at, when the provider's terms ask for one.
    # Nominatim's do, for bulk use. Not a secret and not a candidate's: this is the
    # deployment's contact address, and §33 has nothing to say about it.
    contact_email: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _a_key_has_somewhere_to_go(self) -> Self:
        """A named key with no parameter to put it in would never be sent.

        The failure it prevents is quiet: a deployment sets `GEOCODER_API_KEY`,
        the adapter never reads it, the provider answers anonymously and
        rate-limits at ten requests a minute, and the symptom is a slow enrichment
        pass rather than a configuration error.
        """
        if self.api_key_env is not None and self.api_key_param is None:
            raise ValueError(
                "api_key_env names a credential with no api_key_param to send it "
                "in; state both or neither")
        if self.api_key_param is not None and self.api_key_env is None:
            raise ValueError(
                "api_key_param has no api_key_env to read a value from; a "
                "credential is read from the environment, never configured inline")
        return self


class GeocoderProbe(DomainModel):
    """Whether a geocoder is usable *before* a batch is started.

    Not a health model in Phase 5's sense — there is no status enum and no
    latency, because a geocoder is one provider called one address at a time
    rather than a sweep whose partial failure has to be reported. The one question
    worth asking ahead of a run is "is this thing configured", and the answer is a
    boolean plus a sentence: the CLI refuses to start a pass against a provider
    whose API key variable is unset, instead of writing 200 `FAILED` rows.
    """

    provider: ProvenanceKey
    configured: bool
    detail: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _an_unusable_probe_says_why(self) -> Self:
        if not self.configured and self.detail is None:
            raise ValueError("a geocoder that is not configured must say what is "
                             "missing")
        return self


@runtime_checkable
class Geocoder(Protocol):
    """A place description in, coordinates or an explained absence out (§4).

    Structural, so nothing has to inherit from it, and `runtime_checkable` so the
    contract tests can assert conformance the way they do for `OpportunitySource`
    and `CompanyDiscoveryProvider`. Like both of those, it has a non-method member,
    so `isinstance` works and `issubclass` raises `TypeError`.

    Two members and no more:

    - `provider` — the stable key this geocoder's results are attributed to. It is
      written into `Location.geocoder` and into the cache's key, so it is an
      identity: renaming it orphans every coordinate recorded under the old name.
    - `geocode` — never raises for anything the provider did. A 500, a timeout, a
      body that is not JSON and an empty result set are all `GeocodingResult`s,
      because §21's bounded, retry-safe enrichment pass has to be able to record
      "this one failed" and move on.

    What is *not* here is as deliberate. No `reverse_geocode` (nothing asks). No
    batch method (a provider that accepts one is free to implement it privately;
    exposing it here would make every adapter implement a loop and call it an
    optimization). No `close` — resource lifetime belongs to whoever built the
    HTTP client, which in this package is `bootstrap`.
    """

    @property
    def provider(self) -> str: ...

    async def geocode(self, request: GeocodingRequest) -> GeocodingResult: ...

    async def probe(self) -> GeocoderProbe: ...


class GeocodingCacheEntry(DomainModel):
    """One remembered answer, as the cache repository speaks it (§23).

    A model rather than the row, because the cache is a `Geocoder` decorator and
    the repository behind it is swappable — an in-memory one is what the adapter
    tests use. The fields are the phase order's list read literally: the
    normalized key parts, the result, and when it was checked.

    `country_hint` is `None` here and `''` in the column, and the translation
    happens once in the repository. The model prefers `None` because "no country
    was stated" is an absence; the column prefers `''` because two NULLs are
    distinct in a PostgreSQL unique constraint, which would let the unhinted
    question be cached twice.

    `expires_at` is present only for `FAILED`, which the database also enforces.
    Stating the rule in both places is the same choice `models.py` makes
    throughout: the database refuses what the domain refuses, so a write that
    bypassed the model still cannot create the row.
    """

    provider: ProvenanceKey
    country_hint: CountryCode | None = None
    normalized_query: NonEmptyStr
    result: GeocodingResult
    checked_at: UtcDatetime
    expires_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _only_a_failure_expires(self) -> Self:
        failed = self.result.outcome is GeocodingOutcome.FAILED
        if failed and self.expires_at is None:
            raise ValueError(
                "a FAILED result is the absence of an answer, so it must expire; "
                "caching one permanently turns an outage into a permanently "
                "unresolvable address (§23)")
        if not failed and self.expires_at is not None:
            raise ValueError(
                f"{self.result.outcome} is an answer and does not expire; a "
                "lifetime on it would quietly restore the repeated provider call "
                "the cache exists to prevent")
        return self

    @model_validator(mode="after")
    def _the_entry_is_filed_under_what_it_answers(self) -> Self:
        """The key describes the request, or the cache returns the wrong place.

        Cheap to check and expensive to get wrong: an entry filed under a
        different provider or a different question is a wrong answer served
        confidently, which is worse than no cache at all.
        """
        if self.result.provider != self.provider:
            raise ValueError(
                f"entry is filed under {self.provider!r} but holds "
                f"{self.result.provider!r}'s answer")
        if self.result.request.normalized_query != self.normalized_query:
            raise ValueError("entry's key does not match the question its result "
                             "answers")
        if self.result.request.country != self.country_hint:
            raise ValueError("entry's country hint does not match the request's; "
                             "'Lausanne' and 'Lausanne, CH' are two questions")
        return self

    def is_fresh_at(self, now: UtcDatetime) -> bool:
        """Whether this entry may still be served (§23).

        Only a `FAILED` entry can go stale, so this is `True` for every answer.
        The alternative — expiring successes after a month — was considered and
        rejected: an address does not move, re-asking would spend a rate-limited
        provider's budget on questions already answered, and a location that
        genuinely changed is a re-geocode an operator can force by clearing the
        row.
        """
        return self.expires_at is None or self.expires_at > now


@runtime_checkable
class GeocodingCacheRepository(Protocol):
    """Where remembered answers live.

    A Protocol in this package rather than in `repositories.contracts`, unlike
    every other repository, and the reason is who reads it: the cache is an
    implementation detail of `CachingGeocoder` and of nothing else. Putting it with
    the domain repositories would suggest an application service might read the
    cache directly, which is exactly the coupling that turns a cache into a second
    source of truth.
    """

    async def get(self, provider: str, country_hint: str | None,
                  normalized_query: str) -> GeocodingCacheEntry | None: ...

    async def put(self, entry: GeocodingCacheEntry) -> None: ...
