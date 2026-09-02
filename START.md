# Start The App

Every command below runs from the project root: the folder that contains
`pyproject.toml`, `start.sh` and `webapp/`.

## One-command full app start

```bash
./start.sh
```

Open:

```text
http://127.0.0.1:8765
```

This builds the React frontend in `webapp/`, then starts the FastAPI backend. The backend serves the built frontend and the `/api` routes from one address.

Stop it with `Ctrl+C`.

## If dependencies are missing

Python dependencies should already be in `.venv`. Frontend dependencies should already be in `webapp/node_modules`.

If the frontend dependencies are missing:

```bash
npm install --prefix webapp
```

## Dev mode with frontend and backend separate

Terminal 1, backend:

```bash
.venv/bin/python -m uvicorn --factory server.app:create_app --host 127.0.0.1 --port 8765
```

Terminal 2, frontend:

```bash
npm run dev --prefix webapp
```

Open:

```text
http://localhost:5173
```

In dev mode, Vite proxies `/api` requests to the backend on `http://127.0.0.1:8765`.

## Existing helper

The repo also has this equivalent helper:

```bash
./scripts/webapp.sh
```
