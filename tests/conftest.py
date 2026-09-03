import sqlite3
from pathlib import Path

import pytest

from pipeline import paths
from pipeline.db import connect, init_db

# A tracked, fully synthetic CV library. The real one (cv/base_cv.yaml) carries
# the operator's identity, is gitignored, and is therefore absent from a clean
# clone — 31 tests used to fail there with FileNotFoundError, and the six that
# assert on the profile's DENSITY could not be satisfied by the sparse
# cv/base_cv.template.yaml either. See docs/V1_BASELINE.md §9.
BASE_CV_FIXTURE = Path(__file__).parent / "fixtures" / "base_cv.yaml"


@pytest.fixture(autouse=True)
def synthetic_base_cv(monkeypatch):
    """Point every test at the synthetic CV library, never the operator's.

    Autouse and unconditional on purpose: if it fell back to the real profile
    when present, the suite would assert different things on a developer's
    machine than in CI, which is the reproducibility bug this closes. To check
    a freshly onboarded real profile instead, run `python -m pipeline.cv_render`
    (prints pages and fill for cv/base_cv.yaml in both languages).
    """
    monkeypatch.setattr(paths, "BASE_CV_PATH", BASE_CV_FIXTURE)


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
