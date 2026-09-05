# tests/test_v2_discovery_contracts.py
"""The refusals: the states §6 makes unrepresentable rather than merely unlikely.

Every other Phase 5 test file travels *through* these models — a sweep builds a
`DiscoveryRequest`, an adapter returns a `DiscoveryResult`, `failures.py` composes a
`SourceHealth`. None of them asks what happens when a model is wrong, so the
validators that keep a report honest are, as far as the rest of the suite is
concerned, deletable: remove `DiscoveryResult._payload_matches_its_own_report` and
every other Phase 5 test still passes. This file is what makes them undeletable.

Most of what is refused here is refused because of one V1 habit: a run summary that
counted what it liked. V1 reports `{"ok": false}` per source and totals that nothing
reconciles, so "16 requests, 0 postings seen, 4 opportunities returned" is a sentence
V1 can print and nobody can question. `DiscoveryMetrics._counts_add_up` and the four
`DiscoveryResult` validators are the arithmetic that makes each variant of that
sentence impossible to write down.

The base alias types — `NonEmptyStr`, `UtcDatetime`, `CountryCode`, frozen-ness,
unknown-field rejection — belong to `tests/test_v2_base.py` and are not repeated
here. What is tested here is what this module adds on top of them: the bounds it
declares and the five validators it wrote.
"""
import pytest
from pydantic import ValidationError

from backend.app.discovery.capabilities import SourceCapability
from backend.app.discovery.contracts import (
    DiscoveryMetrics,
    DiscoveryRequest,
    DiscoveryResult,
    DiscoveryWarning,
    DiscoveryWarningCode,
    OpportunitySource,
    RadiusConstraint,
    SourceFailureCode,
    SourceHealth,
    SourceHealthStatus,
)
from backend.app.discovery.normalization import opportunity_from_posting
from backend.app.domain.common import GeoPoint
from backend.app.domain.opportunity import Opportunity
from tests.v2_discovery import (
    NOW,
    FakeSource,
    a_health,
    a_metadata,
    a_pack,
    a_request,
    a_result,
)

Status = SourceHealthStatus
Code = SourceFailureCode
Warn = DiscoveryWarningCode
Cap = SourceCapability
PACK = a_pack("CH")
LAUSANNE = GeoPoint(latitude=46.5197, longitude=6.6323)

# The whole point of the four statuses, as a table rather than as a chain of `if`s:
# `is_usable` is the only place the difference between them is ever spent, so a
# fifth member has to appear here to be usable at all — and a `KeyError` in the
# parametrized test below is what says so.
USABILITY = {Status.HEALTHY: True, Status.DEGRADED: True,
             Status.UNAVAILABLE: False, Status.MISCONFIGURED: False}


def an_opportunity(url: str = "https://example.ch/offre/1") -> Opportunity:
    """A real `Opportunity`, because the validators count them.

    A `object()` would satisfy nothing: `DiscoveryResult.opportunities` is typed,
    and the counting validator has to see the same tuple a source would return.
    """
    return opportunity_from_posting(
        {"company": "Neocraft SA", "title": "Developpeur backend", "url": url},
        metadata=a_metadata("jobup"), pack=PACK, fetched_at=NOW)


# --- §6: a request that cannot contradict itself -------------------------------

def test_a_remote_only_request_cannot_also_exclude_remote_work():
    """The one pair of flags with no meaning together.

    `include_remote` says whether remote-first sources are worth querying and
    `remote_only` narrows the search to them, so "only remote, and no remote" leaves
    an adapter to guess which of the two the caller meant. Guessing is the failure
    mode: whichever flag it picked, half the callers would be surprised.
    """
    with pytest.raises(ValidationError, match="remote-only request cannot exclude"):
        a_request(remote_only=True, include_remote=False)


def test_remote_only_is_the_ordinary_narrowing_and_not_a_contradiction():
    request = a_request(remote_only=True)
    assert request.remote_only and request.include_remote


def test_excluding_remote_work_without_asking_for_it_only_is_coherent():
    """The other half of the pair, and the common case: an on-site search."""
    assert not a_request(include_remote=False).remote_only


def test_a_request_asks_for_v1s_window_and_a_hundred_postings_by_default():
    """The defaults are behaviour: V1's `lookback_days=3` is the inherited window.

    `limit` is per source and per request — a cap on one board's answer must not
    depend on how many boards ran before it — so 100 is 100 each, not 100 shared.
    """
    request = a_request()
    assert (request.lookback_days, request.limit) == (3, 100)
    assert request.include_remote and not request.remote_only
    assert request.cursor is None and request.radius is None


@pytest.mark.parametrize("days", [0, -1, 91, 365])
def test_a_lookback_outside_the_answerable_window_is_refused(days):
    """1 to 90 days: zero asks for nothing, and no board indexes a year back.

    `requests.discovery_requests_for` clamps instead of raising, so a profile can
    never trip this — but a caller assembling a request by hand can, and this is
    where it finds out.
    """
    with pytest.raises(ValidationError):
        a_request(lookback_days=days)


@pytest.mark.parametrize("limit", [0, -1, 1001])
def test_a_limit_is_bounded_at_both_ends(limit):
    """Zero would be a request for nothing; the ceiling is what stops a typo.

    An unbounded limit is how one mistyped profile turns into a thousand pages of
    scraping against a board that never asked for it.
    """
    with pytest.raises(ValidationError):
        a_request(limit=limit)


def test_the_ceiling_is_inclusive_so_a_thousand_is_askable():
    assert a_request(limit=1000).limit == 1000


def test_a_request_naming_no_source_admits_every_one_of_them():
    """The repo-wide convention: an empty collection restricts nothing."""
    request = a_request()
    assert request.source_keys == ()
    assert request.allows_source("jobup") and request.allows_source("greenhouse")


def test_an_allow_list_admits_exactly_the_keys_it_names():
    request = a_request(source_keys=("jobup", "jooble"))
    assert request.allows_source("jooble")
    assert not request.allows_source("indeed")


def test_an_allow_listed_key_has_to_look_like_an_identity():
    """Why `discovery_requests_for` lowercases a profile's keys instead of passing them.

    `SourceKey` is the same constrained type the registry indexes by, so `"JobUp"`
    cannot travel on a request at all — an allow-list that silently matched nothing
    would read exactly like a source being down.
    """
    with pytest.raises(ValidationError):
        a_request(source_keys=("JobUp",))


def test_a_blank_keyword_is_refused_rather_than_swept_as_everything():
    """A whitespace keyword reaching a board is a search for its entire catalogue."""
    with pytest.raises(ValidationError):
        a_request(keywords=("developpeur", "   "))


def test_a_cursor_is_a_token_or_absent_and_never_an_empty_string():
    """`""` and `None` mean opposite things: "resume from here" and "start over"."""
    with pytest.raises(ValidationError):
        a_request(cursor="")
    assert a_request(cursor="page=2").cursor == "page=2"


# --- §11: the circle no current source can honour ------------------------------

@pytest.mark.parametrize("radius_km", [0.0, -5.0, 500.1, 20_000.0])
def test_a_radius_is_a_bounded_distance(radius_km):
    """Zero is a point, and 20 000 km is a number pasted into the wrong field.

    The ceiling is deliberately generous — Switzerland is some 350 km across, so
    500 km already reaches every neighbour — which is what makes anything above it a
    mistake rather than an ambitious search.
    """
    with pytest.raises(ValidationError):
        RadiusConstraint(center=LAUSANNE, radius_km=radius_km)


def test_the_radius_ceiling_is_inclusive():
    assert RadiusConstraint(center=LAUSANNE, radius_km=500.0).radius_km == 500.0


def test_a_circle_may_go_unlabelled_because_a_coordinate_is_not_a_place_name():
    """`label` is optional, and §11's policy warning has to describe both shapes.

    A profile drawn on a map has coordinates and no name; one typed as "20 km around
    Lausanne" has both. The model accepts either, and the orchestrator's warning
    falls back to the coordinates.
    """
    assert RadiusConstraint(center=LAUSANNE, radius_km=20.0).label is None
    assert RadiusConstraint(center=LAUSANNE, radius_km=20.0,
                            label="Lausanne").label == "Lausanne"


# --- §6: metrics that reconcile with themselves --------------------------------

def test_a_source_cannot_return_more_postings_than_it_saw():
    """The V1 summary sentence this makes unwriteable."""
    with pytest.raises(ValidationError, match="cannot be smaller than"):
        DiscoveryMetrics(postings_seen=1, opportunities_returned=2)


def test_returned_and_skipped_together_cannot_exceed_what_was_seen():
    """Every posting has one fate, so the two counters partition the same rows."""
    with pytest.raises(ValidationError, match=r"returned \+ skipped"):
        DiscoveryMetrics(postings_seen=3, opportunities_returned=2, postings_skipped=2)


def test_seeing_more_than_was_accounted_for_is_the_ordinary_case():
    """The slack is what `limit` truncated: seen but neither normalized nor rejected."""
    metrics = DiscoveryMetrics(requests_made=1, postings_seen=10,
                               opportunities_returned=4, postings_skipped=1)
    assert metrics.postings_seen == 10


@pytest.mark.parametrize("counter", ["requests_made", "postings_seen",
                                     "opportunities_returned", "postings_skipped",
                                     "duration_ms"])
def test_no_counter_can_run_backwards(counter):
    with pytest.raises(ValidationError):
        DiscoveryMetrics(**{counter: -1})


def test_merging_two_turns_sums_every_counter():
    """How `SweepReport.metrics` is built: one fold over the sources that answered."""
    merged = DiscoveryMetrics(requests_made=1, postings_seen=10,
                              opportunities_returned=8, postings_skipped=1).merge(
        DiscoveryMetrics(requests_made=16, postings_seen=4,
                         opportunities_returned=2, postings_skipped=2))
    assert (merged.requests_made, merged.postings_seen) == (17, 14)
    assert (merged.opportunities_returned, merged.postings_skipped) == (10, 3)


def test_merging_deliberately_forgets_how_long_each_source_took():
    """Two sources that each took a second did not take two seconds.

    They ran concurrently, so a summed `duration_ms` would overstate every sweep it
    described. The orchestrator times the sweep itself and writes that number back
    onto the merged metrics; `merge` leaving `None` is what makes the absence of a
    fabricated total visible.
    """
    merged = DiscoveryMetrics(duration_ms=900).merge(DiscoveryMetrics(duration_ms=1_100))
    assert merged.duration_ms is None


def test_merging_with_a_source_that_did_nothing_changes_nothing():
    """Which also pins the empty record: a source that never ran counts zero, not one.

    This is the shape a raising source reports, so the identity matters — a sweep of
    one healthy and one broken board must read as the healthy board's own numbers.
    """
    turn = DiscoveryMetrics(requests_made=2, postings_seen=5, opportunities_returned=5)
    assert turn.merge(DiscoveryMetrics()) == turn


# --- §6: a result that cannot misreport its own payload -------------------------

def test_a_result_must_count_the_postings_it_actually_carries():
    """The counter and the tuple are two statements of one fact, so they are checked.

    A metric maintained by hand beside the payload it describes is a metric that
    drifts the first time an adapter grows a filtering step.
    """
    with pytest.raises(ValidationError, match="must equal the number of"):
        DiscoveryResult(health=a_health("jobup"), opportunities=(an_opportunity(),),
                        metrics=DiscoveryMetrics(postings_seen=1))


def test_a_result_claiming_more_than_it_carries_is_refused_too():
    """The other direction: a board that says two and hands over one."""
    with pytest.raises(ValidationError, match="must equal the number of"):
        DiscoveryResult(health=a_health("jobup"), opportunities=(an_opportunity(),),
                        metrics=DiscoveryMetrics(postings_seen=2,
                                                 opportunities_returned=2))


@pytest.mark.parametrize("status", [Status.UNAVAILABLE, Status.MISCONFIGURED])
def test_a_source_that_failed_outright_must_not_also_hand_back_postings(status):
    """Because a reader would have to decide whether to trust them, and cannot.

    The status is the answer to "is this result complete?", and UNAVAILABLE with
    postings attached answers both ways at once. DEGRADED is the status for the
    half-answer, and the error message says so.
    """
    with pytest.raises(ValidationError, match="must not also return postings"):
        DiscoveryResult(health=a_health("jobup", status),
                        opportunities=(an_opportunity(),),
                        metrics=DiscoveryMetrics(postings_seen=1,
                                                 opportunities_returned=1))


def test_a_degraded_source_carries_the_postings_it_did_manage_to_collect():
    """Half an answer is real data: eight of ten pages fetched is eight pages.

    This is the case that makes the whole DEGRADED status worth having, and the
    reason the failure above is an error rather than a silent discard.
    """
    result = DiscoveryResult(
        health=a_health("jobup", Status.DEGRADED, reason=Code.SOURCE_PARTIAL_FAILURE,
                        detail="1 of 3 requests failed"),
        opportunities=(an_opportunity(),),
        metrics=DiscoveryMetrics(postings_seen=1, opportunities_returned=1))
    assert result.is_usable and len(result.opportunities) == 1


def test_an_empty_answer_from_a_working_source_is_not_a_failure():
    """A board with nothing new this week is HEALTHY and returns nothing.

    The distinction §18 asks for at sweep level starts here: "nobody is hiring" and
    "nobody answered" must not be the same value.
    """
    result = a_result("jobup")
    assert result.is_usable and result.opportunities == ()
    assert result.health.reason is None


def test_a_warning_cannot_be_attributed_to_another_source():
    """A misattributed warning sends an operator to a board that is working.

    Concurrency is what makes this a real risk: an adapter holding a warning list it
    did not build would blame whoever it copied from.
    """
    with pytest.raises(ValidationError, match="another source"):
        a_result("jobup", warnings=(DiscoveryWarning(
            code=Warn.KEYWORD_IGNORED, detail="the listing is fixed",
            source_key="jooble"),))


def test_a_source_may_warn_about_itself_by_name():
    result = a_result("jobup", warnings=(DiscoveryWarning(
        code=Warn.LOCATION_IGNORED, detail="the listing is fixed",
        source_key="jobup"),))
    assert result.warnings[0].source_key == result.source_key


def test_a_warning_belonging_to_nobody_in_particular_is_allowed_through():
    """Attribution is optional: §11's radius policy is about the request, not a board."""
    result = a_result("jobup", warnings=(DiscoveryWarning(
        code=Warn.RADIUS_NOT_SUPPORTED, detail="no selected source filters by distance",
        capability=Cap.RADIUS_SEARCH),))
    assert result.warnings[0].source_key is None


def test_a_source_that_failed_must_not_hand_back_a_resume_cursor():
    """Resuming from a failed page would skip whatever the failure swallowed."""
    with pytest.raises(ValidationError, match="must not hand back a resume cursor"):
        DiscoveryResult(health=a_health("jobup", Status.UNAVAILABLE), cursor="page=2")


def test_a_degraded_source_may_still_say_where_it_stopped():
    result = DiscoveryResult(
        health=a_health("jobup", Status.DEGRADED, reason=Code.SOURCE_PARTIAL_FAILURE),
        cursor="page=2")
    assert result.cursor == "page=2"


def test_a_result_reads_its_key_from_its_own_health_record():
    """There is no second `source_key` field, so a result cannot disagree with itself.

    V1's shape — postings in one structure, an `ok` flag in another — is what makes a
    mismatch possible at all; one field owned by one model is the fix.
    """
    result = a_result("jooble")
    assert result.source_key == result.health.source_key == "jooble"


@pytest.mark.parametrize("status", list(Status))
def test_a_result_is_worth_reading_exactly_when_its_source_was(status):
    assert DiscoveryResult(health=a_health("jobup", status)).is_usable is USABILITY[status]


# --- §12: a health record that always says why ----------------------------------

@pytest.mark.parametrize("status", [Status.DEGRADED, Status.UNAVAILABLE,
                                    Status.MISCONFIGURED])
def test_a_failure_must_carry_a_reason_code(status):
    """A bare "it broke" is not a report an operator can act on.

    The code is what a dashboard groups by and what separates "wait" from "set one
    variable", so a status without one is refused at construction rather than
    rendered as a blank column later.
    """
    with pytest.raises(ValidationError, match="must carry a reason code"):
        SourceHealth(source_key="jobup", status=status, checked_at=NOW)


def test_a_healthy_source_must_not_carry_a_failure_reason():
    """The symmetric refusal, and the one that keeps a green dashboard green.

    A HEALTHY record with `SOURCE_TIMEOUT` attached is a leftover from a previous
    attempt, and there is no reading of it that is both true and useful.
    """
    with pytest.raises(ValidationError, match="must not carry a failure reason"):
        SourceHealth(source_key="jobup", status=Status.HEALTHY, checked_at=NOW,
                     reason=Code.SOURCE_TIMEOUT)


def test_a_reason_is_required_but_prose_never_is():
    """`detail` stays optional so no adapter has to invent a sentence to report a fact."""
    record = SourceHealth(source_key="jobup", status=Status.UNAVAILABLE,
                          checked_at=NOW, reason=Code.SOURCE_UNAVAILABLE)
    assert record.detail is None and not record.is_usable


@pytest.mark.parametrize("status", list(Status))
def test_every_status_decides_whether_its_answer_is_usable(status):
    """The table in this module's header is the whole vocabulary of that decision."""
    assert a_health("jobup", status).is_usable is USABILITY[status]


def test_a_latency_of_zero_is_a_measurement_and_not_a_missing_one():
    """`None` means "not timed"; `0` means "faster than the clock could see".

    They differ where it matters: a report that treated a falsy latency as absent
    would drop exactly the fastest cached answers from every average it computed.
    """
    timed = a_health("jobup", latency_ms=0)
    assert timed.latency_ms == 0
    assert a_health("jobup").latency_ms is None


def test_a_latency_cannot_be_negative():
    with pytest.raises(ValidationError):
        a_health("jobup", latency_ms=-1)


# --- §6: a warning that has something to say ------------------------------------

def test_a_warning_without_a_detail_is_not_a_warning():
    """The code is for grouping and the detail is for reading; both are mandatory.

    `LOCATION_IGNORED` alone does not tell anyone *which* location a fixed listing
    ignored, and that is the only part a profile's owner can act on.
    """
    with pytest.raises(ValidationError):
        DiscoveryWarning(code=Warn.LOCATION_IGNORED)
    with pytest.raises(ValidationError):
        DiscoveryWarning(code=Warn.LOCATION_IGNORED, detail="  ")


def test_a_warning_may_name_the_capability_it_is_about():
    """Which is what lets a reader group "nobody does radius" without parsing prose."""
    warning = DiscoveryWarning(code=Warn.CAPABILITY_NOT_SUPPORTED,
                               detail="the source does not filter by distance",
                               source_key="jobup", capability=Cap.RADIUS_SEARCH)
    assert warning.capability is Cap.RADIUS_SEARCH


# --- §3: the Protocol, and how to assert against it -----------------------------

def test_a_thirty_line_fake_is_a_source_as_far_as_the_orchestrator_can_tell():
    """The property §3 asks for, stated as cheaply as it can be stated.

    Three members and no inheritance: `FakeSource` never imports a base class, and
    the orchestrator accepts it beside a wrapped job board.
    """
    assert isinstance(FakeSource(), OpportunitySource)


def test_something_missing_a_member_is_not_a_source():
    """A half-written adapter fails the check instead of failing mid-sweep.

    `healthcheck` is the one most easily forgotten — nothing in a discovery run calls
    it — so it is the one left out here.
    """

    class Halfway:
        @property
        def metadata(self):
            return a_metadata()

        async def discover(self, request):
            return a_result()

    assert not isinstance(Halfway(), OpportunitySource)


def test_the_protocol_is_asserted_with_isinstance_and_never_with_issubclass():
    """A note in `contracts.py` that would otherwise only be prose.

    `metadata` is a non-method member, which makes this a data protocol: `issubclass`
    raises `TypeError` rather than answering `False`. A test written the other way
    round would fail for a reason that has nothing to do with the source it was
    checking, so the working form is pinned here once.
    """
    with pytest.raises(TypeError):
        issubclass(FakeSource, OpportunitySource)
    assert isinstance(FakeSource(), OpportunitySource)
