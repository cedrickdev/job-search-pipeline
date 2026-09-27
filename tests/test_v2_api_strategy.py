# tests/test_v2_api_strategy.py
"""`/api/v2/career/strategy-proposals` — l'unique mutation de l'échine, gardée derrière un humain.

Les tests de service (`test_v2_strategy.py`) tranchent le cycle de vie ; celui-ci tient la couche
route et son câblage. La règle porteuse de la phase se lit au fil : une proposition est une
description typée et bornée d'un changement, elle ne bouge rien tant que l'utilisateur ne confirme
pas, et un changement qui desserre un frein de sécurité (abaisser le plancher de score, par
exemple) est refusé par `SENSITIVE_CONFIRMATION_REQUIRED` tant que le second acquittement n'est pas
donné — « le système n'élargit jamais silencieusement la politique de candidature » rendu en un
statut qu'une surface doit gérer (§41-43).

Le propriétaire n'est jamais dans le chemin ni dans le corps : c'est le compte résolu depuis la
session. Une proposition étrangère ou manquante se lit *absente* (404 `proposal_not_found`).
L'approbation revalide la cible vive et l'applique par le *même* service qui possède l'édition —
jamais en écrivant une recherche ou une politique ici. Tout tourne sur les fakes.
"""
from datetime import UTC, datetime

import pytest

from tests.v2_api import api_harness
from tests.v2_builders import POLICY, SEARCH_PROFILE, a_policy, a_search_profile

pytestmark = pytest.mark.asyncio

SEEDED = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
GHOST = "00000000-0000-4000-8000-0000000000ff"


async def _seed_search(api, *, updated_at=SEEDED):
    """Seed a saved search owned by the signed-in account; return its id (the change's target)."""
    user_id = next(iter(api.users.users))
    await api.searches.upsert(a_search_profile(
        user_id=user_id, created_at=updated_at, updated_at=updated_at))
    return SEARCH_PROFILE


async def _seed_policy(api, *, minimum_overall_score, updated_at=SEEDED):
    """Seed an application policy owned by the signed-in account; return its id."""
    user_id = next(iter(api.users.users))
    await api.application_policies.upsert(a_policy(
        user_id=user_id, minimum_overall_score=minimum_overall_score,
        created_at=updated_at, updated_at=updated_at))
    return POLICY


def _keywords_change(search_id, *, title=("python", "rust")):
    """A non-sensitive search edit routed to `OnboardingService.set_search_keywords`."""
    return {
        "kind": "SET_SEARCH_KEYWORDS", "search_profile_id": str(search_id),
        "title_keywords": list(title), "before_title_keywords": [],
        "excluded_keywords": [], "before_excluded_keywords": []}


def _score_change(policy_id, *, after, before):
    """A policy edit routed to `ApplicationPolicyService.set_minimum_score`.

    Sensitive when it lowers or removes the floor — `after < before`, or `after is None`.
    """
    return {"kind": "SET_MINIMUM_SCORE", "application_policy_id": str(policy_id),
            "minimum_overall_score": after, "before_minimum_overall_score": before}


async def _propose(api, change, *, summary="Un changement de stratégie."):
    """POST a draft; the response is returned unasserted for a status check."""
    return await api.write("POST", "/career/strategy-proposals",
                           json={"change": change, "summary": summary})


# --- proposing: a bounded diff, never the raw levers -------------------------

async def test_proposing_a_search_change_is_201_and_shows_the_diff_not_the_levers(tmp_path):
    """A draft is a 201: it carries the before → after diff and its sensitivity, not the payload."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        search_id = await _seed_search(api)
        response = await _propose(api, _keywords_change(search_id))
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["change_kind"] == "SET_SEARCH_KEYWORDS"
        assert body["target"] == "SEARCH_PROFILE"
        assert body["target_id"] == str(search_id)
        assert body["status"] == "PROPOSED"
        assert body["is_open"] is True
        assert body["is_sensitive"] is False  # tightening keywords loosens no brake
        assert len(body["field_changes"]) >= 1  # the fields that move, rendered before → after
        # A surface renders the diff, not the typed levers — the raw change is never echoed back.
        assert "change" not in body
        assert "user_id" not in body

async def test_proposing_against_a_missing_target_is_409(tmp_path):
    """The live target is read once to capture its version; an absent one is a 409, not a 404."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        response = await _propose(api, _keywords_change(SEARCH_PROFILE))  # nothing seeded
        assert response.status_code == 409
        assert response.json()["error"] == "strategy_target_not_found"

async def test_an_unknown_change_kind_is_refused_at_the_boundary(tmp_path):
    """A `kind` the platform does not offer is a 422 at the union, never a silently widened match."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await _seed_search(api)
        response = await _propose(api, {"kind": "SET_SEARCH_TELEPORT",
                                        "search_profile_id": str(SEARCH_PROFILE)})
        assert response.status_code == 422


# --- the queues, and the read that pairs a proposal with its execution -------

async def test_history_resolves_before_the_id_route_and_holds_every_status(tmp_path):
    """`/history` is a literal, not a proposal id, and it keeps dismissed drafts the queue drops.

    A 200 carrying a `proposals` list proves the literal is not parsed as a UUID (a 422). The
    pending queue holds only the still-open draft; history holds it and the dismissed one both.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        search_id = await _seed_search(api)
        dismissed = (await _propose(api, _keywords_change(search_id))).json()["id"]
        await api.write("POST", f"/career/strategy-proposals/{dismissed}/dismiss", json={})
        await _propose(api, _keywords_change(search_id, title=("go",)))  # a second, still open
        pending = await api.read("/career/strategy-proposals")
        assert pending.status_code == 200, pending.text
        assert [p["status"] for p in pending.json()["proposals"]] == ["PROPOSED"]
        history = await api.read("/career/strategy-proposals/history")
        assert history.status_code == 200, history.text
        assert {p["status"] for p in history.json()["proposals"]} == {"PROPOSED", "DISMISSED"}

async def test_reading_an_open_proposal_carries_a_null_execution(tmp_path):
    """A `PROPOSED` proposal has been acted on by nothing, so its paired execution is null."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        search_id = await _seed_search(api)
        pid = (await _propose(api, _keywords_change(search_id))).json()["id"]
        response = await api.read(f"/career/strategy-proposals/{pid}")
        assert response.status_code == 200, response.text
        assert response.json()["proposal"]["id"] == pid
        assert response.json()["execution"] is None

async def test_reading_a_missing_proposal_is_404(tmp_path):
    """Missing and foreign read the same — absent — so a proposal id cannot be probed."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        response = await api.read(f"/career/strategy-proposals/{GHOST}")
        assert response.status_code == 404
        assert response.json()["error"] == "proposal_not_found"


# --- approving: applied through the owning service, gated on sensitivity -----

async def test_approving_applies_the_search_change_through_its_owning_service(tmp_path):
    """Approval hands the confirmed change to the service that owns the edit — the search moves."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = next(iter(api.users.users))
        search_id = await _seed_search(api)
        pid = (await _propose(api, _keywords_change(search_id))).json()["id"]
        approved = await api.write(
            "POST", f"/career/strategy-proposals/{pid}/approve", json={})
        assert approved.status_code == 200, approved.text
        assert approved.json()["outcome"] == "SUCCEEDED"
        assert approved.json()["succeeded"] is True
        assert approved.json()["proposal_id"] == pid
        # The edit landed on the live target, written by the onboarding service, not by this route.
        stored = await api.searches.get(user_id, search_id)
        assert stored is not None
        assert stored.title_keywords == ("python", "rust")
        # The proposal is now closed and its detail carries the execution audit.
        detail = await api.read(f"/career/strategy-proposals/{pid}")
        assert detail.json()["proposal"]["status"] == "EXECUTED"
        assert detail.json()["execution"]["outcome"] == "SUCCEEDED"

async def test_approving_is_idempotent_on_the_execution_id(tmp_path):
    """A double-confirm returns the recorded execution rather than applying the change twice."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        search_id = await _seed_search(api)
        pid = (await _propose(api, _keywords_change(search_id))).json()["id"]
        first = await api.write("POST", f"/career/strategy-proposals/{pid}/approve", json={})
        second = await api.write("POST", f"/career/strategy-proposals/{pid}/approve", json={})
        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"]


# --- the acceptance rule: a loosening edit costs a second, deliberate yes -----

async def test_a_loosening_change_needs_a_second_confirmation(tmp_path):
    """THE acceptance rule at the wire: the platform never silently widens the application policy.

    Lowering the score floor loosens a safety brake, so the proposal is born `is_sensitive` and a
    plain approval is refused with `SENSITIVE_CONFIRMATION_REQUIRED` — the policy is left untouched
    and the proposal stays open. Only a second, deliberate `confirm_sensitive` applies it (§41-43).
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = next(iter(api.users.users))
        policy_id = await _seed_policy(api, minimum_overall_score=0.8)
        drafted = await _propose(api, _score_change(policy_id, after=0.5, before=0.8))
        pid = drafted.json()["id"]
        assert drafted.json()["is_sensitive"] is True
        refused = await api.write(
            "POST", f"/career/strategy-proposals/{pid}/approve", json={})
        assert refused.status_code == 409
        assert refused.json()["error"] == "sensitive_confirmation_required"
        # The brake did not move, and the proposal is still open for a confirmed retry.
        held = await api.application_policies.get(user_id, policy_id)
        assert held is not None and held.minimum_overall_score == 0.8
        assert (await api.read(f"/career/strategy-proposals/{pid}")).json()[
            "proposal"]["status"] == "PROPOSED"
        # The deliberate second acknowledgement lets it through, and only now does the floor move.
        confirmed = await api.write(
            "POST", f"/career/strategy-proposals/{pid}/approve",
            json={"confirm_sensitive": True})
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["outcome"] == "SUCCEEDED"
        lowered = await api.application_policies.get(user_id, policy_id)
        assert lowered is not None and lowered.minimum_overall_score == 0.5

async def test_dismissing_then_approving_is_409(tmp_path):
    """A dismissed proposal leaves the open set for good — a later approval is a 409, not a run."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        search_id = await _seed_search(api)
        pid = (await _propose(api, _keywords_change(search_id))).json()["id"]
        dismissed = await api.write(
            "POST", f"/career/strategy-proposals/{pid}/dismiss", json={})
        assert dismissed.status_code == 200, dismissed.text
        assert dismissed.json()["status"] == "DISMISSED"
        assert dismissed.json()["is_open"] is False
        approved = await api.write(
            "POST", f"/career/strategy-proposals/{pid}/approve", json={})
        assert approved.status_code == 409
        assert approved.json()["error"] == "proposal_not_open"




