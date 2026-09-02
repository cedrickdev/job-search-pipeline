"""Request-scoped helpers shared by routers."""
import sqlite3

from fastapi import Request

from pipeline.db import connect


def get_conn(request: Request):
    """Yield a per-request sqlite connection with the dashboard's busy timeout.

    ``check_same_thread=False``: FastAPI sets up this sync generator dependency
    and runs the sync endpoint on different threadpool threads, so the
    connection is created on one thread and used on another. Safe here — each
    request owns its connection and uses it serially, never concurrently.
    """
    conn = connect(request.app.state.db_path, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
    finally:
        conn.close()
