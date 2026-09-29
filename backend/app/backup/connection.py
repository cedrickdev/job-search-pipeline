"""Turning one SQLAlchemy DSN into the connection a `pg_dump`/`pg_restore` child reads (§45).

The libpq tools (`pg_dump`, `pg_restore`, `psql`, `createdb`, `dropdb`) do not speak SQLAlchemy's
`postgresql+psycopg://` URL, and — the property §45 turns on — a password handed to them **must not
appear on the command line**, where `ps`, a shell history or a process listing would expose it. So
this value parses the application's one DSN into discrete libpq parameters and hands the password to
a child **only through its environment** (`PGPASSWORD`), never as an argv token.

`redacted` is the only string form that ever reaches a log, a report or an exception: `host:port/db`
with no user and no password, so nothing here can leak a credential the way a raw DSN would.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import make_url

# The libpq environment variables the tools read. `PGPASSWORD` is the whole point of passing the
# connection this way: it is the one credential, and it travels in the child's environment, never in
# its argv (docs/BACKUP_RECOVERY.md, docs/ENGINEERING_STANDARDS.md §Security).
_PGHOST = "PGHOST"
_PGPORT = "PGPORT"
_PGUSER = "PGUSER"
_PGPASSWORD = "PGPASSWORD"
_PGDATABASE = "PGDATABASE"

_REQUIRED_BACKEND = "postgresql"


@dataclass(frozen=True)
class PostgresConnectionParams:
    """The discrete connection a libpq tool needs, with the password kept off the command line.

    Frozen: a backup run is handed one target and cannot have it changed under it. `database` is the
    only part `for_database` varies — the isolated-restore path (§46) builds a scratch target from
    the same host, port and credentials but a different database name, so a verification never
    touches the source.
    """

    host: str | None
    port: int | None
    user: str | None
    password: str | None
    database: str

    @classmethod
    def from_url(cls, url: str) -> PostgresConnectionParams:
        """Parse the application's DSN, refusing anything that is not PostgreSQL.

        The same safety property `normalize_database_url` enforces for the engine: a `sqlite://`
        or other non-PostgreSQL URL slipping in here would point `pg_dump` at nothing it can read,
        so it is refused by name rather than failing obscurely inside the child.
        """
        parsed = make_url(url)
        if parsed.get_backend_name() != _REQUIRED_BACKEND:
            raise ValueError(
                "backup tooling is PostgreSQL-only; refusing a "
                f"{parsed.get_backend_name()!r} URL")
        if not parsed.database:
            raise ValueError("the database URL must name a database to back up")
        return cls(
            host=parsed.host,
            port=parsed.port,
            user=parsed.username,
            password=parsed.password,
            database=parsed.database)

    def for_database(self, name: str) -> PostgresConnectionParams:
        """The same server and credentials, pointed at a different database (the scratch target)."""
        return PostgresConnectionParams(
            host=self.host, port=self.port, user=self.user,
            password=self.password, database=name)

    def libpq_env(self) -> dict[str, str]:
        """The libpq variables a child reads — including the password, which lives ONLY here.

        Only the parts that are set are emitted, so peer authentication (no password) or a
        socket connection (no host) produces no empty override that would confuse the child.
        """
        env: dict[str, str] = {_PGDATABASE: self.database}
        if self.host is not None:
            env[_PGHOST] = self.host
        if self.port is not None:
            env[_PGPORT] = str(self.port)
        if self.user is not None:
            env[_PGUSER] = self.user
        if self.password is not None:
            env[_PGPASSWORD] = self.password
        return env

    def targets_same_database(self, other: PostgresConnectionParams) -> bool:
        """Whether two params point at the *same* database — the destructive-restore guard (§46).

        Host, port and database name, never the password: a restore that would overwrite the very
        database a dump came from is refused on identity of the target, not of the credential.
        """
        return (self.host, self.port, self.database) == (
            other.host, other.port, other.database)

    @property
    def redacted(self) -> str:
        """`host:port/database`, safe to log — no user, no password ever."""
        location = self.host or "localhost"
        if self.port is not None:
            location = f"{location}:{self.port}"
        return f"{location}/{self.database}"
