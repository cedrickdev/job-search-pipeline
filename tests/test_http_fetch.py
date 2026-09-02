import pytest

from pipeline import http_fetch
from pipeline.http_fetch import FetchError


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def test_fetch_json_returns_payload(monkeypatch):
    monkeypatch.setattr(http_fetch.requests, "request",
                        lambda *a, **k: FakeResp(payload={"ok": 1}))
    assert http_fetch.fetch_json("https://x.test/api") == {"ok": 1}


def test_fetch_retries_on_5xx_then_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(http_fetch.requests, "request",
                        lambda *a, **k: calls.append(1) or FakeResp(status_code=500))
    monkeypatch.setattr(http_fetch.time, "sleep", lambda s: None)
    with pytest.raises(FetchError):
        http_fetch.fetch("https://x.test")
    assert len(calls) == 3  # initial + RETRIES


def test_fetch_403_fails_without_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(http_fetch.requests, "request",
                        lambda *a, **k: calls.append(1) or FakeResp(status_code=403))
    monkeypatch.setattr(http_fetch.time, "sleep", lambda s: None)
    with pytest.raises(FetchError):
        http_fetch.fetch("https://x.test")
    assert len(calls) == 1
