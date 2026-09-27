# tests/test_v2_career_analytics.py
"""`CareerAnalyticsService` : le maillon « mesurer », de l'arithmétique déterministe, jamais un LLM.

Ce module lit les candidatures d'un compte, ses `ApplicationOutcome` *effectifs*, ses classements
de rôle et les postings qu'ils nomment, puis les replie en un unique `CareerAnalytics`
auto-descriptif et versionné (§13-25). Quatre disciplines portent les tests :

- **compter des candidatures, pas des événements (§13)** : trois tours d'entretien sur une
  candidature la placent une seule fois au stade `INTERVIEW` ;
- **observer sans jamais piloter (§2, §84)** : le service *lit* l'`ApplicationState` mais n'en
  écrit aucun — un `REJECTED` n'est pas un barreau de l'entonnoir et ne fait pas basculer une
  candidature `SUBMITTED` en `FAILED` ;
- **le silence est censuré, jamais un échec (§15-16)** : une candidature trop fraîche est retirée
  du dénominateur, pas comptée comme un refus, et la coupe est reportée dans `MaturityCensoring` ;
- **auto-descriptif, versionné (§14, §24-25)** : un rôle, une source ou un type inconnu est une
  cellule à clé `None`, jamais un fourre-tout.

Tout tourne sur `tests/v2_fakes.py` : pas d'horloge, pas de socket, pas de base.
"""
from datetime import UTC, datetime, timedelta

import pytest

from backend.app.career.analytics import CareerAnalyticsService
from backend.app.domain.analytics import (
    CAREER_ANALYTICS_VERSION,
    DimensionKind,
    FunnelStage,
    RateKind,
    TimingKind,
)
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
    new_company_id,
)
from backend.app.domain.opportunity import OpportunityType
from backend.app.domain.outcome import OutcomeKind, OutcomeStatus
from backend.app.domain.role import RoleFamily, RoleFamilyProvenance
from tests.v2_builders import (
    OPPORTUNITY,
    OTHER_USER,
    PROFILE,
    USER,
    a_role_classification,
    a_source_record,
    an_application_outcome,
    an_opportunity,
)
from tests.v2_fakes import (
    FakeApplicationOutcomeRepository,
    FakeApplicationRepository,
    FakeOpportunityRepository,
    FakeRoleClassificationRepository,
)

pytestmark = pytest.mark.asyncio

# A `now` fixed well after the horizon so "old" applications are mature and "fresh" ones censored;
# the two anchors keep the maturity split in the tests explicit rather than relative to wall-clock.
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
OLD = NOW - timedelta(days=60)      # observed past the 30-day horizon → mature
FRESH = NOW - timedelta(days=2)     # too fresh to tell → censored, never a refus


def _oid(n: int):
    """A stable, distinct opportunity id, so applications on different postings never collide."""
    return discovered_opportunity_id("test_board", f"posting-{n}")


def build_service() -> tuple[CareerAnalyticsService, FakeApplicationOutcomeRepository,
                             FakeApplicationRepository, FakeRoleClassificationRepository,
                             FakeOpportunityRepository]:
    outcomes = FakeApplicationOutcomeRepository()
    applications = FakeApplicationRepository()
    roles = FakeRoleClassificationRepository()
    opportunities = FakeOpportunityRepository()
    service = CareerAnalyticsService(outcomes, applications, roles, opportunities)
    return service, outcomes, applications, roles, opportunities


def an_application(*, opportunity_id=OPPORTUNITY, company_id=None, user_id=USER,
                   state: ApplicationState = ApplicationState.SUBMITTED,
                   created_at: datetime = OLD) -> Application:
    """A Phase 12 application keyed the way the engine keys it, in `state`, applied at `created_at`.

    No dedicated builder exists, so — like the outcomes test — one is composed here. The id is
    derived from the idempotency key exactly as production does, so the id the outcome fixtures
    reference is the one this seeds; `created_at` is the applied-at anchor the funnel measures from.
    A spontaneous application passes `company_id=` and `opportunity_id=None`.
    """
    key = build_idempotency_key(
        candidate_profile_id=PROFILE, channel=ApplicationChannel.BROWSER,
        opportunity_id=opportunity_id, company_id=company_id)
    return Application(
        id=application_id(key), user_id=user_id, candidate_profile_id=PROFILE,
        decision_id=new_application_decision_id(), channel=ApplicationChannel.BROWSER,
        state=state, idempotency_key=key, opportunity_id=opportunity_id,
        company_id=company_id, created_at=created_at, updated_at=created_at)


async def _seed_application(applications: FakeApplicationRepository, **kwargs) -> Application:
    return await applications.upsert(an_application(**kwargs))


async def _seed_outcome(outcomes: FakeApplicationOutcomeRepository, app: Application,
                        kind: OutcomeKind, occurred_at: datetime, **overrides):
    """Record one outcome against `app`, owned by the same account, recorded when it occurred."""
    return await outcomes.upsert(an_application_outcome(
        application_id=app.id, kind=kind, occurred_at=occurred_at,
        user_id=app.user_id, recorded_at=occurred_at, **overrides))


async def _seed_opportunity(opportunities: FakeOpportunityRepository, *, id, title: str,
                            source_key: str = "test_board", external_id: str = "posting",
                            opportunity_type: OpportunityType = OpportunityType.FULL_TIME):
    return await opportunities.upsert(an_opportunity(
        id=id, title=title, opportunity_type=opportunity_type,
        source=a_source_record(source_key=source_key, external_id=external_id)))


# --- the funnel: applications, not events -----------------------------------------------


async def test_funnel_counts_one_application_across_repeated_interview_rounds() -> None:
    """Three interview rounds on one candidature advance it to INTERVIEW exactly once (§13)."""
    service, outcomes, applications, _, _ = build_service()
    app = await _seed_application(applications, created_at=OLD)
    await _seed_outcome(outcomes, app, OutcomeKind.INTERVIEW, OLD + DAY)
    await _seed_outcome(outcomes, app, OutcomeKind.INTERVIEW, OLD + 3 * DAY)
    await _seed_outcome(outcomes, app, OutcomeKind.INTERVIEW, OLD + 8 * DAY)

    funnel = (await service.report(USER, now=NOW)).funnel

    # Cumulative: reaching INTERVIEW implies every shallower stage, and it is one application.
    assert funnel.count_at(FunnelStage.SUBMITTED) == 1
    assert funnel.count_at(FunnelStage.ACKNOWLEDGED) == 1
    assert funnel.count_at(FunnelStage.INTERVIEW) == 1
    assert funnel.count_at(FunnelStage.OFFER) == 0
    assert len(outcomes.outcomes) == 3      # three rows, one counted application


async def test_only_effective_outcomes_advance_the_funnel() -> None:
    """A retracted round is not believed, so it never places the application on its rung (§48)."""
    service, outcomes, applications, _, _ = build_service()
    app = await _seed_application(applications, created_at=OLD)
    await _seed_outcome(outcomes, app, OutcomeKind.INTERVIEW, OLD + 2 * DAY,
                        status=OutcomeStatus.RETRACTED)
    await _seed_outcome(outcomes, app, OutcomeKind.ACKNOWLEDGED, OLD + DAY)

    funnel = (await service.report(USER, now=NOW)).funnel

    assert funnel.count_at(FunnelStage.ACKNOWLEDGED) == 1
    assert funnel.count_at(FunnelStage.INTERVIEW) == 0


async def test_a_failed_execution_with_no_outcome_is_not_in_the_funnel() -> None:
    """A submission that failed never reached the employer, so it is not a hiring fact (§2)."""
    service, outcomes, applications, _, _ = build_service()
    await _seed_application(applications, opportunity_id=_oid(1),
                            state=ApplicationState.SUBMITTED, created_at=OLD)
    await _seed_application(applications, opportunity_id=_oid(2),
                            state=ApplicationState.FAILED, created_at=OLD)
    answered = await _seed_application(applications, opportunity_id=_oid(3),
                                       state=ApplicationState.FAILED, created_at=OLD)
    await _seed_outcome(outcomes, answered, OutcomeKind.ACKNOWLEDGED, OLD + DAY)

    funnel = (await service.report(USER, now=NOW)).funnel

    # The bare FAILED application is excluded; the one carrying an employer signal is kept.
    assert funnel.submitted == 2
    assert funnel.count_at(FunnelStage.ACKNOWLEDGED) == 1


# --- conversion rates: maturity gates the denominator -----------------------------------


async def test_response_rate_censors_the_fresh_and_counts_the_mature_silent() -> None:
    """A fresh application is withheld; a mature silent one is a real "no response" (§15-16)."""
    service, outcomes, applications, _, _ = build_service()
    # Mature, silent: old enough that its silence means something → counts against the base.
    await _seed_application(applications, opportunity_id=_oid(1), created_at=OLD)
    # Fresh, silent: not yet given time to answer → censored out of the denominator.
    await _seed_application(applications, opportunity_id=_oid(2), created_at=FRESH)
    # Mature, answered: an acknowledgement → the one success.
    answered = await _seed_application(applications, opportunity_id=_oid(3), created_at=OLD)
    await _seed_outcome(outcomes, answered, OutcomeKind.ACKNOWLEDGED, OLD + DAY)

    report = await service.report(USER, now=NOW)
    response = report.rate_for(RateKind.RESPONSE)

    assert response is not None
    assert response.numerator == 1        # only the answered one
    assert response.denominator == 2      # mature-silent + answered; the fresh one is censored
    assert response.rate == 0.5
    assert report.funnel.censoring.mature_count == 2
    assert report.funnel.censoring.censored_count == 1


async def test_offer_conversion_measures_offers_over_interviews_not_over_submissions() -> None:
    """The base is INTERVIEW, so a strong closer with a thin top of funnel is not punished (§23)."""
    service, outcomes, applications, _, _ = build_service()
    interviewed = await _seed_application(applications, opportunity_id=_oid(1), created_at=OLD)
    await _seed_outcome(outcomes, interviewed, OutcomeKind.INTERVIEW, OLD + DAY)
    offered = await _seed_application(applications, opportunity_id=_oid(2), created_at=OLD)
    await _seed_outcome(outcomes, offered, OutcomeKind.INTERVIEW, OLD + DAY)
    await _seed_outcome(outcomes, offered, OutcomeKind.OFFER_RECEIVED, OLD + 5 * DAY)

    offer_conversion = (await service.report(USER, now=NOW)).rate_for(RateKind.OFFER_CONVERSION)

    assert offer_conversion is not None
    assert offer_conversion.numerator == 1        # the offered application
    assert offer_conversion.denominator == 2      # both reached the INTERVIEW base


# --- the separation: observe, never drive -----------------------------------------------


async def test_a_rejection_is_no_funnel_rung_and_never_touches_the_application_state() -> None:
    """THE acceptance test at the measure layer: a recruiter's "no" is observed, not executed (§2, §84).

    The rejection concludes the process (so the application is mature) but does not advance
    progress — the funnel still reads the interview it reached — and the service, holding the
    application store only to read it, leaves the `SUBMITTED` state exactly as it found it.
    """
    service, outcomes, applications, _, _ = build_service()
    app = await _seed_application(applications, state=ApplicationState.SUBMITTED, created_at=OLD)
    await _seed_outcome(outcomes, app, OutcomeKind.INTERVIEW, OLD + 2 * DAY)
    await _seed_outcome(outcomes, app, OutcomeKind.REJECTED, OLD + 10 * DAY)

    report = await service.report(USER, now=NOW)

    assert report.funnel.count_at(FunnelStage.INTERVIEW) == 1   # the rejection did not un-reach it
    assert report.funnel.count_at(FunnelStage.OFFER) == 0       # nor advance it
    assert report.funnel.censoring.mature_count == 1            # a terminal fact concludes it
    assert report.funnel.censoring.censored_count == 0
    stored = await applications.get(USER, app.id)
    assert stored is not None
    assert stored.state is ApplicationState.SUBMITTED           # read, never written
    assert stored.updated_at == OLD


# --- timings: a median with an ordered spread, over real durations ----------------------


async def test_time_to_first_response_reports_an_ordered_quartile_spread() -> None:
    """A median with p25 <= median <= p75, measured in real days from the anchor (§17)."""
    service, outcomes, applications, _, _ = build_service()
    for index, delay in enumerate((2, 5, 10), start=1):
        app = await _seed_application(applications, opportunity_id=_oid(index), created_at=OLD)
        await _seed_outcome(outcomes, app, OutcomeKind.ACKNOWLEDGED, OLD + delay * DAY)

    timing = (await service.report(USER, now=NOW)).timing_for(TimingKind.TIME_TO_FIRST_RESPONSE)

    assert timing is not None
    assert timing.sample_size == 3
    assert timing.p25_days == 3.5
    assert timing.median_days == 5.0
    assert timing.p75_days == 7.5
    assert timing.p25_days <= timing.median_days <= timing.p75_days


async def test_a_response_predating_the_application_is_dropped_not_trusted() -> None:
    """A negative span is a data anomaly, excluded rather than folded into the median (§17)."""
    service, outcomes, applications, _, _ = build_service()
    good = await _seed_application(applications, opportunity_id=_oid(1), created_at=OLD)
    await _seed_outcome(outcomes, good, OutcomeKind.ACKNOWLEDGED, OLD + 4 * DAY)
    anomaly = await _seed_application(applications, opportunity_id=_oid(2), created_at=OLD)
    await _seed_outcome(outcomes, anomaly, OutcomeKind.ACKNOWLEDGED, OLD - DAY)

    timing = (await service.report(USER, now=NOW)).timing_for(TimingKind.TIME_TO_FIRST_RESPONSE)

    assert timing is not None
    assert timing.sample_size == 1        # the negative-span response was dropped
    assert timing.median_days == 4.0


# --- breakdowns: the funnel sliced, with an honest None cell ----------------------------


async def test_breakdowns_slice_by_role_source_and_type_with_a_none_unclassified_cell() -> None:
    """Each axis keys a known value to its cell and everything unplaceable to the `None` cell (§18-21)."""
    service, outcomes, applications, _, opportunities = build_service()
    await _seed_opportunity(opportunities, id=_oid(1), title="Data Scientist",
                            source_key="linkedin", external_id="p1")
    classified = await _seed_application(applications, opportunity_id=_oid(1), created_at=OLD)
    await _seed_outcome(outcomes, classified, OutcomeKind.ACKNOWLEDGED, OLD + DAY)
    await _seed_opportunity(opportunities, id=_oid(2), title="Zookeeper and Falconer",
                            source_key="indeed", external_id="p2",
                            opportunity_type=OpportunityType.INTERNSHIP)
    await _seed_application(applications, opportunity_id=_oid(2), created_at=OLD)
    # Spontaneous: no posting at all, so every axis reads unknown.
    await _seed_application(applications, opportunity_id=None, company_id=new_company_id(),
                            created_at=OLD)

    report = await service.report(USER, now=NOW)
    role = report.breakdown_for(DimensionKind.ROLE_FAMILY)
    source = report.breakdown_for(DimensionKind.SOURCE)
    kind = report.breakdown_for(DimensionKind.OPPORTUNITY_TYPE)

    assert role is not None and source is not None and kind is not None
    # A recognised title fills its family; the unclassified title and the spontaneous target share None.
    assert [cell.key for cell in role.cells] == ["DATA_AND_ANALYTICS", None]
    assert role.cell_for("DATA_AND_ANALYTICS").applications == 1
    assert role.cell_for(None).applications == 2
    assert role.cell_for("DATA_AND_ANALYTICS").rate_for(RateKind.RESPONSE).numerator == 1
    assert {cell.key for cell in source.cells} == {"linkedin", "indeed", None}
    assert {cell.key for cell in kind.cells} == {"FULL_TIME", "INTERNSHIP", None}


async def test_a_manual_role_classification_outranks_the_title_rule_in_the_breakdown() -> None:
    """A human's correction decides the role cell, even against what the title would classify (§18)."""
    service, _, applications, roles, opportunities = build_service()
    await _seed_opportunity(opportunities, id=_oid(1), title="Data Scientist")
    await _seed_application(applications, opportunity_id=_oid(1), created_at=OLD)
    await roles.upsert(a_role_classification(
        opportunity_id=_oid(1), role_family=RoleFamily.PRODUCT_MANAGEMENT,
        provenance=RoleFamilyProvenance.MANUAL))

    role = (await service.report(USER, now=NOW)).breakdown_for(DimensionKind.ROLE_FAMILY)

    assert role is not None
    assert role.cell_for("PRODUCT_MANAGEMENT").applications == 1
    assert role.cell_for("DATA_AND_ANALYTICS") is None      # the title rule is overruled


# --- the honest empty account and the ownership boundary --------------------------------


async def test_the_empty_account_reports_an_honest_zero_not_a_gap() -> None:
    """A brand-new account is an empty window and null rates, versioned and stamped (§14)."""
    service, _, _, _, _ = build_service()

    report = await service.report(USER, now=NOW)

    assert report.analytics_version == CAREER_ANALYTICS_VERSION
    assert report.computed_at == NOW
    assert report.window.is_empty
    assert report.funnel.submitted == 0
    assert report.funnel.censoring.total_count == 0
    assert all(rate.denominator == 0 and rate.rate is None for rate in report.rates)
    assert all(timing.sample_size == 0 for timing in report.timings)
    assert {rate.kind for rate in report.rates} == set(RateKind)
    assert {timing.kind for timing in report.timings} == set(TimingKind)
    assert {b.dimension for b in report.breakdowns} == {
        DimensionKind.ROLE_FAMILY, DimensionKind.SOURCE, DimensionKind.OPPORTUNITY_TYPE}
    assert report.breakdown_for(DimensionKind.ROLE_FAMILY).cells == ()


async def test_one_accounts_report_never_counts_anothers_applications() -> None:
    """The population is read owner-scoped, so a foreign funnel is invisible (§18-20)."""
    service, outcomes, applications, _, _ = build_service()
    theirs = await _seed_application(applications, user_id=OTHER_USER, created_at=OLD)
    await _seed_outcome(outcomes, theirs, OutcomeKind.INTERVIEW, OLD + DAY)

    mine = await service.report(USER, now=NOW)
    hers = await service.report(OTHER_USER, now=NOW)

    assert mine.window.is_empty
    assert mine.funnel.submitted == 0
    assert hers.funnel.submitted == 1
    assert hers.funnel.count_at(FunnelStage.INTERVIEW) == 1



