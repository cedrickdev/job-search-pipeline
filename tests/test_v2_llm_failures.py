# tests/test_v2_llm_failures.py
"""Every provider failure becomes one typed, secret-free code (§61).

Two things this module must guarantee, and both are security properties: a detail is
composed from a fixed table (never a provider's own message, which can echo the key
it rejected), and `redact_secrets` blanks anything credential-shaped anyway. These
tests pin the classification table, the retryable set the router branches on, and the
redaction — including the concrete-secret path a caller uses when it holds the key.
"""
from backend.app.llm.failures import (
    REDACTED,
    LLMError,
    LLMFailureCode,
    classify_provider_failure,
    redact_secrets,
)


# --- classification --------------------------------------------------------

def test_an_http_401_is_auth_required():
    error = classify_provider_failure(RuntimeError("boom"), http_status=401)
    assert error.code is LLMFailureCode.PROVIDER_AUTH_REQUIRED


def test_an_http_429_is_rate_limited_and_retryable():
    error = classify_provider_failure(RuntimeError("boom"), http_status=429)
    assert error.code is LLMFailureCode.PROVIDER_RATE_LIMITED
    assert error.retryable is True


def test_an_http_500_is_internal_error():
    error = classify_provider_failure(RuntimeError("boom"), http_status=503)
    assert error.code is LLMFailureCode.PROVIDER_INTERNAL_ERROR


def test_a_timeout_error_is_classified_from_the_type():
    error = classify_provider_failure(TimeoutError("timed out"))
    assert error.code is LLMFailureCode.PROVIDER_TIMEOUT


def test_a_connection_refused_message_is_unavailable():
    error = classify_provider_failure(OSError("Connection refused"))
    assert error.code is LLMFailureCode.PROVIDER_UNAVAILABLE


def test_a_context_length_message_is_classified():
    error = classify_provider_failure(RuntimeError("maximum context length exceeded"))
    assert error.code is LLMFailureCode.CONTEXT_LENGTH_EXCEEDED


def test_an_unrecognised_exception_is_internal_error():
    error = classify_provider_failure(RuntimeError("something odd"))
    assert error.code is LLMFailureCode.PROVIDER_INTERNAL_ERROR


def test_an_already_typed_error_passes_through_unchanged():
    original = LLMError(LLMFailureCode.STRUCTURED_OUTPUT_INVALID)
    assert classify_provider_failure(original) is original


def test_a_status_in_the_message_is_read_out():
    error = classify_provider_failure(RuntimeError("server returned status: 403"))
    assert error.code is LLMFailureCode.PROVIDER_AUTH_REQUIRED


# --- the detail table, never the provider's message ------------------------

def test_the_detail_comes_from_the_table_not_the_exception():
    # The exception message must not appear in the composed detail.
    error = classify_provider_failure(RuntimeError("KEY sk-abcdef leaked here"),
                                      http_status=500)
    assert "sk-abcdef" not in error.detail
    assert error.detail == "the provider failed unexpectedly"


# --- redaction -------------------------------------------------------------

def test_a_bearer_token_is_redacted():
    assert "sk-abcdef123456" not in redact_secrets("Authorization: Bearer sk-abcdef123456")


def test_an_sk_key_is_redacted():
    out = redact_secrets("the key sk-verylongkeymaterial was rejected")
    assert "sk-verylongkeymaterial" not in out
    assert REDACTED in out


def test_a_query_param_secret_is_redacted():
    out = redact_secrets("GET /v1?api_key=supersecretvalue&x=1")
    assert "supersecretvalue" not in out


def test_a_concrete_secret_value_is_blanked():
    out = redact_secrets("the value MyPla1nGatewayKey was refused",
                         secret_values=["MyPla1nGatewayKey"])
    assert "MyPla1nGatewayKey" not in out
    assert REDACTED in out


def test_a_very_short_secret_is_not_blanked():
    # Blanking a two-char value would redact ordinary prose.
    out = redact_secrets("the value ab was refused", secret_values=["ab"])
    assert "ab" in out


def test_a_long_detail_is_truncated():
    out = redact_secrets("x" * 500)
    assert len(out) <= 200


def test_the_error_carries_the_http_status():
    error = classify_provider_failure(RuntimeError("boom"), http_status=429)
    assert error.http_status == 429
