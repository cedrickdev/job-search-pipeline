"""Turning any execution failure into a normalized, secret-free typed error.

The application engine's counterpart to `backend.app.llm.failures`: every way a
*submission* can fail — an adapter that raised, a channel with no automated path, a
form that changed under the prepared answers, an ambiguous confirmation the adapter
could not read — arrives here and leaves as one of a fixed set of `APPLICATION_*`
codes (§80-82). Business code and the API catch `ApplicationError`, read `.code`,
and map it to a status; they never inspect an adapter-specific exception, which is
what keeps the "no vendor branching in business code" rule (§8) true on the failure
path as well as the success path.

The same secret-safety rule the LLM layer follows applies (§83,
docs/ENGINEERING_STANDARDS.md §Security): a detail is composed from a fixed table,
never forwarded from an adapter's own message, because a page's error text or an
email server's reply can echo a credential or a full-page HTML dump (§85). An
adapter that raised is classified by code, and its message is dropped.
"""
from enum import StrEnum
from typing import Final

# A detail is a sentence for an operator, not a log line — the same cap the LLM
# failures module uses, for the same reason.
MAX_DETAIL_LENGTH: Final = 200


class ApplicationFailureCode(StrEnum):
    """The whole vocabulary of an application-execution failure (§80-82).

    Closed on purpose: an unrecognised adapter exception is
    `APPLICATION_ADAPTER_ERROR`, never a new ad-hoc string. These describe failures
    of *execution* — a gate refusal is not here, because a refusal is a typed
    authorization outcome (`backend.app.domain.execution_gate`), not an error.

    - `APPLICATION_CHANNEL_UNSUPPORTED` — no adapter can drive this channel;
    - `APPLICATION_DOCUMENT_NOT_READY` — a pinned document version is not a RENDERED,
      guard-cleared artifact, so there is nothing safe to submit (§15);
    - `APPLICATION_FORM_CHANGED` — the form's fingerprint changed between preparation
      and submission, so the prepared answers can no longer be trusted (§34);
    - `APPLICATION_MISSING_ANSWER` — a required question had no trustworthy answer
      and the run should have become REQUIRES_HUMAN, not submitted (§25);
    - `APPLICATION_ADAPTER_ERROR` — the adapter raised while preparing or submitting;
      the catch-all, so an unknown failure is still typed;
    - `APPLICATION_SUBMISSION_UNKNOWN` — the submission left the platform but no
      confirmation could be read, so whether it landed is genuinely unknown; a blind
      retry could double-submit, so it never retries automatically (§38, §84, §88);
    - `APPLICATION_RATE_LIMITED` — the policy's daily/weekly budget is exhausted (§49);
    - `APPLICATION_DUPLICATE` — an application for this exact target and channel already
      exists (§36); the idempotency guard, surfaced rather than silently swallowed.
    """

    APPLICATION_CHANNEL_UNSUPPORTED = "APPLICATION_CHANNEL_UNSUPPORTED"
    APPLICATION_DOCUMENT_NOT_READY = "APPLICATION_DOCUMENT_NOT_READY"
    APPLICATION_FORM_CHANGED = "APPLICATION_FORM_CHANGED"
    APPLICATION_MISSING_ANSWER = "APPLICATION_MISSING_ANSWER"
    APPLICATION_ADAPTER_ERROR = "APPLICATION_ADAPTER_ERROR"
    APPLICATION_SUBMISSION_UNKNOWN = "APPLICATION_SUBMISSION_UNKNOWN"
    APPLICATION_RATE_LIMITED = "APPLICATION_RATE_LIMITED"
    APPLICATION_DUPLICATE = "APPLICATION_DUPLICATE"


# The sentence each code carries. Composing from this table is the secret defence:
# there is no path from an adapter's message to a detail, because the only text ever
# used is one of these fixed strings.
_DETAILS: Final[dict[ApplicationFailureCode, str]] = {
    ApplicationFailureCode.APPLICATION_CHANNEL_UNSUPPORTED:
        "no adapter can submit through this application channel",
    ApplicationFailureCode.APPLICATION_DOCUMENT_NOT_READY:
        "a required document is not a rendered, guard-cleared version",
    ApplicationFailureCode.APPLICATION_FORM_CHANGED:
        "the application form changed after preparation and must be reviewed again",
    ApplicationFailureCode.APPLICATION_MISSING_ANSWER:
        "a required question had no trustworthy answer and needs a human",
    ApplicationFailureCode.APPLICATION_ADAPTER_ERROR:
        "the application adapter failed to complete the request",
    ApplicationFailureCode.APPLICATION_SUBMISSION_UNKNOWN:
        "the submission left the platform but could not be confirmed",
    ApplicationFailureCode.APPLICATION_RATE_LIMITED:
        "the policy's application rate limit is exhausted",
    ApplicationFailureCode.APPLICATION_DUPLICATE:
        "an application for this target and channel already exists",
}


class ApplicationError(Exception):
    """A normalized application-execution failure, with a code and a safe detail.

    The one exception type the engine raises across its boundary. `code` is stable
    and for a caller to branch on; `detail` is a composed sentence for a human and is
    never an adapter's own message (§83, §85). An adapter that raised is passed to
    `classify_adapter_failure`, which drops its text and keeps only a typed code.
    """

    def __init__(self, code: ApplicationFailureCode, *,
                 detail: str | None = None) -> None:
        composed = detail if detail is not None else _DETAILS[code]
        collapsed = " ".join(composed.split())
        if len(collapsed) > MAX_DETAIL_LENGTH:
            collapsed = collapsed[: MAX_DETAIL_LENGTH - 1].rstrip() + "…"
        self.code = code
        self.detail = collapsed
        super().__init__(f"{code}: {self.detail}")


def classify_adapter_failure(exc: BaseException) -> ApplicationError:
    """Read an adapter exception; return a typed, secret-free `ApplicationError`.

    An `ApplicationError` is returned unchanged — an adapter that already classified
    is trusted. Anything else becomes `APPLICATION_ADAPTER_ERROR` with the fixed
    table detail, and the original message is deliberately dropped: an adapter's
    exception text can carry page content, an upload path or a credential (§85), so
    it never becomes a detail.
    """
    if isinstance(exc, ApplicationError):
        return exc
    return ApplicationError(ApplicationFailureCode.APPLICATION_ADAPTER_ERROR)
