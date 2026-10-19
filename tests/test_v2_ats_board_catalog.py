"""Reviewed ATS boards are the only boards available to V2 discovery."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from backend.app.discovery.ats_board_catalog import (
    ReviewedAtsCatalogError,
    load_reviewed_boards,
)


def _board(**overrides: object) -> dict[str, object]:
    board: dict[str, object] = {
        "token": "sample-board",
        "company": "Sample SA",
        "country": "CH",
        "provenance_url": "https://example.ch/careers",
        "conditions_url": "https://example.ch/terms",
        "reviewed_at": date.today().isoformat(),
        "access_decision": "approved",
        "request_budget_per_day": 5,
    }
    board.update(overrides)
    return board


def _write(path: Path, *, greenhouse: list[dict[str, object]] | None = None,
           lever: list[dict[str, object]] | None = None) -> Path:
    import yaml

    path.write_text(yaml.safe_dump({"version": 1, "boards": {
        "greenhouse": greenhouse or [], "lever": lever or [],
    }}), encoding="utf-8")
    return path


def test_committed_catalog_is_empty():
    catalog = load_reviewed_boards(Path("config/ats_reviewed_boards.yaml"))
    assert catalog.version == 1
    assert catalog.boards["greenhouse"] == ()
    assert catalog.boards["lever"] == ()
    assert catalog.reviews == ()


def test_only_approved_ch_boards_become_runnable(tmp_path):
    path = _write(tmp_path / "boards.yaml", greenhouse=[_board()],
                  lever=[_board(token="held", access_decision="hold")])  # noqa: S106 - public board slug
    catalog = load_reviewed_boards(path)
    assert [board.token for board in catalog.boards["greenhouse"]] == ["sample-board"]
    assert catalog.boards["lever"] == ()
    assert len(catalog.reviews) == 2
    with pytest.raises(TypeError):
        catalog.boards["lever"] = ()


@pytest.mark.parametrize("change", [
    {"country": "FR"},
    {"provenance_url": "http://example.ch/careers"},
    {"conditions_url": "https://user:secret@example.ch/terms"},
    {"request_budget_per_day": 0},
    {"request_budget_per_day": -1},
    {"reviewed_at": None},
    {"reviewed_at": (date.today() - timedelta(days=366)).isoformat()},
    {"token": "x" * 129},
    {"access_decision": "unknown"},
])
def test_bad_review_is_refused_without_echoing_values(tmp_path, change):
    path = _write(tmp_path / "boards.yaml", greenhouse=[_board(**change)])
    with pytest.raises(ReviewedAtsCatalogError) as error:
        load_reviewed_boards(path)
    assert "secret" not in str(error.value)


def test_duplicate_source_and_token_are_refused(tmp_path):
    path = _write(tmp_path / "boards.yaml", greenhouse=[_board(), _board()])
    with pytest.raises(ReviewedAtsCatalogError):
        load_reviewed_boards(path)


def test_unknown_source_key_is_refused(tmp_path):
    path = tmp_path / "boards.yaml"
    path.write_text("version: 1\nboards:\n  greenhouse: []\n  lever: []\n  other: []\n",
                    encoding="utf-8")
    with pytest.raises(ReviewedAtsCatalogError):
        load_reviewed_boards(path)


def test_invalid_review_catalog_fails_http_app_startup(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from server import app as server_app

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("version: 1\nboards: []\n", encoding="utf-8")
    monkeypatch.setattr(server_app, "ATS_REVIEWED_BOARDS_PATH", invalid)
    with pytest.raises(ReviewedAtsCatalogError), TestClient(
            server_app.create_app(db_path=tmp_path / "v1.db")):
        pass


def test_http_app_keeps_startup_catalog_when_file_changes_before_first_request(
        tmp_path, monkeypatch):
    from fastapi import Request
    from fastapi.testclient import TestClient

    from backend.app.api.dependencies import reviewed_ats_catalog
    from server import app as server_app

    path = _write(tmp_path / "boards.yaml")
    monkeypatch.setattr(server_app, "ATS_REVIEWED_BOARDS_PATH", path)
    app = server_app.create_app(db_path=tmp_path / "v1.db")
    with TestClient(app):
        startup_snapshot = app.state.reviewed_ats_catalog
        assert startup_snapshot.boards["greenhouse"] == ()
        _write(path, greenhouse=[_board()])
        request = Request({"type": "http", "app": app, "method": "GET",
                           "path": "/api/v2/me/morning-runs", "headers": []})
        assert reviewed_ats_catalog(request) is startup_snapshot
        assert reviewed_ats_catalog(request).boards["greenhouse"] == ()
