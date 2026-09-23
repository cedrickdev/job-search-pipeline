# tests/test_v2_assessment_service.py
"""`AssessmentService` — the thin orchestrator, tested on the fakes directly.

The API tests (`test_v2_api_matches.py`) cover the HTTP contract; this covers the
service decisions a route cannot show cleanly: that the two axes are stored in two
records, that a match may be absent while an eligibility never is, that the three
empty-read cases are indistinguishable `None`s, and that the list is chronological
rather than ranked. Driven over the in-memory repositories, no database and no
HTTP — the decisions live in the service, and this is where they are pinned.
"""
from datetime import UTC, datetime, timedelta

import pytest

from backend.app.discovery.bootstrap import build_country_packs
from backend.app.domain.candidate import WorkAuthorizationStatus
from backend.app.domain.common import (
    LanguageLevel,
    LanguageProficiency,
    LanguageRequirement,
    Location,
    WorkloadRange,
)
from backend.app.domain.eligibility import EligibilityStatus
from backend.app.domain.identifiers import OpportunityId
from backend.app.domain.opportunity import WorkplaceMode
from backend.app.services.assessment import (
    AssessmentService,
    CandidateProfileNotFound,
    OpportunityNotFound,
)
from tests.v2_builders import (
    LAUSANNE,
    OTHER_OPPORTUNITY,
    OTHER_USER,
    USER,
    a_candidate_profile,
    a_work_authorization,
    an_opportunity,
)
from tests.v2_fakes import (
    FakeCandidateProfileRepository,
    FakeEligibilityResultRepository,
    FakeMatchEvaluationRepository,
    FakeOpportunityRepository,
)

NOW = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


def _service(profiles, postings, matches, eligibilities):
    """The service over four fakes and the real country packs.

    Real packs, because the CH pack is what the permit path reads; they load from
    YAML and hold no connection, so they are safe without a database.
    """
    return AssessmentService(profiles, postings, matches, eligibilities,
                             build_country_packs())


def _fakes():
    return (FakeCandidateProfileRepository(), FakeOpportunityRepository(),
            FakeMatchEvaluationRepository(), FakeEligibilityResultRepository())


def _lausanne_posting(**overrides):
    fields = {
        "location": Location(country="CH", region="Vaud", city="Lausanne",
                             point=LAUSANNE),
        "workplace_mode": WorkplaceMode.ON_SITE,
        "workload": WorkloadRange(min_percent=80, max_percent=100),
        "language_requirements": (
            LanguageRequirement(language="fr", minimum_level=LanguageLevel.C1),),
    }
    fields.update(overrides)
    return an_opportunity(**fields)


@pytest.mark.asyncio
async def test_evaluate_stores_the_two_axes_in_two_records():
    """Both engines run, and each verdict is upserted into its own repository."""
    profiles, postings, matches, eligibilities = _fakes()
    await profiles.upsert(a_candidate_profile())
    await postings.upsert(_lausanne_posting())
    service = _service(profiles, postings, matches, eligibilities)

    assessment = await service.evaluate(USER, an_opportunity().id, now=NOW)

    assert assessment.match is not None
    assert assessment.eligibility is not None
    assert len(matches.evaluations) == 1
    assert len(eligibilities.results) == 1


@pytest.mark.asyncio
async def test_an_unscorable_pair_stores_an_eligibility_but_no_match():
    """A pair with nothing to score leaves no match row — absence, not a zero.

    A posting that states no location, workload or language, against a profile with
    none of those either, gives the match engine nothing to average, so it returns
    `None` and no match is stored. The eligibility engine still emits its baseline
    gate, so the assessment exists.
    """
    profiles, postings, matches, eligibilities = _fakes()
    await profiles.upsert(a_candidate_profile(languages=(), availability=None,
                                              base_location=None))
    barren = an_opportunity(location=None, workload=None,
                            language_requirements=(),
                            workplace_mode=WorkplaceMode.ON_SITE)
    await postings.upsert(barren)
    service = _service(profiles, postings, matches, eligibilities)

    assessment = await service.evaluate(USER, barren.id, now=NOW)

    assert assessment.match is None
    assert matches.evaluations == {}
    assert assessment.eligibility is not None
    assert len(eligibilities.results) == 1


@pytest.mark.asyncio
async def test_evaluate_without_a_profile_raises_candidate_profile_not_found():
    profiles, postings, matches, eligibilities = _fakes()
    await postings.upsert(_lausanne_posting())
    service = _service(profiles, postings, matches, eligibilities)

    with pytest.raises(CandidateProfileNotFound):
        await service.evaluate(USER, an_opportunity().id, now=NOW)


@pytest.mark.asyncio
async def test_evaluate_without_a_posting_raises_opportunity_not_found():
    profiles, postings, matches, eligibilities = _fakes()
    await profiles.upsert(a_candidate_profile())
    service = _service(profiles, postings, matches, eligibilities)

    with pytest.raises(OpportunityNotFound):
        await service.evaluate(USER, OpportunityId(OTHER_OPPORTUNITY), now=NOW)


@pytest.mark.asyncio
async def test_the_score_is_never_touched_by_the_eligibility_verdict():
    """The core separation, at the service boundary: a blocked pair keeps its score."""
    profiles, postings, matches, eligibilities = _fakes()
    await profiles.upsert(a_candidate_profile(
        languages=(LanguageProficiency(language="fr", level=LanguageLevel.C2),),
        work_authorizations=(a_work_authorization(
            country="CH", status=WorkAuthorizationStatus.NOT_AUTHORIZED),)))
    await postings.upsert(_lausanne_posting())
    service = _service(profiles, postings, matches, eligibilities)

    assessment = await service.evaluate(USER, an_opportunity().id, now=NOW)

    assert assessment.eligibility.status is EligibilityStatus.INELIGIBLE
    assert assessment.eligibility.is_blocking is True
    assert assessment.match is not None
    assert assessment.match.overall > 0.5  # a real, high score beside the block


@pytest.mark.asyncio
async def test_assessment_for_is_none_before_any_evaluation():
    profiles, postings, matches, eligibilities = _fakes()
    await profiles.upsert(a_candidate_profile())
    await postings.upsert(_lausanne_posting())
    service = _service(profiles, postings, matches, eligibilities)

    assert await service.assessment_for(USER, an_opportunity().id) is None


@pytest.mark.asyncio
async def test_assessment_for_is_none_without_a_profile():
    """No profile is indistinguishable from not-evaluated — the same `None`."""
    profiles, postings, matches, eligibilities = _fakes()
    await postings.upsert(_lausanne_posting())
    service = _service(profiles, postings, matches, eligibilities)

    assert await service.assessment_for(USER, an_opportunity().id) is None


@pytest.mark.asyncio
async def test_assessment_for_is_none_for_another_users_verdict():
    """A verdict this account did not produce reads as absent — no id probing."""
    profiles, postings, matches, eligibilities = _fakes()
    # Both accounts hold a profile; only the other one evaluates the pair.
    await profiles.upsert(a_candidate_profile())
    await profiles.upsert(a_candidate_profile(
        id=a_candidate_profile(user_id=OTHER_USER).id, user_id=OTHER_USER))
    await postings.upsert(_lausanne_posting())
    service = _service(profiles, postings, matches, eligibilities)
    await service.evaluate(OTHER_USER, an_opportunity().id, now=NOW)

    # This account has never evaluated it, so its read is None even though a verdict
    # for the pair exists under another owner.
    assert await service.assessment_for(USER, an_opportunity().id) is None


@pytest.mark.asyncio
async def test_list_is_chronological_newest_first_not_ranked():
    """Order follows determination time, not the match score.

    The higher-scoring pair is evaluated first (earlier `now`); the list still puts
    the later evaluation first, proving the order is chronological rather than a
    ranking by fit.
    """
    profiles, postings, matches, eligibilities = _fakes()
    await profiles.upsert(a_candidate_profile(
        languages=(LanguageProficiency(language="fr", level=LanguageLevel.C2),)))
    first = _lausanne_posting()
    second = _lausanne_posting(id=OpportunityId(OTHER_OPPORTUNITY))
    await postings.upsert(first)
    await postings.upsert(second)
    service = _service(profiles, postings, matches, eligibilities)

    await service.evaluate(USER, first.id, now=NOW)
    await service.evaluate(USER, second.id, now=NOW + timedelta(hours=1))

    listed = await service.list_assessments(USER)

    assert len(listed) == 2
    # Newest determination first: the second evaluation leads.
    assert listed[0].opportunity.id == second.id
    assert listed[1].opportunity.id == first.id
