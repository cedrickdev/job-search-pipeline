# tests/test_v2_onboarding.py
"""`OnboardingService`: the two writes, the gate, and the cross-user boundary.

The gate is the interesting part and the reason `complete()` exists as a method
rather than as a `PATCH` on the user: it re-reads both aggregates, so a client
that skipped a step cannot mark itself done. Half of this module is that, and the
other half is the boundary — every method takes a `user_id` the caller cannot
choose, and the tests here pass somebody else's id on purpose to prove it is
honoured rather than accepted.

Everything runs on `tests/v2_fakes.py`: no clock, no socket, no database.
"""
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.app.domain.candidate import (
    Availability,
    WeeklyAvailabilitySlot,
    Weekday,
    WorkAuthorization,
    WorkAuthorizationStatus,
)
from backend.app.domain.common import LanguageLevel, LanguageProficiency, Location
from backend.app.domain.identifiers import (
    EvidenceId,
    UserId,
    default_candidate_profile_id,
    new_search_profile_id,
    new_user_id,
)
from backend.app.domain.search import CountrySearchArea, RadiusSearchArea
from backend.app.domain.user import User
from backend.app.services.onboarding import (
    CandidateProfileDraft,
    OnboardingIncomplete,
    OnboardingService,
    SearchProfileDraft,
    SearchProfileNotFound,
)
from tests.v2_fakes import (
    FakeCandidateProfileRepository,
    FakeSearchProfileRepository,
    FakeUserRepository,
)

NOW = datetime(2026, 4, 1, 8, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)


def build_service() -> tuple[OnboardingService, FakeCandidateProfileRepository,
                             FakeSearchProfileRepository, FakeUserRepository]:
    profiles = FakeCandidateProfileRepository()
    searches = FakeSearchProfileRepository()
    users = FakeUserRepository()
    return OnboardingService(profiles, searches, users), profiles, searches, users


def account(email: str = "candidate@example.com") -> User:
    """An account as `AuthenticationService` would have left it: no stamp yet."""
    return User(id=new_user_id(), email=email,
                password_hash=_UNUSED_HASH, created_at=NOW, updated_at=NOW)


# Never verified by these tests — `OnboardingService` does not touch credentials.
# A syntactically valid Argon2 string rather than a placeholder, because `User`
# would happily hold nonsense and a reader should not have to check.
_UNUSED_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHRzb21lc2E$" \
               "3RCPqf7xJdWiXPHRRXR2Bg"


def profile_draft(**overrides: object) -> CandidateProfileDraft:
    return CandidateProfileDraft.model_validate({
        "display_name": "Candidate",
        "headline": "Backend engineer",
        "base_location": Location(city="Yverdon-les-Bains", country="CH"),
        "languages": (LanguageProficiency(language="fr", level=LanguageLevel.NATIVE),),
        **overrides})


def search_draft(**overrides: object) -> SearchProfileDraft:
    return SearchProfileDraft.model_validate({
        "name": "Backend in Romandie",
        "areas": (CountrySearchArea(country="CH"),),
        "queries": ("backend engineer",),
        **overrides})


@pytest.mark.asyncio
async def test_save_profile_derives_its_id_from_the_account() -> None:
    """So a double-submitted form updates one profile instead of creating two."""
    service, profiles, _, _ = build_service()
    user = account()

    first = await service.save_profile(user.id, profile_draft(), now=NOW)
    second = await service.save_profile(
        user.id, profile_draft(display_name="Candidate II"), now=LATER)

    assert first.id == default_candidate_profile_id(user.id)
    assert second.id == first.id
    assert len(profiles.profiles) == 1
    assert profiles.profiles[first.id].display_name == "Candidate II"
    assert profiles.profiles[first.id].updated_at == LATER


@pytest.mark.asyncio
async def test_save_profile_keeps_every_value_object_the_draft_carried() -> None:
    """The draft is the domain's own types, so nothing is flattened on the way in."""
    service, _, _, _ = build_service()
    user = account()
    draft = profile_draft(
        work_authorizations=(WorkAuthorization(
            country="CH", status=WorkAuthorizationStatus.STUDENT_PERMIT_WITH_WORK_RIGHTS,
            permit_label="Permis B — étudiant", valid_until=date(2027, 9, 30),
            permit_hours_cap=15.0),),
        availability=Availability(
            earliest_start=date(2026, 5, 1), notice_period_days=30,
            weekly_slots=(WeeklyAvailabilitySlot(
                weekday=Weekday.SATURDAY, start_hour=9, end_hour=17),)))

    saved = await service.save_profile(user.id, draft, now=NOW)

    assert saved.user_id == user.id
    assert saved.work_authorizations[0].permit_hours_cap == 15.0
    assert saved.availability is not None
    assert saved.availability.weekly_slots[0].weekday is Weekday.SATURDAY
    # Left empty rather than defaulted to something: they have no storage yet.
    assert saved.evidence == ()
    assert saved.claims == ()


def test_a_profile_draft_may_not_cite_evidence_yet() -> None:
    """Phase 10 owns the evidence store; the refusal names the reason."""
    with pytest.raises(ValidationError) as refusal:
        profile_draft(work_authorizations=(WorkAuthorization(
            country="CH", status=WorkAuthorizationStatus.STUDENT_PERMIT_WITH_WORK_RIGHTS,
            evidence_ids=(EvidenceId(new_user_id()),)),))

    assert "evidence store" in str(refusal.value)


def test_a_search_draft_must_bound_its_geography() -> None:
    """An unbounded search is what makes a discovery run sweep the planet."""
    with pytest.raises(ValidationError):
        search_draft(areas=())


@pytest.mark.asyncio
async def test_create_search_assigns_a_fresh_id_each_time() -> None:
    service, _, searches, _ = build_service()
    user = account()

    first = await service.create_search(user.id, search_draft(), now=NOW)
    second = await service.create_search(
        user.id, search_draft(name="Remote only"), now=NOW)

    assert first.id != second.id
    assert len(searches.searches) == 2
    assert {search.user_id for search in searches.searches.values()} == {user.id}


@pytest.mark.asyncio
async def test_update_search_preserves_the_creation_timestamp() -> None:
    """`created_at` belongs to the row: an edit must not backdate or reset it."""
    service, _, _, _ = build_service()
    user = account()
    created = await service.create_search(user.id, search_draft(), now=NOW)

    updated = await service.update_search(
        user.id, created.id,
        search_draft(name="Backend in Romandie, 30 km",
                     areas=(RadiusSearchArea(center={"latitude": 46.78,
                                                     "longitude": 6.64},
                                             radius_km=30.0),)),
        now=LATER)

    assert updated.id == created.id
    assert updated.created_at == NOW
    assert updated.updated_at == LATER
    assert updated.areas[0].kind.value == "RADIUS"


@pytest.mark.asyncio
async def test_updating_a_search_that_does_not_exist_is_refused() -> None:
    service, _, _, _ = build_service()

    with pytest.raises(SearchProfileNotFound):
        await service.update_search(account().id, new_search_profile_id(),
                                    search_draft(), now=NOW)


@pytest.mark.asyncio
async def test_delete_search_is_not_silently_forgiving() -> None:
    service, _, searches, _ = build_service()
    user = account()
    created = await service.create_search(user.id, search_draft(), now=NOW)

    await service.delete_search(user.id, created.id)

    assert searches.searches == {}
    with pytest.raises(SearchProfileNotFound):
        await service.delete_search(user.id, created.id)


@pytest.mark.asyncio
async def test_searches_can_be_narrowed_to_the_active_ones() -> None:
    service, _, _, _ = build_service()
    user = account()
    await service.create_search(user.id, search_draft(), now=NOW)
    await service.create_search(
        user.id, search_draft(name="Paused", is_active=False), now=NOW)

    assert len(await service.searches(user.id)) == 2
    active = await service.searches(user.id, active_only=True)
    assert [search.name for search in active] == ["Backend in Romandie"]


@pytest.mark.asyncio
async def test_state_reports_counts_rather_than_booleans() -> None:
    """A paused search is a different screen from no searches at all."""
    service, _, _, _ = build_service()
    user = account()

    empty = await service.state(user)
    assert (empty.has_profile, empty.search_profiles, empty.active_search_profiles) \
        == (False, 0, 0)
    assert not empty.may_complete
    assert not empty.is_complete

    await service.save_profile(user.id, profile_draft(), now=NOW)
    await service.create_search(
        user.id, search_draft(name="Paused", is_active=False), now=NOW)

    paused = await service.state(user)
    assert (paused.has_profile, paused.search_profiles,
            paused.active_search_profiles) == (True, 1, 0)
    assert not paused.may_complete

    await service.create_search(user.id, search_draft(), now=NOW)

    ready = await service.state(user)
    assert (ready.search_profiles, ready.active_search_profiles) == (2, 1)
    assert ready.may_complete


@pytest.mark.asyncio
async def test_complete_refuses_when_there_is_no_profile() -> None:
    """And says which half is missing, because the client has to fix one of them."""
    service, _, _, users = build_service()
    user = await users.upsert(account())
    await service.create_search(user.id, search_draft(), now=NOW)

    with pytest.raises(OnboardingIncomplete) as refusal:
        await service.complete(user, now=NOW)

    assert refusal.value.has_profile is False
    assert refusal.value.active_searches == 1
    assert users.users[user.id].onboarding_completed_at is None


@pytest.mark.asyncio
async def test_complete_refuses_when_every_search_is_paused() -> None:
    service, _, _, users = build_service()
    user = await users.upsert(account())
    await service.save_profile(user.id, profile_draft(), now=NOW)
    await service.create_search(
        user.id, search_draft(is_active=False), now=NOW)

    with pytest.raises(OnboardingIncomplete) as refusal:
        await service.complete(user, now=NOW)

    assert refusal.value.has_profile is True
    assert refusal.value.active_searches == 0
    assert "at least one active search" in str(refusal.value)


@pytest.mark.asyncio
async def test_complete_stamps_the_account_once_both_halves_exist() -> None:
    service, _, _, users = build_service()
    user = await users.upsert(account())
    await service.save_profile(user.id, profile_draft(), now=NOW)
    await service.create_search(user.id, search_draft(), now=NOW)

    completed = await service.complete(user, now=LATER)

    assert completed.onboarding_completed_at == LATER
    assert completed.has_completed_onboarding
    assert users.users[user.id].onboarding_completed_at == LATER
    assert (await service.state(completed)).is_complete


@pytest.mark.asyncio
async def test_complete_keeps_the_first_timestamp() -> None:
    """When onboarding finished is a fact; a second click must not rewrite it."""
    service, _, _, users = build_service()
    user = await users.upsert(account())
    await service.save_profile(user.id, profile_draft(), now=NOW)
    await service.create_search(user.id, search_draft(), now=NOW)
    completed = await service.complete(user, now=NOW)

    again = await service.complete(completed, now=LATER)

    assert again.onboarding_completed_at == NOW


@pytest.mark.asyncio
async def test_a_completed_account_that_deletes_its_search_stays_completed() -> None:
    """The stamp records what happened, not a live invariant to be re-derived."""
    service, _, _, users = build_service()
    user = await users.upsert(account())
    await service.save_profile(user.id, profile_draft(), now=NOW)
    created = await service.create_search(user.id, search_draft(), now=NOW)
    completed = await service.complete(user, now=NOW)

    await service.delete_search(user.id, created.id)

    state = await service.state(completed)
    assert state.is_complete
    assert not state.may_complete


@pytest.mark.asyncio
async def test_one_account_cannot_read_anothers_profile_or_searches() -> None:
    service, _, _, _ = build_service()
    mine, theirs = account(), account("someone.else@example.com")
    await service.save_profile(theirs.id, profile_draft(display_name="Them"), now=NOW)
    await service.create_search(theirs.id, search_draft(), now=NOW)

    assert await service.profile(mine.id) is None
    assert await service.searches(mine.id) == ()
    state = await service.state(mine)
    assert (state.has_profile, state.search_profiles) == (False, 0)


@pytest.mark.asyncio
async def test_one_account_cannot_edit_or_delete_anothers_search() -> None:
    """A known id plus the wrong owner reads as absent, so nothing is written."""
    service, _, searches, _ = build_service()
    mine, theirs = account(), account("someone.else@example.com")
    hers = await service.create_search(theirs.id, search_draft(), now=NOW)

    with pytest.raises(SearchProfileNotFound):
        await service.update_search(mine.id, hers.id,
                                    search_draft(name="Hijacked"), now=LATER)
    with pytest.raises(SearchProfileNotFound):
        await service.delete_search(mine.id, hers.id)

    stored = searches.searches[hers.id]
    assert stored.name == "Backend in Romandie"
    assert stored.user_id == theirs.id
    assert stored.updated_at == NOW


@pytest.mark.asyncio
async def test_two_accounts_get_two_different_default_profile_ids() -> None:
    """The derivation is per account, so it cannot collide across users."""
    service, profiles, _, _ = build_service()
    mine, theirs = account(), account("someone.else@example.com")

    await service.save_profile(mine.id, profile_draft(display_name="Me"), now=NOW)
    await service.save_profile(theirs.id, profile_draft(display_name="Them"), now=NOW)

    assert len(profiles.profiles) == 2
    my_profile = await service.profile(mine.id)
    assert my_profile is not None
    assert my_profile.display_name == "Me"
    assert my_profile.id != default_candidate_profile_id(theirs.id)


def test_the_default_profile_id_is_stable_and_not_the_user_id() -> None:
    """Recomputable across processes, and not a value the account already exposes."""
    user_id = UserId(new_user_id())

    assert default_candidate_profile_id(user_id) \
        == default_candidate_profile_id(user_id)
    assert str(default_candidate_profile_id(user_id)) != str(user_id)
