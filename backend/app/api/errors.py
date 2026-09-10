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
from backend.app.services.authentication import (
    AccountDisabled,
    AccountLocked,
    EmailAlreadyRegistered,
    InvalidCredentials,
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
