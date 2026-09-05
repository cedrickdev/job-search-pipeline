"""The two cookies, written in one place.

A session cookie and a CSRF cookie, and the difference between them is the whole
design:

* the **session cookie** is `HttpOnly`, so no script can read it. It is the
  credential;
* the **CSRF cookie** is deliberately readable by JavaScript, because the browser
  client has to copy it into an `X-CSRF-Token` header. It is not a credential —
  holding it proves nothing, and `AuthenticationService` compares it against the
  digest stored with the session rather than against the cookie.

Four attributes are fixed rather than configurable, because `__Host-` requires
exactly this combination and a browser silently drops a cookie that gets it wrong:

* `Path=/` — always;
* no `Domain` — a host-only cookie, so a compromised sibling subdomain cannot set
  or overwrite it;
* `Secure` — from `AuthSettings.cookie_secure`, which defaults to `True` and is
  **never** inferred from `request.url.scheme`: behind a TLS-terminating proxy the
  application sees `http://` while the browser is on HTTPS, so inference would
  drop `Secure` in production, which is precisely where it matters;
* `SameSite` — `lax` by default, defence in depth rather than the CSRF control.
  An explicitly configured `SameSite=Lax` cookie does not normally accompany a
  cross-site top-level `POST`; Lax permits cross-site top-level navigation with
  safe methods. The double-submit check stays regardless
  (docs/AUTHENTICATION.md §CSRF).

`max_age` is set from the session's own remaining lifetime rather than from the
configured lifetime, so the cookie a *refreshed* page holds expires with the
session row rather than outliving it.
"""
from datetime import datetime
from math import ceil

from fastapi import Response
from pydantic import SecretStr

from backend.app.core.settings import AuthSettings
from backend.app.domain.user import UserSession

# Fixed, not configurable: `__Host-` is only honoured for a cookie sent with
# `Secure`, `Path=/` and no `Domain`. Making either one a setting would create
# combinations every browser rejects.
COOKIE_PATH = "/"


def set_session_cookies(response: Response, *, settings: AuthSettings,
                        session: UserSession, token: SecretStr,
                        csrf_token: SecretStr, now: datetime) -> None:
    """Write both cookies for a session that has just been issued.

    Called by register and login, and by nothing else: a route that wants a
    session in a browser goes through here so no future handler can invent its own
    attribute set.
    """
    max_age = _remaining_seconds(session, now)
    response.set_cookie(
        settings.session_cookie_name,
        token.get_secret_value(),
        max_age=max_age,
        path=COOKIE_PATH,
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_same_site)
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token.get_secret_value(),
        max_age=max_age,
        path=COOKIE_PATH,
        secure=settings.cookie_secure,
        # Readable on purpose. The client must copy this value into the
        # `X-CSRF-Token` header, and a `HttpOnly` cookie it cannot read would make
        # the double submit impossible to perform.
        httponly=False,
        samesite=settings.cookie_same_site)


def clear_session_cookies(response: Response, *, settings: AuthSettings) -> None:
    """Remove both cookies, with the attributes they were set with.

    The attributes are not decoration: a browser matches a deletion against name,
    path and domain, so a `delete_cookie` that omitted `path` would leave the
    original cookie in place and the user logged out on the server but still
    carrying a dead credential.
    """
    for name in (settings.session_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(
            name, path=COOKIE_PATH, secure=settings.cookie_secure,
            httponly=name == settings.session_cookie_name,
            samesite=settings.cookie_same_site)


def _remaining_seconds(session: UserSession, now: datetime) -> int:
    """How long the cookie may live, from the session row rather than the settings.

    Rounded up, and floored at one second: a zero `Max-Age` is an instruction to
    delete the cookie, which would turn a session with a second left into an
    immediate logout.
    """
    return max(1, ceil((session.expires_at - now).total_seconds()))
