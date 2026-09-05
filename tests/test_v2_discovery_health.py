# tests/test_v2_discovery_health.py
"""What a report may say about a failure, and what it may never say.

§12 asks for four normalized statuses and an actionable reason.
docs/ENGINEERING_STANDARDS.md §Security asks for secrets to be redacted from errors
and logs. Those two requirements meet in one concrete leak, which is what this file
is really about: V1's `http_fetch.fetch` raises `FetchError(f"{url}: {last_error}")`
and `pipeline/sources/jooble.py` fetches `https://jooble.org/api/{key}`, so the
obvious implementation of "report why the source failed" writes a live API key into
a health record, from there into a run summary, and from there into whatever
dashboard reads it.

Two halves, mirroring the two defences. The first is `classify_failure`: nine codes,
four statuses, and a `detail` *composed* from a fixed table rather than forwarded —
there is no code path from `str(exc)` to a report. The second is `redact_secrets`,
which runs over that composed sentence anyway, belt and braces for the day someone
adds a path that does forward a message.

The last test is the one that would have caught the leak: a real V1 `FetchError`
carrying a key-shaped URL, through a real adapter, to a `SourceHealth` containing
neither.
"""
from json import JSONDecodeError

import pytest

from backend.app.discovery.adapters.v1_sources import V1QuerySourceAdapter
from backend.app.discovery.contracts import (
    SourceFailureCode,
    SourceHealthStatus,
)
from backend.app.discovery.failures import (
    MAX_DETAIL_LENGTH,
    REDACTED,
    SourceFetchError,
    classify_failure,
    degraded,
    health_from_exception,
    healthy,
    misconfigured,
    redact_secrets,
)
from pipeline.http_fetch import FetchError
from tests.v2_discovery import NOW, a_metadata, a_pack, a_request, frozen_clock

Code = SourceFailureCode
Status = SourceHealthStatus

# Shaped like a credential and belonging to nobody: 24 characters of the alphabet
# `_looks_like_a_key` inspects, which is what a real jooble key looks like to the
# redactor. No test in this file needs a real one, and §5 says none may exist here.
SHAPED_LIKE_A_KEY = "b7f3c1e9d4a5f6b8c1d2e3f4"

KEYED = a_metadata("jooble", requires_credentials=True,
                   credential_env_vars=("JOOBLE_API_KEY",))
PLAIN = a_metadata("jobup")


# --- the ladder: which of the nine codes an exception is -----------------------
#
# Read top to bottom, because `_failure_code` is a ladder and the order is the
# interesting part: an adapter that knows beats a message that hints, a declared
# variable beats an HTTP status, and "the adapter itself broke" is the floor.

def test_an_adapter_that_knows_the_kind_is_believed_before_anything_else():
    """`SourceFetchError(kind=…)` is an adapter saying what it already established.

    Nothing below it on the ladder can override that: re-deriving the code from a
    message the adapter wrote would be guessing at what it already knows.
    """
    failure = classify_failure(
        SourceFetchError("HTTP 429 while parsing", kind=Code.SOURCE_PARSE_FAILED),
        PLAIN)
    assert failure.reason is Code.SOURCE_PARSE_FAILED
    assert failure.status is Status.UNAVAILABLE
    assert "page layout may have changed" in failure.detail


def test_a_declared_variable_named_in_the_message_is_a_configuration_gap():
    """V1's jooble source raises exactly this, and it is not an outage.

    MISCONFIGURED exists to separate "wait for the board to come back" from "set
    one variable", and the detail names the variable because that is the whole
    actionable half.
    """
    failure = classify_failure(
        FetchError("JOOBLE_API_KEY not set (see .env.example)"), KEYED)
    assert failure.status is Status.MISCONFIGURED
    assert failure.reason is Code.SOURCE_MISCONFIGURED
    assert failure.detail == "required configuration is missing: JOOBLE_API_KEY"


def test_another_sources_variable_is_not_this_sources_misconfiguration():
    """Only the variables *this* source declared count.

    A message that happens to quote someone else's variable name would otherwise
    turn a board's outage into a configuration gap nobody can fix.
    """
    failure = classify_failure(
        SourceFetchError("JOOBLE_API_KEY not set (see .env.example)"), PLAIN)
    assert failure.status is Status.UNAVAILABLE
    assert failure.reason is Code.SOURCE_UNAVAILABLE


@pytest.mark.parametrize(("status", "expected"), [
    (401, Code.SOURCE_FORBIDDEN),
    (403, Code.SOURCE_FORBIDDEN),
    (404, Code.SOURCE_NOT_FOUND),
    (410, Code.SOURCE_NOT_FOUND),
    (429, Code.SOURCE_RATE_LIMITED),
    # Everything unlisted — every 5xx, and any 4xx a board invents — is an outage
    # as far as a sweep is concerned.
    (500, Code.SOURCE_UNAVAILABLE),
    (503, Code.SOURCE_UNAVAILABLE),
    (418, Code.SOURCE_UNAVAILABLE),
])
def test_the_status_line_in_a_v1_message_decides_the_code(status, expected):
    """V1 writes `HTTP {code}` into its `FetchError`, so the number is readable."""
    failure = classify_failure(SourceFetchError(f"https://example.ch: HTTP {status}"),
                              PLAIN)
    assert failure.reason is expected
    assert failure.detail.endswith(f"(HTTP {status})")


def test_an_adapters_own_status_is_read_before_the_message():
    """Two sources of truth, and the structured one wins."""
    failure = classify_failure(SourceFetchError("something went wrong",
                                                http_status=429), PLAIN)
    assert failure.reason is Code.SOURCE_RATE_LIMITED


def test_a_rate_limit_is_not_an_outage_by_another_name():
    """Distinct because the operator's response differs: slow down, do not redeploy."""
    failure = classify_failure(SourceFetchError("HTTP 429"), PLAIN)
    assert failure.reason is Code.SOURCE_RATE_LIMITED
    assert failure.detail.startswith("the source refused the request as too frequent")


@pytest.mark.parametrize("exc", [
    TimeoutError(),
    SourceFetchError("the read timed out after 20s"),
    SourceFetchError("Timeout while connecting"),
])
def test_a_timeout_is_its_own_code_whether_typed_or_merely_written(exc):
    """A 20-second wait and an instant refusal are different problems."""
    assert classify_failure(exc, PLAIN).reason is Code.SOURCE_TIMEOUT


@pytest.mark.parametrize("exc", [
    JSONDecodeError("Expecting value", "<html>", 0),
    SourceFetchError("could not parse the listing"),
    SourceFetchError("the layout changed"),
])
def test_a_parse_failure_is_told_apart_from_an_outage(exc):
    """The signal that a board redesigned its page while nobody was looking.

    Reported as an outage it would look like something that fixes itself; reported
    as a parse failure it is a scraper to repair.
    """
    assert classify_failure(exc, PLAIN).reason is Code.SOURCE_PARSE_FAILED


def test_a_fetch_that_said_nothing_recognisable_is_an_outage():
    """DNS, a reset connection, a proxy: from a sweep's view the source is down."""
    failure = classify_failure(SourceFetchError("connection reset by peer"), PLAIN)
    assert failure.reason is Code.SOURCE_UNAVAILABLE
    assert failure.status is Status.UNAVAILABLE


def test_anything_that_is_not_a_fetch_failure_is_the_adapters_own_fault():
    """The floor of the ladder, and the one code that means "do not blame the board".

    A `KeyError` in a wrapper is a bug in this repository. Filing it under
    SOURCE_UNAVAILABLE would send someone to check a board that is working.
    """
    failure = classify_failure(KeyError("title"), PLAIN)
    assert failure.reason is Code.SOURCE_ADAPTER_ERROR
    assert failure.status is Status.UNAVAILABLE
    assert failure.detail == "the adapter failed before the source could answer"


@pytest.mark.parametrize(("reason", "status"), [
    (Code.SOURCE_PARTIAL_FAILURE, Status.DEGRADED),
    (Code.SOURCE_MISCONFIGURED, Status.MISCONFIGURED),
    (Code.SOURCE_UNAVAILABLE, Status.UNAVAILABLE),
    (Code.SOURCE_TIMEOUT, Status.UNAVAILABLE),
    (Code.SOURCE_FORBIDDEN, Status.UNAVAILABLE),
    (Code.SOURCE_NOT_FOUND, Status.UNAVAILABLE),
    (Code.SOURCE_RATE_LIMITED, Status.UNAVAILABLE),
    (Code.SOURCE_PARSE_FAILED, Status.UNAVAILABLE),
    (Code.SOURCE_ADAPTER_ERROR, Status.UNAVAILABLE),
])
def test_only_two_of_the_nine_codes_are_not_an_outage(reason, status):
    """A partial answer still carries postings; a missing variable is a minute's work."""
    assert classify_failure(SourceFetchError("x", kind=reason), PLAIN).status is status


@pytest.mark.parametrize("reason", list(SourceFailureCode))
def test_every_code_has_a_sentence_an_operator_can_read(reason):
    """The table is the whole vocabulary of `detail`, so a new code needs an entry.

    Adding a member without one raises a `KeyError` inside `classify_failure`, which
    is a failure nobody would understand at three in the morning — hence this test.
    """
    detail = classify_failure(SourceFetchError("x", kind=reason), PLAIN).detail
    assert detail and detail[0].islower()


@pytest.mark.parametrize("reason", list(SourceFailureCode))
def test_a_detail_is_one_short_sentence_and_never_a_traceback(reason):
    """Bounded and single-line by construction: nobody can paste a stack into it."""
    detail = classify_failure(SourceFetchError("x" * 500, kind=reason), PLAIN).detail
    assert len(detail) <= MAX_DETAIL_LENGTH
    assert "\n" not in detail


# --- §12: redaction, the second defence ---------------------------------------

@pytest.mark.parametrize("param", [
    "key", "api_key", "api-key", "apikey", "token", "access_token", "auth",
    "password", "passwd", "secret", "signature", "sig",
])
def test_a_credential_in_a_query_string_is_blanked_and_its_name_kept(param):
    """The name is the actionable half; only the value goes."""
    redacted = redact_secrets(
        f"https://example.ch/search?{param}={SHAPED_LIKE_A_KEY}&q=dev")
    assert SHAPED_LIKE_A_KEY not in redacted
    assert f"{param}={REDACTED}" in redacted
    assert "q=dev" in redacted


def test_a_credential_in_a_url_path_is_blanked_on_its_shape():
    """Jooble's actual shape, which no parameter-name rule can catch.

    `https://jooble.org/api/{key}` puts the key in the path, so the redactor
    inspects runs of credential-alphabet characters instead of parameter names.
    """
    redacted = redact_secrets(
        f"https://jooble.org/api/{SHAPED_LIKE_A_KEY}: HTTP 500")
    assert SHAPED_LIKE_A_KEY not in redacted
    assert REDACTED in redacted
    assert "jooble.org/api" in redacted


def test_a_long_word_is_not_a_secret_however_long_it_is():
    """`welcometothejungle` and `wttj_jobs_production_fr` trip a naive length rule.

    Neither is a credential, and a redactor that blanked them would make every
    report about those two sources unreadable.
    """
    text = "welcometothejungle: wttj_jobs_production_fr answered nothing"
    assert redact_secrets(text) == text


def test_a_declared_variables_value_is_blanked_wherever_it_appears(monkeypatch):
    """The one check that cannot be fooled by an unfamiliar URL shape.

    A key made purely of letters slips past the shape heuristic — so the value of
    every variable the source declared is read from the environment and blanked, and
    the second assertion is what proves that pass is the one doing the work.
    """
    monkeypatch.setenv("JOOBLE_API_KEY", "totallysecretvalue")
    leaky = "https://jooble.org/api/totallysecretvalue: HTTP 500"
    assert redact_secrets(leaky, env_var_names=("JOOBLE_API_KEY",)) == \
        f"https://jooble.org/api/{REDACTED}: HTTP 500"
    assert "totallysecretvalue" in redact_secrets(leaky)


def test_a_variable_holding_something_too_short_to_be_a_key_is_left_alone(monkeypatch):
    """Blanking every occurrence of a three-character value would redact prose."""
    monkeypatch.setenv("JOOBLE_API_KEY", "dev")
    text = "developpeur backend, dev tools"
    assert redact_secrets(text, env_var_names=("JOOBLE_API_KEY",)) == text


def test_an_unset_variable_redacts_nothing():
    """An empty value must not turn into a match on every empty string."""
    text = "the source did not answer"
    assert redact_secrets(text, env_var_names=("NEVER_SET_ANYWHERE",)) == text


def test_a_long_detail_is_truncated_rather_than_forwarded_whole():
    redacted = redact_secrets("word " * 200)
    assert len(redacted) == MAX_DETAIL_LENGTH
    assert redacted.endswith("…")


def test_whitespace_is_collapsed_so_a_record_stays_one_line():
    """A multi-line message in a status page is how a traceback gets in sideways."""
    assert redact_secrets("the source\n  did not\tanswer") == \
        "the source did not answer"


# --- the four records a source can produce ------------------------------------

def test_a_healthy_record_carries_no_reason_at_all():
    """`SourceHealth` refuses one, so "healthy, because…" cannot be written."""
    record = healthy(PLAIN, checked_at=NOW, latency_ms=12)
    assert record.status is Status.HEALTHY
    assert record.reason is None and record.detail is None
    assert record.latency_ms == 12
    assert record.is_usable


def test_a_degraded_record_says_what_part_failed_and_stays_usable():
    """Half an answer is real data: the postings it did return are still postings."""
    record = degraded(PLAIN, checked_at=NOW, detail="1 of 3 requests failed: HTTP 500")
    assert record.status is Status.DEGRADED
    assert record.reason is Code.SOURCE_PARTIAL_FAILURE
    assert record.detail == "1 of 3 requests failed: HTTP 500"
    assert record.is_usable


def test_a_detail_an_adapter_wrote_itself_is_redacted_on_the_way_in():
    """The one string that does not come from the fixed table, so it goes through too."""
    record = degraded(KEYED, checked_at=NOW,
                      detail=f"1 of 3 requests failed: "
                             f"https://jooble.org/api/{SHAPED_LIKE_A_KEY}")
    assert SHAPED_LIKE_A_KEY not in record.detail
    assert record.detail.startswith("1 of 3 requests failed")


def test_a_misconfigured_record_names_the_variable_and_never_its_value(monkeypatch):
    """§5: the name is actionable and cannot itself be a secret.

    `EnvVarName` constrains it to `^[A-Z][A-Z0-9_]*$`, which no API key matches — so
    "set this variable" is sayable without ever holding the value.
    """
    monkeypatch.setenv("JOOBLE_API_KEY", SHAPED_LIKE_A_KEY)
    record = misconfigured(KEYED, checked_at=NOW)
    assert record.status is Status.MISCONFIGURED
    assert record.reason is Code.SOURCE_MISCONFIGURED
    assert record.detail == "required configuration is missing: JOOBLE_API_KEY"
    assert not record.is_usable


def test_a_misconfiguration_can_name_only_the_variables_actually_missing():
    """Two variables, one unset: sending the operator to both would waste their time."""
    both = a_metadata("multi", requires_credentials=True,
                      credential_env_vars=("FIRST_TOKEN", "SECOND_TOKEN"))
    record = misconfigured(both, checked_at=NOW, missing=("SECOND_TOKEN",))
    assert record.detail.endswith("SECOND_TOKEN")
    assert "FIRST_TOKEN" not in record.detail


def test_a_failure_keeps_the_latency_it_took_to_fail():
    """A 20-second timeout and an instant refusal are the same code, not the same bug."""
    record = health_from_exception(TimeoutError(), PLAIN, checked_at=NOW,
                                   latency_ms=20_000)
    assert record.reason is Code.SOURCE_TIMEOUT
    assert record.latency_ms == 20_000
    assert record.checked_at == NOW
    assert not record.is_usable


# --- the leak this module exists to prevent -----------------------------------

@pytest.mark.asyncio
async def test_a_v1_url_carrying_a_key_never_reaches_a_health_record(monkeypatch):
    """End to end, through the real adapter: the test that would have caught it.

    `pipeline/http_fetch.py` raises `FetchError(f"{url}: {last_error}")` and
    `pipeline/sources/jooble.py` fetches `https://jooble.org/api/{key}`. The message
    is therefore a live credential, and it crosses into `SourceFetchError` exactly
    once — where it is read for its status line and never copied out. The detail
    asserted here is the *whole* detail, which is what makes that verifiable.
    """
    monkeypatch.setenv("JOOBLE_API_KEY", SHAPED_LIKE_A_KEY)

    def search(query: str, location: str, lookback_days: int = 3) -> list[dict]:
        raise FetchError(
            f"https://jooble.org/api/{SHAPED_LIKE_A_KEY}: HTTP 500 Server Error")

    source = V1QuerySourceAdapter(metadata=KEYED, search=search,
                                  packs=lambda country: a_pack("CH"),
                                  clock=frozen_clock())
    result = await source.discover(a_request())
    assert result.health.status is Status.UNAVAILABLE
    assert result.health.reason is Code.SOURCE_UNAVAILABLE
    assert result.health.detail == "the source did not answer (HTTP 500)"
    serialized = result.model_dump_json()
    assert SHAPED_LIKE_A_KEY not in serialized
    assert "jooble.org" not in serialized







