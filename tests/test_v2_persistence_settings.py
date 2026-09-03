# tests/test_v2_persistence_settings.py
"""How the database URL is resolved, normalized and redacted.

Three questions, none of which needs a database. Which environment variable wins,
because an operator with a `DATABASE_URL` already exported for something else must
not silently redirect this application. What a bare `postgresql://` DSN becomes,
because a URL with no driver resolves to whichever DBAPI happens to be installed.
And what a URL looks like once it reaches a log line, because a DSN is the one
configuration value that routinely carries a password
(docs/ENGINEERING_STANDARDS.md §Security).

The safety property worth stating plainly: `for_tests` reads *different* variables
from `from_env`. `tests/conftest.py` drops and recreates the public schema of
whatever database it is handed, so a `DATABASE_URL` pointing at a development
database holding imported V1 data must not reach it.
"""
import pytest
from pydantic import ValidationError

from backend.app.core.settings import (
    LOCAL_DEV_DATABASE_URL,
    LOCAL_TEST_DATABASE_URL,
    DatabaseSettings,
    normalize_database_url,
    redact_database_url,
)

# Obviously synthetic, and the value the redaction tests look for the absence of.
CREDENTIAL = "not-a-real-password"
DSN_WITH_CREDENTIAL = f"postgresql+psycopg://jobsearch:{CREDENTIAL}@db.internal:5432/jobsearch"

def test_the_project_scoped_variable_wins():
    """`JOBSEARCH_DATABASE_URL` is checked before `DATABASE_URL`.

    The reason the project-scoped name exists at all: `DATABASE_URL` is a shared
    convention, and a developer with one exported for another project should not
    have this application connect there.
    """
    settings = DatabaseSettings.from_env({
        "JOBSEARCH_DATABASE_URL": "postgresql://host/scoped",
        "DATABASE_URL": "postgresql://host/conventional"})
    assert settings.url.endswith("/scoped")


def test_the_conventional_variable_is_used_when_the_scoped_one_is_absent():
    """What docker-compose and CI actually inject."""
    settings = DatabaseSettings.from_env({"DATABASE_URL": "postgresql://host/injected"})
    assert settings.url.endswith("/injected")


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_variable_counts_as_unset(value):
    """`DATABASE_URL=` in a `.env` file is an empty string, not a missing key.

    Without the `strip()`, that empty value would win the resolution and fail
    validation — turning a harmless leftover line into a startup error instead of
    a fallback.
    """
    settings = DatabaseSettings.from_env({"DATABASE_URL": value,
                                          "JOBSEARCH_DATABASE_URL": value})
    assert settings.url == LOCAL_DEV_DATABASE_URL


def test_nothing_in_the_environment_reaches_the_compose_database():
    """The documented fallback: loopback, non-default port, compose-only password.

    Deliberately not a production-shaped default. A deployment that forgets to set
    `DATABASE_URL` fails to reach 127.0.0.1 rather than connecting somewhere it was
    not meant to and writing candidate data there.
    """
    settings = DatabaseSettings.from_env({})
    assert settings.url == LOCAL_DEV_DATABASE_URL
    assert "127.0.0.1:55432" in settings.url


def test_the_test_suite_reads_its_own_variables():
    """`for_tests` cannot be redirected by the variables `from_env` reads.

    This is the guard on a destructive operation: the fixture that builds the
    schema runs `DROP SCHEMA public CASCADE` on the database it is given. A
    `DATABASE_URL` exported for development work must therefore be invisible here.
    """
    environment = {"DATABASE_URL": "postgresql://host/development",
                   "JOBSEARCH_DATABASE_URL": "postgresql://host/development"}
    assert DatabaseSettings.for_tests(environment).url == LOCAL_TEST_DATABASE_URL
    assert DatabaseSettings.for_tests({
        "TEST_DATABASE_URL": "postgresql://host/ci"}).url.endswith("/ci")


def test_the_two_defaults_are_different_databases():
    """A different database name, not a different host: the same server is fine.

    If these ever collapsed to one value, every test run would drop the schema of
    the development database — including any imported V1 data in it.
    """
    assert LOCAL_DEV_DATABASE_URL != LOCAL_TEST_DATABASE_URL
    assert LOCAL_DEV_DATABASE_URL.endswith("/jobsearch_dev")
    assert LOCAL_TEST_DATABASE_URL.endswith("/jobsearch_test")


def test_a_url_with_no_driver_is_given_one():
    """`postgresql://` is ambiguous; `postgresql+psycopg://` is not.

    The form hosting providers and compose files hand out. Left alone, SQLAlchemy
    picks whichever DBAPI it finds first, which is how one machine runs psycopg2
    and another psycopg 3 from the same configuration — and only one of them
    supports the async engine this project uses.
    """
    assert normalize_database_url("postgresql://host/jobsearch") == \
        "postgresql+psycopg://host/jobsearch"


def test_a_url_that_names_its_driver_is_left_alone():
    """Normalization adds a driver; it does not impose one."""
    assert normalize_database_url(DSN_WITH_CREDENTIAL) == DSN_WITH_CREDENTIAL


def test_surrounding_whitespace_is_removed():
    """A trailing newline from a secrets file or a copied shell line."""
    assert normalize_database_url("  postgresql+psycopg://host/db\n") == \
        "postgresql+psycopg://host/db"


@pytest.mark.parametrize("url", [
    # The V1 database. It is the import *source* and read-only; pointing the V2
    # schema at it is the mistake this refusal exists for.
    "sqlite:///data/tracker.db",
    "mysql://host/jobsearch",
    "postgres://host/jobsearch",   # the deprecated scheme, not a dialect name
    "",
    "   ",
])
def test_anything_that_is_not_postgresql_is_refused(url):
    """PostGIS is a requirement of the schema, not a preference.

    Two migrations create a `geography(Point,4326)` column and one enables the
    extension. Nothing else can run them, so a non-PostgreSQL URL fails at
    configuration time rather than part-way through `alembic upgrade head`.
    """
    with pytest.raises(ValueError):
        normalize_database_url(url)


def test_a_refused_url_is_named_in_the_error_without_its_password():
    """The error has to be actionable and must not print the credential.

    A refusal is exactly the moment a DSN gets copied into a terminal, a log
    aggregator or a bug report.
    """
    with pytest.raises(ValueError, match="refusing database URL") as raised:
        normalize_database_url(f"mysql://jobsearch:{CREDENTIAL}@db.internal/jobsearch")
    assert CREDENTIAL not in str(raised.value)
    assert "jobsearch:***@db.internal" in str(raised.value)


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://host/jobsearch",       # no credentials at all
    "postgresql+psycopg://jobsearch@host/db",    # user, no password
])
def test_redacting_a_url_without_a_password_changes_nothing(url):
    """The regex must not mangle the forms it has nothing to blank."""
    assert redact_database_url(url) == url


def test_the_password_survives_where_it_is_needed_and_nowhere_else():
    """The connection needs the real DSN; every readable form must not have it.

    `repr` is the one that matters, because it is what an unhandled exception
    renders when a settings object appears in a frame — and Pydantic's default
    `repr` prints every field, which is why `url` is declared `repr=False`.
    """
    settings = DatabaseSettings(url=DSN_WITH_CREDENTIAL)
    assert settings.url == DSN_WITH_CREDENTIAL
    for rendered in (repr(settings), str(settings), settings.redacted_url):
        assert CREDENTIAL not in rendered
        assert "jobsearch:***@db.internal" in rendered


def test_the_repr_still_says_which_database_and_whether_echo_is_on():
    """Redaction must not make the object useless to look at.

    "Cannot connect" is unanswerable without the host and database name, so the
    redacted form keeps everything except the password.
    """
    rendered = repr(DatabaseSettings(url=DSN_WITH_CREDENTIAL, echo=True))
    assert "db.internal:5432/jobsearch" in rendered
    assert "echo=True" in rendered


def test_statement_echo_is_off_unless_it_is_asked_for():
    """`echo=True` prints every statement and its parameters to stderr.

    Fine while debugging a migration; not something a deployment should switch on
    by accident with candidate data in the tables.
    """
    assert DatabaseSettings(url=LOCAL_DEV_DATABASE_URL).echo is False


def test_the_settings_object_cannot_be_changed_or_extended():
    """Frozen and `extra="forbid"`, like the domain models.

    A service handed settings that change under it is a class of bug nobody
    reproduces, and a misspelled keyword silently ignored is how `echo` ends up
    left on.
    """
    settings = DatabaseSettings(url=LOCAL_DEV_DATABASE_URL)
    with pytest.raises(ValidationError):
        settings.echo = True
    with pytest.raises(ValidationError):
        DatabaseSettings(url=LOCAL_DEV_DATABASE_URL, ehco=True)


def test_a_url_read_from_the_environment_is_normalized_too():
    """Validation lives on the field, so no caller can route around it.

    `from_env` is not the only constructor — the importer and Alembic both build
    settings directly — and the check has to apply to all of them.
    """
    settings = DatabaseSettings.from_env({"DATABASE_URL": " postgresql://host/db "})
    assert settings.url == "postgresql+psycopg://host/db"
    with pytest.raises(ValidationError):
        DatabaseSettings.from_env({"DATABASE_URL": "sqlite:///data/tracker.db"})
