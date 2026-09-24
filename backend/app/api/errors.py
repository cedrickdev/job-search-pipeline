"""One place that turns a refusal into a status code and a JSON body.

Routes here never build an error response. They call a service, and the service
raises a typed exception which one handler in this module maps — so the status code
for "that search is not yours" is decided once rather than in every handler that
loads a search.

**The body shape is `{"error": "<code>", "detail": "<sentence>"}`.** The code is
for the client to branch on and is stable; the sentence is for a human reading a
log or a network tab, and no client should parse it. V1 answers with FastAPI's
`{"detail": ...}`, which stays exactly as it is — V2 is a separate surface under
`/api/v2` and nothing translates between the two.

**Validation replies are stripped of the input they rejected.** FastAPI's default
422 body echoes the offending value back in `input`, which for a login request is
the password. `_validation` therefore rebuilds each error with the type, the
location and the message only — and *only for `/api/v2` paths*: V1 keeps FastAPI's
default body, because an exception handler is registered per application and
rewriting every 422 in the process would change V1 behaviour. This is the one
place the redaction can be done; a handler per route would eventually miss one
(docs/ENGINEERING_STANDARDS.md §Security: redact secrets from errors and logs).
"""
from typing import Any, Final

from fastapi import FastAPI, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError

from backend.app.api import API_V2_PREFIX
from backend.app.chat.conversation import (
    ConversationNotFound,
    ConversationScopeNotFound,
    EmptyChatMessage,
)
from backend.app.chat.executor import ChatProposalNotActionable, ChatProposalNotFound
from backend.app.documents import ArtifactNotFound
from backend.app.documents.generator import InsufficientEvidence
from backend.app.domain.application_failure import ApplicationError, ApplicationFailureCode
from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.services.applications import (
    ApplicationDecisionMissing,
    ApplicationNotActionable,
    ApplicationNotFound,
)
from backend.app.services.assessment import (
    CandidateProfileNotFound,
    OpportunityNotFound,
)
from backend.app.services.authentication import (
    AccountDisabled,
    AccountLocked,
    EmailAlreadyRegistered,
    InvalidCredentials,
)
from backend.app.services.documents import (
    DocumentArtifactMissing,
    DocumentNotFound,
)
from backend.app.services.evidence import ClaimCitesUnknownEvidence
from backend.app.services.llm_connections import (
    LLMConnectionInvalid,
    LLMConnectionNotFound,
    LLMSecretKeyUnavailable,
)
from backend.app.services.onboarding import OnboardingIncomplete, SearchProfileNotFound

# What a client is told when the database cannot be reached. Deliberately not
# V1's `{"error": "db_busy"}`: that code means "SQLite is locked, retry", and a
# frontend that treated an unreachable PostgreSQL as a transient lock would
# hammer it.
DATABASE_UNAVAILABLE: Final[str] = "database_unavailable"

# Spelled as a number because the name moved. Starlette renamed the constant to
# `HTTP_422_UNPROCESSABLE_CONTENT` (the wording RFC 9110 uses) and deprecated
# `HTTP_422_UNPROCESSABLE_ENTITY`, which warns on new versions; the new name does
# not exist on the older Starlette that `fastapi>=0.110` still allows. The integer
# is the one form that is correct on both, and 422 is what FastAPI's own validation
# handler returns — this handler only changes the body.
UNPROCESSABLE_CONTENT: Final[int] = 422

# Which HTTP status each LLM failure becomes when one surfaces to a client. The LLM is
# an upstream dependency, so an unmapped failure is a 502 (`_llm_error` defaults there):
# from the caller's side a provider fault is a bad answer from a gateway, not a fault of
# the request. The mapped ones are the failures a client can act on differently — a bad
# credential or a missing capability is the operator's to fix (409, not a retry), a rate
# limit is retryable after a wait (429), a timeout or an outage is a transient upstream
# state (504/503). `STRUCTURED_OUTPUT_INVALID` stays a 502: the model misbehaved, which
# is the gateway's problem to the caller, not the request's.
_LLM_STATUS: Final[dict[LLMFailureCode, int]] = {
    LLMFailureCode.PROVIDER_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    LLMFailureCode.PROVIDER_TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
    LLMFailureCode.PROVIDER_AUTH_REQUIRED: status.HTTP_409_CONFLICT,
    LLMFailureCode.PROVIDER_RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
    LLMFailureCode.PROVIDER_MISCONFIGURED: status.HTTP_409_CONFLICT,
    LLMFailureCode.CAPABILITY_NOT_SUPPORTED: status.HTTP_409_CONFLICT,
}

# Which HTTP status each application-execution failure becomes. A duplicate is the
# idempotency guard surfacing (409, not a retry); an exhausted rate budget is
# retryable after a wait (429); everything else is a 409 — a well-formed request the
# engine refused on a state the caller must resolve (a document not ready, a form
# that changed), not a fault of the request's shape. An unmapped code defaults to 409
# in the handler. The body's `error` is the failure code lowercased, so a client
# branches on the same closed vocabulary the engine uses (§80-82).
_APPLICATION_STATUS: Final[dict[ApplicationFailureCode, int]] = {
    ApplicationFailureCode.APPLICATION_DUPLICATE: status.HTTP_409_CONFLICT,
    ApplicationFailureCode.APPLICATION_RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
}


class ApiError(Exception):
    """A refusal the API layer itself decides, with its status code attached.

    Used for the two things no service can know about: that a request arrived
    without a usable session cookie, and that an unsafe request failed the CSRF
    check. Everything else is a service exception, mapped below.
    """

    def __init__(self, status_code: int, error: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.error = error
        self.detail = detail


def not_authenticated() -> ApiError:
    """401 for every reason a session may not be honoured.

    One message for unknown, expired, revoked and disabled, because
    `AuthenticationService.authenticate` deliberately does not tell them apart
    (docs/AUTHENTICATION.md §Sessions).
    """
    return ApiError(status.HTTP_401_UNAUTHORIZED, "not_authenticated",
                    "this endpoint requires a signed-in session")


def csrf_failed() -> ApiError:
    """403, and it says which header was expected rather than guessing why."""
    return ApiError(status.HTTP_403_FORBIDDEN, "csrf_failed",
                    "unsafe requests must carry the X-CSRF-Token header matching "
                    "the CSRF cookie issued with this session")


def company_not_found() -> ApiError:
    """404 for a company id nothing is stored under.

    An `ApiError` rather than a service exception, and for once that is not a
    shortcut: a company is a shared fact with no owner (§21), so "no such company"
    is the only reason this can happen — there is no "not yours" to keep
    indistinguishable from it, which is what `SearchProfileNotFound` exists for.
    """
    return ApiError(status.HTTP_404_NOT_FOUND, "company_not_found",
                    "no company is stored under that id")


def _json(status_code: int, error: str, detail: str,
          **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code,
                        content={"error": error, "detail": detail, **extra})


def install_v2_error_handlers(app: FastAPI) -> None:
    """Register every V2 handler on the application.

    Called by `server.app.create_app`. Registering on the app rather than the
    router is not a choice: Starlette resolves exception handlers per application,
    and `APIRouter` has no handler registry of its own.
    """

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _json(exc.status_code, exc.error, exc.detail)

    @app.exception_handler(InvalidCredentials)
    async def _invalid_credentials(request: Request,
                                   exc: InvalidCredentials) -> JSONResponse:
        # Fixed sentence, not `str(exc)`: the exception carries the address that
        # was tried, and echoing it back would confirm what was submitted.
        return _json(status.HTTP_401_UNAUTHORIZED, "invalid_credentials",
                     "that email address and password do not match an account")

    @app.exception_handler(AccountLocked)
    async def _account_locked(request: Request, exc: AccountLocked) -> JSONResponse:
        # 423, and the deadline is included: the password was correct, so the
        # caller has proved ownership and telling them when to come back is help
        # rather than disclosure.
        return _json(status.HTTP_423_LOCKED, "account_locked",
                     "too many failed sign-in attempts; this account is locked "
                     "temporarily",
                     locked_until=exc.locked_until.isoformat())

    @app.exception_handler(AccountDisabled)
    async def _account_disabled(request: Request,
                                exc: AccountDisabled) -> JSONResponse:
        return _json(status.HTTP_403_FORBIDDEN, "account_disabled",
                     "this account has been disabled")

    @app.exception_handler(EmailAlreadyRegistered)
    async def _email_taken(request: Request,
                           exc: EmailAlreadyRegistered) -> JSONResponse:
        return _json(status.HTTP_409_CONFLICT, "email_already_registered",
                     "an account already exists for that email address")

    @app.exception_handler(SearchProfileNotFound)
    async def _search_missing(request: Request,
                              exc: SearchProfileNotFound) -> JSONResponse:
        # 404 for "no such search" and for "not yours" alike — the service raises
        # one exception for both so a caller cannot enumerate other users' ids.
        return _json(status.HTTP_404_NOT_FOUND, "search_profile_not_found",
                     "no such saved search")

    @app.exception_handler(OnboardingIncomplete)
    async def _onboarding_incomplete(request: Request,
                                     exc: OnboardingIncomplete) -> JSONResponse:
        return _json(status.HTTP_409_CONFLICT, "onboarding_incomplete",
                     "onboarding needs a candidate profile and at least one active "
                     "saved search",
                     has_profile=exc.has_profile,
                     active_search_profiles=exc.active_searches)

    @app.exception_handler(CandidateProfileNotFound)
    async def _no_candidate_profile(request: Request,
                                    exc: CandidateProfileNotFound) -> JSONResponse:
        # Its own code, and the same one `GET /me/profile` uses for the same state:
        # a client reads it as "onboarding is not finished" and sends the user to
        # the profile form, not as "that posting does not exist".
        return _json(status.HTTP_404_NOT_FOUND, "candidate_profile_not_found",
                     "this account has not saved a candidate profile yet")

    @app.exception_handler(OpportunityNotFound)
    async def _no_opportunity(request: Request,
                              exc: OpportunityNotFound) -> JSONResponse:
        # A posting is a shared fact with no owner, so "no such posting" is the only
        # reason this fires — there is no "not yours" to keep indistinguishable from
        # it, unlike a user-owned assessment read.
        return _json(status.HTTP_404_NOT_FOUND, "opportunity_not_found",
                     "no opportunity is stored under that id")

    @app.exception_handler(DocumentNotFound)
    async def _document_missing(request: Request,
                                exc: DocumentNotFound) -> JSONResponse:
        # 404 for "no such document" and "not yours" alike — the service raises one
        # exception for both so a caller cannot enumerate other users' document ids.
        return _json(status.HTTP_404_NOT_FOUND, "document_not_found",
                     "no such document")

    @app.exception_handler(DocumentArtifactMissing)
    async def _document_not_rendered(request: Request,
                                     exc: DocumentArtifactMissing) -> JSONResponse:
        # 409, not 404: the document is real and this account's, but no version has
        # cleared the guard and been rendered, so there is nothing to download yet.
        return _json(status.HTTP_409_CONFLICT, "document_not_rendered",
                     "this document has no rendered version to download yet")

    @app.exception_handler(ArtifactNotFound)
    async def _artifact_gone(request: Request,
                             exc: ArtifactNotFound) -> JSONResponse:
        # The row references an artifact the store no longer holds. A server-side
        # fault, not a client error — 500 rather than a 404 that would tell the
        # caller to regenerate over what is really a storage problem. The key is
        # not echoed: it is an internal locator.
        return _json(status.HTTP_500_INTERNAL_SERVER_ERROR, "artifact_unavailable",
                     "the stored document artifact could not be read")

    @app.exception_handler(InsufficientEvidence)
    async def _insufficient_evidence(request: Request,
                                     exc: InsufficientEvidence) -> JSONResponse:
        # 409, and the sentence is the generator's own: a candidate with no evidence
        # on file cannot have a document built from evidence, and the honest reply is
        # to say so rather than invent content or fail opaquely. `str(exc)` is safe —
        # `InsufficientEvidence.detail` is a fixed explanation, never user input.
        return _json(status.HTTP_409_CONFLICT, "insufficient_evidence", str(exc))

    @app.exception_handler(ClaimCitesUnknownEvidence)
    async def _claim_unknown_evidence(request: Request,
                                      exc: ClaimCitesUnknownEvidence) -> JSONResponse:
        # 422: the claim is well-formed but cites evidence the profile does not hold.
        # The offending ids are named so a client fixes the citation; they are the
        # client's own ids, not a secret.
        return _json(UNPROCESSABLE_CONTENT, "claim_cites_unknown_evidence",
                     "the claim cites evidence that is not on your profile",
                     evidence_ids=[str(eid) for eid in exc.evidence_ids])

    @app.exception_handler(LLMConnectionNotFound)
    async def _llm_connection_missing(request: Request,
                                      exc: LLMConnectionNotFound) -> JSONResponse:
        # 404 for "no such connection" and "not yours" alike — the service raises one
        # exception for both so a caller cannot enumerate other users' connection ids.
        # `str(exc)` is not returned: it carries the requested id, which is the client's
        # own but need not be echoed to say "not found".
        return _json(status.HTTP_404_NOT_FOUND, "llm_connection_not_found",
                     "no such LLM connection")

    @app.exception_handler(LLMConnectionInvalid)
    async def _llm_connection_invalid(request: Request,
                                      exc: LLMConnectionInvalid) -> JSONResponse:
        # 422: the fields are well-formed individually but do not make a coherent
        # connection (a CLI carrying a base URL, an API missing one). The `messages`
        # are the model's own validator sentences, not the rejected input, so a form
        # can show which rule failed without a value being echoed back.
        return _json(UNPROCESSABLE_CONTENT, "llm_connection_invalid",
                     "the connection fields do not form a valid connection",
                     messages=list(exc.messages))

    @app.exception_handler(LLMSecretKeyUnavailable)
    async def _llm_secret_unavailable(request: Request,
                                      exc: LLMSecretKeyUnavailable) -> JSONResponse:
        # 409, not 422: the request is well-formed, but storing its credential would
        # need a master key the deployment never configured. The honest answer is that
        # the platform cannot hold a secret, not to store the key in the clear. The
        # sentence is the service's own fixed explanation, never user input.
        return _json(status.HTTP_409_CONFLICT, "llm_secret_key_unavailable",
                     "this deployment is not configured to store an API credential")

    @app.exception_handler(ApplicationNotFound)
    async def _application_missing(request: Request,
                                   exc: ApplicationNotFound) -> JSONResponse:
        # 404 for "no such application" and "not yours" alike — the service raises one
        # exception for both so a caller cannot enumerate other users' application ids.
        return _json(status.HTTP_404_NOT_FOUND, "application_not_found",
                     "no such application")

    @app.exception_handler(ApplicationDecisionMissing)
    async def _application_decision_missing(
            request: Request, exc: ApplicationDecisionMissing) -> JSONResponse:
        # 409, not 404: the posting exists, but no decision of intent has been made for
        # it, so there is nothing to open an application from — decide first, then apply.
        return _json(status.HTTP_409_CONFLICT, "application_decision_missing",
                     "no decision has been made for this opportunity yet")

    @app.exception_handler(ApplicationNotActionable)
    async def _application_not_actionable(
            request: Request, exc: ApplicationNotActionable) -> JSONResponse:
        # 409: the application is real and this account's, but the operation does not
        # apply in its current state (approving one never prepared, submitting one not
        # approved). The state is named so a UI can re-render, not as a secret.
        return _json(status.HTTP_409_CONFLICT, "application_not_actionable",
                     f"cannot {exc.operation} an application in state {exc.state.value}",
                     state=exc.state.value, operation=exc.operation)

    @app.exception_handler(ApplicationError)
    async def _application_error(request: Request,
                                 exc: ApplicationError) -> JSONResponse:
        # A typed, secret-free execution failure (the detail is composed from a fixed
        # table, never an adapter's own message — see
        # `backend.app.domain.application_failure`). The status comes from the code; the
        # body's `error` is the code lowercased, the same closed vocabulary the engine
        # branches on.
        status_code = _APPLICATION_STATUS.get(exc.code, status.HTTP_409_CONFLICT)
        return _json(status_code, exc.code.value.lower(), exc.detail)

    @app.exception_handler(ConversationNotFound)
    async def _conversation_missing(request: Request,
                                    exc: ConversationNotFound) -> JSONResponse:
        # 404 for "no such conversation" and "not yours" alike — the service raises one
        # exception for both, so a caller cannot learn another account holds a thread by
        # asking for it. The id it carries is the client's own but is not echoed.
        return _json(status.HTTP_404_NOT_FOUND, "conversation_not_found",
                     "no such conversation")

    @app.exception_handler(ConversationScopeNotFound)
    async def _conversation_scope_missing(
            request: Request, exc: ConversationScopeNotFound) -> JSONResponse:
        # 404: a new thread named a scope resource that is not this account's (or does not
        # exist). One response for both, exactly like `conversation_not_found`, so a foreign
        # or missing anchor cannot be told apart and probed. The scope/id are the client's
        # own but are not echoed.
        return _json(status.HTTP_404_NOT_FOUND, "conversation_scope_not_found",
                     "no such resource to open a conversation about")

    @app.exception_handler(EmptyChatMessage)
    async def _empty_chat_message(request: Request,
                                  exc: EmptyChatMessage) -> JSONResponse:
        # 422: the request is well-formed but its message is empty or whitespace, so there
        # is no turn to run. The fixed sentence is the service's own, never echoed input.
        return _json(UNPROCESSABLE_CONTENT, "empty_chat_message",
                     "a chat message must not be empty")

    @app.exception_handler(ChatProposalNotFound)
    async def _chat_proposal_missing(request: Request,
                                     exc: ChatProposalNotFound) -> JSONResponse:
        # 404 for "no such proposal" and "not yours" alike — one exception for both, so a
        # confirm or dismiss naming another account's proposal reads as absent.
        return _json(status.HTTP_404_NOT_FOUND, "chat_proposal_not_found",
                     "no such proposal")

    @app.exception_handler(ChatProposalNotActionable)
    async def _chat_proposal_not_actionable(
            request: Request, exc: ChatProposalNotActionable) -> JSONResponse:
        # 409: the proposal is real and this account's, but it is no longer open — it was
        # already executed, rejected, failed or dismissed. The status is named so a UI can
        # re-render the card, not as a secret.
        return _json(status.HTTP_409_CONFLICT, "chat_proposal_not_actionable",
                     f"a proposal in status {exc.status.value} cannot be acted on",
                     state=exc.status.value)

    @app.exception_handler(LLMError)
    async def _llm_error(request: Request, exc: LLMError) -> JSONResponse:
        # A provider failure, normalized upstream to a typed, secret-free `LLMError`
        # (the detail is composed from a fixed table, never a provider's own message —
        # see `backend.app.llm.failures`). The status comes from the code; the body's
        # `error` is the failure code lowercased, so a client branches on the same
        # closed vocabulary the LLM layer uses (§61) rather than parsing the sentence.
        status_code = _LLM_STATUS.get(exc.code, status.HTTP_502_BAD_GATEWAY)
        return _json(status_code, exc.code.value.lower(), exc.detail)

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError) -> JSONResponse:
        # The race the read-then-write checks cannot close: two simultaneous
        # registrations both pass `get_by_email`, and `uq_users_email` decides.
        # `str(exc)` is not returned — it contains the statement and its
        # parameters, which for a registration includes the password hash.
        return _json(status.HTTP_409_CONFLICT, "conflict",
                     "that write conflicts with an existing record")

    @app.exception_handler(OperationalError)
    async def _operational(request: Request, exc: OperationalError) -> JSONResponse:
        return _json(status.HTTP_503_SERVICE_UNAVAILABLE, DATABASE_UNAVAILABLE,
                     "the database is not reachable")

    @app.exception_handler(InterfaceError)
    async def _interface(request: Request, exc: InterfaceError) -> JSONResponse:
        return _json(status.HTTP_503_SERVICE_UNAVAILABLE, DATABASE_UNAVAILABLE,
                     "the database connection was lost")

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request,
                          exc: RequestValidationError) -> Response:
        if not request.url.path.startswith(API_V2_PREFIX):
            # V1's 422 body is part of V1's behaviour. Delegating rather than
            # re-implementing keeps it byte-identical to what FastAPI produced
            # before this handler existed.
            return await request_validation_exception_handler(request, exc)
        return JSONResponse(
            status_code=UNPROCESSABLE_CONTENT,
            content={"error": "validation_failed",
                     "detail": "the request body or query is not valid",
                     "errors": redacted_validation_errors(exc)})


def redacted_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """The rejected fields, without the values that were rejected.

    Public so a test can assert the redaction directly rather than through a
    response body. `type`, `loc` and `msg` are enough for a form to highlight a
    field; `input` and `ctx` are what would carry a password, a token or a whole
    request body into a log line.
    """
    return [{"type": error.get("type", "value_error"),
             "loc": [str(part) for part in error.get("loc", ())],
             "msg": error.get("msg", "invalid value")}
            for error in exc.errors()]
