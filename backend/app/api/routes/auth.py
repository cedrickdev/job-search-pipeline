"""`/api/v2/auth`: register, log in, read the current session, log out.

The four routes share one shape — take a body, call a service, write cookies —
and the parts that are not obvious are all about the cookies:

* **register and login are the only routes that set them.** Both go through
  `set_session_cookies`, so there is one attribute set in the process rather than
  one per handler;
* **`GET /auth/session` sets nothing.** It answers from the cookie the client
  already has. The one write it causes is the throttled `last_seen_at` touch inside
  `authenticate`, which cannot extend the session because `expires_at` is absolute;
* **logout clears the cookies whether or not the session was still live.** A client
  whose session had already expired still needs the dead cookies gone, and
  answering 401 would leave them in the jar.

`response.status_code` is set on the injected `Response` rather than returned from
a second object, because the same `Response` is what carries the `Set-Cookie`
headers; building a `JSONResponse` here would mean re-serializing the model by hand
and losing the OpenAPI type.
"""
from fastapi import APIRouter, Response, status

from backend.app.api.cookies import clear_session_cookies, set_session_cookies
from backend.app.api.dependencies import Auth, Authentication, CurrentSession, Now
from backend.app.api.schemas import (
    AccountResponse,
    LoginRequest,
    RegisterRequest,
    SessionResponse,
    SignedInResponse,
)
from backend.app.domain.user import User, UserSession

router = APIRouter(prefix="/auth", tags=["v2-auth"])


@router.post("/register", response_model=SignedInResponse,
             status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, response: Response,
                   service: Authentication, settings: Auth,
                   instant: Now) -> SignedInResponse:
    """Create an account and sign it in.

    201, and the body is the same `SignedInResponse` a login returns, so the client
    has one code path for "I now have a session" rather than two.
    """
    signed_in = await service.register(
        email=body.email, password=body.password,
        display_name=body.display_name, now=instant)
    set_session_cookies(response, settings=settings, session=signed_in.session,
                        token=signed_in.token, csrf_token=signed_in.csrf_token,
                        now=instant)
    return _signed_in_response(signed_in.user, signed_in.session)


@router.post("/login", response_model=SignedInResponse)
async def log_in(body: LoginRequest, response: Response, service: Authentication,
                 settings: Auth, instant: Now) -> SignedInResponse:
    """Verify a password and issue a session.

    Every refusal is raised by the service and mapped by `install_v2_error_handlers`:
    401 for credentials, 403 for a disabled account, 423 for a locked one. There is
    no branching here, which is what keeps the enumeration property a property of
    the service rather than of this handler.
    """
    signed_in = await service.log_in(email=body.email, password=body.password,
                                     now=instant)
    set_session_cookies(response, settings=settings, session=signed_in.session,
                        token=signed_in.token, csrf_token=signed_in.csrf_token,
                        now=instant)
    return _signed_in_response(signed_in.user, signed_in.session)


@router.get("/session", response_model=SignedInResponse)
async def read_session(current: CurrentSession) -> SignedInResponse:
    """Who the caller is, for a page that has just loaded.

    This is how the frontend rehydrates: the session cookie is `HttpOnly`, so the
    client cannot inspect it and has to ask. A 401 here is the normal answer for a
    visitor, not an error to report.
    """
    return _signed_in_response(current.user, current.session)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def log_out(response: Response, current: CurrentSession,
                  service: Authentication, settings: Auth, instant: Now) -> Response:
    """Revoke this session server-side and drop both cookies.

    Revocation is what makes it real: clearing a cookie only asks the browser to
    forget a credential that would otherwise still work if it had been copied.
    """
    await service.log_out(session=current.session, now=instant)
    clear_session_cookies(response, settings=settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


def _signed_in_response(user: User, session: UserSession) -> SignedInResponse:
    """Assemble the two response models from an account and a session."""
    return SignedInResponse(
        account=AccountResponse.of(user),
        session=SessionResponse(issued_at=session.issued_at,
                                expires_at=session.expires_at,
                                last_seen_at=session.last_seen_at))
