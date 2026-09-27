# tests/test_v2_api_career.py
"""`/api/v2/career` — le rapport d'entonnoir et les suggestions étayées, vus par un navigateur.

Les tests de service (`test_v2_career_analytics.py`, `test_v2_career_recommendations.py`)
tranchent l'arithmétique et la dérivation ; celui-ci tient la couche route et son câblage. Deux
règles de la phase se lisent au fil : l'analytique est une *lecture* pure, versionnée et
auto-descriptive, qui ne renvoie jamais de probabilité d'embauche ; et générer des
recommandations n'*ajoute* que des observations à un magasin write-once, sans aucune autorité pour
changer une recherche ou une politique — cela reste l'étape « proposition de stratégie » que
l'utilisateur approuve explicitement.

Le propriétaire n'est jamais dans le chemin ni dans le corps : chaque chiffre est le compte résolu
depuis la session, et aucun `user_id` ne ressort. Tout tourne sur les fakes : pas de socket, pas
de base.
"""
from datetime import UTC, datetime, timedelta

import pytest

from backend.app.domain.analytics import CAREER_ANALYTICS_VERSION
from backend.app.domain.application import (
    Application,
    ApplicationState,
    build_idempotency_key,
)
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.identifiers import (
    application_id,
    default_candidate_profile_id,
    discovered_opportunity_id,
    new_application_decision_id,
)
from backend.app.domain.opportunity import OpportunityType
from backend.app.domain.outcome import OutcomeKind
from tests.v2_api import api_harness
from tests.v2_builders import a_source_record, an_application_outcome, an_opportunity

pytestmark = pytest.mark.asyncio

# The harness clock sits at 2026-04-01; `OLD` is well past the 30-day maturity horizon, so a
# seeded application is mature (its silence is a real "no response", not a censored fresh send).
OLD = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
DAY = timedelta(days=1)


async def _seed_slice(api, *, user_id, prefix, title, count, responses,
                      source_key="test_board",
                      opportunity_type=OpportunityType.FULL_TIME):
    """Seed `count` mature applications on distinct postings, `responses` of them acknowledged.

    Each posting is its own opportunity so the applications never collide on the idempotency key;
    the shared `title`/`source_key`/`opportunity_type` place the whole slice in one role family,
    source and type, so a test can vary exactly one axis.
    """
    profile_id = default_candidate_profile_id(user_id)
    for index in range(count):
        external_id = f"{prefix}-{index}"
        oid = discovered_opportunity_id(source_key, external_id)
        await api.postings.upsert(an_opportunity(
            id=oid, title=title, opportunity_type=opportunity_type,
            source=a_source_record(source_key=source_key, external_id=external_id)))
        key = build_idempotency_key(
            candidate_profile_id=profile_id, channel=ApplicationChannel.BROWSER,
            opportunity_id=oid, company_id=None)
        app = await api.applications.upsert(Application(
            id=application_id(key), user_id=user_id, candidate_profile_id=profile_id,
            decision_id=new_application_decision_id(), channel=ApplicationChannel.BROWSER,
            state=ApplicationState.SUBMITTED, idempotency_key=key, opportunity_id=oid,
            company_id=None, created_at=OLD, updated_at=OLD))
        if index < responses:
            await api.career_outcomes.upsert(an_application_outcome(
                application_id=app.id, kind=OutcomeKind.ACKNOWLEDGED,
                occurred_at=OLD + DAY, user_id=user_id, recorded_at=OLD + DAY))


# --- analytics: an honest empty report, then a measured one -----------------

async def test_analytics_of_an_empty_account_is_an_honest_zero(tmp_path):
    """A brand-new account is an empty window and null rates — versioned, stamped, owner-free."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        response = await api.read("/career/analytics")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["analytics_version"] == CAREER_ANALYTICS_VERSION
        assert body["window"]["is_empty"] is True
        assert all(rate["rate"] is None and rate["denominator"] == 0
                   for rate in body["rates"])
        assert "user_id" not in body
        # The measure layer never forecasts hiring — no probability leaks into the report.
        assert "probability" not in body
        assert "hiring_probability" not in body

async def test_analytics_measures_the_response_rate_over_matured_applications(tmp_path):
    """A seeded funnel is counted: one acknowledged of two mature is a 0.5 response rate."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = next(iter(api.users.users))
        await _seed_slice(api, user_id=user_id, prefix="data", title="Data Scientist",
                          count=2, responses=1)
        response = await api.read("/career/analytics")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["window"]["is_empty"] is False
        rate = next(r for r in body["rates"] if r["kind"] == "RESPONSE")
        assert rate["numerator"] == 1
        assert rate["denominator"] == 2
        assert rate["rate"] == 0.5

async def test_analytics_never_counts_another_accounts_applications(tmp_path):
    """The population is read owner-scoped, so a foreign funnel is invisible in this report."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        first = next(iter(api.users.users))
        await _seed_slice(api, user_id=first, prefix="data", title="Data Scientist",
                          count=3, responses=2)
        await api.register(email="someone.else@example.com")  # switches to a second account
        response = await api.read("/career/analytics")
        assert response.status_code == 200
        assert response.json()["window"]["is_empty"] is True  # sees none of the first's funnel

async def test_analytics_rejects_an_out_of_range_horizon(tmp_path):
    """`horizon_days` is bounded [1, 365]: a zero or an absurd window is a 422, not a silent clamp."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        assert (await api.read("/career/analytics?horizon_days=0")).status_code == 422
        assert (await api.read("/career/analytics?horizon_days=400")).status_code == 422
        assert (await api.read("/career/analytics?horizon_days=30")).status_code == 200


# --- recommendations: an evidence-backed suggestion, never a lever ----------

async def test_recommendations_start_empty_then_a_generate_adds_a_snapshot(tmp_path):
    """The GET is empty on a new account; a POST computes, derives, persists and returns a set."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = next(iter(api.users.users))
        assert (await api.read("/career/recommendations")).json()["recommendations"] == []
        # Data answers 4/5, engineering 1/5 — one source and type, so only role varies: a pair.
        await _seed_slice(api, user_id=user_id, prefix="data", title="Data Scientist",
                          count=5, responses=4)
        await _seed_slice(api, user_id=user_id, prefix="swe",
                          title="Senior Software Engineer", count=5, responses=1)
        generated = await api.write("POST", "/career/recommendations")
        assert generated.status_code == 201, generated.text
        recs = generated.json()["recommendations"]
        assert [r["kind"] for r in recs] == [
            "PRIORITIZE_ROLE_FAMILY", "DEPRIORITIZE_ROLE_FAMILY"]
        prioritize = recs[0]
        assert prioritize["analytics_version"] == CAREER_ANALYTICS_VERSION
        assert "user_id" not in prioritize
        # Every recommendation cites its evidence, and carries nothing it could execute.
        assert len(prioritize["evidence"]) >= 1
        assert prioritize["evidence"][0]["dimension_key"] == "DATA_AND_ANALYTICS"
        assert "target_profile" not in prioritize
        assert "change" not in prioritize
        # The write-once store now reads the persisted snapshot back.
        listed = await api.read("/career/recommendations")
        assert len(listed.json()["recommendations"]) == 2

async def test_generating_on_thin_evidence_persists_nothing(tmp_path):
    """Below the sample minimum there is no comparison to make — a 201 carrying an empty set."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = next(iter(api.users.users))
        # A perfect but thin slice: 3 applications is not a rate, so no suggestion rests on it.
        await _seed_slice(api, user_id=user_id, prefix="data", title="Data Scientist",
                          count=3, responses=3)
        generated = await api.write("POST", "/career/recommendations")
        assert generated.status_code == 201, generated.text
        assert generated.json()["recommendations"] == []
