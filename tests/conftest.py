import sqlite3
import pytest

from pipeline.db import connect, init_db


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def api_client(tmp_path):
    from fastapi.testclient import TestClient
    from server.app import create_app
    db_path = tmp_path / "api.db"
    settings_path = tmp_path / "settings.json"
    bootstrap = connect(db_path)
    init_db(bootstrap)
    bootstrap.close()
    with TestClient(create_app(db_path=db_path, settings_path=settings_path)) as c:
        c.db_path = db_path
        c.settings_path = settings_path
        yield c


@pytest.fixture
def db_conn(api_client):
    """A raw connection to the SAME database the app under test reads/writes.

    Phase 8/9 route tests seed rows via this fixture and then assert through
    api_client. It MUST bind to api_client.db_path — NOT the existing `conn`
    fixture, which opens a different file (tmp_path/"test.db") the app never sees.
    """
    c = connect(api_client.db_path)
    c.execute("PRAGMA busy_timeout = 5000")
    yield c
    c.close()
