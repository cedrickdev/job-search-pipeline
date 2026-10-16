# syntax=docker/dockerfile:1

# The browser worker image (Phase 16 §35, §57). Its one job is the `browser` lane:
# Playwright-driven application submissions. Browsers are large and their system
# dependencies are many, so they live ONLY here — the API and the general worker run
# on the lean `runtime` stage of docker/backend.Dockerfile and carry no browser at all.
# A wedged submission can never consume a general-lane worker because it is a different
# container built from a different image (§35).
#
# Install Chromium and its system dependencies for exactly the Python Playwright
# package resolved in this build. This avoids an image/package browser revision drift.
FROM python:3.12-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    # A path both root (which installs) and pwuser (which runs) can reach.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# The whole tree in one layer, exactly as backend.Dockerfile: the editable install needs
# the real package directories present, and `.dockerignore` — not this COPY — is what
# keeps `.env`, `data/`, `cv/` and secrets out of the build context and the image (§55).
COPY . .

# Runtime dependencies only (no linters, no pytest), then the matching browser. Chromium
# alone: ATS submission drives Chromium, and Firefox/WebKit would only add weight.
RUN pip install -e . \
    && playwright install --with-deps chromium \
    && chmod -R a+rx /ms-playwright

# Match the API's uid/gid so both processes can use the shared named volumes:
# /app/data contains the V1/V2 lock and browser profile, and /app/var holds the
# rendered documents the browser uploads. Fresh volumes inherit this ownership.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app app \
    && mkdir -p /app/data /app/var \
    && chown 10001:10001 /app/data /app/var
ENV XDG_CACHE_HOME=/tmp/jobsearch-browser-cache
USER 10001:10001

# A default, not a policy: docker-compose.yml's `browser-worker` states the full command.
# The lane is fixed to `browser`, so this image never serves the general lane (§35).
CMD ["python", "-m", "backend.app.cli.run_worker", "--lane", "browser"]

# Fixture-only image: the test dependency and Chromium stay out of the API image.
FROM runtime AS browser-tests
USER root
RUN pip install -e ".[dev]"
USER 10001:10001
CMD ["python", "-m", "pytest", "tests/test_v2_browser_automation.py", "tests/test_v2_browser_uploads.py", "tests/test_v2_browser_confirmation.py", "tests/test_v2_browser_final_gate.py", "tests/test_v2_browser_form_scope.py", "-q", "-rs"]
