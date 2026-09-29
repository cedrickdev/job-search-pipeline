# syntax=docker/dockerfile:1

# The backend image: one environment that runs Alembic, the V1 importer, the FastAPI
# application and — since Phase 16 — the general and browser task-queue workers.
# docker-compose.yml `migrate`, `import-v1`, `api` and `worker` each spell out their
# own command against the lean `runtime` stage below.
#
# Two stages, one base (Phase 16 §57 — keep ordinary images minimal):
#   - `runtime` is the default target: WeasyPrint's shared libraries and nothing else,
#     so the API and the general worker stay small and carry no database tooling.
#   - `tools` adds the PostgreSQL 17 client (`pg_dump`/`pg_restore`/`psql`) that the
#     backup and restore-check jobs shell out to (§45-46). It is a superset built only
#     by those jobs, so a `pg_dump` binary is never shipped in the request-serving image.
# The browser image is separate again (docker/browser-worker.Dockerfile): Playwright's
# browsers are large and belong only on the lane that drives them (§57).
#
# Python 3.12, matching `requires-python = ">=3.12"` and the version CI pins. The
# local interpreter is newer; the image is what production would run, so it tracks
# the floor rather than the developer's machine.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim AS base

# `PYTHONDONTWRITEBYTECODE` keeps root-owned .pyc files out of a tree the
# non-root user cannot write to; `PYTHONUNBUFFERED` makes the import report show
# up in `docker compose run` output as it is produced rather than at exit.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# WeasyPrint links these at import time, and it is a hard dependency of the
# project rather than an extra — so an image that can `import pipeline.cv_render`
# needs them even though `alembic upgrade head` does not. libpq is absent on
# purpose: psycopg is installed as `[binary]`, which ships its own.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libfontconfig1 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# A fixed high uid/gid, not a name lookup: the same numbers appear in file
# ownership on a mounted volume, so pinning them keeps host-side permissions
# predictable. Nothing in this image needs root.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app app

WORKDIR /app

# The whole tree in one layer: the install is editable and `[tool.setuptools]`
# lists the packages explicitly, so metadata generation needs the real
# directories present. `.dockerignore` is what keeps `.env`, `data/`, `.venv` and
# the frontend's node_modules out of it — the build context, not this COPY, is the
# boundary that keeps V1 credentials out of the image.
COPY . .

# Editable, so the layout inside the container matches a source checkout and a
# traceback points at /app/backend/... rather than a copy under site-packages.
# Runtime dependencies only: the test suite runs on the host (or on CI's runner)
# against the exposed port, so pytest and the linters have no business here.
RUN pip install -e .

# `data/` is excluded from the build context (it holds the operator's tracker and
# job-board logins), so the directory has to be created here — and owned by the
# runtime user, because Docker seeds a fresh named volume from the image path
# including its ownership. Without this the `api` service would mount a root-owned
# empty volume and create_app would fail to write tracker.db as uid 10001.
# `data/` and `var/` are excluded from the build context (`data/` holds the operator's
# tracker and job-board logins; `var/` holds generated artifacts), so both directories
# are created here — and owned by the runtime user, because Docker seeds a fresh named
# volume from the image path including its ownership. Without this the mounted volumes
# would be root-owned and create_app / the workers would fail to write as uid 10001.
RUN mkdir -p /app/data /app/var && chown 10001:10001 /app/data /app/var

# ── tools ───────────────────────────────────────────────────────────────────────
# The backup and restore-check jobs (§45-46) shell out to `pg_dump`, `pg_restore` and
# `psql`. They must match the server: Debian bookworm ships client 15, and `pg_dump`
# refuses to dump from a newer server, so this stage adds the PostgreSQL 17 client from
# the PGDG apt repository. It is a superset of `runtime`, built ONLY by the backup jobs,
# so no libpq tooling is ever shipped in the request-serving image (§57).
FROM base AS tools
USER root
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl ca-certificates gnupg \
    && install -d /usr/share/keyrings \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install --no-install-recommends -y postgresql-client-17 \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
# The dump directory, owned by the runtime user, because Docker seeds a fresh named
# volume from the image path including its ownership — see the `backups` volume.
RUN mkdir -p /app/var/backups && chown 10001:10001 /app/var/backups
USER 10001:10001
# A safe default — listing managed dumps writes nothing; the jobs state `create`/`verify`.
CMD ["python", "-m", "backend.app.cli.run_backup", "list"]

# ── runtime ─────────────────────────────────────────────────────────────────────
# The default target: the API, the general worker, `migrate` and the V1 importer. No
# database client and no browsers — the minimal request-serving surface (§57). A
# default command, not a policy: every service states its own, so the composition
# alone tells you what runs. Migrating is the safe no-argument thing to do — idempotent.
FROM base AS runtime
USER 10001:10001
CMD ["alembic", "upgrade", "head"]
