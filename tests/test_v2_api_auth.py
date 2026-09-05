# tests/test_v2_api_auth.py
"""`/api/v2/auth` over HTTP: the cookies, the CSRF check, revocation, expiry.

Nine properties were required for Phase 4 to close, and six of them are here —
cookie attributes, CSRF rejection, logout/revocation, expired-session rejection,
locked-account behaviour, and no auth or session secret in a response body. The
other three (cross-user isolation, unauthenticated `/api/v2/**`, V1 unchanged) are
in `test_v2_api_me.py` and `test_v2_api_surface.py`.

What makes these different from `test_v2_authentication.py` is that nothing here
calls a service. Every assertion is made against a status code, a header or a JSON
body — and, where the point is server-side, against the store behind the fakes: a
403 that also revoked the session would pass a response-only test.
"""
from datetime import timedelta

import pytest

from backend.app.api.dependencies import CSRF_HEADER
from backend.app.core.settings import AuthSettings
from backend.app.domain.user import UserStatus
from tests.v2_api import (
    EMAIL,
    HTTPS_BASE_URL,
    LOCAL_HTTP_BASE_URL,
    OTHER_EMAIL,
    PASSWORD,
    SHORT_PASSWORD,
    WRONG_PASSWORD,
    api_harness,
    cookie_attributes,
    credentials,
)


@pytest.mark.asyncio
async def test_registering_signs_the_browser_in_and_issues_both_cookies(tmp_path):
    """201, an account, a session window, and the two cookies that carry it.

    Registration signs in on purpose — onboarding is the next screen — so the
    response has to be a *usable* session rather than a confirmation.
    """
    async with api_harness(tmp_path) as api:
        response = await api.register()

        assert response.status_code == 201
        body = response.json()
        assert body["account"]["email"] == EMAIL
        assert body["account"]["display_name"] == "Candidate"
        assert body["account"]["onboarding_completed_at"] is None
        assert body["session"]["expires_at"] > body["session"]["issued_at"]
        assert api.session_token is not None
        assert api.csrf_token is not None
        assert api.session_token != api.csrf_token
        assert len(api.users.users) == 1
        assert len(api.sessions.sessions) == 1


@pytest.mark.asyncio
async def test_no_signed_in_body_carries_a_token_a_hash_or_a_digest(tmp_path):
    """The tokens leave in two `Set-Cookie` headers and nowhere else.

    Asserted against the *text* of the body as well as its keys: a nested model
    that grew a field would still be caught, and the raw session token is the one
    value whose appearance in a JSON body would be a working credential in every
    log that recorded the response.
    """
    async with api_harness(tmp_path) as api:
        registered = await api.register()
        session = await api.read("/auth/session")

        for response in (registered, session):
            body = response.json()
            assert set(body) == {"account", "session"}
            assert set(body["account"]) == {"id", "email", "display_name", "status",
                                            "onboarding_completed_at", "created_at"}
            assert set(body["session"]) == {"issued_at", "expires_at", "last_seen_at"}
            text = response.text
            assert api.session_token not in text
            assert api.csrf_token not in text
            assert "argon2" not in text
            assert "password" not in text
            assert "digest" not in text


@pytest.mark.asyncio
async def test_a_taken_address_is_refused_and_leaves_one_account_standing(tmp_path):
    """409 with a code the form can branch on, and no second row.

    Registration is the one place account existence is necessarily disclosed:
    refusing a duplicate requires saying so, and the alternative needs mail
    delivery, which is a later phase.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        response = await api.client.post(api.url("/auth/register"),
                                         json=credentials(display_name="Impostor"))

        assert response.status_code == 409
        assert response.json()["error"] == "email_already_registered"
        assert len(api.users.users) == 1
        assert next(iter(api.users.users.values())).display_name == "Candidate"


@pytest.mark.asyncio
async def test_a_rejected_password_is_not_echoed_back(tmp_path):
    """The 422 names the field and nothing else.

    FastAPI's default body carries an `input` key holding the value it rejected,
    which for this request is a password. `install_v2_error_handlers` rebuilds the
    reply for `/api/v2` paths, and this is the assertion that it did.
    """
    async with api_harness(tmp_path) as api:
        response = await api.client.post(
            api.url("/auth/register"),
            json=credentials(password=SHORT_PASSWORD, display_name="Candidate"))

        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "validation_failed"
        assert [error["loc"] for error in body["errors"]] == [["body", "password"]]
        assert set(body["errors"][0]) == {"type", "loc", "msg"}
        assert SHORT_PASSWORD.get_secret_value() not in response.text
        assert api.users.users == {}


@pytest.mark.asyncio
async def test_an_unknown_address_and_a_wrong_password_answer_identically(tmp_path):
    """Byte-identical, because a difference is an account-enumeration oracle.

    The service is what guarantees it; this is the check that no handler between
    the service and the wire reintroduces a distinction — a `WWW-Authenticate`
    header on one of them would be enough.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        unknown = await api.client.post(api.url("/auth/login"),
                                        json=credentials(email=OTHER_EMAIL))
        wrong = await api.client.post(
            api.url("/auth/login"), json=credentials(password=WRONG_PASSWORD))

        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json() == wrong.json() == {
            "error": "invalid_credentials",
            "detail": "that email address and password do not match an account"}
        assert "set-cookie" not in unknown.headers
        assert "set-cookie" not in wrong.headers


@pytest.mark.asyncio
async def test_logging_in_issues_a_second_session_and_replaces_the_cookies(tmp_path):
    """A new browser gets a new session; the first one is not revoked by it.

    Logging in on a phone must not sign the laptop out. Revocation is what
    `/auth/logout` is for, and it is deliberately not a side effect of logging in
    somewhere else.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        first_token = api.session_token

        response = await api.log_in()

        assert response.status_code == 200
        assert api.session_token != first_token
        assert len(api.sessions.sessions) == 2
        assert all(session.revoked_at is None
                   for session in api.sessions.sessions.values())
        assert (await api.read("/auth/session")).status_code == 200


@pytest.mark.asyncio
async def test_the_session_route_needs_the_cookie_and_says_so_once(tmp_path):
    """401 with one code for every reason a session may be missing or dead.

    This is the route the frontend calls on every page load, so the visitor case —
    no cookie at all — is the normal answer rather than an error.
    """
    async with api_harness(tmp_path) as api:
        anonymous = await api.read("/auth/session")

        assert anonymous.status_code == 401
        assert anonymous.json()["error"] == "not_authenticated"

        await api.sign_in()
        assert (await api.read("/auth/session")).status_code == 200


@pytest.mark.asyncio
async def test_an_expired_session_is_refused_although_the_browser_still_sends_it(
        tmp_path):
    """The lifetime is absolute and enforced server-side, not by the cookie's age.

    A cookie's `Max-Age` is advice to a browser; an attacker holding a copied token
    ignores it. So the test keeps sending the cookie and expects the *server* to
    refuse — and expects the row to still be there, because sweeping expired rows
    is a background job's business rather than a request's.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        api.clock.advance(timedelta(hours=api.settings.session_hours, minutes=1))

        response = await api.read("/auth/session")

        assert response.status_code == 401
        assert response.json()["error"] == "not_authenticated"
        assert api.session_token is not None, "the browser must still be sending it"
        assert len(api.sessions.sessions) == 1


@pytest.mark.asyncio
async def test_logging_out_revokes_the_row_and_the_token_stops_working(tmp_path):
    """204, `revoked_at` set, and the same token refused afterwards.

    Revocation is the half that matters: clearing a cookie asks a browser to forget
    a credential that would otherwise keep working for anybody who copied it. The
    second request re-plants the cleared cookie by hand, which is exactly what a
    stolen token is.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        token = api.session_token
        assert token is not None

        response = await api.write("POST", "/auth/logout")

        assert response.status_code == 204
        assert response.content == b""
        revoked = next(iter(api.sessions.sessions.values()))
        assert revoked.revoked_at == api.clock.instant
        assert api.session_token is None, "the cookie should have been cleared"

        api.plant_cookie(api.settings.session_cookie_name, token)
        replayed = await api.read("/auth/session")
        assert replayed.status_code == 401


@pytest.mark.asyncio
async def test_logging_out_clears_both_cookies_with_the_attributes_they_carried(
        tmp_path):
    """A deletion is matched on name, path and domain, so it must repeat them.

    `Max-Age=0` with a different `Path` would leave the original cookie in place —
    logged out on the server and still carrying a dead credential in the browser,
    which is the failure this asserts against.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        response = await api.write("POST", "/auth/logout")

        for name in (api.settings.session_cookie_name,
                     api.settings.csrf_cookie_name):
            cleared = cookie_attributes(response, name)
            assert cleared["value"] == ""
            assert cleared["max-age"] == "0"
            assert cleared["path"] == "/"
            assert "domain" not in cleared
        assert cookie_attributes(
            response, api.settings.session_cookie_name)["httponly"] is True


@pytest.mark.asyncio
async def test_logging_out_twice_is_refused_by_the_missing_session(tmp_path):
    """The second call has no cookie left, so it is a 401 rather than a 204.

    Worth pinning: `log_out` itself is idempotent, and the reason a second call
    fails is that the credential is gone — not that revocation objected.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        assert (await api.write("POST", "/auth/logout")).status_code == 204

        again = await api.write("POST", "/auth/logout")

        assert again.status_code == 401
        assert again.json()["error"] == "not_authenticated"


@pytest.mark.asyncio
async def test_an_unsafe_request_without_the_csrf_header_changes_nothing(tmp_path):
    """403, and the session it was made with is still live.

    The cookie alone is not authority to write. A refusal that also ended the
    session would be a denial of service any third-party page could trigger, so the
    store is checked as well as the status.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        response = await api.client.post(api.url("/auth/logout"))

        assert response.status_code == 403
        assert response.json()["error"] == "csrf_failed"
        assert CSRF_HEADER in response.json()["detail"]
        assert next(iter(api.sessions.sessions.values())).revoked_at is None
        assert (await api.read("/auth/session")).status_code == 200


@pytest.mark.asyncio
async def test_a_csrf_token_that_is_not_this_session_s_is_refused(tmp_path):
    """Three near-misses, none of which is the digest stored with the session.

    The planted cookie is the case a plain double submit cannot survive: anything
    able to *write* the CSRF cookie — a compromised sibling subdomain, say —
    satisfies a header-equals-cookie comparison. Here the header is compared
    against server-side state, so writing the cookie buys nothing.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        first_csrf = api.csrf_token
        assert first_csrf is not None
        await api.log_in()  # a second session, with a CSRF token of its own

        for header in ({CSRF_HEADER: "not-a-token"},
                       {CSRF_HEADER: first_csrf},
                       {CSRF_HEADER: api.session_token or ""}):
            response = await api.write("POST", "/auth/logout", headers=header)
            assert response.status_code == 403, header
            assert response.json()["error"] == "csrf_failed"

        api.plant_cookie(api.settings.csrf_cookie_name, "planted")
        planted = await api.write("POST", "/auth/logout")
        assert planted.status_code == 403
        assert all(session.revoked_at is None
                   for session in api.sessions.sessions.values())


@pytest.mark.asyncio
async def test_a_write_the_browser_calls_cross_site_is_refused_before_the_password(
        tmp_path):
    """Login CSRF: an attacker's page must not be able to sign a visitor in.

    `login` and `register` have no session yet and therefore no CSRF token to
    check, so `Sec-Fetch-Site` is what closes them — and it is applied at router
    level, before the body is read, which is why no account exists afterwards.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in(email=OTHER_EMAIL)
        accounts = len(api.users.users)

        for path, body in (("/auth/login", credentials(email=OTHER_EMAIL)),
                           ("/auth/register", credentials())):
            response = await api.client.post(
                api.url(path), json=body,
                headers={"Sec-Fetch-Site": "cross-site"})
            assert response.status_code == 403, path
            assert response.json()["error"] == "csrf_failed"

        assert len(api.users.users) == accounts
        assert len(api.sessions.sessions) == 1


@pytest.mark.asyncio
async def test_a_same_site_or_absent_sec_fetch_site_header_is_allowed(tmp_path):
    """`curl`, the test suite and every server-to-server client send no such header.

    Requiring it would reject all of them, so a missing header passes and the
    double-submit check is what protects an authenticated write. Pinned as a test
    because "reject unless same-site" is the tempting change to make here.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        response = await api.client.post(
            api.url("/auth/login"), json=credentials(),
            headers={"Sec-Fetch-Site": "same-origin"})

        assert response.status_code == 200


@pytest.mark.asyncio
async def test_too_many_wrong_passwords_lock_the_account_and_the_right_one_waits(
        tmp_path):
    """423 with `locked_until`, and no session issued by the correct password.

    The refusal a wrong password gets is 401 all the way through — including the
    attempt that trips the lock — because saying "locked" to somebody who does not
    know the password is an enumeration oracle. 423 is reserved for a caller who
    proved ownership and still has to wait, which is the only case where the word
    is help rather than a leak.

    `max_failed_logins` is shortened to three: Argon2id costs ~50 ms a verification
    by design, so ten would spend a second of the suite proving nothing extra.
    """
    settings = AuthSettings.for_local_http().model_copy(
        update={"max_failed_logins": 3})
    async with api_harness(tmp_path, settings=settings) as api:
        await api.sign_in()
        sessions_before = len(api.sessions.sessions)

        for attempt in range(settings.max_failed_logins):
            refused = await api.log_in(password=WRONG_PASSWORD)
            assert refused.status_code == 401, attempt
            assert refused.json()["error"] == "invalid_credentials", attempt

        locked = await api.log_in()

        assert locked.status_code == 423
        body = locked.json()
        assert body["error"] == "account_locked"
        assert body["locked_until"] == (
            api.clock.instant + settings.lockout_duration).isoformat()
        assert "set-cookie" not in locked.headers
        assert len(api.sessions.sessions) == sessions_before


@pytest.mark.asyncio
async def test_a_wrong_password_during_a_lockout_does_not_extend_it(tmp_path):
    """The lock is temporary, and guessing at a locked account cannot prolong it.

    Otherwise anyone who knows an address could keep it locked for ever by failing
    a login every fourteen minutes — a denial of service with no cost to run. So
    attempts made during the lock are refused without counting, and the original
    deadline still stands.
    """
    settings = AuthSettings.for_local_http().model_copy(
        update={"max_failed_logins": 2})
    async with api_harness(tmp_path, settings=settings) as api:
        await api.sign_in()
        for _ in range(settings.max_failed_logins):
            assert (await api.log_in(password=WRONG_PASSWORD)).status_code == 401
        deadline = api.clock.instant + settings.lockout_duration

        api.clock.advance(settings.lockout_duration / 2)
        assert (await api.log_in(password=WRONG_PASSWORD)).status_code == 401
        still_locked = await api.log_in()

        assert still_locked.status_code == 423
        assert still_locked.json()["locked_until"] == deadline.isoformat()

        api.clock.advance(settings.lockout_duration)
        assert (await api.log_in()).status_code == 200, "the lock must have lapsed"


@pytest.mark.asyncio
async def test_disabling_an_account_stops_the_session_it_already_had(tmp_path):
    """One `UPDATE status` ends an account, without a second one over sessions.

    The row is deliberately left alone: `authenticate` checks the account on every
    request, so an operator disabling somebody does not have to remember to revoke
    their sessions too — and the answer is the same 401 as any other dead cookie,
    because "disabled" is not something an unauthenticated caller may learn.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        assert (await api.read("/auth/session")).status_code == 200
        account = next(iter(api.users.users.values()))

        await api.users.upsert(
            account.model_copy(update={"status": UserStatus.DISABLED}))

        response = await api.read("/auth/session")
        assert response.status_code == 401
        assert response.json()["error"] == "not_authenticated"
        assert api.sessions.sessions, "the session row should still be there"
        assert all(session.revoked_at is None
                   for session in api.sessions.sessions.values())

        refused = await api.log_in()
        assert refused.status_code == 403
        assert refused.json()["error"] == "account_disabled"


@pytest.mark.asyncio
async def test_the_production_policy_emits_two_host_prefixed_hardened_cookies(
        tmp_path):
    """The attributes the browser is asked for, read off the header it was sent.

    `__Host-` is only honoured when the cookie is `Secure`, `Path=/` and carries no
    `Domain`, so the prefix and those three attributes are asserted together — a
    name with the prefix and a `Domain` attribute is a cookie the browser drops
    outright, which would look like a broken login rather than a policy mistake.

    The two differ in exactly one attribute: the session cookie is `HttpOnly` and
    the CSRF cookie is not, because the frontend has to read the latter to echo it
    in `X-CSRF-Token`. That the readable one is *not* the session token is asserted
    here too — it is the whole reason two cookies exist.
    """
    async with api_harness(tmp_path, settings=AuthSettings(),
                           base_url=HTTPS_BASE_URL) as api:
        response = await api.register()

        assert response.status_code == 201
        assert api.settings.session_cookie_name == "__Host-jobsearch_session"
        assert api.settings.csrf_cookie_name == "__Host-jobsearch_csrf"
        session = cookie_attributes(response, api.settings.session_cookie_name)
        csrf = cookie_attributes(response, api.settings.csrf_cookie_name)
        for cookie in (session, csrf):
            assert cookie["secure"] is True
            assert cookie["path"] == "/"
            assert "domain" not in cookie
            assert cookie["samesite"] == "lax"
            assert cookie["max-age"] == str(api.settings.session_hours * 3600)
        assert session["httponly"] is True
        assert "httponly" not in csrf, "the frontend must be able to read it"
        assert session["value"] != csrf["value"]
        assert (await api.read("/auth/session")).status_code == 200


@pytest.mark.asyncio
async def test_secure_is_a_deployment_setting_and_not_read_off_the_request(tmp_path):
    """The default policy still says `Secure` when the request arrives over HTTP.

    This is the reverse-proxy case: the browser speaks HTTPS to the edge, the edge
    speaks HTTP to the application, and a cookie flag inferred from
    `request.url.scheme` would be dropped precisely where it matters. So the flag
    comes from `AuthSettings.cookie_secure` and nothing else.

    The 401 at the end is the other half of the same fact, and the reason
    `AuthSettings.for_local_http()` exists: a jar that has just accepted a `Secure`
    cookie will not send it back over `http://`, so a developer who runs the
    production policy on `localhost` gets a login that appears to succeed and then
    does not.
    """
    async with api_harness(tmp_path, settings=AuthSettings(),
                           base_url=LOCAL_HTTP_BASE_URL) as api:
        response = await api.register()

        assert response.status_code == 201
        assert api.client.base_url.scheme == "http"
        for name in (api.settings.session_cookie_name,
                     api.settings.csrf_cookie_name):
            assert cookie_attributes(response, name)["secure"] is True
        assert (await api.read("/auth/session")).status_code == 401
