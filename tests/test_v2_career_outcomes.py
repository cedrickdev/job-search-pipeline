# tests/test_v2_career_outcomes.py
"""`OutcomeService` : observer le monde réel sans jamais toucher l'état d'exécution (§2, §84).

C'est le maillon « observer » de l'échine de la phase 15. Un `ApplicationOutcome` est un fait
sur le *processus de recrutement* — un accusé de réception, un entretien, une offre, un refus —
et l'enregistrer ne déplace **jamais** l'`ApplicationState` de la phase 12. Le test porteur de ce
module est donc celui qui enregistre un `REJECTED` et prouve que la candidature reste `SUBMITTED`
et ne bascule pas en `FAILED` : la séparation sur laquelle repose toute la phase est vérifiée ici,
pas seulement affirmée.

Le reste tient les disciplines du service : l'idempotence par l'id dérivé (un « marqué comme
entretenu » double-cliqué retombe sur une ligne, deux vrais tours restent deux), la correction qui
*supersede* sans détruire, la rétractation qui survit pour rester auditable, et la frontière —
chaque méthode prend un `user_id` que l'appelant ne choisit pas, si bien que la candidature d'un
autre compte se lit *absente* (`APPLICATION_NOT_FOUND`).

Tout tourne sur `tests/v2_fakes.py` : pas d'horloge, pas de socket, pas de base.
"""
from datetime import UTC, datetime

import pytest

from backend.app.career.errors import CareerError, CareerErrorCode
from backend.app.career.outcomes import OutcomeService
from backend.app.domain.application import (
    Application,
    ApplicationState,
    build_idempotency_key,
)
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.identifiers import (
    ApplicationId,
    UserId,
    application_id,
    application_outcome_id,
    new_application_decision_id,
)
from backend.app.domain.outcome import (
    OutcomeKind,
    OutcomeSource,
    OutcomeStatus,
    build_outcome_key,
)
from tests.v2_builders import OPPORTUNITY, OTHER_USER, PROFILE, USER
from tests.v2_fakes import FakeApplicationOutcomeRepository, FakeApplicationRepository

pytestmark = pytest.mark.asyncio

# A monotonic clock: `occurred_at` (when it happened in the world) is kept strictly apart from
# `recorded_at` (when we wrote it down), because the gap between them is signal (§8).
OCCURRED = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
LATER_OCCURRED = datetime(2026, 5, 8, 10, 0, tzinfo=UTC)
RECORDED = datetime(2026, 5, 2, 9, 0, tzinfo=UTC)
RERECORDED = datetime(2026, 5, 9, 9, 0, tzinfo=UTC)


def build_service() -> tuple[OutcomeService, FakeApplicationOutcomeRepository,
                             FakeApplicationRepository]:
    outcomes = FakeApplicationOutcomeRepository()
    applications = FakeApplicationRepository()
    return OutcomeService(outcomes, applications), outcomes, applications


def an_application(*, user_id: UserId = USER,
                   state: ApplicationState = ApplicationState.SUBMITTED) -> Application:
    """A Phase 12 application keyed the way the real engine keys it, in `state`.

    The id derives from the idempotency key exactly as production does, so the row a test seeds
    is the one the service's ownership check resolves. `state` defaults to `SUBMITTED` so a
    rejection-recording test can prove the state is left untouched.
    """
    key = build_idempotency_key(
        candidate_profile_id=PROFILE, channel=ApplicationChannel.BROWSER,
        opportunity_id=OPPORTUNITY, company_id=None)
    return Application(
        id=application_id(key), user_id=user_id, candidate_profile_id=PROFILE,
        decision_id=new_application_decision_id(), channel=ApplicationChannel.BROWSER,
        state=state, idempotency_key=key, opportunity_id=OPPORTUNITY,
        created_at=OCCURRED, updated_at=OCCURRED)


async def _seed(applications: FakeApplicationRepository, *,
                user_id: UserId = USER,
                state: ApplicationState = ApplicationState.SUBMITTED) -> Application:
    return await applications.upsert(an_application(user_id=user_id, state=state))


# --- recording a milestone --------------------------------------------------------------


async def test_record_derives_a_stable_id_so_a_once_only_milestone_collapses() -> None:
    """A double-clicked "mark as acknowledged" writes one row, not two (§46, §81)."""
    service, outcomes, applications = build_service()
    app = await _seed(applications)

    first = await service.record(USER, app.id, kind=OutcomeKind.ACKNOWLEDGED,
                                 occurred_at=OCCURRED, now=RECORDED)
    second = await service.record(USER, app.id, kind=OutcomeKind.ACKNOWLEDGED,
                                  occurred_at=LATER_OCCURRED, now=RERECORDED)

    assert first.id == second.id
    assert len(outcomes.outcomes) == 1
    assert first.outcome_key == "ACKNOWLEDGED"


async def test_record_keeps_two_genuine_rounds_of_a_repeatable_milestone() -> None:
    """Two interviews on different days are two rows: the key folds in `occurred_at`."""
    service, outcomes, applications = build_service()
    app = await _seed(applications)

    first = await service.record(USER, app.id, kind=OutcomeKind.INTERVIEW,
                                 occurred_at=OCCURRED, now=RECORDED)
    second = await service.record(USER, app.id, kind=OutcomeKind.INTERVIEW,
                                  occurred_at=LATER_OCCURRED, now=RERECORDED)

    assert first.id != second.id
    assert len(outcomes.outcomes) == 2


async def test_record_keeps_occurred_at_and_recorded_at_apart() -> None:
    """When it happened is one fact; when we learned it is another (§8)."""
    service, _, applications = build_service()
    app = await _seed(applications)

    outcome = await service.record(USER, app.id, kind=OutcomeKind.SCREEN,
                                   occurred_at=OCCURRED, now=RECORDED,
                                   source=OutcomeSource.EMAIL, detail="Appel de tri.")

    assert outcome.occurred_at == OCCURRED
    assert outcome.recorded_at == RECORDED
    assert outcome.source is OutcomeSource.EMAIL
    assert outcome.status is OutcomeStatus.EFFECTIVE


# APPEND-MARKER


async def test_recording_a_rejection_never_touches_the_application_state() -> None:
    """THE acceptance test: a recruiter's "no" is an outcome, not an execution failure (§2, §84).

    The service holds the application store only to *read* it for ownership; it has no method
    that could transition an application. So recording a `REJECTED` outcome against a `SUBMITTED`
    application leaves that application `SUBMITTED` — never `FAILED`.
    """
    service, _, applications = build_service()
    app = await _seed(applications, state=ApplicationState.SUBMITTED)

    outcome = await service.record(USER, app.id, kind=OutcomeKind.REJECTED,
                                   occurred_at=OCCURRED, now=RECORDED)

    assert outcome.kind is OutcomeKind.REJECTED
    assert outcome.is_terminal
    stored = await applications.get(USER, app.id)
    assert stored is not None
    assert stored.state is ApplicationState.SUBMITTED
    assert stored.updated_at == OCCURRED      # untouched: the record advanced nothing


async def test_recording_against_a_foreign_application_is_not_found() -> None:
    """The application is read owner-first, so another account's is refused, not written."""
    service, outcomes, applications = build_service()
    app = await _seed(applications, user_id=OTHER_USER)

    with pytest.raises(CareerError) as refusal:
        await service.record(USER, app.id, kind=OutcomeKind.ACKNOWLEDGED,
                             occurred_at=OCCURRED, now=RECORDED)

    assert refusal.value.code is CareerErrorCode.APPLICATION_NOT_FOUND
    assert outcomes.outcomes == {}


async def test_recording_against_a_missing_application_is_not_found() -> None:
    """A missing application and a foreign one read the same: absent (§7-9)."""
    service, _, _ = build_service()
    key = build_idempotency_key(candidate_profile_id=PROFILE,
                                channel=ApplicationChannel.BROWSER,
                                opportunity_id=OPPORTUNITY, company_id=None)

    with pytest.raises(CareerError) as refusal:
        await service.record(USER, application_id(key), kind=OutcomeKind.ACKNOWLEDGED,
                             occurred_at=OCCURRED, now=RECORDED)

    assert refusal.value.code is CareerErrorCode.APPLICATION_NOT_FOUND


# --- correcting a milestone -------------------------------------------------------------


async def test_correct_supersedes_the_predecessor_with_a_new_outcome() -> None:
    """A correction is a new row pointing back; the predecessor survives as SUPERSEDED (§9, §64)."""
    service, outcomes, applications = build_service()
    app = await _seed(applications)
    original = await service.record(USER, app.id, kind=OutcomeKind.INTERVIEW,
                                    occurred_at=OCCURRED, now=RECORDED)

    correction = await service.correct(USER, original.id, kind=OutcomeKind.INTERVIEW,
                                       occurred_at=LATER_OCCURRED, now=RERECORDED,
                                       detail="Date corrigée.")

    assert correction.supersedes_id == original.id
    assert correction.is_correction
    assert correction.status is OutcomeStatus.EFFECTIVE
    assert correction.occurred_at == LATER_OCCURRED
    assert correction.application_id == app.id
    assert correction.outcome_key == f"correction:{original.id}"
    superseded = await outcomes.get(USER, original.id)
    assert superseded is not None
    assert superseded.status is OutcomeStatus.SUPERSEDED
    assert superseded.recorded_at == RERECORDED
    assert len(outcomes.outcomes) == 2


async def test_correcting_an_already_superseded_outcome_is_refused() -> None:
    """Correcting a corrected outcome would fork history; it raises OUTCOME_NOT_EFFECTIVE."""
    service, _, applications = build_service()
    app = await _seed(applications)
    original = await service.record(USER, app.id, kind=OutcomeKind.INTERVIEW,
                                    occurred_at=OCCURRED, now=RECORDED)
    await service.correct(USER, original.id, kind=OutcomeKind.INTERVIEW,
                         occurred_at=LATER_OCCURRED, now=RERECORDED)

    with pytest.raises(CareerError) as refusal:
        await service.correct(USER, original.id, kind=OutcomeKind.INTERVIEW,
                             occurred_at=OCCURRED, now=RERECORDED)

    assert refusal.value.code is CareerErrorCode.OUTCOME_NOT_EFFECTIVE


async def test_correcting_a_missing_outcome_is_not_found() -> None:
    service, _, applications = build_service()
    app = await _seed(applications)

    with pytest.raises(CareerError) as refusal:
        await service.correct(USER, application_outcome_id(app.id, "ghost"),
                             kind=OutcomeKind.INTERVIEW, occurred_at=OCCURRED,
                             now=RECORDED)

    assert refusal.value.code is CareerErrorCode.OUTCOME_NOT_FOUND


async def test_one_account_cannot_correct_anothers_outcome() -> None:
    """A known outcome id with the wrong owner reads as absent, so nothing is written."""
    service, outcomes, applications = build_service()
    app = await _seed(applications, user_id=OTHER_USER)
    hers = await service.record(OTHER_USER, app.id, kind=OutcomeKind.INTERVIEW,
                               occurred_at=OCCURRED, now=RECORDED)

    with pytest.raises(CareerError) as refusal:
        await service.correct(USER, hers.id, kind=OutcomeKind.INTERVIEW,
                             occurred_at=LATER_OCCURRED, now=RERECORDED)

    assert refusal.value.code is CareerErrorCode.OUTCOME_NOT_FOUND
    stored = await outcomes.get(OTHER_USER, hers.id)
    assert stored is not None
    assert stored.status is OutcomeStatus.EFFECTIVE


# --- retracting a milestone -------------------------------------------------------------


async def test_retract_flips_the_status_and_keeps_the_row() -> None:
    """A retraction is a status flip to RETRACTED; the row stays so it is itself auditable (§64)."""
    service, outcomes, applications = build_service()
    app = await _seed(applications)
    outcome = await service.record(USER, app.id, kind=OutcomeKind.OFFER_RECEIVED,
                                   occurred_at=OCCURRED, now=RECORDED)

    retracted = await service.retract(USER, outcome.id, now=RERECORDED)

    assert retracted.id == outcome.id
    assert retracted.status is OutcomeStatus.RETRACTED
    assert retracted.recorded_at == RERECORDED
    assert len(outcomes.outcomes) == 1


async def test_retracting_a_non_effective_outcome_is_refused() -> None:
    service, _, applications = build_service()
    app = await _seed(applications)
    outcome = await service.record(USER, app.id, kind=OutcomeKind.OFFER_RECEIVED,
                                   occurred_at=OCCURRED, now=RECORDED)
    await service.retract(USER, outcome.id, now=RERECORDED)

    with pytest.raises(CareerError) as refusal:
        await service.retract(USER, outcome.id, now=RERECORDED)

    assert refusal.value.code is CareerErrorCode.OUTCOME_NOT_EFFECTIVE


# --- the timeline -----------------------------------------------------------------------


async def test_timeline_returns_every_status_oldest_first() -> None:
    """The timeline keeps the correction history: superseded and effective rows side by side."""
    service, _, applications = build_service()
    app = await _seed(applications)
    original = await service.record(USER, app.id, kind=OutcomeKind.INTERVIEW,
                                    occurred_at=OCCURRED, now=RECORDED)
    await service.correct(USER, original.id, kind=OutcomeKind.INTERVIEW,
                         occurred_at=LATER_OCCURRED, now=RERECORDED)

    timeline = await service.timeline(USER, app.id)

    assert len(timeline) == 2
    assert [o.occurred_at for o in timeline] == [OCCURRED, LATER_OCCURRED]
    assert {o.status for o in timeline} == {OutcomeStatus.SUPERSEDED,
                                            OutcomeStatus.EFFECTIVE}


async def test_timeline_of_a_foreign_application_is_not_found() -> None:
    """A foreign application raises rather than returning an empty list a caller can't read."""
    service, _, applications = build_service()
    app = await _seed(applications, user_id=OTHER_USER)

    with pytest.raises(CareerError) as refusal:
        await service.timeline(USER, app.id)

    assert refusal.value.code is CareerErrorCode.APPLICATION_NOT_FOUND

