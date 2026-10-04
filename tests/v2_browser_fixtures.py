"""Deterministic local ATS fixtures for the real-Playwright suite (§35).

No test ever touches a live job board (CLAUDE.md §Testing, §35). These are static HTML pages that
reproduce the *structural* patterns of Greenhouse, Lever and Ashby forms — labelled inputs, a file
upload, a submit control, a confirmation page — plus the hard-stop pages (CAPTCHA, MFA, login) and
the changed-form and no-confirmation variants the safety tests need. They are served from a
throwaway threaded HTTP server on a random localhost port, so a real Chromium drives real forms
against them; the server counts submissions so a test can assert the one-irreversible-click and
duplicate-protection invariants (§20, §39, §44).

Forms submit via GET to a confirmation path so the click navigates to a page whose URL and body
carry positive confirmation (§21); the server records each confirmation hit. A "no-confirmation"
variant submits to a page with no success cue, which is the STATE_UNKNOWN case (§22).
"""
from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ------------------------------------------------------------------ page bodies

_CONFIRMATION = (
    "<html><body><div id='application_confirmation'>"
    "Thank you! Your application was received. Reference REF12345"
    "</div></body></html>"
)
_NO_CONFIRMATION = "<html><body><div id='pending'>Processing…</div></body></html>"


def _greenhouse_form(*, csrf: str = "TOK_A", extra_required: str = "") -> str:
    """A Greenhouse-shaped form. `csrf` is a volatile hidden token (must not affect the
    fingerprint); `extra_required` injects a structural change for the changed-form test."""
    return f"""<html><body>
      <form id="application_form" method="GET" action="/greenhouse/confirmation">
        <input type="hidden" name="csrf_token" value="{csrf}">
        <label for="fn">First Name</label><input id="fn" name="first_name" required>
        <label for="ln">Last Name</label><input id="ln" name="last_name" required>
        <label for="em">Email</label><input id="em" name="email" type="email" required>
        <label for="ph">Phone</label><input id="ph" name="phone" type="tel">
        <label for="cv">Resume / CV</label><input id="cv" name="resume" type="file">
        <label for="q1">Why do you want this role?</label>
        <textarea id="q1" name="q_why"></textarea>
        {extra_required}
        <button id="submit_app" type="submit">Submit Application</button>
      </form>
    </body></html>"""


def _lever_form() -> str:
    return """<html><body>
      <form class="application-form" method="GET" action="/lever/confirmation">
        <label for="fn">Full name</label><input id="fn" name="name" required>
        <label for="em">Email</label><input id="em" name="email" type="email" required>
        <label for="ph">Phone</label><input id="ph" name="phone" type="tel">
        <label for="cv">Resume</label><input id="cv" name="resume" type="file">
        <button type="submit">Submit application</button>
      </form>
    </body></html>"""


def _ashby_form() -> str:
    return """<html><body>
      <button type="button">Application</button>
      <form class="ashby-application-form" method="GET" action="/ashby/confirmation">
        <label for="fn">First Name</label><input id="fn" name="_first" required>
        <label for="ln">Last Name</label><input id="ln" name="_last" required>
        <label for="em">Email</label><input id="em" name="_email" type="email" required>
        <label for="cv">Resume</label><input id="cv" name="_resume" type="file">
        <button type="submit">Submit Application</button>
      </form>
    </body></html>"""


def _sensitive_form() -> str:
    """A Greenhouse-shaped form whose one extra required field is demographic (§10)."""
    return _greenhouse_form(extra_required=(
        "<label for='g'>Gender identity</label>"
        "<select id='g' name='gender' required aria-required='true'>"
        "<option value=''></option><option>Woman</option><option>Man</option></select>"))


def _unknown_required_form() -> str:
    """A form with a required custom question the profile cannot answer (§11)."""
    return _greenhouse_form(extra_required=(
        "<label for='rc'>Internal referral code</label>"
        "<input id='rc' name='referral_code' required aria-required='true'>"))


def _captcha_form() -> str:
    return ("<html><body><form id='application_form' method='GET' action='/greenhouse/confirmation'>"
            "<label for='em'>Email</label><input id='em' name='email' required>"
            "<div class='g-recaptcha' data-sitekey='x'></div>"
            "<button type='submit'>Submit Application</button></form></body></html>")


def _mfa_form() -> str:
    return ("<html><body><form id='application_form' method='GET' action='/greenhouse/confirmation'>"
            "<label for='otp'>One-time code</label>"
            "<input id='otp' name='otp' autocomplete='one-time-code'>"
            "<button type='submit'>Submit Application</button></form></body></html>")


def _login_form() -> str:
    return ("<html><body><form id='application_form' method='GET' action='/greenhouse/confirmation'>"
            "<label for='u'>Username</label><input id='u' name='username'>"
            "<label for='p'>Password</label><input id='p' name='password' type='password'>"
            "<button type='submit'>Sign in</button></form></body></html>")


def _no_confirmation_form() -> str:
    """A Greenhouse-shaped form that submits to a page with no success cue (§22)."""
    return _greenhouse_form().replace(
        "action=\"/greenhouse/confirmation\"", "action=\"/greenhouse/pending\"")


def build_pages() -> dict[str, str]:
    """The full path→HTML map the fixture server serves."""
    return {
        "/greenhouse/job": _greenhouse_form(),
        "/greenhouse/job-alt-token": _greenhouse_form(csrf="TOK_B"),
        "/greenhouse/confirmation": _CONFIRMATION,
        "/greenhouse/pending": _NO_CONFIRMATION,
        "/greenhouse/sensitive": _sensitive_form(),
        "/greenhouse/unknown-required": _unknown_required_form(),
        "/greenhouse/no-confirm": _no_confirmation_form(),
        "/greenhouse/captcha": _captcha_form(),
        "/greenhouse/mfa": _mfa_form(),
        "/greenhouse/login": _login_form(),
        # Lever's driver appends `/apply`; serve the form there.
        "/lever/job/apply": _lever_form(),
        "/lever/confirmation": _CONFIRMATION,
        "/ashby/job": _ashby_form(),
        "/ashby/confirmation": _CONFIRMATION,
    }


# ------------------------------------------------------------------ the server


@dataclass
class FixtureSite:
    """A running fixture server: its base URL and a per-path submission counter."""

    base_url: str
    hits: dict[str, int] = field(default_factory=dict)

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def confirmations(self, path: str) -> int:
        """How many times a confirmation/target path was navigated to — the click count (§39)."""
        return self.hits.get(path, 0)


@contextlib.contextmanager
def serve_fixture_site(pages: dict[str, str] | None = None) -> Iterator[FixtureSite]:
    """Run the fixture site on a random localhost port for the duration of the block."""
    served = pages if pages is not None else build_pages()
    site = FixtureSite(base_url="")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path).path
            site.hits[path] = site.hits.get(path, 0) + 1
            body = served.get(path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args: object) -> None:  # keep the test output quiet
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    site.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
