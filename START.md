# Start The App

Every command below runs from the project root: the folder that contains
`pyproject.toml`, `start.sh` and `frontend/`.

## One-command full app start

```bash
./start.sh
```

Open:

```text
http://127.0.0.1:8765
```

If port `8765` is already occupied, `start.sh` automatically selects the first
free port up to `8785` and prints the actual URL. To require a specific port,
set `JOBSEARCH_PORT`; an occupied explicit port then fails with a clear message
without stopping the existing process.

This prerenders the Nuxt frontend in `frontend/` (`npm run generate`, which writes `frontend/.output/public`), then starts the FastAPI backend. The backend serves that bundle and the `/api` routes from one address.

Stop it with `Ctrl+C`.

### Complete local browser workflow

Before starting the app, enable reversible form preparation:

```bash
export JOBSEARCH_BROWSER_LOCAL_PREPARATION_ENABLED=true
export JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED=false
./start.sh
```

The dashboard can then evaluate an opportunity, generate its tailored documents,
inspect a supported ATS form and queue an approved browser task. Drain the browser
lane from a second terminal:

```bash
.venv/bin/python -m backend.app.cli.run_worker --lane browser --once
```

With live submissions left `false`, the worker will stop safely before any employer
send. Keep it that way for fixture testing; CAPTCHA, MFA, login walls, unknown fields
and changed forms always require human handling.

### Integrated Claude terminal (optional)

The terminal is separate from LLM connection settings. It creates one short-lived,
isolated Claude Code runtime for the signed-in user inside **Settings → Assistant IA**.
It requires Docker Compose because only the private manager owns the Docker socket and
the deployment Claude credential.

Use `http://127.0.0.1:3000` (or `http://localhost:3000`) for this local stack.
`./start.sh` detects the running API and frontend and reports that address instead of
launching a second API on port 8766. Keep both browser origins in
`JOBSEARCH_TERMINAL_ALLOWED_ORIGINS` when using both spellings.

The three gateway values belong in the ignored `terminal.env` (see
`terminal.env.example`); the manager token and activation flag belong in `.env`.
After changing configuration, recreate the affected services:

```bash
docker compose build claude-terminal-runtime terminal-manager
docker compose --profile terminal up -d --build api frontend terminal-manager
```

Set the terminal-manager token and Anthropic key through local secrets, then enable the
matching API terminal settings. Never put either value in the browser or a committed file.
Stop a session from its in-app panel; an operator can inspect and clean expired sessions with:

```bash
.venv/bin/python -m backend.app.cli.reap_claude_terminals --dry-run
.venv/bin/python -m backend.app.cli.reap_claude_terminals
```

Cleanup is idempotent and never sends a job application.

## If dependencies are missing

Python dependencies should already be in `.venv`. Frontend dependencies should already be in `frontend/node_modules`.

If the frontend dependencies are missing:

```bash
npm ci --prefix frontend
```

## Dev mode with frontend and backend separate

Terminal 1, backend:

```bash
.venv/bin/python -m uvicorn --factory server.app:create_app --host 127.0.0.1 --port 8765
```

Terminal 2, frontend:

```bash
npm run dev --prefix frontend
```

Open:

```text
http://localhost:3000
```

In dev mode Nuxt proxies `/api` to the backend on `http://127.0.0.1:8765`, so there is still only one origin in the browser. Hot reload applies to the frontend only; a backend change needs uvicorn restarted (or `--reload`).

## With Docker Compose

```bash
docker compose up -d
```

That starts PostgreSQL/PostGIS, the API on `http://127.0.0.1:8765` and the frontend in dev mode on `http://127.0.0.1:3000`. The API container runs with its own empty tracker in a named volume, not your `data/` directory, and with the scheduler disabled — see the comments in `docker-compose.yml`.

## Existing helper

The repo also has this equivalent helper, which additionally opens the browser:

```bash
./scripts/frontend.sh
```
