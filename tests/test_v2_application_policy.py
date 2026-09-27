# tests/test_v2_application_policy.py
"""`ApplicationPolicyService` : les trois éditions ciblées qu'une stratégie approuvée exécute (§26-33).

C'est le service « exécuteur » du bout de l'échine `… → l'utilisateur approuve → un service
existant exécute`, côté politique : l'exécuteur de `backend.app.career.strategy` lui remet un
changement déjà confirmé et il écrit le seul champ concerné, borné au propriétaire. Trois
disciplines portent ces tests :

- **borné au propriétaire (§18-20, §Sécurité)** : chaque méthode charge la politique par
  `(user_id, policy_id)` — la lecture *est* le contrôle d'autorisation, si bien que l'id d'un
  autre compte se lit comme absent et lève `ApplicationPolicyNotFound` au lieu d'écrire ;
- **une seule horloge par requête (§convention V2)** : pas d'horloge au constructeur, chaque
  méthode reçoit `now`, et l'édition estampille `updated_at` sans toucher `created_at` ;
- **le vocabulaire du domaine, pas un réglage réinventé** : le tuple vide reste « aucune
  restriction », `None` reste « illimité »/« aucun plancher » — l'éditeur ne fait qu'écrire.

Tout tourne sur `tests/v2_fakes.py` : pas d'horloge, pas de socket, pas de base.
"""
from datetime import UTC, datetime, timedelta

import pytest

from backend.app.domain.opportunity import OpportunityType
from backend.app.services.application_policy import (
    ApplicationPolicyNotFound,
    ApplicationPolicyService,
)
from tests.v2_builders import OTHER_USER, POLICY, USER, a_policy
from tests.v2_fakes import FakeApplicationPolicyRepository

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=3)


def build_service() -> tuple[ApplicationPolicyService, FakeApplicationPolicyRepository]:
    policies = FakeApplicationPolicyRepository()
    return ApplicationPolicyService(policies), policies


# --- the read surface -------------------------------------------------------------------


async def test_reads_expose_one_accounts_policies_and_never_anothers() -> None:
    """`policy`/`default_policy`/`list_for_user` are owner-scoped; a foreign id reads as absent."""
    service, policies = build_service()
    await policies.upsert(a_policy(created_at=NOW, updated_at=NOW))

    assert (await service.policy(USER, POLICY)) is not None
    assert (await service.policy(OTHER_USER, POLICY)) is None
    assert (await service.default_policy(USER)) is not None
    assert (await service.default_policy(OTHER_USER)) is None
    assert len(await service.list_for_user(USER)) == 1
    assert await service.list_for_user(OTHER_USER) == ()


# --- the three focused edits ------------------------------------------------------------


async def test_set_allowed_opportunity_types_replaces_the_list_and_stamps_updated_at() -> None:
    """Application authority is rewritten owner-scoped; `created_at` survives, `updated_at` moves."""
    service, policies = build_service()
    await policies.upsert(a_policy(
        allowed_opportunity_types=(OpportunityType.INTERNSHIP,),
        created_at=NOW, updated_at=NOW))

    edited = await service.set_allowed_opportunity_types(
        USER, POLICY,
        allowed_opportunity_types=(OpportunityType.FULL_TIME, OpportunityType.FREELANCE),
        now=LATER)

    assert edited.allowed_opportunity_types == (
        OpportunityType.FULL_TIME, OpportunityType.FREELANCE)
    assert edited.created_at == NOW and edited.updated_at == LATER
    # Persisted, not just returned.
    stored = await service.policy(USER, POLICY)
    assert stored is not None
    assert stored.allowed_opportunity_types == (
        OpportunityType.FULL_TIME, OpportunityType.FREELANCE)


async def test_set_allowed_opportunity_types_empty_tuple_is_no_restriction() -> None:
    """The domain's convention holds: an empty allow-list restricts nothing (any type)."""
    service, policies = build_service()
    await policies.upsert(a_policy(
        allowed_opportunity_types=(OpportunityType.INTERNSHIP,),
        created_at=NOW, updated_at=NOW))

    edited = await service.set_allowed_opportunity_types(
        USER, POLICY, allowed_opportunity_types=(), now=LATER)

    assert edited.allowed_opportunity_types == ()
    assert edited.allows_opportunity_type(OpportunityType.FULL_TIME)


async def test_set_application_volume_writes_both_caps_together() -> None:
    """The daily and weekly caps are one edit, so their coherence rule is stamped atomically."""
    service, policies = build_service()
    await policies.upsert(a_policy(
        max_applications_per_day=5, max_applications_per_week=20,
        created_at=NOW, updated_at=NOW))

    edited = await service.set_application_volume(
        USER, POLICY, max_applications_per_day=10, max_applications_per_week=40, now=LATER)

    assert edited.max_applications_per_day == 10
    assert edited.max_applications_per_week == 40
    assert edited.updated_at == LATER


async def test_set_application_volume_none_is_unlimited() -> None:
    """`None` is the real, dangerous "no cap" value — the sensitive lift the proposal guards."""
    service, policies = build_service()
    await policies.upsert(a_policy(
        max_applications_per_day=5, max_applications_per_week=20,
        created_at=NOW, updated_at=NOW))

    edited = await service.set_application_volume(
        USER, POLICY, max_applications_per_day=None, max_applications_per_week=None, now=LATER)

    assert edited.max_applications_per_day is None
    assert edited.max_applications_per_week is None
    assert edited.remaining_submissions(submitted_today=1000, submitted_this_week=5000) is None


async def test_set_minimum_score_sets_and_clears_the_floor() -> None:
    """A floor is written owner-scoped; `None` removes it — the least selective, guarded direction."""
    service, policies = build_service()
    await policies.upsert(a_policy(minimum_overall_score=0.8, created_at=NOW, updated_at=NOW))

    tightened = await service.set_minimum_score(
        USER, POLICY, minimum_overall_score=0.9, now=LATER)
    assert tightened.minimum_overall_score == 0.9

    cleared = await service.set_minimum_score(
        USER, POLICY, minimum_overall_score=None, now=LATER)
    assert cleared.minimum_overall_score is None


# --- the ownership boundary: the load is the authorization check ------------------------


async def test_editing_a_foreign_policy_raises_not_found_and_writes_nothing() -> None:
    """Another account's id reads as absent, so every setter refuses rather than crossing owners."""
    service, policies = build_service()
    await policies.upsert(a_policy(
        allowed_opportunity_types=(OpportunityType.INTERNSHIP,),
        minimum_overall_score=0.5, created_at=NOW, updated_at=NOW))

    with pytest.raises(ApplicationPolicyNotFound):
        await service.set_allowed_opportunity_types(
            OTHER_USER, POLICY, allowed_opportunity_types=(), now=LATER)
    with pytest.raises(ApplicationPolicyNotFound):
        await service.set_application_volume(
            OTHER_USER, POLICY, max_applications_per_day=None,
            max_applications_per_week=None, now=LATER)
    with pytest.raises(ApplicationPolicyNotFound):
        await service.set_minimum_score(
            OTHER_USER, POLICY, minimum_overall_score=None, now=LATER)

    # The owner's policy is untouched — no cross-owner write slipped through.
    stored = await service.policy(USER, POLICY)
    assert stored is not None
    assert stored.allowed_opportunity_types == (OpportunityType.INTERNSHIP,)
    assert stored.minimum_overall_score == 0.5
    assert stored.updated_at == NOW


async def test_editing_an_absent_policy_raises_not_found() -> None:
    """No such id for this account — the same refusal as a foreign one, deliberately indistinguishable."""
    service, _ = build_service()

    with pytest.raises(ApplicationPolicyNotFound):
        await service.set_minimum_score(USER, POLICY, minimum_overall_score=0.5, now=LATER)
