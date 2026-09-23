# tests/test_v2_chat_validators.py
"""Re-authorizing a confirmed proposal: "proposal != permission", made mechanical.

The parser proved an action was well-formed; the validator decides whether it may *run*,
for this account, right now. These tests pin the two questions it answers and nothing
more: does every entity the action names belong to this account (loaded through a
`user_id`-scoped read, so another account's id reads as absent), and is that entity in a
state the action can touch (the coarse `is_terminal`/`has-a-radius` guard, never the
service's exact transition rule). Read-only actions skip every check, because there is
nothing on the server to authorize. A refusal is a `ProposalValidation` value carrying a
stable code and a secret-free reason — never an exception, and never a leaked secret.
"""
import pytest

from backend.app.chat.validators import (
    ProposalRejectionCode,
    ProposalValidator,
)
from backend.app.domain.application import (
    Application,
    ApplicationState,
    build_idempotency_key,
)
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.chat import (
    CreateApplicationAction,
    GenerateResumeAction,
    NavigateAction,
    NavigationTarget,
    OpenInterviewPrepAction,
    SetSearchRadiusAction,
    SubmitApplicationAction,
    UpdateSearchKeywordsAction,
)
from backend.app.domain.common import GeoPoint
from backend.app.domain.identifiers import (
    CandidateProfileId,
    OpportunityId,
    UserId,
    application_id,
    new_application_decision_id,
)
from backend.app.domain.search import RadiusSearchArea
from tests.v2_builders import (
    NOW,
    OPPORTUNITY,
    OTHER_USER,
    PROFILE,
    SEARCH_PROFILE,
    USER,
    a_search_profile,
    an_opportunity,
)
from tests.v2_fakes import (
    FakeApplicationRepository,
    FakeOpportunityRepository,
    FakeSearchProfileRepository,
)


def _validator(opportunities=None, applications=None, searches=None) -> ProposalValidator:
    return ProposalValidator(
        opportunities=opportunities or FakeOpportunityRepository(),
        applications=applications or FakeApplicationRepository(),
        searches=searches or FakeSearchProfileRepository())


def _an_application(*, user_id: UserId = USER, profile_id: CandidateProfileId = PROFILE,
                    opportunity_id: OpportunityId | None = OPPORTUNITY,
                    state: ApplicationState = ApplicationState.PLANNED) -> Application:
    key = build_idempotency_key(
        candidate_profile_id=profile_id, channel=ApplicationChannel.BROWSER,
        opportunity_id=opportunity_id, company_id=None)
    return Application(
        id=application_id(key), user_id=user_id, candidate_profile_id=profile_id,
        decision_id=new_application_decision_id(), channel=ApplicationChannel.BROWSER,
        state=state, idempotency_key=key, opportunity_id=opportunity_id,
        company_id=None, created_at=NOW, updated_at=NOW)


# --- read-only actions authorize nothing -----------------------------------

@pytest.mark.asyncio
async def test_a_navigate_action_is_permitted_without_any_lookup():
    # Empty repositories: a read-only action must still permit, because it touches nothing.
    verdict = await _validator().validate(
        USER, NavigateAction(target=NavigationTarget.OPPORTUNITIES))
    assert verdict.permitted


@pytest.mark.asyncio
async def test_open_interview_prep_is_permitted_without_any_lookup():
    verdict = await _validator().validate(
        USER, OpenInterviewPrepAction(opportunity_id=OPPORTUNITY))
    assert verdict.permitted


# --- opportunity actions: the posting need only exist ----------------------

@pytest.mark.asyncio
async def test_a_document_action_is_permitted_when_the_posting_exists():
    opportunities = FakeOpportunityRepository()
    await opportunities.upsert(an_opportunity())
    verdict = await _validator(opportunities=opportunities).validate(
        USER, GenerateResumeAction(opportunity_id=OPPORTUNITY))
    assert verdict.permitted


@pytest.mark.asyncio
async def test_a_document_action_is_rejected_when_the_posting_is_unknown():
    verdict = await _validator().validate(
        USER, GenerateResumeAction(opportunity_id=OPPORTUNITY))
    assert not verdict.permitted
    assert verdict.code is ProposalRejectionCode.OPPORTUNITY_NOT_FOUND


@pytest.mark.asyncio
async def test_create_application_is_rejected_when_the_posting_is_unknown():
    verdict = await _validator().validate(
        USER, CreateApplicationAction(opportunity_id=OPPORTUNITY))
    assert not verdict.permitted
    assert verdict.code is ProposalRejectionCode.OPPORTUNITY_NOT_FOUND


# --- application actions: owned, and not closed for good -------------------

@pytest.mark.asyncio
async def test_an_application_action_is_permitted_for_an_owned_open_application():
    applications = FakeApplicationRepository()
    app = _an_application(state=ApplicationState.APPROVED)
    await applications.upsert(app)
    verdict = await _validator(applications=applications).validate(
        USER, SubmitApplicationAction(application_id=app.id))
    assert verdict.permitted


@pytest.mark.asyncio
async def test_an_application_action_is_rejected_when_no_such_application():
    app = _an_application()
    verdict = await _validator().validate(
        USER, SubmitApplicationAction(application_id=app.id))
    assert not verdict.permitted
    assert verdict.code is ProposalRejectionCode.APPLICATION_NOT_FOUND


@pytest.mark.asyncio
async def test_an_application_owned_by_another_account_reads_as_absent():
    applications = FakeApplicationRepository()
    app = _an_application(user_id=OTHER_USER)
    await applications.upsert(app)
    verdict = await _validator(applications=applications).validate(
        USER, SubmitApplicationAction(application_id=app.id))
    assert not verdict.permitted
    assert verdict.code is ProposalRejectionCode.APPLICATION_NOT_FOUND


@pytest.mark.asyncio
async def test_a_terminal_application_is_rejected_as_closed():
    applications = FakeApplicationRepository()
    app = _an_application(state=ApplicationState.CANCELLED)
    await applications.upsert(app)
    verdict = await _validator(applications=applications).validate(
        USER, SubmitApplicationAction(application_id=app.id))
    assert not verdict.permitted
    assert verdict.code is ProposalRejectionCode.APPLICATION_CLOSED


# --- search preferences: owned, and (for radius) has a radius to resize ----

@pytest.mark.asyncio
async def test_set_radius_is_permitted_when_the_search_has_a_radius_area():
    searches = FakeSearchProfileRepository()
    await searches.upsert(a_search_profile(
        RadiusSearchArea(center=GeoPoint(latitude=46.5, longitude=6.6), radius_km=30.0)))
    verdict = await _validator(searches=searches).validate(
        USER, SetSearchRadiusAction(search_profile_id=SEARCH_PROFILE, radius_km=50.0))
    assert verdict.permitted


@pytest.mark.asyncio
async def test_set_radius_is_rejected_when_the_search_has_no_radius_area():
    searches = FakeSearchProfileRepository()
    await searches.upsert(a_search_profile())  # a country area only
    verdict = await _validator(searches=searches).validate(
        USER, SetSearchRadiusAction(search_profile_id=SEARCH_PROFILE, radius_km=50.0))
    assert not verdict.permitted
    assert verdict.code is ProposalRejectionCode.SEARCH_HAS_NO_RADIUS


@pytest.mark.asyncio
async def test_set_radius_is_rejected_when_the_search_is_unknown():
    verdict = await _validator().validate(
        USER, SetSearchRadiusAction(search_profile_id=SEARCH_PROFILE, radius_km=50.0))
    assert not verdict.permitted
    assert verdict.code is ProposalRejectionCode.SEARCH_NOT_FOUND


@pytest.mark.asyncio
async def test_update_keywords_is_permitted_for_an_owned_search():
    searches = FakeSearchProfileRepository()
    await searches.upsert(a_search_profile())
    verdict = await _validator(searches=searches).validate(
        USER, UpdateSearchKeywordsAction(
            search_profile_id=SEARCH_PROFILE, title_keywords=("python",)))
    assert verdict.permitted


@pytest.mark.asyncio
async def test_update_keywords_on_another_accounts_search_reads_as_absent():
    searches = FakeSearchProfileRepository()
    await searches.upsert(a_search_profile(user_id=OTHER_USER))
    verdict = await _validator(searches=searches).validate(
        USER, UpdateSearchKeywordsAction(
            search_profile_id=SEARCH_PROFILE, title_keywords=("python",)))
    assert not verdict.permitted
    assert verdict.code is ProposalRejectionCode.SEARCH_NOT_FOUND
