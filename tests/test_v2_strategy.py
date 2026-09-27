# tests/test_v2_strategy.py
"""`StrategyProposalService` : le dernier maillon de l'échine — « l'utilisateur approuve → un service existant exécute » (§34-45).

C'est le seul maillon qui a le droit de *changer* la stratégie, et même ici rien ne bouge sans
un acte humain explicite. Ces tests exercent les deux opérations et la discipline qui les entoure :

- **`propose`** lit la cible une fois, bornée au propriétaire, pour capturer `target_version`
  (l'`updated_at` du moment) — la précondition que `approve` revalidera — et refuse une cible
  absente (`STRATEGY_TARGET_NOT_FOUND`) ;
- **`approve`** est l'exécuteur gardé : il charge la proposition bornée au propriétaire, court-
  circuite sur une exécution déjà enregistrée (idempotent par l'id dérivé de la proposition),
  refuse une proposition close ou périmée, exige une seconde confirmation pour un changement
  qui desserre un frein (`SENSITIVE_CONFIRMATION_REQUIRED`), revalide la cible vive (refus
  audité `REJECTED` si elle a disparu ou dérivé), puis n'applique le changement qu'à travers le
  service qui en est propriétaire — `OnboardingService` pour une recherche,
  `ApplicationPolicyService` pour une politique — en enregistrant un audit `SUCCEEDED`/`FAILED`.

Tout tourne sur `tests/v2_fakes.py` : pas d'horloge, pas de socket, pas de base. Les services
d'exécution sont les vrais, adossés à des fakes, pour prouver que l'approbation écrit bien la
cible.
"""
from datetime import UTC, datetime, timedelta

import pytest

from backend.app.career.errors import CareerError, CareerErrorCode
from backend.app.career.strategy import (
    _UNEXPECTED_FAILURE_DETAIL,
    DEFAULT_PROPOSAL_TTL,
    StrategyProposalService,
)
from backend.app.domain.strategy_change import (
    SetMinimumScoreChange,
    SetSearchKeywordsChange,
    StrategyChangeExecutionOutcome,
    StrategyChangeProposalStatus,
)
from backend.app.services.application_policy import ApplicationPolicyService
from backend.app.services.onboarding import OnboardingService
from tests.v2_builders import (
    OTHER_USER,
    POLICY,
    SEARCH_PROFILE,
    USER,
    a_policy,
    a_search_profile,
)
from tests.v2_fakes import (
    FakeApplicationPolicyRepository,
    FakeCandidateProfileRepository,
    FakeSearchProfileRepository,
    FakeStrategyChangeExecutionRepository,
    FakeStrategyChangeProposalRepository,
    FakeUserRepository,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
DRIFT = NOW + timedelta(minutes=30)


def build_service() -> tuple[
        StrategyProposalService, FakeSearchProfileRepository,
        FakeApplicationPolicyRepository]:
    """The service wired to the *real* editing services, each over a fake store.

    Returns the search and policy stores too, so a test can seed a target and assert the
    approved change actually reached it.
    """
    searches = FakeSearchProfileRepository()
    policies = FakeApplicationPolicyRepository()
    onboarding = OnboardingService(
        FakeCandidateProfileRepository(), searches, FakeUserRepository())
    service = StrategyProposalService(
        FakeStrategyChangeProposalRepository(),
        FakeStrategyChangeExecutionRepository(),
        onboarding,
        ApplicationPolicyService(policies))
    return service, searches, policies


def a_keywords_change(*, title: tuple[str, ...] = ("python", "rust")) -> SetSearchKeywordsChange:
    """A non-sensitive search edit routed to `OnboardingService.set_search_keywords`."""
    return SetSearchKeywordsChange(
        search_profile_id=SEARCH_PROFILE,
        title_keywords=title, before_title_keywords=(),
        excluded_keywords=(), before_excluded_keywords=())


def a_score_change(*, after: float | None, before: float | None) -> SetMinimumScoreChange:
    """A policy edit routed to `ApplicationPolicyService.set_minimum_score`.

    Sensitive when it lowers or removes the floor (`after < before`, or `after is None`).
    """
    return SetMinimumScoreChange(
        application_policy_id=POLICY,
        minimum_overall_score=after, before_minimum_overall_score=before)


# --- propose ---------------------------------------------------------------------------


async def test_propose_captures_the_live_target_version_and_writes_a_proposed_proposal() -> None:
    """`propose` reads the live target once for `target_version`; the proposal is born open."""
    service, searches, _ = build_service()
    await searches.upsert(a_search_profile(updated_at=NOW))

    proposal = await service.propose(
        USER, a_keywords_change(), summary="Cibler python et rust.", now=NOW)

    assert proposal.status is StrategyChangeProposalStatus.PROPOSED
    assert proposal.is_open
    assert proposal.target_version == NOW
    assert proposal.expires_at == NOW + DEFAULT_PROPOSAL_TTL
    # Readable back, owner-scoped — another account cannot see it.
    stored = await service.proposal(USER, proposal.id)
    assert stored is not None and stored.id == proposal.id
    assert await service.proposal(OTHER_USER, proposal.id) is None


async def test_propose_refuses_a_target_that_does_not_exist() -> None:
    """There is nothing to draft against, so an absent target raises rather than writing."""
    service, _, _ = build_service()

    with pytest.raises(CareerError) as err:
        await service.propose(
            USER, a_keywords_change(), summary="Cibler python.", now=NOW)
    assert err.value.code is CareerErrorCode.STRATEGY_TARGET_NOT_FOUND


# --- approve: the happy paths through each owning service ------------------------------


async def test_approve_applies_a_search_edit_through_onboarding_and_audits_success() -> None:
    """A confirmed search change reaches `OnboardingService`; the audit is `SUCCEEDED`."""
    service, searches, _ = build_service()
    await searches.upsert(a_search_profile(updated_at=NOW))
    proposal = await service.propose(
        USER, a_keywords_change(), summary="Cibler python et rust.", now=NOW)

    execution = await service.approve(USER, proposal.id, now=LATER)

    assert execution.outcome is StrategyChangeExecutionOutcome.SUCCEEDED
    assert execution.succeeded
    assert execution.result_ref == LATER.isoformat()
    # The proposal moved to EXECUTED, and the search actually carries the new keywords.
    moved = await service.proposal(USER, proposal.id)
    assert moved is not None and moved.status is StrategyChangeProposalStatus.EXECUTED
    search = await searches.get(USER, SEARCH_PROFILE)
    assert search is not None
    assert search.title_keywords == ("python", "rust")
    assert search.updated_at == LATER


async def test_approve_applies_a_policy_edit_through_the_policy_service() -> None:
    """A non-sensitive policy change (raising the floor) reaches `ApplicationPolicyService`."""
    service, _, policies = build_service()
    await policies.upsert(a_policy(minimum_overall_score=0.8, updated_at=NOW))
    proposal = await service.propose(
        USER, a_score_change(after=0.9, before=0.8),
        summary="Relever le plancher de score.", now=NOW)

    execution = await service.approve(USER, proposal.id, now=LATER)

    assert execution.outcome is StrategyChangeExecutionOutcome.SUCCEEDED
    policy = await policies.get(USER, POLICY)
    assert policy is not None and policy.minimum_overall_score == 0.9


async def test_approve_is_idempotent_and_never_applies_the_change_twice() -> None:
    """A double-confirm returns the recorded execution unchanged; the target is written once."""
    service, searches, _ = build_service()
    await searches.upsert(a_search_profile(updated_at=NOW))
    proposal = await service.propose(
        USER, a_keywords_change(), summary="Cibler python et rust.", now=NOW)

    first = await service.approve(USER, proposal.id, now=LATER)
    again = await service.approve(USER, proposal.id, now=LATER + timedelta(hours=2))

    assert again.id == first.id
    assert again.created_at == first.created_at == LATER
    # The second approve short-circuits before the service — updated_at stayed at the first.
    search = await searches.get(USER, SEARCH_PROFILE)
    assert search is not None and search.updated_at == LATER


# --- approve: the guarded refusals -----------------------------------------------------


async def test_approve_refuses_another_accounts_proposal_as_not_found() -> None:
    """The load is owner-scoped, so a foreign confirmation reads as absent — no enumeration."""
    service, searches, _ = build_service()
    await searches.upsert(a_search_profile(updated_at=NOW))
    proposal = await service.propose(
        USER, a_keywords_change(), summary="Cibler python.", now=NOW)

    with pytest.raises(CareerError) as err:
        await service.approve(OTHER_USER, proposal.id, now=LATER)
    assert err.value.code is CareerErrorCode.PROPOSAL_NOT_FOUND
    # The owner's proposal is untouched.
    assert (await service.execution(USER, proposal.id)) is None


async def test_approve_refuses_a_proposal_no_longer_open() -> None:
    """A dismissed proposal has no execution, so approve refuses with `PROPOSAL_NOT_OPEN`."""
    service, searches, _ = build_service()
    await searches.upsert(a_search_profile(updated_at=NOW))
    proposal = await service.propose(
        USER, a_keywords_change(), summary="Cibler python.", now=NOW)
    await service.dismiss(USER, proposal.id, now=LATER)

    with pytest.raises(CareerError) as err:
        await service.approve(USER, proposal.id, now=LATER)
    assert err.value.code is CareerErrorCode.PROPOSAL_NOT_OPEN


async def test_approve_lapses_and_refuses_an_expired_proposal() -> None:
    """Past `expires_at`, approve marks the proposal `EXPIRED` and refuses — no execution."""
    service, searches, _ = build_service()
    await searches.upsert(a_search_profile(updated_at=NOW))
    proposal = await service.propose(
        USER, a_keywords_change(), summary="Cibler python.", now=NOW)
    past_expiry = NOW + DEFAULT_PROPOSAL_TTL + timedelta(days=1)

    with pytest.raises(CareerError) as err:
        await service.approve(USER, proposal.id, now=past_expiry)
    assert err.value.code is CareerErrorCode.PROPOSAL_EXPIRED
    moved = await service.proposal(USER, proposal.id)
    assert moved is not None and moved.status is StrategyChangeProposalStatus.EXPIRED
    assert (await service.execution(USER, proposal.id)) is None


async def test_approve_demands_a_second_confirmation_for_a_sensitive_change() -> None:
    """Lowering the score floor loosens a brake: refused until `confirm_sensitive`, then applied."""
    service, _, policies = build_service()
    await policies.upsert(a_policy(minimum_overall_score=0.8, updated_at=NOW))
    proposal = await service.propose(
        USER, a_score_change(after=0.5, before=0.8),
        summary="Abaisser le plancher de score.", now=NOW)

    with pytest.raises(CareerError) as err:
        await service.approve(USER, proposal.id, now=LATER)
    assert err.value.code is CareerErrorCode.SENSITIVE_CONFIRMATION_REQUIRED
    # The proposal stays open and nothing was written — the brake held.
    still_open = await service.proposal(USER, proposal.id)
    assert still_open is not None and still_open.is_open
    assert (await service.execution(USER, proposal.id)) is None
    untouched = await policies.get(USER, POLICY)
    assert untouched is not None and untouched.minimum_overall_score == 0.8

    # A second, explicit confirmation proceeds.
    execution = await service.approve(
        USER, proposal.id, now=LATER, confirm_sensitive=True)
    assert execution.outcome is StrategyChangeExecutionOutcome.SUCCEEDED
    lowered = await policies.get(USER, POLICY)
    assert lowered is not None and lowered.minimum_overall_score == 0.5


# --- approve: the version precondition, audited as REJECTED ----------------------------


async def test_approve_records_a_rejection_when_the_target_vanished() -> None:
    """A target deleted between drafting and approval is a `REJECTED` audit, then a raise."""
    service, searches, _ = build_service()
    await searches.upsert(a_search_profile(updated_at=NOW))
    proposal = await service.propose(
        USER, a_keywords_change(), summary="Cibler python.", now=NOW)
    await searches.delete(USER, SEARCH_PROFILE)  # the search is gone at approval time

    with pytest.raises(CareerError) as err:
        await service.approve(USER, proposal.id, now=LATER)
    assert err.value.code is CareerErrorCode.STRATEGY_TARGET_NOT_FOUND
    execution = await service.execution(USER, proposal.id)
    assert execution is not None
    assert execution.outcome is StrategyChangeExecutionOutcome.REJECTED
    assert execution.observed_target_version is None
    moved = await service.proposal(USER, proposal.id)
    assert moved is not None and moved.status is StrategyChangeProposalStatus.REJECTED


async def test_approve_records_a_rejection_when_the_target_drifted() -> None:
    """A target edited since drafting fails the version precondition — a `REJECTED` audit."""
    service, searches, _ = build_service()
    await searches.upsert(a_search_profile(updated_at=NOW))
    proposal = await service.propose(
        USER, a_keywords_change(), summary="Cibler python.", now=NOW)
    # The user edits the search after drafting: its updated_at moves on.
    existing = await searches.get(USER, SEARCH_PROFILE)
    assert existing is not None
    await searches.upsert(existing.model_copy(update={"updated_at": DRIFT}))

    with pytest.raises(CareerError) as err:
        await service.approve(USER, proposal.id, now=LATER)
    assert err.value.code is CareerErrorCode.STRATEGY_PROPOSAL_STALE
    execution = await service.execution(USER, proposal.id)
    assert execution is not None
    assert execution.outcome is StrategyChangeExecutionOutcome.REJECTED
    assert execution.observed_target_version == DRIFT
    moved = await service.proposal(USER, proposal.id)
    assert moved is not None and moved.status is StrategyChangeProposalStatus.REJECTED


async def test_approve_audits_a_failure_without_leaking_the_services_error() -> None:
    """A service that raises after re-validation is a `FAILED` audit with a secret-free detail."""
    searches = FakeSearchProfileRepository()

    class _BoomOnboarding(OnboardingService):
        async def set_search_keywords(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("boom: postgresql://user:s3cret@db.internal/prod")

    onboarding = _BoomOnboarding(
        FakeCandidateProfileRepository(), searches, FakeUserRepository())
    service = StrategyProposalService(
        FakeStrategyChangeProposalRepository(),
        FakeStrategyChangeExecutionRepository(),
        onboarding,
        ApplicationPolicyService(FakeApplicationPolicyRepository()))
    await searches.upsert(a_search_profile(updated_at=NOW))
    proposal = await service.propose(
        USER, a_keywords_change(), summary="Cibler python.", now=NOW)

    execution = await service.approve(USER, proposal.id, now=LATER)

    assert execution.outcome is StrategyChangeExecutionOutcome.FAILED
    assert execution.detail == _UNEXPECTED_FAILURE_DETAIL
    assert "s3cret" not in (execution.detail or "")
    assert execution.result_ref is None
    moved = await service.proposal(USER, proposal.id)
    assert moved is not None and moved.status is StrategyChangeProposalStatus.FAILED


# --- dismiss and the expiry sweep ------------------------------------------------------


async def test_dismiss_declines_an_open_proposal_without_executing() -> None:
    """`dismiss` advances the status and writes no execution — nothing was attempted."""
    service, searches, _ = build_service()
    await searches.upsert(a_search_profile(updated_at=NOW))
    proposal = await service.propose(
        USER, a_keywords_change(), summary="Cibler python.", now=NOW)

    dismissed = await service.dismiss(USER, proposal.id, now=LATER)

    assert dismissed.status is StrategyChangeProposalStatus.DISMISSED
    assert (await service.execution(USER, proposal.id)) is None
    assert await service.pending(USER) == ()  # it left the open set

    # A second dismiss refuses rather than moving it again.
    with pytest.raises(CareerError) as err:
        await service.dismiss(USER, proposal.id, now=LATER)
    assert err.value.code is CareerErrorCode.PROPOSAL_NOT_OPEN


async def test_dismiss_refuses_another_accounts_proposal() -> None:
    """Owner-scoped like approve: a foreign id reads as absent."""
    service, searches, _ = build_service()
    await searches.upsert(a_search_profile(updated_at=NOW))
    proposal = await service.propose(
        USER, a_keywords_change(), summary="Cibler python.", now=NOW)

    with pytest.raises(CareerError) as err:
        await service.dismiss(OTHER_USER, proposal.id, now=LATER)
    assert err.value.code is CareerErrorCode.PROPOSAL_NOT_FOUND


async def test_expire_due_lapses_only_past_proposals_and_is_idempotent() -> None:
    """The sweep marks expired open proposals `EXPIRED`; a second pass moves nothing."""
    service, searches, _ = build_service()
    await searches.upsert(a_search_profile(updated_at=NOW))
    proposal = await service.propose(
        USER, a_keywords_change(), summary="Cibler python.", now=NOW)
    past_expiry = NOW + DEFAULT_PROPOSAL_TTL + timedelta(days=1)

    # Not yet due — nothing lapses.
    assert await service.expire_due(USER, now=LATER) == ()

    lapsed = await service.expire_due(USER, now=past_expiry)
    assert len(lapsed) == 1
    assert lapsed[0].id == proposal.id
    assert lapsed[0].status is StrategyChangeProposalStatus.EXPIRED

    # Idempotent: the second sweep finds it no longer open.
    assert await service.expire_due(USER, now=past_expiry + timedelta(days=1)) == ()


async def test_pending_shows_only_open_while_history_shows_every_status() -> None:
    """`pending` is the review queue (open only); `history` is the whole record."""
    service, searches, _ = build_service()
    await searches.upsert(a_search_profile(updated_at=NOW))
    kept = await service.propose(
        USER, a_keywords_change(title=("python",)), summary="Cibler python.", now=NOW)
    dropped = await service.propose(
        USER, a_keywords_change(title=("rust",)), summary="Cibler rust.", now=NOW)
    await service.dismiss(USER, dropped.id, now=LATER)

    pending_ids = {p.id for p in await service.pending(USER)}
    history_ids = {p.id for p in await service.history(USER)}

    assert pending_ids == {kept.id}
    assert history_ids == {kept.id, dropped.id}
    # And it is all owner-scoped.
    assert await service.pending(OTHER_USER) == ()
    assert await service.history(OTHER_USER) == ()





