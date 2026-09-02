# tests/test_api_runs.py
"""POST /api/runs/{discover,full} -> 202 (409 when busy); GET /api/runs/status.
The RunManager is replaced with a fake so no subprocess or background task runs;
the real manager's behavior is covered by tests/test_runs.py."""
from server.runs import RunBusyError


class _FakeManager:
    def __init__(self):
        self.calls = []

    async def trigger(self, kind):
        self.calls.append(kind)

    def status(self):
        return {"state": "idle", "kind": None, "started_at": None, "last_run": None}


class _BusyManager:
    async def trigger(self, kind):
        raise RunBusyError("busy")

    def status(self):
        return {"state": "running", "kind": "full", "started_at": "x", "last_run": None}


def test_status_endpoint_shape(api_client):
    r = api_client.get("/api/runs/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"state", "kind", "started_at", "last_run"}
    assert body["state"] == "idle"


def test_discover_returns_202_and_triggers(api_client):
    fake = _FakeManager()
    api_client.app.state.run_manager = fake
    r = api_client.post("/api/runs/discover")
    assert r.status_code == 202
    assert fake.calls == ["discovery"]


def test_full_returns_202_and_triggers(api_client):
    fake = _FakeManager()
    api_client.app.state.run_manager = fake
    r = api_client.post("/api/runs/full")
    assert r.status_code == 202
    assert fake.calls == ["full"]


def test_busy_returns_409(api_client):
    api_client.app.state.run_manager = _BusyManager()
    assert api_client.post("/api/runs/discover").status_code == 409
