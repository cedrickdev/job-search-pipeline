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

This prerenders the Nuxt frontend in `frontend/` (`npm run generate`, which writes `frontend/.output/public`), then starts the FastAPI backend. The backend serves that bundle and the `/api` routes from one address.

Stop it with `Ctrl+C`.

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
