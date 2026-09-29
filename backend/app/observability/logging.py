"""Structured, secret-safe JSON logging (§41).

Production logs are consumed by machines before people: a scraper ships them to an aggregator that
indexes by field, so a log line is a JSON object, not a sentence. This module is that formatter and
the one rule §41 makes non-negotiable — **a secret never reaches the log**. Two defences enforce it:

- the formatter only ever serialises a *closed* set of operational fields (the §41 list) plus
  explicitly-attached structured extras; a request/response body is never one of them, so a CV, an
  answer or a cookie cannot ride along by default;
- every value that is emitted passes through a redactor keyed on the *name* of the field, so an
  extra a caller names `password`, `api_key`, `authorization`, `session_token` (or anything whose
  name carries one of those markers) is replaced by `[REDACTED]` rather than logged — a backstop
  for the caller who attaches a dict without thinking.

`configure_logging` installs this on the root logger idempotently, so importing it twice (a reload,
a second `create_app`) does not stack handlers and multiply every line.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from backend.app.observability.context import get_correlation_id

# A field whose (lower-cased) name contains one of these markers is redacted, value unseen. The set
# is the union of every secret the codebase handles: auth (password/argon2), the LLM vault
# (api_key/secret/ciphertext), the session layer (cookie/session/csrf digests), and billing webhook
# signing secrets. Matching on the name, not the value, is what makes it a reliable backstop.
_SENSITIVE_MARKERS: frozenset[str] = frozenset({
    "password", "passwd", "secret", "token", "cookie", "api_key", "apikey",
    "authorization", "auth_header", "session", "csrf", "credential", "ciphertext",
    "private_key", "signature", "webhook_secret",
})

_REDACTED: str = "[REDACTED]"

# The LogRecord attributes the stdlib sets on every record. Anything on a record NOT in this set was
# attached by the caller as a structured extra and is a candidate for the JSON payload.
_STANDARD_RECORD_ATTRS: frozenset[str] = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    "message", "asctime",
})

# The sentinel a configured handler carries so `configure_logging` is idempotent — a second call
# (a reload, a second `create_app`) finds the mark and does not stack a second handler onto root.
_CONFIGURED_MARKER: str = "_jobsearch_json_handler"


def _is_sensitive(name: str) -> bool:
    """True when a field's *name* carries a secret marker — the redactor's whole decision.

    Substring, case-insensitive: `X-Api-Key`, `db_password`, `refresh_token` and `set-cookie` all
    match without the redactor ever inspecting the value. Deciding on the name is what makes this a
    reliable backstop rather than a guess about the contents.
    """
    # Normalise the hyphen a header-style name carries to the underscore the markers use, so
    # `X-Api-Key` and `set-cookie` match `api_key`/`cookie` exactly as `db_password` matches.
    lowered = name.lower().replace("-", "_")
    return any(marker in lowered for marker in _SENSITIVE_MARKERS)


def _redact(value: Any) -> Any:
    """Recursively replace the *values* of sensitively-named keys with `[REDACTED]`.

    A structured extra is often a nested dict (an event payload, a settings snapshot); redaction
    walks it so a secret buried two levels down is caught, not just a top-level `password=`. Lists
    and tuples are walked element-wise; a scalar under a safe name passes through untouched. Non-
    JSON scalars are handed to the formatter's default coercion, so this only decides *what* to
    keep, never *how* to serialise it.
    """
    if isinstance(value, Mapping):
        return {
            key: (_REDACTED if _is_sensitive(str(key)) else _redact(inner))
            for key, inner in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    """Render a `LogRecord` as one flat JSON object — the §41 line an aggregator indexes.

    The payload is a *closed* base — timestamp, level, logger, message, and the correlation id
    pulled from the context var — merged with the caller's structured extras (anything on the
    record the stdlib did not put there). Every extra is redacted by name before it lands, so a
    caller who attaches `{"api_key": ...}` without thinking logs `[REDACTED]`, not the key. An
    exception, if present, is rendered to a `error`/`error_message`/`stack` triple rather than a
    multi-line traceback that would break one-object-per-line parsing.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        correlation_id = get_correlation_id()
        if correlation_id is not None:
            payload["correlation_id"] = correlation_id

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key.startswith("_"):
                continue
            payload[key] = _REDACTED if _is_sensitive(key) else _redact(value)

        if record.exc_info and record.exc_info[0] is not None:
            exc_type, exc_value, _ = record.exc_info
            payload["error"] = exc_type.__name__
            payload["error_message"] = str(exc_value)
            payload["stack"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["stack"] = record.exc_text

        return json.dumps(payload, default=self._coerce, separators=(",", ":"))

    @staticmethod
    def _coerce(value: Any) -> str:
        """Last resort for a value `json` cannot serialise — its `str`, never a crash mid-line."""
        return str(value)


def configure_logging(*, level: str = "INFO", stream: Any = None,
                      json_output: bool = True) -> None:
    """Install the JSON formatter on the root logger, once (§41).

    Idempotent by design: the handler this installs carries a sentinel attribute, so a second call
    — a test that builds a fresh app, a reload — finds the mark and returns rather than stacking a
    second handler and doubling every line. `json_output=False` keeps the same closed record shape
    but renders the human `logging.Formatter` default, for a developer reading a terminal rather
    than an aggregator reading a stream.

    Only the root handler is touched; library loggers propagate to it, so one install governs the
    whole process. The level is applied to both the root logger and the handler.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, _CONFIGURED_MARKER, False):
            handler.setLevel(level)
            root.setLevel(level)
            return

    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter() if json_output else logging.Formatter())
    setattr(handler, _CONFIGURED_MARKER, True)
    handler.setLevel(level)
    root.addHandler(handler)
    root.setLevel(level)
