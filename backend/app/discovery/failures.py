"""Turning an exception into a code, a status and a detail that carries no secret.

This is the only module in the package allowed to look at an exception, and the
reason is one concrete leak. V1's `http_fetch.fetch` raises
`FetchError(f"{url}: {last_error}")`, and `pipeline/sources/jooble.py` fetches
`https://jooble.org/api/{key}` — so the obvious implementation of "report why the
source failed" writes a live API key into a health report, from there into a run
summary, and from there into whatever dashboard reads it
(docs/ENGINEERING_STANDARDS.md §Security: redact secrets from errors and logs).

Two defences, deliberately redundant:

1. **`detail` is composed, never forwarded.** `classify_failure` picks a sentence
   from a fixed table and appends at most an HTTP status number. `str(exc)` is
   *read* to classify and never copied out.
2. **`redact_secrets` runs over the result anyway.** Belt and braces: the day
   someone adds a code path that does forward a message, it passes through here
   first. It also redacts the *values* of the environment variables the source
   declared, which is the one check that cannot be fooled by an unfamiliar URL
   shape.
"""
import os
import re
from collections.abc import Sequence
from datetime import datetime
from json import JSONDecodeError
from typing import Final

from backend.app.discovery.contracts import (
    SourceFailureCode,
    SourceHealth,
    SourceHealthStatus,
    SourceMetadata,
)
from backend.app.domain.base import DomainModel, NonEmptyStr

REDACTED: Final = "[REDACTED]"

# A detail is a sentence for an operator, not a log line. Long enough to say what
# happened, short enough that nobody is tempted to paste a traceback into it.
MAX_DETAIL_LENGTH: Final = 200

# `?key=…`, `&api_key=…`, `&token=…`: the shapes a query string uses to carry a
# credential. The name is kept — an operator needs to know *which* variable to
# look at — and only the value goes.
_SECRET_QUERY_PARAM: Final = re.compile(
    r"(?i)([?&](?:api[-_]?key|key|token|access[-_]?token|auth|password|passwd"
    r"|secret|signature|sig)=)[^&\s]+"
)

# Jooble puts its key in the *path* (`https://jooble.org/api/{key}`), which no
# parameter-name rule can catch, so runs of credential-alphabet characters are
# inspected on their shape. Runs, not whitespace-delimited tokens: V1's message is
# `f"{url}: {last_error}"`, so the key arrives glued to a colon and a status line,
# and a token rule would test `…e1f2:` against an alphabet that excludes `:` and
# conclude the string was safe.
_ALPHABET_RUN: Final = re.compile(r"[A-Za-z0-9_\-]+")

# An environment variable holding something this short is not a key, and blanking
# every occurrence of a two-character value would redact ordinary prose.
_MIN_SECRET_VALUE_LENGTH: Final = 8

# A key in a URL path is the case that actually happens; elsewhere the bar is
# higher so that an identifier quoted in a sentence survives.
_PATH_KEY_LENGTH: Final = 16
_BARE_KEY_LENGTH: Final = 24


def _looks_like_a_key(token: str, *, min_length: int) -> bool:
    """Whether a token has the shape of a credential rather than of a word.

    Mixed letters *and* digits over a credential alphabet: enough to catch a hex
    or UUID-shaped key, and narrow enough to leave real words alone —
    `welcometothejungle` and `wttj_jobs_production_fr` are both long enough to
    trip a naive length rule, and neither is a secret. A key made purely of
    letters slips through this heuristic; that is what the environment-value pass
    below is for, and it is the reason `detail` is composed rather than forwarded.
    """
    if len(token) < min_length:
        return False
    return any(c.isdigit() for c in token) and any(c.isalpha() for c in token)


def _redact_key_shaped(text: str) -> str:
    """Blank every run that looks like a credential, path runs more readily."""

    def replace(match: re.Match[str]) -> str:
        token = match.group()
        after_slash = match.start() > 0 and text[match.start() - 1] == "/"
        threshold = _PATH_KEY_LENGTH if after_slash else _BARE_KEY_LENGTH
        return REDACTED if _looks_like_a_key(token, min_length=threshold) else token

    return _ALPHABET_RUN.sub(replace, text)


def redact_secrets(text: str, *, env_var_names: Sequence[str] = ()) -> str:
    """Remove anything credential-shaped from a string bound for a report.

    `env_var_names` are the variables the source declared
    (`SourceMetadata.credential_env_vars`). Their *values* are read from the
    environment and blanked wherever they appear, which is the only check that
    does not depend on recognising a URL shape.
    """
    redacted = text
    for name in env_var_names:
        value = os.environ.get(name, "")
        if len(value) >= _MIN_SECRET_VALUE_LENGTH:
            redacted = redacted.replace(value, REDACTED)

    redacted = _SECRET_QUERY_PARAM.sub(rf"\1{REDACTED}", redacted)
    redacted = _redact_key_shaped(redacted)

    collapsed = " ".join(redacted.split())
    if len(collapsed) > MAX_DETAIL_LENGTH:
        collapsed = collapsed[: MAX_DETAIL_LENGTH - 1].rstrip() + "…"
    return collapsed


class SourceFetchError(Exception):
    """A source did not answer. Raised by an adapter, never by a V1 module.

    V1 raises `pipeline.http_fetch.FetchError` and this package deliberately does
    not import it: `failures` would then depend on V1, while §17's strangler
    boundary runs the other way. Translating one into the other is the adapter's
    job — `adapters/v1_sources.py` is the layer already allowed to know V1.

    `http_status` and `kind` are for an adapter that knows more than a message
    does. Both optional: `classify_failure` reads the message when they are absent.
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        kind: SourceFailureCode | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.kind = kind


# The whole vocabulary of `SourceHealth.detail`. Composing from this table is
# defence #1 from the module docstring: there is no code path from `str(exc)` to a
# report, because the only thing ever appended to one of these sentences is an
# HTTP status number.
_DETAILS: Final[dict[SourceFailureCode, str]] = {
    SourceFailureCode.SOURCE_UNAVAILABLE: "the source did not answer",
    SourceFailureCode.SOURCE_RATE_LIMITED: (
        "the source refused the request as too frequent"
    ),
    SourceFailureCode.SOURCE_FORBIDDEN: "the source refused the request",
    SourceFailureCode.SOURCE_NOT_FOUND: (
        "the page the adapter asks for no longer exists at the source"
    ),
    SourceFailureCode.SOURCE_TIMEOUT: "the source did not answer before the timeout",
    SourceFailureCode.SOURCE_MISCONFIGURED: "required configuration is missing",
    SourceFailureCode.SOURCE_PARSE_FAILED: (
        "the response could not be parsed; the page layout may have changed"
    ),
    SourceFailureCode.SOURCE_PARTIAL_FAILURE: (
        "the source answered only part of the request"
    ),
    SourceFailureCode.SOURCE_ADAPTER_ERROR: (
        "the adapter failed before the source could answer"
    ),
}

# Which failure a status line describes. Everything unlisted — every 5xx, and any
# 4xx a board invents — is an outage as far as a sweep is concerned.
_HTTP_FAILURES: Final[dict[int, SourceFailureCode]] = {
    401: SourceFailureCode.SOURCE_FORBIDDEN,
    403: SourceFailureCode.SOURCE_FORBIDDEN,
    404: SourceFailureCode.SOURCE_NOT_FOUND,
    410: SourceFailureCode.SOURCE_NOT_FOUND,
    429: SourceFailureCode.SOURCE_RATE_LIMITED,
}

# The status a failure maps to. Only two are not an outage: a missing variable is
# an operator's five-second fix, and a partial answer still carries opportunities.
_STATUSES: Final[dict[SourceFailureCode, SourceHealthStatus]] = {
    SourceFailureCode.SOURCE_MISCONFIGURED: SourceHealthStatus.MISCONFIGURED,
    SourceFailureCode.SOURCE_PARTIAL_FAILURE: SourceHealthStatus.DEGRADED,
}

# V1 writes `HTTP {code}`; the optional version group covers a message that quoted
# a status line instead.
_HTTP_IN_MESSAGE: Final = re.compile(r"\bHTTP[/ ]?(?:\d\.\d\s+)?(\d{3})\b")
_TIMEOUT_MARKERS: Final = ("timed out", "timeout")
_PARSE_MARKERS: Final = (
    "unparseable",
    "could not parse",
    "expecting value",
    "invalid json",
    "layout changed",
)


def _named_variables(message: str, metadata: SourceMetadata) -> tuple[str, ...]:
    """Which of the source's declared variables the message complains about."""
    return tuple(name for name in metadata.credential_env_vars if name in message)


def _http_status(exc: BaseException) -> int | None:
    """The status the source answered with, from the adapter or from the message."""
    if isinstance(exc, SourceFetchError) and exc.http_status is not None:
        return exc.http_status
    found = _HTTP_IN_MESSAGE.search(str(exc))
    return int(found.group(1)) if found else None


def _failure_code(exc: BaseException, metadata: SourceMetadata) -> SourceFailureCode:
    """Which of the nine codes this exception is, deciding nothing else."""
    fetch_error = exc if isinstance(exc, SourceFetchError) else None
    if fetch_error is not None and fetch_error.kind is not None:
        return fetch_error.kind

    message = str(exc)
    lowered = message.lower()

    # V1's jooble source raises `FetchError("JOOBLE_API_KEY not set …")`, naming
    # the very variable this source declared. That is a configuration gap, not an
    # outage, and telling the two apart is what §12 added MISCONFIGURED for.
    if _named_variables(message, metadata):
        return SourceFailureCode.SOURCE_MISCONFIGURED

    status = _http_status(exc)
    if status is not None:
        return _HTTP_FAILURES.get(status, SourceFailureCode.SOURCE_UNAVAILABLE)

    if isinstance(exc, TimeoutError) or any(m in lowered for m in _TIMEOUT_MARKERS):
        return SourceFailureCode.SOURCE_TIMEOUT
    if isinstance(exc, JSONDecodeError) or any(m in lowered for m in _PARSE_MARKERS):
        return SourceFailureCode.SOURCE_PARSE_FAILED
    if fetch_error is not None:
        # The fetch failed and said nothing recognisable about why — DNS, a reset
        # connection, a proxy. From a sweep's point of view the source is down.
        return SourceFailureCode.SOURCE_UNAVAILABLE
    return SourceFailureCode.SOURCE_ADAPTER_ERROR


class FailureClassification(DomainModel):
    """What a caller needs in order to build a `SourceHealth` — and nothing else.

    Separate from `SourceHealth` because a classification carries no `source_key`
    and no clock: it is the part of the answer that depends only on the exception,
    which is also the part worth testing on its own.
    """

    status: SourceHealthStatus
    reason: SourceFailureCode
    detail: NonEmptyStr


def classify_failure(
    exc: BaseException, metadata: SourceMetadata
) -> FailureClassification:
    """Read an exception; write a status, a code and a secret-free sentence."""
    reason = _failure_code(exc, metadata)
    detail = _DETAILS[reason]

    if reason is SourceFailureCode.SOURCE_MISCONFIGURED:
        # Names, never values: the name is the actionable half and cannot be a
        # secret — `EnvVarName` constrains it to `^[A-Z][A-Z0-9_]*$`.
        named = _named_variables(str(exc), metadata) or metadata.credential_env_vars
        if named:
            detail = f"{detail}: {', '.join(named)}"
    else:
        status = _http_status(exc)
        if status is not None:
            detail = f"{detail} (HTTP {status})"

    return FailureClassification(
        status=_STATUSES.get(reason, SourceHealthStatus.UNAVAILABLE),
        reason=reason,
        detail=redact_secrets(detail, env_var_names=metadata.credential_env_vars),
    )


def healthy(
    metadata: SourceMetadata, *, checked_at: datetime, latency_ms: int | None = None
) -> SourceHealth:
    """The source answered. `SourceHealth` refuses a reason on this status."""
    return SourceHealth(
        source_key=metadata.source_key,
        status=SourceHealthStatus.HEALTHY,
        checked_at=checked_at,
        latency_ms=latency_ms,
    )


def degraded(
    metadata: SourceMetadata,
    *,
    checked_at: datetime,
    detail: str,
    reason: SourceFailureCode = SourceFailureCode.SOURCE_PARTIAL_FAILURE,
    latency_ms: int | None = None,
) -> SourceHealth:
    """The source answered partially: some pages, some postings, some fields.

    The only builder that takes a caller-written `detail`, because only the
    adapter knows what part failed. It is redacted on the way in — defence #2 from
    the module docstring, applied to the one string that did not come from the
    fixed table.
    """
    return SourceHealth(
        source_key=metadata.source_key,
        status=SourceHealthStatus.DEGRADED,
        checked_at=checked_at,
        latency_ms=latency_ms,
        reason=reason,
        detail=redact_secrets(detail, env_var_names=metadata.credential_env_vars),
    )


def misconfigured(
    metadata: SourceMetadata, *, checked_at: datetime, missing: Sequence[str] = ()
) -> SourceHealth:
    """A variable the source needs is not set, discovered before any request.

    Lets an adapter say so without a pointless round trip: V1's jooble source only
    finds out inside `search_jobs`, which is one request the operator never wanted.
    """
    names = tuple(missing) or metadata.credential_env_vars
    detail = _DETAILS[SourceFailureCode.SOURCE_MISCONFIGURED]
    if names:
        detail = f"{detail}: {', '.join(names)}"
    return SourceHealth(
        source_key=metadata.source_key,
        status=SourceHealthStatus.MISCONFIGURED,
        checked_at=checked_at,
        reason=SourceFailureCode.SOURCE_MISCONFIGURED,
        detail=redact_secrets(detail, env_var_names=metadata.credential_env_vars),
    )


def health_from_exception(
    exc: BaseException,
    metadata: SourceMetadata,
    *,
    checked_at: datetime,
    latency_ms: int | None = None,
) -> SourceHealth:
    """The one call an adapter's `except` block needs.

    `latency_ms` is still worth carrying on a failure: a 20-second timeout and an
    instant refusal are different problems with the same code.
    """
    classification = classify_failure(exc, metadata)
    return SourceHealth(
        source_key=metadata.source_key,
        status=classification.status,
        checked_at=checked_at,
        latency_ms=latency_ms,
        reason=classification.reason,
        detail=classification.detail,
    )
