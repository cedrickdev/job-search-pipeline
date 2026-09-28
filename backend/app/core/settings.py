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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The version tag the credential cipher writes new ciphertext under. Imported from the
# crypto module (a leaf that depends only on `cryptography` and Pydantic) so the
# "current secret version" has one source of truth rather than a literal repeated here.
from backend.app.llm.secrets import CURRENT_SECRET_VERSION

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

# Where rendered document PDFs are written. A directory under the working tree by
# default, so a fresh checkout produces artifacts without configuration; a
# deployment overrides it to a mounted volume, and a future object-store adapter
# would read a URL from its own variable instead.
DOCUMENT_ARTIFACT_ROOT_VARIABLE: Final[str] = "JOBSEARCH_DOCUMENT_ARTIFACT_ROOT"
DEFAULT_DOCUMENT_ARTIFACT_ROOT: Final[str] = "var/document_artifacts"

# Where account-export archives are written, and how long they stay downloadable (Phase 16
# §23-25, §30). A directory under the working tree by default, like the document store, so a
# fresh checkout produces exports without configuration; a deployment overrides it to a mounted
# volume and a future object-store adapter would read a URL from its own variable instead. The
# retention window is how long a produced archive stays available before the retention sweep
# (§30) is entitled to purge it — a portable copy of a user's data is temporary by design, so it
# does not linger on disk indefinitely.
EXPORT_ARTIFACT_ROOT_VARIABLE: Final[str] = "JOBSEARCH_EXPORT_ARTIFACT_ROOT"
DEFAULT_EXPORT_ARTIFACT_ROOT: Final[str] = "var/account_exports"
EXPORT_RETENTION_HOURS_VARIABLE: Final[str] = "JOBSEARCH_EXPORT_RETENTION_HOURS"
DEFAULT_EXPORT_RETENTION_HOURS: Final[int] = 168  # seven days

# The Fernet master key the LLM connection store encrypts provider credentials with
# (Phase 11, docs/LLM_PROVIDER_ARCHITECTURE.md §21). Read from the environment,
# never stored in the database and never returned by the API. A deployment that
# manages remote LLM connections must set it; one that uses only CLI and keyless
# local providers never needs it, so its absence is not an error until a credential
# has to be encrypted.
LLM_SECRET_KEY_VARIABLE: Final[str] = "JOBSEARCH_LLM_SECRET_KEY"  # noqa: S105 — an env var name, not a credential
# The version tag `JOBSEARCH_LLM_SECRET_KEY` writes new ciphertext under, and the
# previous keys a rotation keeps available so values written under an earlier version
# still decrypt (Phase 16 §22). During a rotation an operator sets the new key in
# `JOBSEARCH_LLM_SECRET_KEY`, bumps `..._VERSION`, and moves the old key to
# `..._V<N>` (e.g. `JOBSEARCH_LLM_SECRET_KEY_V1`); the rotation CLI then re-encrypts
# every stored credential at the new version. The `_V<N>` scheme takes digits only,
# so it never collides with the `_VERSION` variable.
LLM_SECRET_KEY_VERSION_VARIABLE: Final[str] = "JOBSEARCH_LLM_SECRET_KEY_VERSION"  # noqa: S105 — an env var name, not a credential
_LLM_SECRET_PREVIOUS_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^JOBSEARCH_LLM_SECRET_KEY_V(\d+)$")

# The Stripe billing adapter's credentials and endpoint (Phase 16 §11-13). The API key
# authorises calls *out* (opening a checkout or portal); the webhook secret verifies calls *in*.
# Both are read from the environment, excluded from the repr, and never stored in the database
# or returned by the API — a deployment not selling subscriptions (CLI-only, free tier) sets
# neither, and the adapter raises a clear error only if a billing call is attempted without them.
STRIPE_SECRET_KEY_VARIABLE: Final[str] = "JOBSEARCH_STRIPE_SECRET_KEY"  # noqa: S105 — an env var name, not a credential
STRIPE_WEBHOOK_SECRET_VARIABLE: Final[str] = "JOBSEARCH_STRIPE_WEBHOOK_SECRET"  # noqa: S105 — an env var name, not a credential
STRIPE_API_BASE_URL_VARIABLE: Final[str] = "JOBSEARCH_STRIPE_API_BASE_URL"
STRIPE_SIGNATURE_TOLERANCE_VARIABLE: Final[str] = "JOBSEARCH_STRIPE_SIGNATURE_TOLERANCE_SECONDS"
DEFAULT_STRIPE_API_BASE_URL: Final[str] = "https://api.stripe.com"
# Stripe's own default replay window: a signed payload older than this is rejected even with a
# valid signature, so a captured request cannot be replayed indefinitely.
DEFAULT_STRIPE_SIGNATURE_TOLERANCE_SECONDS: Final[int] = 300

# The public origin the browser reaches this deployment at — the base of the URLs a hosted
# checkout or billing portal sends the user back to (Phase 16 §18). Read from the environment and
# never client-supplied: the success, cancel and return URLs a checkout is opened with are built
# from this on the server, so a request cannot smuggle an attacker's origin into a provider
# redirect (open-redirect prevention). Defaults to the Nuxt dev origin so a fresh checkout works
# without configuration; a real deployment sets it to its own https origin.
SITE_PUBLIC_BASE_URL_VARIABLE: Final[str] = "JOBSEARCH_PUBLIC_BASE_URL"
DEFAULT_PUBLIC_BASE_URL: Final[str] = "http://localhost:3000"
# The path the `/billing` screen lives at, appended to the public base to form the redirect
# targets. A constant rather than a field: the frontend route is fixed, and a deployment tunes the
# origin, not the in-app path.
_BILLING_PATH: Final[str] = "/billing"

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


class DocumentSettings(BaseModel):
    """Where rendered document artifacts live.

    Frozen and closed like every settings model. Only the artifact root today —
    the store is a directory of PDFs (`LocalDocumentArtifactStore`), and the path
    is the one thing a deployment tunes. An object-store adapter would grow its own
    fields here rather than overloading this one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_root: str = DEFAULT_DOCUMENT_ARTIFACT_ROOT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        """Read the artifact root from the environment, or use the default path."""
        source = environ if env is None else env
        value = source.get(DOCUMENT_ARTIFACT_ROOT_VARIABLE, "").strip()
        return cls(artifact_root=value or DEFAULT_DOCUMENT_ARTIFACT_ROOT)


class ExportSettings(BaseModel):
    """Where account-export archives live, and how long they stay downloadable.

    Frozen and closed like every settings model. `artifact_root` is the directory the
    `LocalAccountExportStore` writes JSON archives under; an object-store adapter would
    grow its own fields here rather than overloading this one, exactly as `DocumentSettings`
    would. `retention_hours` is how long a produced archive stays downloadable before the
    retention sweep (Phase 16 §30) may purge it: an export is a temporary, portable copy of a
    user's own data, not a second permanent store, so it expires by design. A non-positive
    window would make every archive born already-expired, so it is refused here rather than
    silently disabling downloads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_root: str = DEFAULT_EXPORT_ARTIFACT_ROOT
    retention_hours: int = Field(default=DEFAULT_EXPORT_RETENTION_HOURS, ge=1)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        """Read the export root and retention window, or use the defaults."""
        source = environ if env is None else env
        value = source.get(EXPORT_ARTIFACT_ROOT_VARIABLE, "").strip()
        retention_hours = _read_int(
            source, EXPORT_RETENTION_HOURS_VARIABLE, DEFAULT_EXPORT_RETENTION_HOURS)
        return cls(
            artifact_root=value or DEFAULT_EXPORT_ARTIFACT_ROOT,
            retention_hours=retention_hours,
        )


class LLMSecretSettings(BaseModel):
    """The master key for encrypting stored LLM credentials — env-sourced, never DB.

    Frozen and closed like every settings model, and with the same custom repr rule
    as `DatabaseSettings`: no key material is ever printed, so a traceback that
    renders this object cannot leak one. `master_key` is optional because a
    deployment using only CLI and keyless local providers never encrypts anything;
    the service raises a clear error only if a credential must be stored while it is
    absent, rather than failing at startup for a feature the deployment does not use.

    `active_version` is the tag new ciphertext is written under, and `previous_keys`
    holds the older-version keys a rotation keeps available so a value stored under an
    earlier version still decrypts (Phase 16 §22). Outside a rotation window both are
    the default: version 1 and no previous keys. A previous key registered under the
    active version would silently shadow the active key, so it is refused here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    master_key: str | None = Field(default=None, repr=False)
    active_version: int = Field(default=CURRENT_SECRET_VERSION, ge=1)
    # version -> key, the keys a rotation can still decrypt with. `repr=False` and
    # never surfaced: a version list is safe to log, the keys are not.
    previous_keys: Mapping[int, str] = Field(default_factory=dict, repr=False)

    @property
    def has_key(self) -> bool:
        return bool(self.master_key)

    @model_validator(mode="after")
    def _previous_keys_are_strictly_older(self) -> Self:
        if self.active_version in self.previous_keys:
            raise ValueError(
                f"a previous key is registered under the active version "
                f"{self.active_version}; a rotation moves the old key to a lower "
                "version and sets the new key as active")
        return self

    def __repr__(self) -> str:
        versions = sorted(self.previous_keys)
        return (f"LLMSecretSettings(master_key={'set' if self.has_key else 'unset'!r}, "
                f"active_version={self.active_version!r}, "
                f"previous_versions={versions!r})")

    __str__ = __repr__

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        """Read the active key, its version, and any rotation-window previous keys.

        A previous key is any `JOBSEARCH_LLM_SECRET_KEY_V<N>` with a positive integer
        `N` — the version it decrypts. The active version defaults to
        `CURRENT_SECRET_VERSION`; an operator bumps it only during a rotation.
        """
        source = environ if env is None else env
        value = source.get(LLM_SECRET_KEY_VARIABLE, "").strip()
        previous: dict[int, str] = {}
        for name, raw in source.items():
            match = _LLM_SECRET_PREVIOUS_KEY_PATTERN.match(name)
            key = raw.strip()
            if match is None or not key:
                continue
            previous[int(match.group(1))] = key
        return cls(
            master_key=value or None,
            active_version=_read_int(source, LLM_SECRET_KEY_VERSION_VARIABLE,
                                     CURRENT_SECRET_VERSION),
            previous_keys=previous)


class StripeSettings(BaseModel):
    """The Stripe billing adapter's credentials and endpoint — env-sourced, never DB (§11-13).

    Frozen and closed like every settings model, and with the same custom repr rule as
    `DatabaseSettings` and `LLMSecretSettings`: both credentials are excluded from the repr so a
    traceback that renders this object cannot print them. `secret_key` (the `sk_...` API key)
    authorises calls *out*; `webhook_secret` (the `whsec_...`) verifies calls *in*. Both are
    optional because a deployment not selling subscriptions never sets them — the adapter raises
    a clear error only if a billing call is attempted while one is absent, rather than failing at
    startup for a feature the deployment does not use. `api_base_url` is overridable so a test can
    point the adapter at a mock transport and a self-hosted proxy can front the API;
    `signature_tolerance_seconds` is the replay window a signed webhook must fall within.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    secret_key: str | None = Field(default=None, repr=False)
    webhook_secret: str | None = Field(default=None, repr=False)
    api_base_url: str = DEFAULT_STRIPE_API_BASE_URL
    signature_tolerance_seconds: int = Field(
        default=DEFAULT_STRIPE_SIGNATURE_TOLERANCE_SECONDS, gt=0, le=86400)

    @property
    def can_call(self) -> bool:
        """Whether the API key needed to open a checkout or portal is configured."""
        return bool(self.secret_key)

    @property
    def can_verify_webhooks(self) -> bool:
        """Whether the signing secret needed to verify an inbound webhook is configured."""
        return bool(self.webhook_secret)

    def __repr__(self) -> str:
        return (f"StripeSettings(secret_key={'set' if self.can_call else 'unset'!r}, "
                f"webhook_secret={'set' if self.can_verify_webhooks else 'unset'!r}, "
                f"api_base_url={self.api_base_url!r}, "
                f"signature_tolerance_seconds={self.signature_tolerance_seconds!r})")

    __str__ = __repr__

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        """Read the Stripe credentials and endpoint, leaving the secrets unset when absent."""
        source = environ if env is None else env
        secret_key = source.get(STRIPE_SECRET_KEY_VARIABLE, "").strip()
        webhook_secret = source.get(STRIPE_WEBHOOK_SECRET_VARIABLE, "").strip()
        base_url = source.get(STRIPE_API_BASE_URL_VARIABLE, "").strip()
        return cls(
            secret_key=secret_key or None,
            webhook_secret=webhook_secret or None,
            api_base_url=base_url or DEFAULT_STRIPE_API_BASE_URL,
            signature_tolerance_seconds=_read_int(
                source, STRIPE_SIGNATURE_TOLERANCE_VARIABLE,
                DEFAULT_STRIPE_SIGNATURE_TOLERANCE_SECONDS))


class SiteSettings(BaseModel):
    """The public origin this deployment is reached at, and the redirect URLs derived from it.

    Frozen and closed like every settings model. The one tunable is `public_base_url`; the three
    redirect URLs a hosted checkout or portal needs are *derived* here rather than accepted from a
    request, which is the whole point (Phase 16 §18). A provider-hosted checkout takes a
    `success_url`, a `cancel_url` and a portal takes a `return_url`, and if any of those came from
    the client an attacker could open a checkout that, on success, bounced the victim's browser to
    a page the attacker controls. Building them from a server-held origin closes that: the client
    names only a plan slug, never a URL.

    The trailing slash is stripped so the join is unambiguous — `https://x/` and `https://x` both
    yield `https://x/billing`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    public_base_url: str = DEFAULT_PUBLIC_BASE_URL

    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        trimmed = value.strip().rstrip("/")
        if not trimmed:
            raise ValueError("the public base URL must not be empty")
        return trimmed

    @property
    def billing_url(self) -> str:
        """The in-app billing screen — where a portal returns and a checkout lands."""
        return f"{self.public_base_url}{_BILLING_PATH}"

    @property
    def checkout_success_url(self) -> str:
        """Where a hosted checkout sends the browser on success — server-built, not client-sent."""
        return f"{self.billing_url}?checkout=success"

    @property
    def checkout_cancel_url(self) -> str:
        """Where a hosted checkout sends the browser on cancel — server-built, never client-sent."""
        return f"{self.billing_url}?checkout=cancelled"

    @property
    def portal_return_url(self) -> str:
        """Where the billing portal returns the browser — server-built, never client-sent."""
        return self.billing_url

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        """Read the public origin from the environment, or use the local dev default."""
        source = environ if env is None else env
        value = source.get(SITE_PUBLIC_BASE_URL_VARIABLE, "").strip()
        return cls(public_base_url=value or DEFAULT_PUBLIC_BASE_URL)



