# tests/test_v2_chat_context.py
"""The chat context builder: bounded, user-isolated, secret-free by construction.

The snapshot is what lets the model propose actions against ids that exist. These tests
pin the three properties that make it safe to hand to a model on every turn: it is capped
per section (a busy account yields a page, not a dump), it is scoped to the signed-in
account (another user's rows never appear), and its very shape carries no secret (there
is no field on a `ChatContext` a credential could ride in). The rendering is checked too,
because the snapshot's whole job is to become the text the model reads.
"""
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.app.chat.context import (
    _MAX_APPLICATIONS,
    _MAX_OPPORTUNITIES,
    _MAX_SEARCH_PROFILES,
    ChatApplicationSummary,
    ChatContext,
    ChatContextBuilder,
    ChatOpportunitySummary,
    ChatProfileSummary,
    ChatSearchSummary,
)
from backend.app.domain.application import (
    Application,
    ApplicationState,
    build_idempotency_key,
)
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.common import GeoPoint
from backend.app.domain.identifiers import (
    CandidateProfileId,
    OpportunityId,
    SearchProfileId,
    UserId,
    application_id,
    default_candidate_profile_id,
    new_application_decision_id,
    new_company_id,
)
from backend.app.domain.search import RadiusSearchArea
from tests.v2_builders import (
    OTHER_USER,
    USER,
    a_candidate_profile,
    a_search_profile,
    an_opportunity,
)
from tests.v2_fakes import (
    FakeApplicationRepository,
    FakeCandidateProfileRepository,
    FakeOpportunityRepository,
    FakeSearchProfileRepository,
)

NOW = datetime(2026, 9, 20, tzinfo=UTC)
LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)


def _builder(profiles=None, searches=None, applications=None, opportunities=None):
    return ChatContextBuilder(
        profiles=profiles or FakeCandidateProfileRepository(),
        searches=searches or FakeSearchProfileRepository(),
        applications=applications or FakeApplicationRepository(),
        opportunities=opportunities or FakeOpportunityRepository())


def _an_application(*, user_id: UserId, profile_id: CandidateProfileId,
                    opportunity_id: OpportunityId | None = None,
                    company_id=None,
                    state: ApplicationState = ApplicationState.PLANNED) -> Application:
    # Exactly one of opportunity_id / company_id, as build_idempotency_key demands.
    key = build_idempotency_key(
        candidate_profile_id=profile_id, channel=ApplicationChannel.BROWSER,
        opportunity_id=opportunity_id, company_id=company_id)
    return Application(
        id=application_id(key), user_id=user_id, candidate_profile_id=profile_id,
        decision_id=new_application_decision_id(), channel=ApplicationChannel.BROWSER,
        state=state, idempotency_key=key, opportunity_id=opportunity_id,
        company_id=company_id, created_at=NOW, updated_at=NOW)


# --- the snapshot is assembled from every section --------------------------

@pytest.mark.asyncio
async def test_the_snapshot_carries_profile_searches_applications_and_postings():
    profiles = FakeCandidateProfileRepository()
    searches = FakeSearchProfileRepository()
    applications = FakeApplicationRepository()
    opportunities = FakeOpportunityRepository()

    profile = a_candidate_profile(headline="Data engineer")
    await profiles.upsert(profile)
    opportunity = an_opportunity()
    await opportunities.upsert(opportunity)
    await searches.upsert(a_search_profile(
        RadiusSearchArea(center=GeoPoint(latitude=46.5, longitude=6.6), radius_km=30.0),
        title_keywords=("python", "data"), excluded_keywords=("senior",)))
    await applications.upsert(_an_application(
        user_id=USER, profile_id=profile.id, opportunity_id=opportunity.id,
        state=ApplicationState.READY_FOR_REVIEW))

    context = await _builder(profiles, searches, applications, opportunities).build(USER)

    assert context.profile is not None
    assert context.profile.display_name == profile.display_name
    assert context.profile.headline == "Data engineer"
    assert context.profile.languages == ("FR (C2)",)
    assert len(context.search_profiles) == 1
    assert context.search_profiles[0].radius_km == 30.0
    assert context.search_profiles[0].title_keywords == ("python", "data")
    assert context.search_profiles[0].excluded_keywords == ("senior",)
    assert len(context.applications) == 1
    assert context.applications[0].state is ApplicationState.READY_FOR_REVIEW
    assert context.applications[0].opportunity_id == opportunity.id
    assert len(context.opportunities) == 1


@pytest.mark.asyncio
async def test_a_fresh_account_yields_an_empty_but_valid_snapshot():
    context = await _builder().build(USER)
    assert context.profile is None
    assert context.search_profiles == ()
    assert context.applications == ()
    assert context.opportunities == ()
    rendered = context.render()
    assert "none yet" in rendered  # profile
    assert "Saved searches: none." in rendered
    assert "Applications: none." in rendered
    assert "Recent opportunities: none." in rendered


# --- user isolation --------------------------------------------------------

@pytest.mark.asyncio
async def test_another_accounts_rows_never_enter_the_snapshot():
    profiles = FakeCandidateProfileRepository()
    searches = FakeSearchProfileRepository()
    applications = FakeApplicationRepository()
    opportunities = FakeOpportunityRepository()

    # Distinct ids per user: the default builder id is fixed, so two profiles that
    # shared it would overwrite each other in the fake and make the test vacuous.
    mine = a_candidate_profile(
        id=default_candidate_profile_id(USER), user_id=USER, display_name="Me")
    theirs = a_candidate_profile(
        id=default_candidate_profile_id(OTHER_USER), user_id=OTHER_USER,
        display_name="Someone Else")
    await profiles.upsert(mine)
    await profiles.upsert(theirs)
    await searches.upsert(a_search_profile(
        id=SearchProfileId(UUID(int=0x5ea0)), user_id=OTHER_USER, name="Their search"))
    await applications.upsert(_an_application(
        user_id=OTHER_USER, profile_id=theirs.id, company_id=new_company_id()))

    context = await _builder(profiles, searches, applications, opportunities).build(USER)

    assert context.profile is not None
    assert context.profile.display_name == "Me"
    assert context.search_profiles == ()
    assert context.applications == ()
    rendered = context.render()
    assert "Someone Else" not in rendered
    assert "Their search" not in rendered


# --- bounded ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_each_section_is_capped():
    profiles = FakeCandidateProfileRepository()
    searches = FakeSearchProfileRepository()
    applications = FakeApplicationRepository()
    opportunities = FakeOpportunityRepository()

    profile = a_candidate_profile()
    await profiles.upsert(profile)
    for index in range(_MAX_SEARCH_PROFILES + 5):
        await searches.upsert(a_search_profile(
            id=SearchProfileId(UUID(int=0x2000 + index)), name=f"Search {index}"))
    for index in range(_MAX_OPPORTUNITIES + 5):
        await opportunities.upsert(an_opportunity(
            id=OpportunityId(UUID(int=0x1000 + index)), title=f"Role {index}"))
    for index in range(_MAX_APPLICATIONS + 5):
        # Distinct target per application -> distinct idempotency key -> distinct id.
        await applications.upsert(_an_application(
            user_id=USER, profile_id=profile.id,
            opportunity_id=OpportunityId(UUID(int=0x3000 + index))))

    context = await _builder(profiles, searches, applications, opportunities).build(USER)

    assert len(context.search_profiles) == _MAX_SEARCH_PROFILES
    assert len(context.opportunities) == _MAX_OPPORTUNITIES
    assert len(context.applications) == _MAX_APPLICATIONS


# --- target resolution and radius extraction -------------------------------

@pytest.mark.asyncio
async def test_an_application_shows_its_posting_even_when_not_in_the_recent_feed():
    """The target is resolved by a direct read when it is not among recent postings."""
    profiles = FakeCandidateProfileRepository()
    applications = FakeApplicationRepository()
    opportunities = FakeOpportunityRepository()

    profile = a_candidate_profile()
    await profiles.upsert(profile)
    # An old posting the application targets: fresh fillers crowd it out of the recent
    # feed, so it is *not* in the already-loaded set and the builder must read it back
    # directly through `.get()`.
    target = an_opportunity(id=OpportunityId(UUID(int=1)), title="Staff Engineer",
                            discovered_at=LONG_AGO)
    await opportunities.upsert(target)
    for index in range(_MAX_OPPORTUNITIES):
        await opportunities.upsert(an_opportunity(
            id=OpportunityId(UUID(int=0x100 + index)), title=f"Filler {index}"))
    await applications.upsert(_an_application(
        user_id=USER, profile_id=profile.id, opportunity_id=target.id))

    context = await _builder(profiles, None, applications, opportunities).build(USER)

    # The feed genuinely excludes the target, so the label proves the `.get()` fallback.
    assert all(opp.id != target.id for opp in context.opportunities)
    assert context.applications[0].target_label == (
        f'"Staff Engineer" at {target.company_name}')


@pytest.mark.asyncio
async def test_a_spontaneous_application_is_labelled_without_a_posting():
    profiles = FakeCandidateProfileRepository()
    applications = FakeApplicationRepository()
    profile = a_candidate_profile()
    await profiles.upsert(profile)
    await applications.upsert(_an_application(
        user_id=USER, profile_id=profile.id, company_id=new_company_id()))

    context = await _builder(profiles, None, applications, None).build(USER)

    assert context.applications[0].opportunity_id is None
    assert "spontaneous" in context.applications[0].target_label


@pytest.mark.asyncio
async def test_a_search_reports_its_largest_radius_and_none_when_it_has_none():
    searches = FakeSearchProfileRepository()
    await searches.upsert(a_search_profile(  # two radius areas: the largest wins
        RadiusSearchArea(center=GeoPoint(latitude=46.5, longitude=6.6), radius_km=25.0),
        RadiusSearchArea(center=GeoPoint(latitude=47.4, longitude=8.5), radius_km=60.0),
        id=SearchProfileId(UUID(int=0xdada)), name="With radius"))
    await searches.upsert(a_search_profile(  # default is a COUNTRY area: no radius
        id=SearchProfileId(UUID(int=0xda1a)), name="No radius"))

    context = await _builder(None, searches, None, None).build(USER)

    by_name = {s.name: s for s in context.search_profiles}
    assert by_name["With radius"].radius_km == 60.0
    assert by_name["No radius"].radius_km is None


# --- secret-free by construction -------------------------------------------

def test_the_snapshot_shape_carries_no_credential_field():
    """The type is the guarantee: a future edit that added a secret field breaks this."""
    assert set(ChatContext.model_fields) == {
        "profile", "search_profiles", "applications", "opportunities"}
    assert set(ChatProfileSummary.model_fields) == {
        "display_name", "headline", "languages"}
    assert set(ChatSearchSummary.model_fields) == {
        "id", "name", "is_active", "title_keywords", "excluded_keywords", "radius_km"}
    assert set(ChatApplicationSummary.model_fields) == {
        "id", "state", "target_label", "opportunity_id"}
    assert set(ChatOpportunitySummary.model_fields) == {
        "id", "title", "company_name", "location_label"}


def test_the_snapshot_parts_are_frozen():
    summary = ChatProfileSummary(display_name="Me")
    with pytest.raises(ValidationError):
        summary.display_name = "Someone Else"


# --- rendering -------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_render_shows_ids_and_marks_labels_untrusted():
    profiles = FakeCandidateProfileRepository()
    opportunities = FakeOpportunityRepository()
    await profiles.upsert(a_candidate_profile())
    opportunity = an_opportunity()
    await opportunities.upsert(opportunity)

    text = (await _builder(profiles, None, None, opportunities).build(USER)).render()

    assert str(opportunity.id) in text
    assert opportunity.title in text
    assert "untrusted" in text.lower()
