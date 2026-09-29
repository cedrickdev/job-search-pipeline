# syntax=docker/dockerfile:1

# The browser worker image (Phase 16 §35, §57). Its one job is the `browser` lane:
# Playwright-driven application submissions. Browsers are large and their system
# dependencies are many, so they live ONLY here — the API and the general worker run
# on the lean `runtime` stage of docker/backend.Dockerfile and carry no browser at all.
# A wedged submission can never consume a general-lane worker because it is a different
# container built from a different image (§35).
#
# Based on Microsoft's Playwright image, which ships the browser system libraries and a
# non-root `pwuser`. The `playwright install chromium` step below re-fetches the browser
# that matches whatever `playwright` version pip resolves, so the image is correct even
# when the pinned dependency drifts ahead of the base tag; bump the tag to keep it small.
FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    # A path both root (which installs) and pwuser (which runs) can reach.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# A virtualenv keeps the install off Ubuntu's externally-managed system Python (PEP 668)
# without reaching for --break-system-packages.
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /app

# The whole tree in one layer, exactly as backend.Dockerfile: the editable install needs
# the real package directories present, and `.dockerignore` — not this COPY — is what
# keeps `.env`, `data/`, `cv/` and secrets out of the build context and the image (§55).
COPY . .

# Runtime dependencies only (no linters, no pytest), then the matching browser. Chromium
# alone: ATS submission drives Chromium, and Firefox/WebKit would only add weight.
RUN pip install -e . \
    && playwright install chromium \
    && chmod -R a+rx /ms-playwright

# Non-root, as the base image intends: pwuser (uid 1000) owns nothing it must write to —
# it reads the venv, the source and the browsers, all world-readable above.
USER pwuser

# A default, not a policy: docker-compose.yml's `browser-worker` states the full command.
# The lane is fixed to `browser`, so this image never serves the general lane (§35).
CMD ["python", "-m", "backend.app.cli.run_worker", "--lane", "browser"]
