"""Deployment settings: where the V2 database is, and how the session cookie is set.

Both answers are read from the environment, both are frozen once built, and both
default to the safe value rather than the convenient one — a deployment that
forgets a variable must fail or stay strict, never silently become permissive.

**The database URL.** One URL serves both engines. `postgresql+psycopg` is the
psycopg 3 dialect, and SQLAlchemy accepts it for `create_engine` *and*
`create_async_engine`, so the async repositories and the synchronous Alembic run
share a single DSN with no second driver to keep in step (asyncpg would have
forced two).

Resolution order, most specific first:

1. `JOBSEARCH_DATABASE_URL` — project-scoped, so an unrelated `DATABASE_URL`
   already exported in a shell cannot silently redirect this application;
2. `DATABASE_URL` — the conventional name, which is what docker-compose and CI
   inject;
3. `LOCAL_DEV_DATABASE_URL` — the docker-compose development database.

The fallback is deliberate but narrow: it points at 127.0.0.1 on a non-default
port with a password that only exists inside `docker-compose.yml`. Production
never reaches it, because a deployment that forgets to set `DATABASE_URL` fails
to connect to a loopback address rather than writing somewhere unexpected.

**The session cookie.** `AuthSettings` is configuration, never inference. The
tempting shortcut — `secure=request.url.scheme == "https"` — is wrong behind a
TLS-terminating proxy, where the browser speaks HTTPS and the application sees
plain HTTP on an internal address: the cookie would lose its `Secure` attribute
in exactly the deployment that needs it most. So `cookie_secure` defaults to true
and only an explicit environment variable turns it off, for local HTTP work.
"""
import re
from collections.abc import Mapping
from datetime import timedelta
from os import environ
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Matches docker-compose.yml. Not a secret: the port is bound to 127.0.0.1 and
# the credentials exist only in that file, which is why it is safe to write here
# and why `docs/PERSISTENCE.md` tells deployments to override it.
LOCAL_DEV_DATABASE_URL: Final[str] = (
    "postgresql+psycopg://jobsearch:jobsearch_dev_only@127.0.0.1:55432/jobsearch_dev"
)

# The test suite's own database. Kept separate from the development URL so that
# running the tests can never drop and recreate a database holding imported V1
# data — `tests/conftest.py` does exactly that to the database it is given.
LOCAL_TEST_DATABASE_URL: Final[str] = (
    "postgresql+psycopg://jobsearch:jobsearch_dev_only@127.0.0.1:55432/jobsearch_test"
)

DATABASE_URL_VARIABLES: Final[tuple[str, ...]] = (
    "JOBSEARCH_DATABASE_URL", "DATABASE_URL",
)
TEST_DATABASE_URL_VARIABLES: Final[tuple[str, ...]] = (
    "JOBSEARCH_TEST_DATABASE_URL", "TEST_DATABASE_URL",
)

_DRIVERLESS_PREFIX: Final[str] = "postgresql://"
_REQUIRED_DIALECT: Final[str] = "postgresql"
_DRIVER_URL: Final[str] = "postgresql+psycopg://"

# `user:password@` inside a DSN. Used to blank the password before a URL reaches
# a log line or a traceback (docs/ENGINEERING_STANDARDS.md §Security).
_CREDENTIALS_RE: Final[re.Pattern[str]] = re.compile(r"://([^:/@]+):([^@]*)@")


def redact_database_url(url: str) -> str:
    """The same URL with the password replaced, safe to log or print.

    Every user-facing report and error in Phase 2 goes through this: a DSN is
    the one configuration value that routinely carries a credential, and a
    connection failure is exactly the moment something prints it.
    """
    return _CREDENTIALS_RE.sub(r"://\1:***@", url)


def normalize_database_url(url: str) -> str:
    """Force an explicit driver, and refuse anything that is not PostgreSQL.

    A bare `postgresql://` DSN (what compose files and hosting providers hand
    out) resolves to whichever DBAPI SQLAlchemy finds first, which is how one
    machine ends up on psycopg2 and another on psycopg 3. Naming the driver
    removes the ambiguity.

    Refusing a non-PostgreSQL URL is a safety property, not pedantry: the V1
    SQLite file is the import *source*, and a `sqlite://` value slipping into
    this setting would point the V2 schema at it.
    """
    trimmed = url.strip()
    if not trimmed:
        raise ValueError("database URL must not be empty")
    if trimmed.startswith(_DRIVERLESS_PREFIX):
        trimmed = _DRIVER_URL + trimmed[len(_DRIVERLESS_PREFIX):]
    if not trimmed.startswith(f"{_REQUIRED_DIALECT}+") and \
            not trimmed.startswith(f"{_REQUIRED_DIALECT}:"):
        raise ValueError(
            "V2 persistence is PostgreSQL/PostGIS only; refusing database URL "
            f"{redact_database_url(trimmed)!r}")
    return trimmed


class DatabaseSettings(BaseModel):
    """How to reach PostgreSQL, and nothing else.

    Frozen so a service cannot be handed settings that change under it, and
    `extra="forbid"` so a misspelled keyword is an error rather than a silently
    ignored option — the same contract the domain models use.

    `url` is excluded from the repr on purpose. Pydantic's default repr prints
    every field, which would put the password in any traceback that renders a
    settings object; `__repr__` below shows the redacted form instead.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(repr=False)
    # SQLAlchemy's statement echo. Off by default: `echo=True` writes every
    # statement, including parameter values, to stderr — fine while debugging a
    # migration, not something a deployment should be able to switch on by
    # accident with candidate data in the tables.
    echo: bool = False

    @field_validator("url")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return normalize_database_url(value)

    @property
    def redacted_url(self) -> str:
        """The URL with its password blanked, for logs and error messages."""
        return redact_database_url(self.url)

    def __repr__(self) -> str:
        return f"DatabaseSettings(url={self.redacted_url!r}, echo={self.echo})"

    __str__ = __repr__

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None,
                 *, variables: tuple[str, ...] = DATABASE_URL_VARIABLES,
                 default: str = LOCAL_DEV_DATABASE_URL) -> Self:
        """Read the URL from the environment, most specific variable first.

        `env` is injectable so the resolution order can be tested without
        mutating `os.environ`, which would leak between tests.
        """
        source = environ if env is None else env
        for name in variables:
            value = source.get(name, "").strip()
            if value:
                return cls(url=value)
        return cls(url=default)

    @classmethod
    def for_tests(cls, env: Mapping[str, str] | None = None) -> Self:
        """The test database, which the suite is allowed to drop and recreate."""
        return cls.from_env(env, variables=TEST_DATABASE_URL_VARIABLES,
                            default=LOCAL_TEST_DATABASE_URL)


# The `__Host-` prefix is a browser-enforced guarantee, not a naming convention: a
# cookie so named is accepted only when it is `Secure`, `Path=/` and carries no
# `Domain`, and — the property that matters here — it cannot be set by a sibling
# subdomain. That closes cookie-shadowing, where `evil.example.com` writes a
# `jobsearch_csrf` that `app.example.com` would otherwise read back.
#
# The prefix is therefore tied to `cookie_secure`: with `Secure` off the browser
# rejects the cookie outright, so local HTTP development uses the bare names.
HOST_COOKIE_PREFIX: Final[str] = "__Host-"
SESSION_COOKIE_BASE_NAME: Final[str] = "jobsearch_session"
CSRF_COOKIE_BASE_NAME: Final[str] = "jobsearch_csrf"

# Not `X-XSRF-TOKEN`: that name is Angular's convention and carries the
# expectation that the framework fills it in. Ours is filled in by
# `frontend/app/utils/api-client.ts`, which is the only place that reads the CSRF
# cookie.
CSRF_HEADER: Final[str] = "X-CSRF-Token"

AUTH_COOKIE_SECURE_VARIABLE: Final[str] = "JOBSEARCH_AUTH_COOKIE_SECURE"
AUTH_SAME_SITE_VARIABLE: Final[str] = "JOBSEARCH_AUTH_COOKIE_SAMESITE"
AUTH_SESSION_HOURS_VARIABLE: Final[str] = "JOBSEARCH_AUTH_SESSION_HOURS"
AUTH_MAX_FAILED_LOGINS_VARIABLE: Final[str] = "JOBSEARCH_AUTH_MAX_FAILED_LOGINS"
AUTH_LOCKOUT_MINUTES_VARIABLE: Final[str] = "JOBSEARCH_AUTH_LOCKOUT_MINUTES"

_TRUE_WORDS: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})


def _read_bool(source: Mapping[str, str], name: str, default: bool) -> bool:
    """Parse a boolean environment variable, or refuse to guess.

    An unrecognised value raises instead of falling back. `COOKIE_SECURE=flase`
    must not quietly mean "insecure": the typo that disables a security attribute
    has to stop the process, which is the whole reason this is not
    `value.lower() == "true"`.
    """
    raw = source.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_WORDS:
        return True
    if raw in _FALSE_WORDS:
        return False
    raise ValueError(f"{name} must be one of "
                     f"{sorted(_TRUE_WORDS | _FALSE_WORDS)}; got {raw!r}")


def _read_int(source: Mapping[str, str], name: str, default: int) -> int:
    """Parse an integer environment variable, or refuse to guess."""
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from error


def _read_same_site(source: Mapping[str, str],
                    name: str) -> Literal["lax", "strict"]:
    """Parse the SameSite policy, case-insensitively, and reject `none` by name.

    `none` gets its own error message because it is the value someone will
    deliberately reach for when a cross-origin frontend fails to send the cookie.
    The answer is that `SameSite=None` makes the session cookie accompany requests
    from *any* site, and the fix is a same-site deployment, not this setting.
    """
    raw = source.get(name, "").strip().lower()
    if not raw or raw == "lax":
        return "lax"
    if raw == "strict":
        return "strict"
    if raw == "none":
        raise ValueError(
            f"{name}=none would attach the session cookie to cross-site requests; "
            "use 'lax' or 'strict'")
    raise ValueError(f"{name} must be 'lax' or 'strict'; got {raw!r}")



class AuthSettings(BaseModel):
    """The session cookie policy and the lockout policy, as deployment settings.

    Frozen and `extra="forbid"`, like `DatabaseSettings`. Nothing here is a secret,
    so there is no custom repr: every field is safe to print, and that is itself
    the design — no signing key, because sessions are server-side rows rather than
    self-contained signed tokens (docs/AUTHENTICATION.md §Why server-side).

    `Path=/` and the absence of `Domain` are *not* fields. They are fixed in
    `backend.app.api.cookies` because `__Host-` requires exactly that combination;
    making them configurable would only create combinations the browser rejects.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # True by default and never inferred from the request. See the module
    # docstring: behind a TLS-terminating proxy the application sees `http://`
    # while the browser is on HTTPS, so inference drops `Secure` precisely where
    # it is needed. Turning it off is an explicit act, for local HTTP only.
    cookie_secure: bool = True

    # `lax` rather than `strict` because `strict` withholds the cookie on a
    # top-level navigation *from another site* — a user following a link from
    # their mail client to a job page would land logged out. `none` is not
    # offered: it exists to allow cross-site sends, and nothing here is embedded
    # in a third-party page.
    cookie_same_site: Literal["lax", "strict"] = "lax"

    # Seven days. Long enough that a working session survives a weekend, short
    # enough that a stolen cookie is not indefinite. Absolute, not sliding: a
    # sliding window keeps a stolen session alive for as long as it is used.
    session_hours: int = Field(default=168, gt=0, le=8760)

    # Ten attempts, then a temporary lock. Low enough to stop online guessing,
    # high enough that a typing mistake does not lock a real user out.
    max_failed_logins: int = Field(default=10, ge=1, le=1000)

    # Fifteen minutes, and temporary on purpose: a permanent lock on failures is
    # a denial of service anybody can trigger against a known address.
    lockout_minutes: int = Field(default=15, gt=0, le=1440)

    @field_validator("cookie_same_site", mode="before")
    @classmethod
    def _lower_same_site(cls, value: object) -> object:
        """Accept `Lax` and `LAX`: the attribute is case-insensitive in browsers.

        Only the case is normalized — an unknown word still fails the `Literal`,
        which is what keeps `none` out no matter how it is spelled.
        """
        return value.strip().lower() if isinstance(value, str) else value

    @property
    def session_cookie_name(self) -> str:
        """`__Host-jobsearch_session` when `Secure`, `jobsearch_session` when not."""
        prefix = HOST_COOKIE_PREFIX if self.cookie_secure else ""
        return f"{prefix}{SESSION_COOKIE_BASE_NAME}"

    @property
    def csrf_cookie_name(self) -> str:
        """The JS-readable half of the double submit, prefixed on the same rule."""
        prefix = HOST_COOKIE_PREFIX if self.cookie_secure else ""
        return f"{prefix}{CSRF_COOKIE_BASE_NAME}"

    @property
    def session_lifetime(self) -> timedelta:
        return timedelta(hours=self.session_hours)

    @property
    def lockout_duration(self) -> timedelta:
        return timedelta(minutes=self.lockout_minutes)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        """Build the settings from the environment, defaulting to the strict form.

        `env` is injectable for the same reason as in `DatabaseSettings`: the
        resolution can then be tested without mutating `os.environ`.
        """
        source = environ if env is None else env
        return cls(
            cookie_secure=_read_bool(source, AUTH_COOKIE_SECURE_VARIABLE, True),
            cookie_same_site=_read_same_site(source, AUTH_SAME_SITE_VARIABLE),
            session_hours=_read_int(source, AUTH_SESSION_HOURS_VARIABLE, 168),
            max_failed_logins=_read_int(
                source, AUTH_MAX_FAILED_LOGINS_VARIABLE, 10),
            lockout_minutes=_read_int(source, AUTH_LOCKOUT_MINUTES_VARIABLE, 15))

    @classmethod
    def for_local_http(cls) -> Self:
        """The one supported way to run without `Secure`, named so it is greppable.

        Used by `docker-compose` development and by the request-flow tests, whose
        cookie jar refuses to send a `Secure` cookie over `http://testserver`.
        A deployment that reaches for this in production has to write the words
        "local http" to do it.
        """
        return cls(cookie_secure=False)



