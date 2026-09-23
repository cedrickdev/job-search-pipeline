# tests/test_v2_api_matches.py
"""Phase 9 assessment routes, over HTTP: the two axes on the fakes.

The engine tests prove what the match and eligibility engines conclude; the
service test proves the wiring. This proves what a browser meets, which is the
route layer's job: that an anonymous caller is refused, that the owner is the
session's account and never the body, that a pair another account evaluated is the
same 404 as one nobody has, and — the fact the whole phase turns on — that the two
axes travel side by side and neither collapses into the other.

The candidate here is deliberately ineligible but well-matched: not authorised to
work in Switzerland (a candidate declaration, so a real refusal) yet a native
French speaker in the posting's own city. That pair is the shape the phase order
insists must be representable — a high match beside a blocking verdict — and a
response that let eligibility touch the score could not produce it.
"""
from uuid import UUID

import pytest

from backend.app.domain.candidate import WorkAuthorizationStatus
from backend.app.domain.common import (
    LanguageLevel,
    LanguageProficiency,
    LanguageRequirement,
    Location,
    WorkloadRange,
)
from backend.app.domain.identifiers import OpportunityId
from backend.app.domain.opportunity import WorkplaceMode
from tests.v2_api import PLACEHOLDER_ID, api_harness
from tests.v2_builders import (
    LAUSANNE,
    a_candidate_profile,
    a_work_authorization,
    an_opportunity,
)

# Ids in a band this file owns, so a failure names a value from here.
POSTING = OpportunityId(UUID("00000000-0000-4000-8000-0000000009e1"))
OTHER_POSTING = OpportunityId(UUID("00000000-0000-4000-8000-0000000009e2"))


def _lausanne_posting(**overrides):
    """A Swiss posting in French, in the candidate's own city.

    Percent workload and a required French C1 by default, so the match engine can
    score language and location and the eligibility engine can reason about the
    permit and the language gate.
    """
    fields = {
        "id": POSTING,
        "location": Location(country="CH", region="Vaud", city="Lausanne",
                             point=LAUSANNE, raw="Lausanne, Suisse"),
        "workplace_mode": WorkplaceMode.ON_SITE,
        "workload": WorkloadRange(min_percent=80, max_percent=100),
        "language_requirements": (
            LanguageRequirement(language="fr", minimum_level=LanguageLevel.C1),),
    }
    fields.update(overrides)
    return an_opportunity(**fields)


async def _sign_in_with_profile(api, *, email="candidate@example.com",
                                **profile_overrides):
    """Register an account and give it a default profile the assessment reads.

    Returns the account's `user_id`. The profile is stored with `user_id` set to the
    freshly registered account, because the assessment resolves the candidate from
    the session — a fixture profile owned by the module's default user would never be
    found. `email` is separate from the profile overrides so a second account signs
    in under a distinct address while still getting a full profile.
    """
    signed_in = await api.register(email=email)
    assert signed_in.status_code == 201, signed_in.text
    user_id = signed_in.json()["account"]["id"]
    # The default profile the service loads via `get_default`: French C2, so a
    # French requirement is met, and based in Lausanne so location scores full.
    await api.profiles.upsert(a_candidate_profile(
        user_id=user_id,
        languages=(LanguageProficiency(language="fr", level=LanguageLevel.C2),),
        **profile_overrides))
    return user_id


# --- authentication and ownership ---------------------------------------------

@pytest.mark.asyncio
async def test_the_assessment_routes_all_require_a_session(tmp_path):
    """401 before anything is evaluated, on all three routes."""
    async with api_harness(tmp_path) as api:
        anonymous = (
            ("POST", "/matches/evaluate"),
            ("GET", f"/opportunities/{PLACEHOLDER_ID}/match"),
            ("GET", "/matches"))
        for method, path in anonymous:
            response = await api.client.request(method, api.url(path),
                                                json={} if method == "POST" else None)
            assert response.status_code == 401, path
            assert response.json()["error"] == "not_authenticated", path


@pytest.mark.asyncio
async def test_the_body_names_only_the_opportunity_not_the_owner(tmp_path):
    """A `candidate_profile_id` or `user_id` in the body is a 422, not an override.

    The owner is the session's account; the request has no field for it, and
    `extra="forbid"` turns an attempt to add one into a refusal rather than a
    silently ignored value (docs/ENGINEERING_STANDARDS.md §Security).
    """
    async with api_harness(tmp_path) as api:
        await _sign_in_with_profile(api)
        await api.postings.upsert(_lausanne_posting())

        smuggled = await api.write("POST", "/matches/evaluate", json={
            "opportunity_id": str(POSTING),
            "user_id": "00000000-0000-4000-8000-000000000002"})

        assert smuggled.status_code == 422
        assert smuggled.json()["error"] == "validation_failed"


# --- the evaluate endpoint ----------------------------------------------------

@pytest.mark.asyncio
async def test_evaluate_returns_200_with_both_axes(tmp_path):
    """A POST that assesses a pair is a 200 (not a 201) carrying both verdicts.

    Not a 201 because the two verdicts are keyed on the pair — a re-run replaces,
    it does not create — and the body holds `match` and `eligibility` as two
    independent objects.
    """
    async with api_harness(tmp_path) as api:
        await _sign_in_with_profile(api)
        await api.postings.upsert(_lausanne_posting())

        response = await api.write("POST", "/matches/evaluate",
                                   json={"opportunity_id": str(POSTING)})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["opportunity"]["id"] == str(POSTING)
        assert body["match"] is not None
        assert body["eligibility"] is not None


@pytest.mark.asyncio
async def test_the_two_axes_are_independent_a_high_match_can_be_blocked(tmp_path):
    """The phase's core promise on the wire: eligibility never touches the score.

    An unauthorised-to-work candidate who nonetheless fits well is INELIGIBLE and
    blocking, yet the match score stays high — the two numbers come from two engines
    that never read each other.
    """
    async with api_harness(tmp_path) as api:
        await _sign_in_with_profile(api, work_authorizations=(a_work_authorization(
            country="CH", status=WorkAuthorizationStatus.NOT_AUTHORIZED),))
        await api.postings.upsert(_lausanne_posting())

        response = await api.write("POST", "/matches/evaluate",
                                   json={"opportunity_id": str(POSTING)})

        assert response.status_code == 200, response.text
        body = response.json()
        # Eligibility refuses — a candidate's own declaration may block.
        assert body["eligibility"]["status"] == "INELIGIBLE"
        assert body["eligibility"]["is_blocking"] is True
        # The match is untouched by that refusal: a real, high score survives beside it.
        assert body["match"] is not None
        assert body["match"]["overall_percent"] >= 50
        assert body["match"]["classification"] != "UNKNOWN"


@pytest.mark.asyncio
async def test_a_percentage_is_served_beside_the_canonical_score(tmp_path):
    """Both scales travel: the 0.0–1.0 canonical value and the 0–100 UI integer."""
    async with api_harness(tmp_path) as api:
        await _sign_in_with_profile(api)
        await api.postings.upsert(_lausanne_posting())

        response = await api.write("POST", "/matches/evaluate",
                                   json={"opportunity_id": str(POSTING)})

        match = response.json()["match"]
        assert 0.0 <= match["overall"] <= 1.0
        assert 0 <= match["overall_percent"] <= 100
        assert match["overall_percent"] == round(match["overall"] * 100)


@pytest.mark.asyncio
async def test_evaluate_is_404_when_the_account_has_no_profile(tmp_path):
    """No profile yet sends the user to onboarding, with its own distinct code."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()  # signed in, but no profile saved
        await api.postings.upsert(_lausanne_posting())

        response = await api.write("POST", "/matches/evaluate",
                                   json={"opportunity_id": str(POSTING)})

        assert response.status_code == 404
        assert response.json()["error"] == "candidate_profile_not_found"


@pytest.mark.asyncio
async def test_evaluate_is_404_when_no_posting_is_stored(tmp_path):
    """A distinct code from the missing profile: the frontend acts on each differently."""
    async with api_harness(tmp_path) as api:
        await _sign_in_with_profile(api)

        response = await api.write("POST", "/matches/evaluate",
                                   json={"opportunity_id": str(POSTING)})

        assert response.status_code == 404
        assert response.json()["error"] == "opportunity_not_found"


@pytest.mark.asyncio
async def test_re_evaluating_a_pair_replaces_rather_than_accumulates(tmp_path):
    """Idempotent: two evaluations of one pair leave one match and one eligibility."""
    async with api_harness(tmp_path) as api:
        await _sign_in_with_profile(api)
        await api.postings.upsert(_lausanne_posting())

        first = await api.write("POST", "/matches/evaluate",
                                json={"opportunity_id": str(POSTING)})
        second = await api.write("POST", "/matches/evaluate",
                                 json={"opportunity_id": str(POSTING)})

        assert first.status_code == second.status_code == 200
        assert len(api.matches.evaluations) == 1
        assert len(api.eligibilities.results) == 1


# --- the single-pair read -----------------------------------------------------

@pytest.mark.asyncio
async def test_reading_an_unevaluated_pair_is_404(tmp_path):
    """A pure read before any evaluation is `match_not_evaluated`, not an engine run."""
    async with api_harness(tmp_path) as api:
        await _sign_in_with_profile(api)
        await api.postings.upsert(_lausanne_posting())

        response = await api.read(f"/opportunities/{POSTING}/match")

        assert response.status_code == 404
        assert response.json()["error"] == "match_not_evaluated"
        # A read never writes: nothing was stored on the way to the 404.
        assert api.eligibilities.results == {}
        assert api.matches.evaluations == {}


@pytest.mark.asyncio
async def test_reading_after_evaluating_returns_the_stored_assessment(tmp_path):
    """Once evaluated, the pure read returns the same pair on both axes."""
    async with api_harness(tmp_path) as api:
        await _sign_in_with_profile(api)
        await api.postings.upsert(_lausanne_posting())
        await api.write("POST", "/matches/evaluate",
                        json={"opportunity_id": str(POSTING)})

        response = await api.read(f"/opportunities/{POSTING}/match")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["opportunity"]["id"] == str(POSTING)
        assert body["match"] is not None
        assert body["eligibility"] is not None


# --- cross-user isolation -----------------------------------------------------

@pytest.mark.asyncio
async def test_a_pair_another_account_evaluated_is_an_indistinguishable_404(tmp_path):
    """One account's verdict is invisible to another — the same 404 as never-evaluated.

    The first account evaluates a posting; a second account, signed in with its own
    profile, reads the same posting and gets the identical `match_not_evaluated`. A
    stored verdict that is not yours reads as absent, so an id cannot be probed to
    learn that someone else assessed it.
    """
    async with api_harness(tmp_path) as api:
        # First account: evaluate the shared posting.
        await _sign_in_with_profile(api)
        await api.postings.upsert(_lausanne_posting())
        evaluated = await api.write("POST", "/matches/evaluate",
                                    json={"opportunity_id": str(POSTING)})
        assert evaluated.status_code == 200, evaluated.text
        await api.write("POST", "/auth/logout")

        # Second account: its own profile, reading the same posting.
        await _sign_in_with_profile(api, email="second@example.com")
        theirs = await api.read(f"/opportunities/{POSTING}/match")

        assert theirs.status_code == 404
        assert theirs.json()["error"] == "match_not_evaluated"


# --- the list ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_list_returns_only_this_accounts_assessments(tmp_path):
    """`GET /matches` lists what this account evaluated, and nothing another did."""
    async with api_harness(tmp_path) as api:
        await _sign_in_with_profile(api)
        await api.postings.upsert(_lausanne_posting())
        await api.write("POST", "/matches/evaluate",
                        json={"opportunity_id": str(POSTING)})
        await api.write("POST", "/auth/logout")

        await _sign_in_with_profile(api, email="second@example.com")
        listed = await api.read("/matches")

        assert listed.status_code == 200, listed.text
        assert listed.json()["assessments"] == []


@pytest.mark.asyncio
async def test_the_list_carries_both_axes_and_is_not_ranked(tmp_path):
    """Both verdicts travel per item; an INELIGIBLE pair is not pushed down.

    The list is chronological, not ranked by fit — a blocked pair keeps its real
    match score and its place in the order rather than being demoted to the bottom.
    """
    async with api_harness(tmp_path) as api:
        await _sign_in_with_profile(api, work_authorizations=(a_work_authorization(
            country="CH", status=WorkAuthorizationStatus.NOT_AUTHORIZED),))
        await api.postings.upsert(_lausanne_posting())
        await api.postings.upsert(_lausanne_posting(id=OTHER_POSTING))
        await api.write("POST", "/matches/evaluate",
                        json={"opportunity_id": str(POSTING)})
        await api.write("POST", "/matches/evaluate",
                        json={"opportunity_id": str(OTHER_POSTING)})

        listed = await api.read("/matches")

        assert listed.status_code == 200, listed.text
        assessments = listed.json()["assessments"]
        assert len(assessments) == 2
        for item in assessments:
            assert item["eligibility"]["status"] == "INELIGIBLE"
            # The score survives on every item, untouched by the block.
            assert item["match"] is not None
            assert item["match"]["overall_percent"] >= 50
