"""The secret-safe JSON log formatter and its idempotent install (§41).

§41 makes one rule non-negotiable — a secret never reaches the log. The formatter enforces it two
ways: it serialises only a *closed* base plus explicitly-attached structured extras (a body, a CV,
a cookie is never one of them), and every extra is redacted by the *name* of its field before it
lands. These tests pin both, plus the correlation id the formatter pulls from the context var and
the idempotent handler install that keeps a second `create_app` from doubling every line.
"""
import io
import json
import logging

import pytest

from backend.app.observability.context import bind_correlation_id
from backend.app.observability.logging import (
    _CONFIGURED_MARKER,
    _REDACTED,
    JsonFormatter,
    configure_logging,
)


def _record(msg: str = "hello", level: int = logging.INFO, exc_info=None,
            **extra: object) -> logging.LogRecord:
    """A LogRecord with `extra` attached exactly as `logger.info(..., extra=...)` would."""
    record = logging.LogRecord(
        name="backend.test", level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=exc_info)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _format(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


def test_the_base_payload_is_a_closed_set_of_operational_fields() -> None:
    payload = _format(_record())
    assert set(payload) == {"timestamp", "level", "logger", "message"}
    assert payload["level"] == "INFO"
    assert payload["logger"] == "backend.test"
    assert payload["message"] == "hello"
    # An ISO-8601 UTC instant, so an aggregator can index by time.
    assert payload["timestamp"].endswith("+00:00")


def test_the_correlation_id_is_attached_only_when_one_is_in_flight() -> None:
    assert "correlation_id" not in _format(_record())
    with bind_correlation_id("abc123"):
        assert _format(_record())["correlation_id"] == "abc123"


def test_a_sensitively_named_extra_is_redacted_by_name_at_every_depth() -> None:
    payload = _format(_record(
        authorization="Bearer sk-live-xyz",
        route="/api/v2/me",
        event={"api_key": "sk-secret", "user": {"password": "p4ss"}, "note": "ok"},
        cookies=["session=abc", "csrf=def"]))
    # Top-level secret-named field: value never seen.
    assert payload["authorization"] == _REDACTED
    # A safe field passes through untouched.
    assert payload["route"] == "/api/v2/me"
    # Nested dicts are walked — a secret two levels down is caught.
    assert payload["event"]["api_key"] == _REDACTED
    assert payload["event"]["user"]["password"] == _REDACTED
    assert payload["event"]["note"] == "ok"
    # `cookies` is itself a sensitive name, so the whole value is replaced.
    assert payload["cookies"] == _REDACTED


@pytest.mark.parametrize("name", [
    "password", "api_key", "apikey", "session_token", "csrf_token", "webhook_secret",
    "ciphertext", "private_key", "X-Api-Key", "db_password", "set-cookie"])
def test_every_secret_marker_family_is_caught(name: str) -> None:
    payload = _format(_record(**{name: "should-never-appear"}))
    assert payload[name] == _REDACTED
    assert "should-never-appear" not in json.dumps(payload)


def test_an_exception_becomes_a_typed_triple_not_a_multiline_traceback() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        payload = _format(_record(exc_info=sys.exc_info()))
    assert payload["error"] == "ValueError"
    assert payload["error_message"] == "boom"
    assert "Traceback" in payload["stack"]
    # Still one JSON object per line: the stack is a string field, not raw newlines in the record.
    assert isinstance(payload["stack"], str)


def test_a_non_json_value_is_coerced_to_its_string_never_a_crash() -> None:
    sentinel = object()
    payload = _format(_record(thing=sentinel))
    assert payload["thing"] == str(sentinel)


@pytest.fixture
def clean_root_logger():
    """Save the root logger's handlers and level, strip our marker handlers, and restore after."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers = [h for h in saved_handlers
                     if not getattr(h, _CONFIGURED_MARKER, False)]
    try:
        yield root
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_configure_logging_installs_one_handler_and_is_idempotent(clean_root_logger) -> None:
    root = clean_root_logger
    stream = io.StringIO()
    configure_logging(level="DEBUG", stream=stream)
    marked = [h for h in root.handlers if getattr(h, _CONFIGURED_MARKER, False)]
    assert len(marked) == 1
    assert root.level == logging.DEBUG

    # A second call — a reload, a second create_app — finds the mark and stacks nothing.
    configure_logging(level="WARNING")
    marked_again = [h for h in root.handlers if getattr(h, _CONFIGURED_MARKER, False)]
    assert len(marked_again) == 1
    # The level is still re-applied on the idempotent path.
    assert root.level == logging.WARNING


def test_configure_logging_writes_one_json_object_per_line(clean_root_logger) -> None:
    root = clean_root_logger
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    with bind_correlation_id("cid-1"):
        logging.getLogger("backend.test").info("access", extra={"password": "nope"})
    line = stream.getvalue().strip()
    payload = json.loads(line)   # parses, so it is exactly one JSON object
    assert payload["message"] == "access"
    assert payload["correlation_id"] == "cid-1"
    assert payload["password"] == _REDACTED
