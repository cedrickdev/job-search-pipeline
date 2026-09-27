# tests/test_v2_api_outcomes.py
"""`/api/v2` — enregistrer le monde réel contre une candidature, vu par un navigateur.

Les tests de service (`test_v2_career_outcomes.py`) tranchent les décisions ; celui-ci tient la
couche route et son câblage. La règle porteuse de la phase se lit au fil : un `ApplicationOutcome`
est un fait sur le *processus de recrutement*, jamais l'état d'exécution de la phase 12. Le test
central enregistre un `REJECTED` par HTTP puis vérifie côté serveur que la candidature reste
`SUBMITTED` — la séparation sur laquelle toute la phase repose, prouvée au fil et pas seulement
dans le domaine.

Le propriétaire n'est jamais dans le chemin ni dans le corps : c'est le compte résolu depuis la
session. Une candidature d'un autre compte se lit *absente* (404 `application_not_found`), un
outcome étranger ou manquant *absent* (404 `outcome_not_found`), si bien qu'aucun id ne se sonde.
`occurred_at` arrive comme un instant conscient du fuseau, validé au bord (un instant naïf est un
422, pas un 500 du domaine).
"""
from datetime import UTC, datetime

import pytest

from backend.app.domain.application import (
    Application,
    ApplicationState,
    build_idempotency_key,
)
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.identifiers import (
    application_id,
    default_candidate_profile_id,
    new_application_decision_id,
)
from tests.v2_api import api_harness
from tests.v2_builders import OPPORTUNITY

pytestmark = pytest.mark.asyncio

# When it happened in the world, kept strictly apart from when the platform learned of it: the
# request only sends `occurred_at`, and `recorded_at` is stamped from the harness clock (§8).
OCCURRED = "2026-03-15T10:00:00Z"
LATER_OCCURRED = "2026-03-22T10:00:00Z"


async def _seed_application(api, *, state=ApplicationState.SUBMITTED, owner=None):
    """Seed a Phase 12 application keyed exactly as the real engine keys it.

    The id derives from the idempotency key, so the seeded row is the one the outcome service
    resolves owner-first when a route names it. `owner` defaults to the signed-in account, and
    `state` defaults to `SUBMITTED` so a rejection-recording test can prove it is left untouched.
    """
    owner_id = owner if owner is not None else next(iter(api.users.users))
    profile_id = default_candidate_profile_id(owner_id)
    key = build_idempotency_key(
        candidate_profile_id=profile_id, channel=ApplicationChannel.BROWSER,
        opportunity_id=OPPORTUNITY, company_id=None)
    instant = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    application = Application(
        id=application_id(key), user_id=owner_id, candidate_profile_id=profile_id,
        decision_id=new_application_decision_id(), channel=ApplicationChannel.BROWSER,
        state=state, idempotency_key=key, opportunity_id=OPPORTUNITY, company_id=None,
        created_at=instant, updated_at=instant)
    await api.applications.upsert(application)
    return application


async def _record(api, app_id, *, kind, occurred_at=OCCURRED, **extra):
    """POST one milestone; the response is returned unasserted for a status check."""
    body = {"kind": kind, "occurred_at": occurred_at, **extra}
    return await api.write("POST", f"/applications/{app_id}/outcomes", json=body)


# --- recording: shape, idempotency, and the phase's central separation -----

async def test_recording_a_milestone_is_201_and_owner_free(tmp_path):
    """A record is a 201: the outcome comes back effective, and no `user_id` is surfaced."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        app = await _seed_application(api)
        response = await _record(api, app.id, kind="INTERVIEW")
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["kind"] == "INTERVIEW"
        assert body["status"] == "EFFECTIVE"
        assert body["is_effective"] is True
        assert body["application_id"] == str(app.id)
        assert "user_id" not in body

async def test_recording_a_rejection_never_touches_the_application_state(tmp_path):
    """THE acceptance test at the wire: a recruiter's "no" is an outcome, not an execution failure.

    Recorded over HTTP against a `SUBMITTED` application, a `REJECTED` outcome comes back terminal
    while the application — read back from the store the route wrote through — is still
    `SUBMITTED`, never `FAILED`. The route has no path that could transition it (§2, §84).
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        app = await _seed_application(api, state=ApplicationState.SUBMITTED)
        response = await _record(api, app.id, kind="REJECTED")
        assert response.status_code == 201, response.text
        assert response.json()["is_terminal"] is True
        stored = await api.applications.get(next(iter(api.users.users)), app.id)
        assert stored is not None
        assert stored.state is ApplicationState.SUBMITTED  # observed, never driven

async def test_recording_the_same_milestone_twice_collapses_onto_one_row(tmp_path):
    """A double-clicked "mark as acknowledged" is idempotent by the derived id — one row."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        app = await _seed_application(api)
        first = await _record(api, app.id, kind="ACKNOWLEDGED")
        second = await _record(api, app.id, kind="ACKNOWLEDGED", occurred_at=LATER_OCCURRED)
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] == second.json()["id"]

async def test_recording_a_naive_occurred_at_is_422(tmp_path):
    """A timezone-naive instant is refused at the boundary — a 422, never a domain 500."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        app = await _seed_application(api)
        response = await _record(api, app.id, kind="INTERVIEW",
                                 occurred_at="2026-03-15T10:00:00")
        assert response.status_code == 422

async def test_recording_against_a_foreign_application_is_404(tmp_path):
    """An application owned by another account reads as absent — never « forbidden »."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()  # account one
        owner_id = next(iter(api.users.users))
        app = await _seed_application(api, owner=owner_id)
        await api.register(email="someone.else@example.com")  # switches to a second account
        response = await _record(api, app.id, kind="INTERVIEW")
        assert response.status_code == 404
        assert response.json()["error"] == "application_not_found"


# --- the timeline: every status, oldest first -------------------------------

async def test_the_timeline_keeps_corrections_oldest_first(tmp_path):
    """The timeline returns every status, so a correction and its superseded predecessor coexist."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        app = await _seed_application(api)
        recorded = await _record(api, app.id, kind="INTERVIEW")
        outcome_id = recorded.json()["id"]
        correction = await api.write(
            "POST", f"/outcomes/{outcome_id}/correct",
            json={"kind": "INTERVIEW", "occurred_at": LATER_OCCURRED})
        assert correction.status_code == 201, correction.text
        assert correction.json()["supersedes_id"] == outcome_id
        assert correction.json()["is_correction"] is True
        timeline = await api.read(f"/applications/{app.id}/outcomes")
        assert timeline.status_code == 200
        outcomes = timeline.json()["outcomes"]
        assert len(outcomes) == 2
        assert [o["occurred_at"] for o in outcomes] == sorted(o["occurred_at"] for o in outcomes)
        assert {o["status"] for o in outcomes} == {"SUPERSEDED", "EFFECTIVE"}

async def test_the_timeline_of_a_missing_application_is_404(tmp_path):
    """A missing application raises rather than returning an empty list a caller can't read.

    Missing and foreign read the same — absent — so a signed-in account querying a posting it
    never applied to gets a 404, not an empty timeline it could mistake for "no outcomes yet".
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        owner_id = next(iter(api.users.users))
        phantom = application_id(build_idempotency_key(
            candidate_profile_id=default_candidate_profile_id(owner_id),
            channel=ApplicationChannel.BROWSER, opportunity_id=OPPORTUNITY, company_id=None))
        response = await api.read(f"/applications/{phantom}/outcomes")
        assert response.status_code == 404
        assert response.json()["error"] == "application_not_found"


# --- correcting and retracting ----------------------------------------------

async def test_correcting_a_missing_outcome_is_404(tmp_path):
    """An outcome id that resolves to nothing is a 404, not a write."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        ghost = "00000000-0000-4000-8000-0000000000ff"
        response = await api.write("POST", f"/outcomes/{ghost}/correct",
                                   json={"kind": "INTERVIEW", "occurred_at": LATER_OCCURRED})
        assert response.status_code == 404
        assert response.json()["error"] == "outcome_not_found"

async def test_retracting_flips_the_status_then_a_second_retract_is_409(tmp_path):
    """A retract flips the row to `RETRACTED`; retracting a non-effective outcome is a 409."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        app = await _seed_application(api)
        outcome_id = (await _record(api, app.id, kind="OFFER_RECEIVED")).json()["id"]
        first = await api.write("POST", f"/outcomes/{outcome_id}/retract")
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "RETRACTED"
        again = await api.write("POST", f"/outcomes/{outcome_id}/retract")
        assert again.status_code == 409
        assert again.json()["error"] == "outcome_not_effective"

