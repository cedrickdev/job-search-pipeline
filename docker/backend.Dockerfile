# syntax=docker/dockerfile:1

# The backend image: one environment that can run Alembic, the V1 importer and —
# from Phase 8 on — the FastAPI application and the workers. Phase 2 uses only the
# first two (docker-compose.yml `migrate` and `import-v1`).
#
# Python 3.12, matching `requires-python = ">=3.12"` and the version CI pins. The
# local interpreter is newer; the image is what production would run, so it tracks
# the floor rather than the developer's machine.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

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
# `webapp/` out of it — the build context, not this COPY, is the boundary that
# keeps V1 credentials out of the image.
COPY . .

# Editable, so the layout inside the container matches a source checkout and a
# traceback points at /app/backend/... rather than a copy under site-packages.
# Runtime dependencies only: the test suite runs on the host (or on CI's runner)
# against the exposed port, so pytest and the linters have no business here.
RUN pip install -e .

USER 10001:10001

# The only thing this image is asked to do in Phase 2. docker-compose.yml repeats
# it explicitly, so the composition alone tells you what will run.
CMD ["alembic", "upgrade", "head"]
