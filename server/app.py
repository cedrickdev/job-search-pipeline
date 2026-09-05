"""create_app factory: JSON API + built SPA, the only HTTP surface."""
import asyncio
import contextlib
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.app.api.dependencies import ENGINE_ATTRIBUTE
from backend.app.api.errors import install_v2_error_handlers
from backend.app.api.router import create_v2_router
from pipeline import paths
from pipeline.db import connect, init_db
from server.apply_dispatch import ApplyDispatcher
from server.runs import RunManager


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Run the in-process daily scheduler for the life of the app, then cancel
    it. The tick loop sleeps before its first tick, so a short-lived TestClient
    never fires a run during a test.

    The V2 engine is disposed on the way out. It is created lazily by the first
    ``/api/v2`` request that needs it (see ``backend.app.api.dependencies``), so
    the attribute is usually absent — an app that served only V1 has no pool to
    close."""
    from server.scheduler import scheduler_loop
    task = asyncio.create_task(scheduler_loop(app))
    app.state.scheduler_task = task
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        engine = getattr(app.state, ENGINE_ATTRIBUTE, None)
        if engine is not None:
            await engine.dispose()


def _mount_spa(app: FastAPI, spa_dist: Path) -> None:
    """Serve the built SPA additively.

    Hashed bundles are served from ``/assets``. Client-side routes (deep links)
    fall back to ``index.html`` via a 404 handler, which fires ONLY when no
    route matched — so it never shadows real or later-registered API routes,
    and ``/api`` / ``/assets`` 404s keep their original JSON bodies.
    """
    assets = spa_dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    index = spa_dist / "index.html"

    async def spa_fallback(request: Request, exc):
        path = request.url.path
        is_client_route = (
            not path.startswith("/api")
            and not path.startswith("/assets")
            and request.method in ("GET", "HEAD")
            and index.is_file()
        )
        if is_client_route:
            return FileResponse(index, media_type="text/html")
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail})

    app.add_exception_handler(404, spa_fallback)


def create_app(db_path: str | Path | None = None,
               settings_path: str | Path | None = None,
               spa_dist: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Job Search Command Center", lifespan=_lifespan)

    if db_path is None:
        env_db = os.environ.get("JOBSEARCH_DB_PATH")
        db_path = Path(env_db) if env_db else paths.DB_PATH
    app.state.db_path = Path(db_path)
    app.state.settings_path = (
        Path(settings_path) if settings_path is not None else paths.SETTINGS_PATH)

    # Bootstrap: ensure schema exists (cheap; idempotent).
    bootstrap = connect(app.state.db_path)
    init_db(bootstrap)
    bootstrap.close()

    # One run manager guards all in-app runs. Set in the factory body (not just
    # the lifespan) so non-lifespan test clients still resolve it.
    app.state.run_manager = RunManager(app.state.db_path)
    app.state.apply_dispatcher = ApplyDispatcher()

    @app.exception_handler(sqlite3.OperationalError)
    async def busy_handler(request: Request, exc: sqlite3.OperationalError):
        return JSONResponse(
            status_code=503, content={"error": "db_busy", "detail": str(exc)})

    from server.routes import overview, approved, jobs, files, prep, analytics
    from server.routes import settings as settings_routes
    from server.routes import actions as action_routes
    from server.routes import chat as chat_routes
    from server.routes import transcribe as transcribe_routes
    from server.routes import runs as runs_routes
    app.include_router(overview.router)
    app.include_router(analytics.router)
    app.include_router(approved.router)
    app.include_router(settings_routes.router)
    app.include_router(jobs.router)
    app.include_router(action_routes.router)
    app.include_router(files.router)
    app.include_router(prep.router)
    app.include_router(chat_routes.router)
    app.include_router(transcribe_routes.router)
    app.include_router(runs_routes.router)

    # V2, mounted at /api/v2 alongside V1 rather than replacing anything. The
    # error handlers are registered on the app because Starlette resolves them
    # per application; the /api/v2-only ones are scoped by path inside
    # backend.app.api.errors so V1 bodies are untouched.
    install_v2_error_handlers(app)
    app.include_router(create_v2_router())

    # SPA mount is registered LAST so every /api route keeps precedence.
    spa_path = Path(spa_dist) if spa_dist is not None else paths.FRONTEND_DIST
    _mount_spa(app, spa_path)

    return app
