"""Turning a provider problem into a `GeocodingResult` nobody has to catch.

The same argument as `backend.app.discovery.failures`, applied to a different
port: an adapter's `except` block is where a credential leaks. A geocoding URL
carries its query string, a query string carries the API key, and `str(exc)` on an
httpx error contains the URL. So no adapter in this package ever writes `str(exc)`
anywhere — it calls `failed()` with a code, and the sentence comes from the fixed
table below.

`redact_secrets` runs over the composed sentence anyway, as a second pass, because
the first pass being correct is a property of today's table rather than of every
future edit to it.

The codes are a deliberately short list. §38 asks for tests covering timeout,
provider error and malformed response; each of those is a member here, and
`GEOCODER_UNAVAILABLE` is the catch-all so an unanticipated exception is still a
result rather than a traceback out of a batch job.
"""
from collections.abc import Sequence
from enum import StrEnum
from typing import Final

from backend.app.discovery.failures import MAX_DETAIL_LENGTH, redact_secrets
from backend.app.domain.geo import (
    GeocodingOutcome,
    GeocodingRequest,
    GeocodingResult,
)


class GeocodingFailureCode(StrEnum):
    """Why a geocoding attempt produced no answer.

    Every member is a fact the adapter *knows* at the moment it happens, never an
    interpretation of a provider's prose. `GEOCODER_MISCONFIGURED` is the one that
    saves the most operator time — a provider whose API key variable is unset is
    not an outage, and reporting it as one sends somebody to a status page instead
    of to their environment file.
    """

    GEOCODER_TIMEOUT = "GEOCODER_TIMEOUT"
    GEOCODER_RATE_LIMITED = "GEOCODER_RATE_LIMITED"
    GEOCODER_UNAUTHORIZED = "GEOCODER_UNAUTHORIZED"
    GEOCODER_HTTP_ERROR = "GEOCODER_HTTP_ERROR"
    GEOCODER_MALFORMED_RESPONSE = "GEOCODER_MALFORMED_RESPONSE"
    GEOCODER_MISCONFIGURED = "GEOCODER_MISCONFIGURED"
    GEOCODER_UNAVAILABLE = "GEOCODER_UNAVAILABLE"


# The whole vocabulary. A detail is *composed* from this table, never forwarded:
# the strings are written once, reviewed once, and contain no interpolation of
# anything a provider sent back.
_DETAILS: Final[dict[GeocodingFailureCode, str]] = {
    GeocodingFailureCode.GEOCODER_TIMEOUT:
        "the geocoder did not answer within the configured timeout",
    GeocodingFailureCode.GEOCODER_RATE_LIMITED:
        "the geocoder refused the request as too frequent; the pass should run "
        "again later with a smaller batch",
    GeocodingFailureCode.GEOCODER_UNAUTHORIZED:
        "the geocoder rejected the credentials it was given",
    GeocodingFailureCode.GEOCODER_HTTP_ERROR:
        "the geocoder returned an error status",
    GeocodingFailureCode.GEOCODER_MALFORMED_RESPONSE:
        "the geocoder's response could not be read as the shape this adapter "
        "expects",
    GeocodingFailureCode.GEOCODER_MISCONFIGURED:
        "the geocoder is missing configuration it needs before it can be called",
    GeocodingFailureCode.GEOCODER_UNAVAILABLE:
        "the geocoder could not be reached",
}

# HTTP statuses worth distinguishing. Everything else is `GEOCODER_HTTP_ERROR`:
# the status *number* is safe to state and is stated, but only through the fixed
# suffix below, never by formatting the provider's body.
_HTTP_FAILURES: Final[dict[int, GeocodingFailureCode]] = {
    401: GeocodingFailureCode.GEOCODER_UNAUTHORIZED,
    403: GeocodingFailureCode.GEOCODER_UNAUTHORIZED,
    429: GeocodingFailureCode.GEOCODER_RATE_LIMITED,
}

_TIMEOUT_MARKERS: Final[tuple[str, ...]] = ("timed out", "timeout")


def code_for_status(status: int) -> GeocodingFailureCode:
    """The failure code one HTTP status means. Never reads a response body."""
    return _HTTP_FAILURES.get(status, GeocodingFailureCode.GEOCODER_HTTP_ERROR)


def code_for_exception(exc: BaseException) -> GeocodingFailureCode:
    """Classify an exception by its *shape*, not by its message.

    Only one text test survives here, for timeouts, because every async HTTP
    client spells its timeout error differently and the marker is a fixed
    lower-case substring rather than anything extracted from the message. Nothing
    from `str(exc)` reaches the returned detail either way — the code selects a
    sentence from `_DETAILS`.
    """
    if isinstance(exc, TimeoutError):
        return GeocodingFailureCode.GEOCODER_TIMEOUT
    if isinstance(exc, ValueError | TypeError | KeyError):
        return GeocodingFailureCode.GEOCODER_MALFORMED_RESPONSE
    message = str(exc).casefold()
    if any(marker in message for marker in _TIMEOUT_MARKERS):
        return GeocodingFailureCode.GEOCODER_TIMEOUT
    return GeocodingFailureCode.GEOCODER_UNAVAILABLE


def detail_for(code: GeocodingFailureCode, *, status: int | None = None,
               env_var_names: Sequence[str] = ()) -> str:
    """The operator-facing sentence for one failure, composed and redacted.

    `status` is appended as a bare number when there is one, because "the geocoder
    returned an error status (502)" is actionable and a three-digit integer cannot
    carry a secret. Nothing else is interpolated.
    """
    detail = _DETAILS[code]
    if status is not None:
        detail = f"{detail} ({status})"
    return redact_secrets(detail, env_var_names=env_var_names)[:MAX_DETAIL_LENGTH]


def failed(request: GeocodingRequest, *, provider: str,
           code: GeocodingFailureCode, status: int | None = None,
           env_var_names: Sequence[str] = ()) -> GeocodingResult:
    """A `FAILED` result: the provider did not answer the question.

    `FAILED` rather than `NOT_FOUND`, always, and the distinction is the one §23
    caches on. Nothing found is an answer and is remembered; a timeout is the
    absence of one and must expire, so an adapter that reported an outage as
    `NOT_FOUND` would permanently poison the cache for every address it touched
    during the outage.
    """
    return GeocodingResult(
        request=request, provider=provider, outcome=GeocodingOutcome.FAILED,
        detail=detail_for(code, status=status, env_var_names=env_var_names))


def not_found(request: GeocodingRequest, *, provider: str) -> GeocodingResult:
    """A `NOT_FOUND` result: the provider looked and there is nothing there.

    The detail is fixed and says which of the two "no result" cases this is,
    because the difference decides whether a re-run will ask again.
    """
    return GeocodingResult(
        request=request, provider=provider, outcome=GeocodingOutcome.NOT_FOUND,
        detail="the geocoder found no place matching this description")


__all__ = [
    "GeocodingFailureCode",
    "code_for_exception",
    "code_for_status",
    "detail_for",
    "failed",
    "not_found",
]
