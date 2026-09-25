# tests/test_v2_chat_validators.py
"""Re-authorizing a confirmed proposal: "proposal != permission", made mechanical.

The parser proved an action was well-formed; the validator decides whether it may *run*,
for this account, right now. These tests pin the two questions it answers and nothing
more: does every entity the action names belong to this account (loaded through a
`user_id`-scoped read, so another account's id reads as absent), and is that entity in a
state the action can touch (the coarse `is_terminal`/`has-a-radius` guard, never the
service's exact transition rule). Read-only actions skip the ownership and state reads —
there is nothing on the server to authorize — but not the scope wall: one that names a
resource is still held to the anchored thread's scope. A refusal is a `ProposalValidation`
value carrying a stable code and a secret-free reason — never an exception, never a leaked
secret.
"""
import pytest

from backend.app.chat.validators import (
    ProposalRejectionCode,
    ProposalValidator,
    action_scope_anchor,
    action_within_scope,
)
from backend.app.domain.application import (
    Application,
    ApplicationState,
    build_idempotency_key,
)
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.chat import (
    ApproveApplicationAction,
    CancelApplicationAction,
    ConversationScope,
    CreateApplicationAction,
    GenerateCoverLetterAction,
    GenerateResumeAction,
    NavigateAction,
    NavigationTarget,
    OpenInterviewPrepAction,
    PrepareApplicationAction,
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
    APPLICATION,
    COMPANY,
    NOW,
    OPPORTUNITY,
    OTHER_APPLICATION,
    OTHER_OPPORTUNITY,
    OTHER_SEARCH_PROFILE,
    OTHER_USER,
    PROFILE,
    SEARCH_PROFILE,
    USER,
    a_conversation,
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


# --- the scope anchor: a pure, read-free fact about the action's shape ------

@pytest.mark.parametrize("action, expected", [
    # A target-only navigation names no resource, so no thread can scope it out.
    (NavigateAction(target=NavigationTarget.OPPORTUNITIES), None),
    # But read-only is not scope-free: prep (always about one posting) and a navigation
    # carrying an opportunity id anchor to that opportunity exactly as a document does.
    (OpenInterviewPrepAction(opportunity_id=OPPORTUNITY),
     (ConversationScope.OPPORTUNITY, OPPORTUNITY)),
    (NavigateAction(target=NavigationTarget.OPPORTUNITIES, opportunity_id=OPPORTUNITY),
     (ConversationScope.OPPORTUNITY, OPPORTUNITY)),
    # Document + create actions anchor to the posting they are about.
    (GenerateResumeAction(opportunity_id=OPPORTUNITY),
     (ConversationScope.OPPORTUNITY, OPPORTUNITY)),
    (GenerateCoverLetterAction(opportunity_id=OPPORTUNITY),
     (ConversationScope.OPPORTUNITY, OPPORTUNITY)),
    (CreateApplicationAction(opportunity_id=OPPORTUNITY),
     (ConversationScope.OPPORTUNITY, OPPORTUNITY)),
    # Every lifecycle action anchors to the application it drives.
    (PrepareApplicationAction(application_id=APPLICATION),
     (ConversationScope.APPLICATION, APPLICATION)),
    (ApproveApplicationAction(application_id=APPLICATION),
     (ConversationScope.APPLICATION, APPLICATION)),
    (SubmitApplicationAction(application_id=APPLICATION),
     (ConversationScope.APPLICATION, APPLICATION)),
    (CancelApplicationAction(application_id=APPLICATION),
     (ConversationScope.APPLICATION, APPLICATION)),
    # Search-preference edits anchor to the saved search.
    (SetSearchRadiusAction(search_profile_id=SEARCH_PROFILE, radius_km=50.0),
     (ConversationScope.SEARCH_PROFILE, SEARCH_PROFILE)),
    (UpdateSearchKeywordsAction(
        search_profile_id=SEARCH_PROFILE, title_keywords=("python",)),
     (ConversationScope.SEARCH_PROFILE, SEARCH_PROFILE)),
])
def test_action_scope_anchor_binds_each_action_to_its_resource(action, expected):
    assert action_scope_anchor(action) == expected


# --- within-scope: GLOBAL admits all, an anchor admits only its own ---------

@pytest.mark.parametrize("action", [
    NavigateAction(target=NavigationTarget.OPPORTUNITIES),
    NavigateAction(target=NavigationTarget.SETTINGS),
])
def test_a_resource_free_read_only_action_is_within_every_scope(action):
    # A target-only navigation has no anchor, so no anchored thread can exclude it — even
    # one of another kind. Read-only *and* resource-free is the only unconditionally-in-scope
    # case; naming a resource is what re-imposes the anchor rule (next test).
    assert action_within_scope(ConversationScope.APPLICATION, OTHER_APPLICATION, action)
    assert action_within_scope(ConversationScope.GLOBAL, None, action)


@pytest.mark.parametrize("action", [
    OpenInterviewPrepAction(opportunity_id=OPPORTUNITY),
    NavigateAction(target=NavigationTarget.OPPORTUNITIES, opportunity_id=OPPORTUNITY),
])
def test_a_resource_bearing_read_only_action_must_match_the_scope(action):
    # It names an opportunity, so it is in scope only in a GLOBAL thread or in an OPPORTUNITY
    # thread anchored to that same posting — never a sibling, and never a thread of another
    # kind. Read-only does not mean scope-independent.
    assert action_within_scope(ConversationScope.GLOBAL, None, action)
    assert action_within_scope(ConversationScope.OPPORTUNITY, OPPORTUNITY, action)
    assert not action_within_scope(
        ConversationScope.OPPORTUNITY, OTHER_OPPORTUNITY, action)
    assert not action_within_scope(ConversationScope.APPLICATION, APPLICATION, action)


def test_a_global_scope_admits_any_mutating_action():
    action = SubmitApplicationAction(application_id=APPLICATION)
    assert action_within_scope(ConversationScope.GLOBAL, None, action)


def test_an_anchored_scope_admits_its_own_id_and_refuses_a_sibling():
    same = SubmitApplicationAction(application_id=APPLICATION)
    sibling = SubmitApplicationAction(application_id=OTHER_APPLICATION)
    assert action_within_scope(ConversationScope.APPLICATION, APPLICATION, same)
    assert not action_within_scope(ConversationScope.APPLICATION, APPLICATION, sibling)


def test_a_scope_of_another_kind_refuses_a_matching_raw_id():
    # An OPPORTUNITY thread cannot host an application action even when the raw ids collide:
    # the anchor's *kind* must match too.
    action = SubmitApplicationAction(application_id=APPLICATION)
    assert not action_within_scope(ConversationScope.OPPORTUNITY, APPLICATION, action)


def test_a_company_scope_admits_no_mutating_action():
    # No action anchors to a company, so a COMPANY thread hosts only read-only turns and a
    # cross-type flow must go through GLOBAL rather than a mis-scoped thread.
    action = SubmitApplicationAction(application_id=APPLICATION)
    assert not action_within_scope(ConversationScope.COMPANY, COMPANY, action)


# --- the validator's scope wall: refused before any ownership read ----------

@pytest.mark.asyncio
async def test_validate_skips_the_scope_wall_when_no_conversation_is_given():
    # The executor's older two-argument call: no conversation, so scope is not checked and
    # ordinary ownership validation stands alone.
    applications = FakeApplicationRepository()
    app = _an_application(state=ApplicationState.APPROVED)
    await applications.upsert(app)
    verdict = await _validator(applications=applications).validate(
        USER, SubmitApplicationAction(application_id=app.id))
    assert verdict.permitted


@pytest.mark.asyncio
async def test_a_global_thread_permits_an_owned_action():
    applications = FakeApplicationRepository()
    app = _an_application(state=ApplicationState.APPROVED)
    await applications.upsert(app)
    verdict = await _validator(applications=applications).validate(
        USER, SubmitApplicationAction(application_id=app.id),
        conversation=a_conversation())  # GLOBAL by default
    assert verdict.permitted


@pytest.mark.asyncio
async def test_an_anchored_thread_permits_the_action_naming_its_own_resource():
    applications = FakeApplicationRepository()
    app = _an_application(state=ApplicationState.APPROVED)
    await applications.upsert(app)
    conversation = a_conversation(
        scope=ConversationScope.APPLICATION, scope_id=app.id)
    verdict = await _validator(applications=applications).validate(
        USER, SubmitApplicationAction(application_id=app.id),
        conversation=conversation)
    assert verdict.permitted


@pytest.mark.asyncio
async def test_an_application_thread_rejects_a_sibling_application_before_any_read():
    # The sibling is this account's own, open application — ownership and state would
    # permit it. The thread is anchored to a *different* application, so scope refuses
    # first: the verdict is SCOPE_MISMATCH, proving the wall precedes the ownership read.
    applications = FakeApplicationRepository()
    sibling = _an_application(state=ApplicationState.APPROVED)
    await applications.upsert(sibling)
    conversation = a_conversation(
        scope=ConversationScope.APPLICATION, scope_id=OTHER_APPLICATION)
    verdict = await _validator(applications=applications).validate(
        USER, SubmitApplicationAction(application_id=sibling.id),
        conversation=conversation)
    assert not verdict.permitted
    assert verdict.code is ProposalRejectionCode.SCOPE_MISMATCH


@pytest.mark.asyncio
async def test_a_company_thread_rejects_a_mutating_action_as_out_of_scope():
    applications = FakeApplicationRepository()
    app = _an_application(state=ApplicationState.APPROVED)
    await applications.upsert(app)
    conversation = a_conversation(
        scope=ConversationScope.COMPANY, scope_id=COMPANY)
    verdict = await _validator(applications=applications).validate(
        USER, SubmitApplicationAction(application_id=app.id),
        conversation=conversation)
    assert not verdict.permitted
    assert verdict.code is ProposalRejectionCode.SCOPE_MISMATCH


@pytest.mark.asyncio
async def test_a_read_only_action_is_permitted_inside_an_anchored_thread():
    # A target-only NAVIGATE has no anchor, so even an APPLICATION-scoped thread admits it.
    conversation = a_conversation(
        scope=ConversationScope.APPLICATION, scope_id=OTHER_APPLICATION)
    verdict = await _validator().validate(
        USER, NavigateAction(target=NavigationTarget.OPPORTUNITIES),
        conversation=conversation)
    assert verdict.permitted


# --- read-only, but still scope-bound when it names a resource --------------

@pytest.mark.asyncio
async def test_an_opportunity_thread_permits_prep_for_its_own_posting():
    conversation = a_conversation(
        scope=ConversationScope.OPPORTUNITY, scope_id=OPPORTUNITY)
    verdict = await _validator().validate(
        USER, OpenInterviewPrepAction(opportunity_id=OPPORTUNITY),
        conversation=conversation)
    assert verdict.permitted


@pytest.mark.asyncio
async def test_an_opportunity_thread_refuses_prep_for_a_sibling_posting():
    # Prep is read-only, but it names opportunity B while the thread is anchored to A. The
    # scope wall refuses it SCOPE_MISMATCH before the read-only guard is reached — a
    # read-only action can still cross a scope boundary, and this is where that is caught.
    conversation = a_conversation(
        scope=ConversationScope.OPPORTUNITY, scope_id=OPPORTUNITY)
    verdict = await _validator().validate(
        USER, OpenInterviewPrepAction(opportunity_id=OTHER_OPPORTUNITY),
        conversation=conversation)
    assert not verdict.permitted
    assert verdict.code is ProposalRejectionCode.SCOPE_MISMATCH


@pytest.mark.asyncio
async def test_a_global_thread_permits_prep_for_any_posting():
    # GLOBAL places no scope restriction, so read-only prep for any posting is in scope.
    verdict = await _validator().validate(
        USER, OpenInterviewPrepAction(opportunity_id=OTHER_OPPORTUNITY),
        conversation=a_conversation())  # GLOBAL by default
    assert verdict.permitted


@pytest.mark.asyncio
async def test_an_application_thread_refuses_prep_as_a_cross_type_mismatch():
    # An APPLICATION-anchored thread and an OPPORTUNITY-anchored action: the anchor's *kind*
    # differs, so even read-only prep is refused rather than passed just for being read-only.
    conversation = a_conversation(
        scope=ConversationScope.APPLICATION, scope_id=APPLICATION)
    verdict = await _validator().validate(
        USER, OpenInterviewPrepAction(opportunity_id=OPPORTUNITY),
        conversation=conversation)
    assert not verdict.permitted
    assert verdict.code is ProposalRejectionCode.SCOPE_MISMATCH


@pytest.mark.asyncio
async def test_an_opportunity_thread_refuses_navigation_to_a_sibling_posting():
    # A NAVIGATE that carries a sibling opportunity id is out of scope and refused; the
    # embedded id is not silently ignored just because NAVIGATE is read-only.
    conversation = a_conversation(
        scope=ConversationScope.OPPORTUNITY, scope_id=OPPORTUNITY)
    verdict = await _validator().validate(
        USER, NavigateAction(target=NavigationTarget.OPPORTUNITIES,
                             opportunity_id=OTHER_OPPORTUNITY),
        conversation=conversation)
    assert not verdict.permitted
    assert verdict.code is ProposalRejectionCode.SCOPE_MISMATCH
