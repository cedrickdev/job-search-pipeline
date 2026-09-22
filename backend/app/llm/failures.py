"""Turning any provider failure into a normalized, secret-free typed error.

Every way an LLM call can fail — a CLI subprocess that exits non-zero, an HTTP 429
from a hosted API, a local server that is not running, a response that does not
match the requested schema — arrives here and leaves as one of a fixed set of
codes (§61). Business code above the router catches `LLMError` and reads `.code`;
it never inspects a provider-specific exception, which is what keeps the "no
`if provider == …`" rule (docs/LLM_PROVIDER_ARCHITECTURE.md §3) true on the failure
path as well as the success path.

Two secret-safety rules, the same two `backend.app.discovery.failures` follows and
for the same reason (docs/ENGINEERING_STANDARDS.md §Security):

1. **A detail is composed from a fixed table, never forwarded from a provider.**
   A hosted API's 401 body can echo the very key that was rejected; a CLI's stderr
   can print the argv. `classify_provider_failure` reads the exception to pick a
   code and never copies its text out.
2. **`redact_secrets` runs over any caller-supplied detail anyway.** Belt and
   braces for the one string that did not come from the table — an adapter that
   knows something specific enough to say — with the declared credential values
   blanked wherever they appear.
"""
import re
from collections.abc import Sequence
from enum import StrEnum
from typing import Final

REDACTED: Final = "[REDACTED]"

# A detail is a sentence for an operator, not a log line. Long enough to say what
# happened, short enough that nobody pastes a traceback into it.
MAX_DETAIL_LENGTH: Final = 200

# An environment variable holding something this short is not a key; blanking every
# occurrence of a two-character value would redact ordinary prose.
_MIN_SECRET_VALUE_LENGTH: Final = 8

# `sk-…`, `Bearer …`, and the query/header shapes a credential travels in. The name
# is kept (an operator needs to know which value leaked); the value goes.
_BEARER: Final = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]+")
_SK_KEY: Final = re.compile(r"\bsk-[A-Za-z0-9._\-]{8,}")
_SECRET_QUERY_PARAM: Final = re.compile(
    r"(?i)([?&](?:api[-_]?key|key|token|access[-_]?token|auth|password|passwd"
    r"|secret)=)[^&\s]+"
)


class LLMFailureCode(StrEnum):
    """The whole vocabulary of an LLM failure (§61).

    Every provider — CLI, hosted API, local server — normalizes to one of these, so
    a service, a telemetry query and a settings page group on a code rather than
    parse a message. The set is closed on purpose: an unrecognised failure is
    `PROVIDER_INTERNAL_ERROR`, never a new ad-hoc string.
    """

    # The provider could not be reached at all: DNS, a refused connection, a CLI
    # binary that is not installed, a local server that is not running.
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    # It did not answer before the deadline. Distinct from unavailable: a 30-second
    # timeout and an instant connection refusal are different problems.
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    # It refused for want of, or with invalid, credentials — a 401/403 from a hosted
    # API. The router maps it to health `AUTH_REQUIRED`: the fix is a credential, not
    # a retry.
    PROVIDER_AUTH_REQUIRED = "PROVIDER_AUTH_REQUIRED"
    # It refused the request as too frequent (a 429). Retryable after a wait.
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    # Required configuration is missing or invalid before any request is made — no
    # base URL, an unparsable model name, a connection with no stored credential.
    PROVIDER_MISCONFIGURED = "PROVIDER_MISCONFIGURED"
    # The task needs a capability this provider does not claim (structured output on
    # a plain CLI, tools on a bare completion endpoint). Caught before execution by
    # the router, never mid-call.
    CAPABILITY_NOT_SUPPORTED = "CAPABILITY_NOT_SUPPORTED"
    # The provider answered, but the JSON did not match the requested schema even
    # after the single bounded repair attempt (§42). The content is untrusted and
    # dropped rather than half-parsed.
    STRUCTURED_OUTPUT_INVALID = "STRUCTURED_OUTPUT_INVALID"
    # The provider's wire output could not be parsed as its protocol promises: a CLI
    # that printed something that is not the stream-json it should, an SSE stream
    # that broke mid-event. Its output is untrusted (§73), so this is a failure, not
    # a best-effort parse.
    PROVIDER_PROTOCOL_ERROR = "PROVIDER_PROTOCOL_ERROR"
    # The prompt (plus history) exceeded the model's context window.
    CONTEXT_LENGTH_EXCEEDED = "CONTEXT_LENGTH_EXCEEDED"
    # The provider's own safety filter refused the request or the completion. A fact
    # about the provider, surfaced rather than hidden, so a caller can choose another.
    PROVIDER_CONTENT_FILTERED = "PROVIDER_CONTENT_FILTERED"
    # The provider's output exceeded the adapter's hard cap and was cut off. The
    # partial output is untrusted and the call fails rather than returning a truncated
    # answer that looks whole.
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
    # The request was cancelled by the caller (a client disconnect, a shutdown). A
    # normal outcome, recorded so a cancelled run is not counted as a failure in the
    # telemetry (§57).
    PROVIDER_CANCELLED = "PROVIDER_CANCELLED"
    # A resume was requested for a session the provider no longer knows. The router's
    # stale-session retry-once (§74) turns this into one fresh attempt before it
    # surfaces.
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    # The provider answered with a server error (a 5xx) or something otherwise
    # unrecognised. The catch-all, so an unknown failure is still typed.
    PROVIDER_INTERNAL_ERROR = "PROVIDER_INTERNAL_ERROR"


# The sentence each code carries. Composing from this table is the first secret
# defence: there is no path from a provider's message to a detail, because the only
# thing ever appended is an HTTP status number.
_DETAILS: Final[dict[LLMFailureCode, str]] = {
    LLMFailureCode.PROVIDER_UNAVAILABLE: "the provider could not be reached",
    LLMFailureCode.PROVIDER_TIMEOUT: "the provider did not answer before the timeout",
    LLMFailureCode.PROVIDER_AUTH_REQUIRED: (
        "the provider refused the request for want of valid credentials"
    ),
    LLMFailureCode.PROVIDER_RATE_LIMITED: (
        "the provider refused the request as too frequent"
    ),
    LLMFailureCode.PROVIDER_MISCONFIGURED: "required provider configuration is missing",
    LLMFailureCode.CAPABILITY_NOT_SUPPORTED: (
        "the provider does not support a capability this task requires"
    ),
    LLMFailureCode.STRUCTURED_OUTPUT_INVALID: (
        "the provider's response did not match the requested schema"
    ),
    LLMFailureCode.PROVIDER_PROTOCOL_ERROR: (
        "the provider's output could not be parsed as its protocol promises"
    ),
    LLMFailureCode.CONTEXT_LENGTH_EXCEEDED: (
        "the prompt exceeded the model's context window"
    ),
    LLMFailureCode.PROVIDER_CONTENT_FILTERED: (
        "the provider's safety filter refused the request"
    ),
    LLMFailureCode.OUTPUT_LIMIT_EXCEEDED: (
        "the provider's output exceeded the adapter's limit"
    ),
    LLMFailureCode.PROVIDER_CANCELLED: "the request was cancelled",
    LLMFailureCode.SESSION_NOT_FOUND: (
        "the provider no longer holds the session to resume"
    ),
    LLMFailureCode.PROVIDER_INTERNAL_ERROR: "the provider failed unexpectedly",
}

# Which failure an HTTP status describes. Everything unlisted — every 5xx, any 4xx a
# provider invents — is an internal error as far as a call is concerned.
_HTTP_FAILURES: Final[dict[int, LLMFailureCode]] = {
    400: LLMFailureCode.PROVIDER_PROTOCOL_ERROR,
    401: LLMFailureCode.PROVIDER_AUTH_REQUIRED,
    403: LLMFailureCode.PROVIDER_AUTH_REQUIRED,
    404: LLMFailureCode.PROVIDER_MISCONFIGURED,
    408: LLMFailureCode.PROVIDER_TIMEOUT,
    413: LLMFailureCode.CONTEXT_LENGTH_EXCEEDED,
    422: LLMFailureCode.PROVIDER_PROTOCOL_ERROR,
    429: LLMFailureCode.PROVIDER_RATE_LIMITED,
}


def redact_secrets(text: str, *, secret_values: Sequence[str] = ()) -> str:
    """Remove anything credential-shaped from a string bound for an error.

    `secret_values` are the concrete secrets this call knows about — a connection's
    decrypted API key, say — blanked wherever they appear. That is the one check
    that does not depend on recognising a token shape, and the reason a caller that
    holds a secret passes it here rather than hoping the regexes catch it.
    """
    redacted = text
    for value in secret_values:
        if len(value) >= _MIN_SECRET_VALUE_LENGTH:
            redacted = redacted.replace(value, REDACTED)
    redacted = _BEARER.sub(rf"\1 {REDACTED}", redacted)
    redacted = _SK_KEY.sub(REDACTED, redacted)
    redacted = _SECRET_QUERY_PARAM.sub(rf"\1{REDACTED}", redacted)
    collapsed = " ".join(redacted.split())
    if len(collapsed) > MAX_DETAIL_LENGTH:
        collapsed = collapsed[: MAX_DETAIL_LENGTH - 1].rstrip() + "…"
    return collapsed


class LLMError(Exception):
    """A normalized LLM failure, with its code and a secret-free detail.

    The one exception type the layer raises across its boundary. `code` is stable
    and for a caller to branch on; `detail` is a composed sentence for a human and
    is never a provider's own message. `retryable` and `http_status` are hints for
    the router and the telemetry, not part of the contract a service reads.

    An adapter that already knows the exact code raises this directly; one that only
    has a provider exception calls `classify_provider_failure` and lets the table
    decide.
    """

    def __init__(self, code: LLMFailureCode, *, detail: str | None = None,
                 http_status: int | None = None,
                 secret_values: Sequence[str] = ()) -> None:
        composed = detail if detail is not None else _DETAILS[code]
        self.code = code
        self.http_status = http_status
        self.detail = redact_secrets(composed, secret_values=secret_values)
        super().__init__(f"{code}: {self.detail}")

    @property
    def retryable(self) -> bool:
        """Whether trying the same provider again could plausibly succeed.

        Timeouts, rate limits and transient outages are; a missing capability, a bad
        credential or a misconfiguration are not — retrying those wastes a call and a
        deadline. The router uses this to decide bounded retry versus immediate
        fallback (§65).
        """
        return self.code in _RETRYABLE


_RETRYABLE: Final[frozenset[LLMFailureCode]] = frozenset({
    LLMFailureCode.PROVIDER_UNAVAILABLE,
    LLMFailureCode.PROVIDER_TIMEOUT,
    LLMFailureCode.PROVIDER_RATE_LIMITED,
    LLMFailureCode.PROVIDER_INTERNAL_ERROR,
})

_TIMEOUT_MARKERS: Final = ("timed out", "timeout", "deadline")
_UNREACHABLE_MARKERS: Final = (
    "connection refused", "could not connect", "connection error",
    "name or service not known", "nodename nor servname", "no route to host",
    "connection reset", "cannot connect",
)
_AUTH_MARKERS: Final = ("unauthorized", "forbidden", "invalid api key",
                        "authentication", "invalid_api_key")
_CONTEXT_MARKERS: Final = ("context length", "context window", "too many tokens",
                          "maximum context")
_CONTENT_MARKERS: Final = ("content filter", "content policy", "safety",
                          "flagged", "refused to")
_HTTP_IN_MESSAGE: Final = re.compile(r"\bHTTP[/ ]?(?:\d\.\d\s+)?(\d{3})\b")
_STATUS_IN_MESSAGE: Final = re.compile(r"\bstatus(?:\s+code)?[:\s]+(\d{3})\b")


def classify_provider_failure(
    exc: BaseException, *,
    http_status: int | None = None,
    secret_values: Sequence[str] = (),
) -> LLMError:
    """Read a provider exception; return a typed, secret-free `LLMError`.

    An `LLMError` is returned unchanged — an adapter that already classified is
    trusted. Otherwise the status (from the argument or read out of the message) and
    the message markers decide, and the detail comes from the fixed table, never from
    `str(exc)`. `secret_values` are blanked in the unlikely event a marker match
    still lets a fragment through.
    """
    if isinstance(exc, LLMError):
        return exc

    status = http_status if http_status is not None else _status_in(exc)
    if status is not None:
        code = _HTTP_FAILURES.get(status)
        if code is None:
            code = (LLMFailureCode.PROVIDER_INTERNAL_ERROR if status >= 500
                    else LLMFailureCode.PROVIDER_PROTOCOL_ERROR)
        return LLMError(code, http_status=status, secret_values=secret_values)

    lowered = str(exc).lower()
    if isinstance(exc, TimeoutError) or any(m in lowered for m in _TIMEOUT_MARKERS):
        code = LLMFailureCode.PROVIDER_TIMEOUT
    elif any(m in lowered for m in _AUTH_MARKERS):
        code = LLMFailureCode.PROVIDER_AUTH_REQUIRED
    elif any(m in lowered for m in _CONTEXT_MARKERS):
        code = LLMFailureCode.CONTEXT_LENGTH_EXCEEDED
    elif any(m in lowered for m in _CONTENT_MARKERS):
        code = LLMFailureCode.PROVIDER_CONTENT_FILTERED
    elif any(m in lowered for m in _UNREACHABLE_MARKERS):
        code = LLMFailureCode.PROVIDER_UNAVAILABLE
    else:
        code = LLMFailureCode.PROVIDER_INTERNAL_ERROR
    return LLMError(code, secret_values=secret_values)


def _status_in(exc: BaseException) -> int | None:
    """An HTTP status carried on the exception or quoted in its message."""
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int) and 100 <= code <= 599:
        return code
    message = str(exc)
    found = _HTTP_IN_MESSAGE.search(message) or _STATUS_IN_MESSAGE.search(message)
    return int(found.group(1)) if found else None
