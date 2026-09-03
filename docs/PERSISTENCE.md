# V2 Persistence

PostgreSQL 17 + PostGIS 3.5, SQLAlchemy 2 and Alembic, introduced in Phase 2.

This document is the reference for the rules that outlive any one module: where the
layer sits, how a database URL is resolved, what a timestamp means, what the schema
guarantees, how a revision is written, and what the V1 import does and reports.

V1 is untouched. `data/tracker.db` remains the live SQLite tracker for the V1
pipeline; V2 reads it, never writes it, and nothing in this document changes V1
behaviour.

## Layout

```
backend/app/domain/                    Pydantic models. No SQLAlchemy, no I/O.
backend/app/infrastructure/database/   base.py, types.py, models.py,
                                       mappers.py, engine.py
backend/app/repositories/              contracts.py (Protocols),
                                       sqlalchemy_repositories.py
backend/app/core/settings.py           DatabaseSettings
backend/app/compat/                    v1_jobs.py (mapping), v1_import.py (run)
backend/app/cli/import_v1.py           the import command
backend/migrations/                    env.py + versions/
```

The dependency direction is one-way: infrastructure imports the domain, the domain
imports nothing from infrastructure. `mappers.py` is the only module that imports
both shapes, which is what keeps that rule checkable by reading one file.

Callers depend on the `Protocol` classes in `repositories/contracts.py`, not on an
`AsyncSession`. The contracts are deliberately small and phrased in domain terms —
`get`, `upsert`, `get_by_source`, `get_by_fingerprint`, `list_recent`, `list_near` —
so an application service never holds a session and never writes SQL.

## Configuration

The database URL is resolved by `DatabaseSettings.from_env`, in this order:

1. `JOBSEARCH_DATABASE_URL`
2. `DATABASE_URL`
3. `postgresql+psycopg://jobsearch:jobsearch_dev_only@127.0.0.1:55432/jobsearch_dev`

A blank or whitespace-only value counts as unset, so a leftover `DATABASE_URL=` line
in a `.env` file falls through instead of failing validation.

The project-scoped name is checked first because `DATABASE_URL` is a shared
convention: a developer who already exports one for another project must not have
this application connect there.

The third entry is the docker-compose development database and nothing else. It is
loopback, on a non-default port, with a password that is only ever a compose
default. **Every deployment overrides it.** A deployment that forgets fails to reach
127.0.0.1 rather than connecting somewhere it was not meant to and writing candidate
data there.

Normalization and refusal happen on the field, so no constructor can route around
them — `from_env`, the importer and Alembic all get the same checks:

- `postgresql://…` becomes `postgresql+psycopg://…`. A driverless URL lets
  SQLAlchemy pick whichever DBAPI it finds first, which is how one machine ends up
  on psycopg2 and another on psycopg 3 from identical configuration, and only one of
  them drives the async engine.
- Surrounding whitespace is stripped (a trailing newline from a secrets file).
- Anything that is not PostgreSQL is refused at configuration time, including
  `sqlite:///data/tracker.db` and the deprecated `postgres://` scheme. PostGIS is a
  requirement of the schema, not a preference: two migrations create a
  `geography(Point,4326)` column, so a non-PostgreSQL URL cannot reach `head`.

A DSN routinely carries a password, so `url` is declared `repr=False` and
`redact_database_url` blanks the credential to `user:***@host`. `repr()`, `str()`,
`redacted_url`, every log line and every refusal message use the redacted form; only
the connection itself gets the real one. The redacted form keeps the host, port and
database name, because "cannot connect" is unanswerable without them.

`echo` defaults to `False`. `DatabaseSettings` is frozen with `extra="forbid"`, like
the domain models: settings cannot change under a running service, and a misspelled
keyword is an error rather than a silently ignored `echo` left switched on.

## Time

The policy, in full:

1. Every V2 timestamp is a timezone-aware `datetime`. A naive value is a bug, not a
   value to be interpreted.
2. Storage is UTC, in a PostgreSQL `TIMESTAMPTZ` column (`sa.DateTime(timezone=True)`).
3. Conversion to a local zone belongs to the presentation layer and happens nowhere
   else.

This is a deliberate break with V1, which stores naive strings. It is enforced by the
column type rather than by convention: `UtcDateTime` raises on a naive bind
parameter — "refusing to store a naive datetime" — and returns `astimezone(UTC)` on
the way out.

Both halves are needed. PostgreSQL accepts a naive literal and interprets it in the
*session's* timezone, so the same insert would mean different instants on a laptop in
Europe/Zurich and on a UTC server. And psycopg returns aware values in the session
timezone, so without the normalization a value read back would not compare equal to
the domain object that was written, on any connection whose `TimeZone` is not UTC.

Two columns are deliberately not `TIMESTAMPTZ`:

- `opportunities.posted_at` is a `DATE`. Boards publish "posted on the 3rd"; storing
  midnight in some zone would invent precision that was never observed.
- `created_at` / `updated_at` default to `now()` **server-side**, so row ordering
  comes from the database's clock. Two processes inserting concurrently cannot
  disagree about order because one of them had a skewed system clock.

## Schema

Eight tables, all keyed by `UUID` (never a serial), created by `rev_0002`:

| Table | Holds | Ownership |
| --- | --- | --- |
| `users` | id and display name only | — |
| `candidate_profiles` | a candidate's profile | `user_id` → `users`, CASCADE |
| `companies` | employer, website, careers URL | shared fact |
| `company_locations` | a site, flattened `Location` | `company_id`, CASCADE |
| `opportunities` | the posting | shared fact |
| `opportunity_source_records` | where it came from, plus `raw` | `opportunity_id`, CASCADE |
| `match_evaluations` | one score per (profile, posting) | `user_id` + `candidate_profile_id`, CASCADE |
| `match_dimension_scores` | per-dimension detail | `match_evaluation_id`, CASCADE |

`users` carries no credentials, no email and no authentication: Phase 3 owns identity.
It exists now so user-scoped rows can carry a real foreign key instead of a loose
column that would have to be backfilled later.

`Opportunity` and `Company` are shared facts and carry no `user_id`: two candidates
looking at the same posting are looking at one row. `match_evaluations` denormalizes
`user_id` next to `candidate_profile_id` on purpose — every query filters on it, so
authorization is one indexed predicate rather than a join a future caller could
forget (docs/ENGINEERING_STANDARDS.md §Security).

Deletion is a decision in both directions, and the two directions differ:

- `opportunities.company_id` is `ON DELETE SET NULL`, and `company_name` is kept
  beside it. A posting is a fact that was observed; the employer it was attributed to
  is an inference. Phase 6 will merge duplicate company records, and losing the
  inference must not lose the fact.
- Everything a user owns is `ON DELETE CASCADE`, so "delete my account" is one
  statement. The shared posting stays: it is not the user's to delete.

Every domain rule a column group can express is *also* a named CHECK. A
`model_validator` protects the rows that go through Python; the V1 importer, a future
backfill and a hand-written `UPDATE` in psql do not. The constraints are named so a
later migration can drop one by name — a salary is complete or absent, bounds are
ordered and non-negative, workload percentages are 1–100, weekly hours are 0–168,
scores are in the unit interval, ISO codes match `^[A-Z]{2}$` / `^[a-z]{2}$` /
`^[A-Z]{3}$`, and a `company_locations` row must locate *something*.

Money is `NUMERIC(14, 2)`, never a float: 4200.10 has to come back as 4200.10.

Three uniqueness rules carry the idempotency of every write path:

- `uq_opportunity_source_records_source_key_external_id` — re-running a discovery
  pass or the V1 import conflicts with an existing row instead of duplicating a
  posting. NULL `external_id` values do not collide, which is correct: a board that
  publishes no stable id cannot be used to claim two postings are the same.
- `uq_opportunities_dedup_fingerprint` — nullable and unique, so a posting whose
  fingerprint could not be computed is still storable.
- `uq_company_locations_company_id_headquarters` — a *partial* unique index
  (`WHERE is_headquarters`), so at most one head office and any number of branches.

## Enums

Enum columns are `VARCHAR(32)` plus a named CHECK, never a PostgreSQL native `ENUM`:
`sa.Enum(..., native_enum=False, create_constraint=True, length=32)`.

Adding a member to a native enum is a DDL change with transactional restrictions on
older servers; widening a CHECK is an ordinary migration — one drop/add pair. The
length is fixed at 32 so a longer member name never needs a second change to the
column type.

**Autogenerate does not see a changed member list.** `sa.Enum` renders as a CHECK on
a string column, and Alembic compares the column type, not the constraint body. A new
enum member is therefore a *hand-written* revision that drops the old CHECK and
creates the new one under the same name.

## Geography

Coordinates live in `geography(Point,4326)` columns — `opportunities.location_point`
and `company_locations.location_point` — each with a **GiST** index. A B-tree on a
geography column would be built happily and then never chosen.

`geography`, not `geometry`: distances and radii are then in **metres**. The same
query on a `geometry(Point,4326)` column would compare degrees, and a `100_000`
radius would cover the planet.

The wire format is EWKT, and its ordering is the single most consequential detail in
the layer: WKT is `POINT(x y)` and **x is longitude**, so a `GeoPoint(latitude=46.5,
longitude=6.6)` becomes `SRID=4326;POINT(6.6 46.5)`. A swapped pair stays inside the
valid range for most European coordinates, so nothing fails — the marker simply
appears in another country. `tests/test_v2_persistence_geo.py` asks PostgreSQL itself
(`ST_Y`, `ST_X`) rather than trusting a round trip, because the same mistake on the
way in and on the way out cancels out.

Any other SRID is refused rather than reinterpreted. Accepting a Swiss LV95 pair
(SRID 2056) as WGS84 would place the point millions of degrees away, so one SRID is
an invariant of the column.

Radius predicates use `ST_DWithin(column, center, radius)`, which the GiST index can
answer; `ST_Distance(...) <= radius` is equivalent in result and cannot use it.
Reported distances come from `ST_Distance` on the same spheroid, so a row can never
be listed as 101 km away by a query for everything within 100 km. An unlocated row
makes `ST_DWithin` return NULL and `WHERE` keeps only TRUE, so a posting nobody can
place drops out of every radius query while staying in the feed — no second predicate
needed.

Phase 2 owes storage and these two predicates. The Geo Opportunity Explorer is
Phase 7.

## Migrations

**A fresh database is constructible from `alembic upgrade head` and nothing else.**
No bootstrap SQL, no `create_all`, no manual `CREATE EXTENSION` step. That is what
makes the test fixtures, CI and a managed PostgreSQL where nobody gets a superuser
psql shell all reach the same schema the same way.

The V1 pattern — `ALTER TABLE` wrapped in a bare `except Exception: pass` — is
explicitly what this replaces (docs/ENGINEERING_STANDARDS.md §Database rules).
Migrations are forward-only, reviewed, and never silent.

```
alembic upgrade head                    apply everything pending
alembic upgrade head --sql              print the DDL instead of running it
alembic revision --autogenerate -m "…"  draft the next revision
alembic downgrade 0001                  destructive; development and tests only
```

Configuration: `alembic.ini` sets `script_location = %(here)s/backend/migrations`,
`prepend_sys_path = %(here)s` and `file_template = rev_%%(rev)s_%%(slug)s`. It holds
**no** `sqlalchemy.url` — `env.py` calls `DatabaseSettings.from_env()`, so a migration
and the application resolve the same database by the same rules, and the DSN is never
committed.

Rules for writing a revision:

- **A revision imports nothing from the application.** A migration is a record of DDL
  that has already run on real databases, so it must keep producing the same schema
  after `UtcDateTime` or `GeographyPoint` is renamed, moved or deleted. Column types
  are spelled out in SQLAlchemy and PostGIS terms — `sa.DateTime(timezone=True)`, a
  local `Geography` user-defined type. A migration creates columns; it never reads or
  writes rows, so it needs none of the Python-side conversions.
- **Every constraint is named**, via `MetaData(naming_convention=NAMING_CONVENTION)`
  and `op.f(...)`. The convention uses `%(column_0_N_name)s` so a composite index or
  unique constraint names every column it covers. An anonymous CHECK cannot be
  dropped by a portable migration.
- The whole upgrade runs in **one transaction** (Alembic's default, kept): a failed
  revision leaves the schema and `alembic_version` exactly as they were, rather than
  half-migrated.
- `compare_type=True`, because column types come from `type_annotation_map` and that
  is the drift this schema is most likely to grow. `compare_server_default` stays
  **off**: PostgreSQL normalizes a default such as `'{}'::jsonb` on the way in, and
  cosmetic diffs train the reader to ignore autogenerate output.
- `include_object` excludes two kinds of object this schema does not own: PostGIS's
  own tables, and the CHECK constraints generated by a column's `Enum` type. Without
  the second exclusion every new revision would propose to drop them all.

Current revisions:

| Revision | Does |
| --- | --- |
| `0001` | `CREATE EXTENSION IF NOT EXISTS postgis`, alone and first |
| `0002` | the eight core tables, their indexes and constraints |

`0001` is separate because no `geography` column can be declared before the extension
exists, and `IF NOT EXISTS` makes it a silent no-op on the development database (where
the image installs PostGIS itself) while doing the real work on the test database,
which is created empty precisely so the migration path is what the suite exercises.
Its `downgrade` is intentionally a no-op: PostGIS is installed per database, so
dropping it would pull it out from under anything else that uses it.

## Docker

One long-running service and two one-shot commands — the persistence foundation and
nothing more. Redis, the background worker, the Playwright browser worker and the Nuxt
frontend are Phase 8+ services and are absent rather than declared and disabled; an
inert service still has to be maintained, and docs/ARCHITECTURE.md §13 already records
the target composition.

```
docker compose up -d postgres            start the database
docker compose run --rm migrate          alembic upgrade head
docker compose run --rm import-v1 --help the V1 SQLite import
docker compose down                      stop, keep the data
docker compose down -v                   stop and delete the data
```

`migrate` and `import-v1` sit behind the `tools` profile, so `docker compose up` never
runs a migration as a side effect.

The image is `imresamu/postgis:17-3.5`, pinned to a minor line: the extension version
decides what `ST_AsEWKT` prints, and exact coordinate round-tripping is asserted. It is
the multi-arch build of the same `postgis/docker-postgis` sources — `postgis/postgis`
publishes amd64 only, which fails with "no matching manifest" on an arm64 machine. One
tag for developers and for CI keeps the two identical.

Details that are decisions rather than defaults:

- The port is published on **`127.0.0.1:55432`**. Loopback only, and a non-default
  port, so a developer's own PostgreSQL on 5432 is left completely alone.
- The health check is `pg_isready -U … -d …`, not a TCP probe: the image restarts
  PostgreSQL part-way through first-time initialization, and a port check reports
  ready while the server is about to go down again. `migrate` and `import-v1` both
  wait on `service_healthy`.
- The volume is **named** (`jobsearch_postgres_data`), so `down` keeps the data and
  only `down -v` throws it away.
- `docker/postgres/init-test-database.sh` runs once, when the data directory is first
  initialized, and creates `jobsearch_test` — refusing if it would equal `POSTGRES_DB`.
  The suite therefore has a database of its own and can never drop the one holding
  imported V1 data. It deliberately does **not** install PostGIS there: that is rev
  `0001`'s job, and a test database where the extension already existed would never
  prove it.
- `./data` is mounted **read-only** into `import-v1`, and the importer opens the file
  read-only as well.
- **No service reads `.env`.** Compose interpolates it into the values in the file —
  which is how a developer overrides the port or the password — but nothing mounts it
  or passes it through: `.env` holds V1's real API credentials and none of them belong
  in a database container (docs/ENGINEERING_STANDARDS.md §Security).

`.env.example` carries the dev-only, non-secret database values: `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_TEST_DB`, `POSTGRES_PORT`, and the
`DATABASE_URL` / `JOBSEARCH_DATABASE_URL` a host-side process uses.

`docker/backend.Dockerfile` builds the environment both tool services run in: Python
**3.12**, tracking `requires-python` and the version CI pins rather than the newer local
interpreter, an editable install so a traceback points at `/app/backend/...`, runtime
dependencies only (the suite runs on the host against the published port), and
`USER 10001:10001` — nothing in the image needs root. From Phase 8 the same image runs
the API and the workers.

The security boundary is the build context, not the `COPY`: `.dockerignore` keeps
`.env`, `.env.*`, `data/`, `cv/`, rendered PDFs and the agent configuration directories
out of it, so V1's credentials and every personal detail cannot reach a layer that
anyone able to pull the image could read (docs/ENGINEERING_STANDARDS.md §Security).

## Import

`python -m backend.app.cli.import_v1 --sqlite PATH` copies the V1 `jobs` table into
the V2 schema. Under compose: `docker compose run --rm import-v1`.

```
--sqlite PATH        required; the V1 database (no default, no guessing)
--on-error skip|fail report the row and continue (default), or stop and roll back
--dry-run            map and write everything against the real schema, then roll back
--limit N            the first N rows by ascending V1 id
--max-issues N       how many offending rows to list (default 20)
```

The V2 target comes from the environment, by the §Configuration rules. `--limit 0` is
rejected: it would read nothing and report a clean run, which is the one outcome a
migration tool must never produce by accident.

### The V1 file is never modified

The connection is opened with `?mode=ro` through a URI, so **SQLite** enforces
read-only, not this code being careful: an `INSERT` on it raises. No future edit to the
module can turn the migration into a mutation of its own source, and the run leaves no
`-wal`/`-shm` files beside a database V1 may have open at the same time. The path goes
through `as_uri()`, so a directory name containing `?` or `#` cannot smuggle a second
URI parameter into the connection string. `tests/test_v2_import_v1.py` asserts the file
is byte-identical (SHA-256) after a run and that no sidecar file appeared.

Rows are streamed `ORDER BY id`, oldest first, so a `--limit` rehearsal reads the same
rows every time.

### Idempotency

Identity comes from the Phase 1 deterministic UUID5 mapping: V1 job *n* always becomes
`opportunity_id_for_v1_job(n)`. Combined with `upsert`, re-running an import updates
rows instead of duplicating them. The report makes that observable — the first run
counts rows under `imported`, the second counts the same rows under `updated`.

Each row is written inside its own savepoint (`session.begin_nested()`), so a row the
database refuses is rolled back alone and the run continues with a usable session. The
transaction boundary belongs to the caller's `session_scope`, which is why `--dry-run`
is a parameter of the boundary (`commit=False`) and not a branch inside the loop.

Nothing is invented and nothing is dropped. A V1 value V2 has no column for is kept in
`opportunity_source_records.raw` — `v1_salary`, `v1_discovered_date`, `v1_id` and the
rest — rather than parsed speculatively. A free-text `"80-100%"` salary string is not
turned into a `SalaryRange`.

### The `discovered_date` risk

V1 declares `jobs.discovered_date TEXT NOT NULL` with no validation of its shape, so
the column is the documented hazard of this import. The importer parses it explicitly
and, when the string carries no offset, interprets it in `default_timezone` before
converting to UTC — `2026-03-01` with `Europe/Zurich` is stored as
`2026-02-28T23:00:00+00:00`, and the original string stays in `raw["v1_discovered_date"]`
so the conversion is auditable. A string that cannot be parsed is reported, never
guessed at and never replaced by "now".

### What a row that does not land reports

| Code | Meaning | Fix |
| --- | --- | --- |
| `V1_ID_INVALID` | no usable `jobs.id` (`v1_id` is then `None`) | the data |
| `V1_REQUIRED_COLUMN_MISSING` | a required column is absent or blank; the message names it | the data |
| `V1_DISCOVERED_DATE_INVALID` | the timestamp could not be parsed; the message quotes it | the data |
| `V1_SCORE_OUT_OF_RANGE` | a V1 score outside 0–100; raised by `v1_score_to_unit_interval`, so not reachable from the `jobs` import | the data |
| `V1_MAPPING_FAILED` | raised without classifying | the mapper |
| `DOMAIN_VALIDATION_FAILED` | a V1 value the domain refuses | the mapper |
| `PERSISTENCE_FAILED` | a constraint the schema enforces, e.g. two rows claiming one `dedup_hash` | the data |

Outcomes are `imported`, `updated`, `skipped` (V1 could not offer the row in a usable
form) and `failed` (it mapped cleanly and the database refused it). `ImportReport`
derives `skipped`, `failed` and `is_clean` from the issue list, so it cannot claim 900
imported rows and list 901 issues.

### Failure levels and exit statuses

Three levels, distinguished on purpose:

- **The database cannot be read at all** — the file is missing, or has no `jobs` table.
  `V1ImportError`. There is nothing to report per row and no policy to apply.
- **One row is unusable.** With `--on-error skip` (default) it is recorded in the
  report and the run continues. With `--on-error fail` the run raises
  `V1ImportAborted`, carrying the partial report and naming the row; the exception
  travels through `session_scope`, which rolls back on any `BaseException`, so `fail`
  means *nothing partial*.
- **Anything else propagates.** Only `IntegrityError` and `DataError` are treated as
  row-local. A dropped connection surfaces as an `OperationalError` on every
  subsequent row, and a run that swallowed it would report "3000 rows failed" for what
  is one unreachable database, then exit as though it had merely lost some rows.

| Status | Meaning |
| --- | --- |
| `0` | clean — every row read was written (an empty V1 database is clean) |
| `1` | nothing was imported: unusable file, unusable DSN, unreachable database |
| `2` | argparse usage error (which is why "ran but lost rows" is not 2) |
| `3` | the run completed and some rows did not land |

The printed report names the source file and the **redacted** target URL, states
"dry run, rolled back" when nothing was kept, groups issues by code with complete
counts, and truncates only the per-row list (`--max-issues`).

## Tests

165 Phase 2 tests, split by what they need:

| Module | Needs PostgreSQL |
| --- | --- |
| `test_v2_persistence_settings.py` | no |
| `test_v2_persistence_mappers.py` | no |
| `test_v2_persistence_schema.py` | no (metadata only) |
| `test_v2_persistence_migrations.py` | yes |
| `test_v2_persistence_constraints.py` | yes |
| `test_v2_persistence_repositories.py` | yes |
| `test_v2_persistence_geo.py` | yes (PostGIS) |
| `test_v2_import_v1.py` | both halves |

The database-backed tests connect to `jobsearch_test`, resolved by
`DatabaseSettings.for_tests`, which reads `JOBSEARCH_TEST_DATABASE_URL` then
`TEST_DATABASE_URL` — **deliberately not the variables `from_env` reads**. The session
fixture runs `DROP SCHEMA public CASCADE` followed by `alembic upgrade head`, so a
`DATABASE_URL` exported for development work must be invisible to it. That is the guard
on a destructive operation, and `test_the_test_suite_reads_its_own_variables` is the
test that keeps it.

The schema under test is therefore always the migrated one, never `create_all`: the
migration path is what the suite exercises. Each test runs in a transaction that is
rolled back afterwards (`join_transaction_mode="create_savepoint"`), so ordering never
matters.

Nothing here touches a live job board or a live LLM (docs/ENGINEERING_STANDARDS.md
§Test pyramid), and the V1 fixtures are built by V1's own schema module into a
temporary directory — never from real candidate data.

### Running them

```
docker compose up -d postgres    then
python -m pytest -q              the whole suite, 1044 tests
```

With no container running, the database-backed tests **skip** with a message naming the
redacted URL and the command that would start it: a developer who has not brought
PostgreSQL up is not looking at a broken build. The `backend` CI job runs in exactly
that state.

Which is why the workflow has a second, additive `database` job: it provides
`imresamu/postgis:17-3.5` as a service container, runs `alembic upgrade head` on an
empty database as a step of its own, then runs the eight modules and **fails if
anything was skipped**. A database test that quietly stops running is a red build
rather than a silent gap.

