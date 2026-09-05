# tests/test_v2_api_me.py
"""`/api/v2/me` and `/api/v2/onboarding` over HTTP, from two accounts at once.

This is where the Phase 4 cross-user isolation requirement is met. The interesting
property is not that one account cannot *read* another's row — no route takes an
owner — but that a request naming another account's search id gets the same 404 as
one naming an id that never existed, and that the refusal leaves the other
account's data exactly as it was. Both accounts live in the same fake store here,
so a scoping mistake would show up as a passing write rather than as a 404.

The rest of the module pins the shapes onboarding depends on: the 404 before a
profile exists, `PUT` replacing in place, `DELETE` refusing the second time, the
`active_only` filter, and `POST /onboarding/complete` re-reading the database
instead of trusting the client.
"""
from datetime import timedelta
from uuid import uuid4

import pytest

from tests.v2_api import (
    EMAIL,
    OTHER_EMAIL,
    PROFILE_BODY,
    SEARCH_BODY,
    api_harness,
)


@pytest.mark.asyncio
async def test_the_profile_is_absent_until_it_is_saved_and_then_replaced_in_place(
        tmp_path):
    """404, then 200, then the same id again — because `PUT` is not `POST`.

    "No profile" and "a profile with nothing filled in" are different screens, so
    the empty case is a 404 with its own code rather than a 200 carrying nulls. The
    second write proves the id is derived from the account: a second row would mean
    an account could accumulate profiles and the frontend would have to choose one.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        missing = await api.read("/me/profile")
        assert missing.status_code == 404
        assert missing.json()["error"] == "candidate_profile_not_found"

        created = await api.write("PUT", "/me/profile", json=PROFILE_BODY)
        assert created.status_code == 200
        assert created.json()["profile"]["headline"] == "Backend engineer"
        assert (await api.read("/me/profile")).json() == created.json()

        api.clock.advance(timedelta(minutes=30))
        replaced = await api.write("PUT", "/me/profile",
                                   json={**PROFILE_BODY, "headline": "Staff engineer"})

        assert replaced.status_code == 200
        assert replaced.json()["id"] == created.json()["id"]
        assert replaced.json()["profile"]["headline"] == "Staff engineer"
        assert replaced.json()["updated_at"] > created.json()["updated_at"]
        assert len(api.profiles.profiles) == 1


@pytest.mark.asyncio
async def test_a_saved_search_is_created_listed_updated_and_deleted_once(tmp_path):
    """The four verbs, and the second `DELETE` answering 404 on purpose.

    A client that deletes the same search twice is working from a stale list, and
    404 tells it so; 204 would let it keep a phantom row on screen. The update keeps
    the id and `created_at`, because `PUT` replaces the contents of one search
    rather than making another.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        created = await api.write("POST", "/me/search-profiles", json=SEARCH_BODY)
        assert created.status_code == 201
        search_id = created.json()["id"]

        listed = await api.read("/me/search-profiles")
        assert listed.status_code == 200
        assert [search["id"] for search in listed.json()["search_profiles"]] == [
            search_id]

        api.clock.advance(timedelta(minutes=5))
        updated = await api.write("PUT", f"/me/search-profiles/{search_id}",
                                  json={**SEARCH_BODY, "name": "Backend in Vaud"})
        assert updated.status_code == 200
        assert updated.json()["id"] == search_id
        assert updated.json()["created_at"] == created.json()["created_at"]
        assert updated.json()["search"]["name"] == "Backend in Vaud"

        deleted = await api.write("DELETE", f"/me/search-profiles/{search_id}")
        assert deleted.status_code == 204
        assert api.searches.searches == {}

        again = await api.write("DELETE", f"/me/search-profiles/{search_id}")
        assert again.status_code == 404
        assert again.json()["error"] == "search_profile_not_found"


@pytest.mark.asyncio
async def test_a_paused_search_is_listed_but_not_when_active_only_is_asked_for(
        tmp_path):
    """Paused is a state, not a deletion, and the discovery run wants the filter.

    `active_only=false` is the default because the settings screen has to show a
    paused search in order to let anybody resume it. The count that decides whether
    onboarding may finish is the *active* one, which is why both numbers exist.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        active = await api.write("POST", "/me/search-profiles", json=SEARCH_BODY)
        paused = await api.write("POST", "/me/search-profiles",
                                 json={**SEARCH_BODY, "name": "Paused",
                                       "is_active": False})
        assert (active.status_code, paused.status_code) == (201, 201)

        everything = await api.read("/me/search-profiles")
        only_active = await api.read("/me/search-profiles",
                                     params={"active_only": "true"})

        assert len(everything.json()["search_profiles"]) == 2
        assert [search["id"] for search in only_active.json()["search_profiles"]] == [
            active.json()["id"]]
        state = (await api.read("/onboarding")).json()
        assert (state["search_profiles"], state["active_search_profiles"]) == (2, 1)


@pytest.mark.asyncio
async def test_a_body_that_tries_to_name_its_own_owner_is_refused(tmp_path):
    """422, because the drafts have no `user_id` field for a request to set.

    The owner comes from the session, and `extra="forbid"` is what makes that
    unbypassable rather than merely conventional: a body carrying `user_id` is
    rejected outright instead of having the key quietly dropped, which is the
    failure mode that would matter if a draft ever grew a field with that name.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        someone_else = str(uuid4())

        for path, body in (("/me/profile", PROFILE_BODY),
                           ("/me/search-profiles", SEARCH_BODY)):
            method = "PUT" if path == "/me/profile" else "POST"
            response = await api.write(method, path,
                                       json={**body, "user_id": someone_else})

            assert response.status_code == 422, path
            assert response.json()["error"] == "validation_failed"
            assert [error["loc"] for error in response.json()["errors"]] == [
                ["body", "user_id"]]
            assert someone_else not in response.text

        assert api.profiles.profiles == {}
        assert api.searches.searches == {}


@pytest.mark.asyncio
async def test_another_accounts_search_is_a_404_and_survives_the_attempt(tmp_path):
    """Cross-user isolation, asserted from the store as well as the status.

    Both accounts are in the same fake store, so this fails loudly if the scoping
    ever moves out of the repository: a `PUT` that returned 200 would have rewritten
    another person's saved search, and one that returned 403 would have confirmed
    the id exists — which is why the answer is 404, the same as for an id that never
    existed.

    The last requests are the half a response-only test would miss: A signs back in
    and finds its search byte-for-byte as it was.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in(email=EMAIL)
        theirs = (await api.finish_onboarding()).json()["id"]
        before = dict(api.searches.searches)
        assert (await api.write("POST", "/auth/logout")).status_code == 204

        await api.sign_in(email=OTHER_EMAIL)

        assert (await api.read("/me/profile")).status_code == 404
        assert (await api.read("/me/search-profiles")).json()["search_profiles"] == []
        for method, body in (("PUT", {**SEARCH_BODY, "name": "Hijacked"}),
                             ("DELETE", None)):
            response = await api.write(method, f"/me/search-profiles/{theirs}",
                                       json=body)
            assert response.status_code == 404, method
            assert response.json()["error"] == "search_profile_not_found"

        assert api.searches.searches == before
        assert len(api.users.users) == 2
        assert len(api.profiles.profiles) == 1

        assert (await api.write("POST", "/auth/logout")).status_code == 204
        assert (await api.log_in(email=EMAIL)).status_code == 200
        mine = (await api.read("/me/search-profiles")).json()["search_profiles"]
        assert [search["id"] for search in mine] == [theirs]
        assert mine[0]["search"]["name"] == SEARCH_BODY["name"]


@pytest.mark.asyncio
async def test_onboarding_refuses_to_finish_early_and_says_which_step_is_missing(
        tmp_path):
    """409 with the same two numbers the `GET` reports, so they cannot disagree.

    `POST /complete` takes no body: there is nothing a client could send that would
    make it succeed, because both preconditions are re-read server-side. The middle
    step is the one worth having — a profile and a *paused* search still refuse,
    because a discovery run needs an active one.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        empty = (await api.read("/onboarding")).json()
        assert empty == {"has_profile": False, "search_profiles": 0,
                         "active_search_profiles": 0, "completed_at": None,
                         "is_complete": False, "may_complete": False}
        refused = await api.write("POST", "/onboarding/complete")
        assert refused.status_code == 409
        assert refused.json() == {
            "error": "onboarding_incomplete",
            "detail": "onboarding needs a candidate profile and at least one active "
                      "saved search",
            "has_profile": False, "active_search_profiles": 0}

        await api.write("PUT", "/me/profile", json=PROFILE_BODY)
        await api.write("POST", "/me/search-profiles",
                        json={**SEARCH_BODY, "is_active": False})

        still_refused = await api.write("POST", "/onboarding/complete")
        assert still_refused.status_code == 409
        assert still_refused.json()["has_profile"] is True
        assert still_refused.json()["active_search_profiles"] == 0
        assert (await api.read("/onboarding")).json()["may_complete"] is False
        assert next(iter(api.users.users.values())).onboarding_completed_at is None


@pytest.mark.asyncio
async def test_completing_onboarding_stamps_the_account_and_keeps_the_first_stamp(
        tmp_path):
    """200 with the account, and a second call that does not rewrite history.

    When onboarding finished is a fact about the account, so a double-submitted
    button returns the original timestamp rather than today's. The route answers with
    the account because the client's cached copy of it just went stale — the stamp
    is the field every screen after this one branches on.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await api.finish_onboarding()
        assert (await api.read("/onboarding")).json()["may_complete"] is True

        completed = await api.write("POST", "/onboarding/complete")

        assert completed.status_code == 200
        stamp = completed.json()["onboarding_completed_at"]
        assert stamp is not None
        assert set(completed.json()) == {"id", "email", "display_name", "status",
                                         "onboarding_completed_at", "created_at"}
        state = (await api.read("/onboarding")).json()
        assert (state["is_complete"], state["completed_at"]) == (True, stamp)

        api.clock.advance(timedelta(hours=1))
        resubmitted = await api.write("POST", "/onboarding/complete")

        assert resubmitted.status_code == 200
        assert resubmitted.json()["onboarding_completed_at"] == stamp
        assert (await api.read("/auth/session")
                ).json()["account"]["onboarding_completed_at"] == stamp
