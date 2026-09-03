"""Where the V2 database is, and how that answer is reached.

One URL serves both engines. `postgresql+psycopg` is the psycopg 3 dialect, and
SQLAlchemy accepts it for `create_engine` *and* `create_async_engine`, so the
async repositories and the synchronous Alembic run share a single DSN with no
second driver to keep in step (asyncpg would have forced two).

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
"""
import re
from collections.abc import Mapping
from os import environ
from typing import Final, Self

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
