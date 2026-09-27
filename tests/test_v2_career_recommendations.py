# tests/test_v2_career_recommendations.py
"""`CareerRecommendationEngine` : suggérer avec des preuves, sans jamais rien pouvoir exécuter (§26-33).

C'est le maillon « recommander » de l'échine. Le moteur est *déterministe* et lit un rapport
`CareerAnalytics` typé — jamais des lignes brutes — puis compare les tranches de l'entonnoir :
quand une famille de rôle, une source ou un type de poste répond nettement mieux (ou moins bien)
qu'une autre sur une base réelle de candidatures, il émet une `CareerRecommendation` dont les
preuves *sont* les taux comparés. Quatre disciplines portent ce module :

- **preuve ou rien, jamais sur des données maigres (§62)** : une tranche n'est citée que si sa
  base franchit `MIN_RECOMMENDATION_SAMPLE_SIZE`, et une comparaison exige deux telles tranches
  séparées d'au moins `NOTABLE_RATE_GAP` — le bruit ne devient jamais une suggestion ;
- **auto-descriptif, versionné (§14, §24)** : chaque recommandation porte l'`analytics_version`
  du rapport dont elle est tirée, et sa confiance découle du plus faible échantillon cité ;
- **cœur déterministe, formulation optionnelle (§33)** : sans narrateur, la prose est un gabarit
  (aucun `generator_key`) ; un narrateur ne peut que reformuler, sous la garde `asserts_causation` ;
- **la frontière** : le rapport est lu par propriétaire, si bien que l'entonnoir d'autrui est
  invisible et n'engendre aucune recommandation.

Tout tourne sur `tests/v2_fakes.py` et le vrai `CareerAnalyticsService` : pas d'horloge, pas de
socket, pas de base.
"""
from datetime import UTC, datetime, timedelta

import pytest

from backend.app.career.analytics import CareerAnalyticsService
from backend.app.career.recommendations import (
    CareerRecommendationEngine,
    NarratedProse,
    RecommendationDraft,
    RecommendationNarrator,
)
from backend.app.domain.analytics import DimensionKind
from backend.app.domain.application import (
    Application,
    ApplicationState,
    build_idempotency_key,
)
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.identifiers import (
    application_id,
    discovered_opportunity_id,
    new_application_decision_id,
)
from backend.app.domain.opportunity import OpportunityType
from backend.app.domain.outcome import OutcomeKind
from backend.app.domain.recommendation import (
    RecommendationConfidence,
    RecommendationKind,
)
from tests.v2_builders import (
    RUN,
    CAREER_ANALYTICS_VERSION,
    OTHER_USER,
    PROFILE,
    USER,
    a_source_record,
    an_application_outcome,
    an_opportunity,
)
from tests.v2_fakes import (
    FakeApplicationOutcomeRepository,
    FakeApplicationRepository,
    FakeCareerRecommendationRepository,
    FakeOpportunityRepository,
    FakeRoleClassificationRepository,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
OLD = NOW - timedelta(days=60)      # mature by the 30-day horizon → counts in the base


def build_engine(*, narrator: RecommendationNarrator | None = None):
    outcomes = FakeApplicationOutcomeRepository()
    applications = FakeApplicationRepository()
    roles = FakeRoleClassificationRepository()
    opportunities = FakeOpportunityRepository()
    recommendations = FakeCareerRecommendationRepository()
    analytics = CareerAnalyticsService(outcomes, applications, roles, opportunities)
    engine = CareerRecommendationEngine(analytics, recommendations, narrator=narrator)
    return engine, outcomes, applications, opportunities, recommendations


def _application(*, opportunity_id, user_id=USER, created_at=OLD) -> Application:
    """One submitted application keyed the way the engine keys it, applied at `created_at`."""
    key = build_idempotency_key(
        candidate_profile_id=PROFILE, channel=ApplicationChannel.BROWSER,
        opportunity_id=opportunity_id, company_id=None)
    return Application(
        id=application_id(key), user_id=user_id, candidate_profile_id=PROFILE,
        decision_id=new_application_decision_id(), channel=ApplicationChannel.BROWSER,
        state=ApplicationState.SUBMITTED, idempotency_key=key,
        opportunity_id=opportunity_id, created_at=created_at, updated_at=created_at)


async def _seed_slice(applications, outcomes, opportunities, *, prefix: str, title: str,
                      count: int, responses: int, user_id=USER,
                      opportunity_type: OpportunityType = OpportunityType.FULL_TIME,
                      source_key: str = "test_board") -> None:
    """Seed `count` mature applications on distinct postings, `responses` of them acknowledged.

    Each posting is its own opportunity so the applications never collide on the idempotency
    key; `title`/`opportunity_type`/`source_key` place the whole slice in one role family, one
    type and one source, so a test can vary exactly one axis at a time.
    """
    for index in range(count):
        external_id = f"{prefix}-{index}"
        oid = discovered_opportunity_id(source_key, external_id)
        await opportunities.upsert(an_opportunity(
            id=oid, title=title, opportunity_type=opportunity_type,
            source=a_source_record(source_key=source_key, external_id=external_id)))
        app = await applications.upsert(
            _application(opportunity_id=oid, user_id=user_id))
        if index < responses:
            await outcomes.upsert(an_application_outcome(
                application_id=app.id, kind=OutcomeKind.ACKNOWLEDGED,
                occurred_at=OLD + DAY, user_id=user_id, recorded_at=OLD + DAY))


# --- the deterministic derivation: a pair per dimension, its evidence the compared rates ---


async def test_role_family_yields_a_prioritize_deprioritize_pair_citing_the_compared_rates(
) -> None:
    """The best and worst role family earn a pair whose evidence *is* the two rates (§26-31)."""
    engine, outcomes, applications, opportunities, _ = build_engine()
    # Data converts 4/5, engineering 1/5 — one source, one type, so only role varies.
    await _seed_slice(applications, outcomes, opportunities,
                      prefix="data", title="Data Scientist", count=5, responses=4)
    await _seed_slice(applications, outcomes, opportunities,
                      prefix="swe", title="Senior Software Engineer", count=5, responses=1)

    recs = await engine.recommend(USER, now=NOW)

    assert [r.kind for r in recs] == [
        RecommendationKind.PRIORITIZE_ROLE_FAMILY,
        RecommendationKind.DEPRIORITIZE_ROLE_FAMILY]
    prioritize = recs[0]
    assert prioritize.analytics_version == CAREER_ANALYTICS_VERSION
    assert prioritize.generator_key is None and prioritize.llm_run_id is None
    assert prioritize.confidence is RecommendationConfidence.LOW    # weakest sample is 5
    # The evidence leads with the focus slice, then the foil it is measured against.
    focus, foil = prioritize.evidence
    assert (focus.ordinal, foil.ordinal) == (0, 1)
    assert focus.dimension is DimensionKind.ROLE_FAMILY
    assert focus.dimension_key == "DATA_AND_ANALYTICS"
    assert (focus.numerator, focus.denominator, focus.sample_size) == (4, 5, 5)
    assert foil.dimension_key == "SOFTWARE_ENGINEERING"
    assert (foil.numerator, foil.denominator) == (1, 5)
    # The deprioritize half is the same comparison, focused on the weaker slice.
    assert recs[1].evidence[0].dimension_key == "SOFTWARE_ENGINEERING"
    # Persisted in the write-once store, retrievable for review.
    assert len(await engine.latest(USER)) == 2


# --- evidence or nothing: the two gates that keep noise from becoming a suggestion --------


async def test_a_slice_below_the_minimum_sample_earns_no_recommendation() -> None:
    """A slice of 3 applications is not a rate, so no comparison rests on it (§62)."""
    engine, outcomes, applications, opportunities, _ = build_engine()
    # Data is a perfect 3/3 but too thin to cite; only one slice clears the minimum, so the
    # comparison never has the two qualifying slices it needs.
    await _seed_slice(applications, outcomes, opportunities,
                      prefix="data", title="Data Scientist", count=3, responses=3)
    await _seed_slice(applications, outcomes, opportunities,
                      prefix="swe", title="Senior Software Engineer", count=5, responses=1)

    assert await engine.recommend(USER, now=NOW) == ()


async def test_a_gap_below_the_notable_threshold_stays_silent() -> None:
    """Two slices a hair apart are within the noise, so the engine says nothing (§62)."""
    engine, outcomes, applications, opportunities, _ = build_engine()
    # 6/12 vs 5/12 — both clear the sample minimum, but the ~8-point gap is under NOTABLE_RATE_GAP.
    await _seed_slice(applications, outcomes, opportunities,
                      prefix="data", title="Data Scientist", count=12, responses=6)
    await _seed_slice(applications, outcomes, opportunities,
                      prefix="swe", title="Senior Software Engineer", count=12, responses=5)

    assert await engine.recommend(USER, now=NOW) == ()


# --- the other two axes: a source pair and a directionless type-mix review ----------------


async def test_source_dimension_yields_a_prioritize_deprioritize_pair() -> None:
    """When one board answers far better than another, the source earns its own pair (§26-31)."""
    engine, outcomes, applications, opportunities, _ = build_engine()
    # One title, one type: only the source varies, so neither role nor type fires.
    await _seed_slice(applications, outcomes, opportunities, source_key="linkedin",
                      prefix="li", title="Data Scientist", count=5, responses=4)
    await _seed_slice(applications, outcomes, opportunities, source_key="indeed",
                      prefix="in", title="Data Scientist", count=5, responses=1)

    recs = await engine.recommend(USER, now=NOW)

    assert [r.kind for r in recs] == [
        RecommendationKind.PRIORITIZE_SOURCE, RecommendationKind.DEPRIORITIZE_SOURCE]
    assert recs[0].evidence[0].dimension is DimensionKind.SOURCE
    assert recs[0].evidence[0].dimension_key == "linkedin"
    assert recs[1].evidence[0].dimension_key == "indeed"


async def test_opportunity_type_mix_yields_a_single_directionless_review() -> None:
    """Types that answer unevenly are flagged for review, without prescribing a direction (§26-31)."""
    engine, outcomes, applications, opportunities, _ = build_engine()
    # One title, one source: only the type varies.
    await _seed_slice(applications, outcomes, opportunities,
                      opportunity_type=OpportunityType.FULL_TIME,
                      prefix="ft", title="Data Scientist", count=5, responses=4)
    await _seed_slice(applications, outcomes, opportunities,
                      opportunity_type=OpportunityType.INTERNSHIP,
                      prefix="in", title="Data Scientist", count=5, responses=1)

    recs = await engine.recommend(USER, now=NOW)

    assert [r.kind for r in recs] == [RecommendationKind.REVIEW_OPPORTUNITY_TYPE_MIX]
    review = recs[0]
    assert review.detail is None
    assert {e.dimension_key for e in review.evidence} == {"FULL_TIME", "INTERNSHIP"}
    assert all(e.dimension is DimensionKind.OPPORTUNITY_TYPE for e in review.evidence)


# --- the honest empty account and the ownership boundary ----------------------------------


async def test_the_empty_account_yields_no_recommendations() -> None:
    """A brand-new account has no funnel to compare, so it earns an empty, honest set."""
    engine, *_ = build_engine()

    assert await engine.recommend(USER, now=NOW) == ()


async def test_one_accounts_recommendations_never_read_anothers_funnel() -> None:
    """The report is read owner-scoped, so a foreign funnel raises no suggestion (§18-20)."""
    engine, outcomes, applications, opportunities, _ = build_engine()
    await _seed_slice(applications, outcomes, opportunities, user_id=OTHER_USER,
                      prefix="data", title="Data Scientist", count=5, responses=4)
    await _seed_slice(applications, outcomes, opportunities, user_id=OTHER_USER,
                      prefix="swe", title="Senior Software Engineer", count=5, responses=1)

    mine = await engine.recommend(USER, now=NOW)
    theirs = await engine.recommend(OTHER_USER, now=NOW)

    assert mine == ()
    assert len(theirs) == 2
    assert {r.user_id for r in theirs} == {OTHER_USER}


# --- the guarded narrator seam: optional wording, never authority over evidence -----------


class _KeepingNarrator(RecommendationNarrator):
    """A narrator whose non-causal French rewrite the guard accepts, stamped with a run (§33)."""

    async def phrase(self, *, user_id, draft: RecommendationDraft) -> NarratedProse:
        return NarratedProse(
            summary="Vos candidatures Data & Analytics reçoivent davantage de réponses.",
            llm_run_id=RUN)


class _CausalNarrator(RecommendationNarrator):
    """A narrator that slips into cause and effect — the wording guard must drop it (§62)."""

    async def phrase(self, *, user_id, draft: RecommendationDraft) -> NarratedProse:
        return NarratedProse(summary="Applying to data roles guarantees more interviews.")


class _RaisingNarrator(RecommendationNarrator):
    """A narrator that fails outright — never fatal to a deterministic recommendation (§33)."""

    async def phrase(self, *, user_id, draft: RecommendationDraft) -> NarratedProse:
        raise RuntimeError("provider unavailable")


async def _seed_role_gap(applications, outcomes, opportunities) -> None:
    """Data 4/5 against engineering 1/5 — the notable role split the pair tests share."""
    await _seed_slice(applications, outcomes, opportunities,
                      prefix="data", title="Data Scientist", count=5, responses=4)
    await _seed_slice(applications, outcomes, opportunities,
                      prefix="swe", title="Senior Software Engineer", count=5, responses=1)


async def test_a_narrators_clean_rewrite_replaces_the_prose_and_records_its_provenance(
) -> None:
    """Guard-clean wording is kept, stamped with the narrator's key and run — evidence untouched."""
    engine, outcomes, applications, opportunities, _ = build_engine(narrator=_KeepingNarrator())
    await _seed_role_gap(applications, outcomes, opportunities)

    top = (await engine.recommend(USER, now=NOW))[0]

    assert top.summary == "Vos candidatures Data & Analytics reçoivent davantage de réponses."
    assert top.generator_key == "llm-worded/1"
    assert top.llm_run_id == RUN
    assert top.evidence[0].dimension_key == "DATA_AND_ANALYTICS"    # the engine's own arithmetic


async def test_a_causal_rewrite_is_dropped_for_the_deterministic_sentence() -> None:
    """A causal claim never survives the guard; the deterministic prose stands, provenance-free."""
    engine, outcomes, applications, opportunities, _ = build_engine(narrator=_CausalNarrator())
    await _seed_role_gap(applications, outcomes, opportunities)

    top = (await engine.recommend(USER, now=NOW))[0]

    assert top.generator_key is None and top.llm_run_id is None
    assert top.summary.startswith("À privilégier")


async def test_a_narrator_that_raises_falls_back_to_the_deterministic_prose() -> None:
    """A provider failure is swallowed: the recommendation keeps its deterministic wording."""
    engine, outcomes, applications, opportunities, _ = build_engine(narrator=_RaisingNarrator())
    await _seed_role_gap(applications, outcomes, opportunities)

    top = (await engine.recommend(USER, now=NOW))[0]

    assert top.generator_key is None and top.llm_run_id is None
    assert top.summary.startswith("À privilégier")
