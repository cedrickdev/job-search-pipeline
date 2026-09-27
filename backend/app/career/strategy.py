"""The "user approves → existing service executes" link — proposals and their audited executor.

Last link of the spine `observe → measure → recommend → user approves → existing service
executes`, and the *only* one that may change strategy (§34-45). Every earlier link is
powerless by construction: a report counts, a recommendation suggests with evidence. Here a
typed `StrategyChangeProposal` finally reaches the platform — and even here nothing moves
until a human confirms it, the confirmed change is re-validated against the *live* target, and
it is handed to the **same** application service that already owns that edit (`OnboardingService`
for searches, `ApplicationPolicyService` for policies). This module never writes a
`SearchProfile` or an `ApplicationPolicy` itself.

Two operations, and the second is where all the care lives:

- **propose** — draft a proposal from a typed change and a human summary. It reads the live
  target once to capture `target_version` (its `updated_at` now) as the approval precondition,
  refusing if the target does not exist; it changes nothing else.
- **approve** — the guarded executor. In one fixed order it: loads the proposal owner-scoped
  (`PROPOSAL_NOT_FOUND`); returns the recorded execution if one exists (idempotent by the
  proposal-derived id, so a double-confirm never applies a change twice); refuses a proposal no
  longer open (`PROPOSAL_NOT_OPEN`) or one lapsed past `expires_at` (`PROPOSAL_EXPIRED`);
  demands an explicit second confirmation for a change that loosens a safety brake
  (`SENSITIVE_CONFIRMATION_REQUIRED` — the acceptance rule "never silently expands the user's
  application policy" made into a status); reloads the target and refuses a vanished
  (`STRATEGY_TARGET_NOT_FOUND`) or drifted (`STRATEGY_PROPOSAL_STALE`) one, recording the
  refusal as a `REJECTED` audit; and only then applies the change through the owning service,
  recording a `SUCCEEDED` (or, if the service raised, `FAILED`) audit and moving the proposal
  to match. Every attempt is audited, refusal included, with a secret-free detail.

`dismiss` is the user's other move — decline an open proposal, no execution written — and
`expire_due` is the sweep that lapses unacted proposals. Nothing here reaches a provider or a
browser; it calls typed service methods and records what they return.
"""
from datetime import datetime, timedelta
from typing import assert_never

from backend.app.career.errors import CareerError, CareerErrorCode
from backend.app.domain.identifiers import (
    ApplicationPolicyId,
    CareerRecommendationId,
    LLMRunId,
    SearchProfileId,
    StrategyChangeProposalId,
    UserId,
    new_strategy_change_proposal_id,
    strategy_change_execution_id,
)
from backend.app.domain.policy import ApplicationPolicy
from backend.app.domain.search import SearchProfile
from backend.app.domain.strategy_change import (
    SetApplicationVolumeChange,
    SetMinimumScoreChange,
    SetPolicyOpportunityTypesChange,
    SetSearchKeywordsChange,
    SetSearchOpportunityTypesChange,
    SetSearchRadiusChange,
    SetSearchSourcesChange,
    StrategyChange,
    StrategyChangeExecution,
    StrategyChangeExecutionOutcome,
    StrategyChangeProposal,
    StrategyChangeTarget,
)
from backend.app.repositories.contracts import (
    DEFAULT_LIMIT,
    StrategyChangeExecutionRepository,
    StrategyChangeProposalRepository,
)
from backend.app.services.application_policy import ApplicationPolicyService
from backend.app.services.onboarding import OnboardingService

# How long a drafted proposal stays live before it lapses (§38). Two weeks: long enough that a
# user returning to a suggestion mid-search still finds it open, short enough that a stale
# before/after does not linger against a funnel that has since moved on. `propose` accepts an
# explicit `ttl` for a caller that wants a different window; this is only the default.
DEFAULT_PROPOSAL_TTL = timedelta(days=14)

# The detail recorded when the owning service raised something the executor did not anticipate
# for a change that had already cleared re-validation. Deliberately generic and secret-free:
# an unexpected exception's own text may quote a driver, a URL or a credential, so it is dropped
# rather than surfaced. The re-validation refusals below compose their own domain-owned details.
_UNEXPECTED_FAILURE_DETAIL = (
    "the change was permitted but could not be completed due to an unexpected error")


class StrategyProposalService:
    """Draft strategy proposals, and apply the approved ones through their owning service (§34-45).

    Holds the proposal store and the execution-audit store it writes, and the two application
    services an approved change routes to — `OnboardingService` for a search edit,
    `ApplicationPolicyService` for a policy edit. No clock in the constructor — every method
    takes `now` — the convention every V2 service keeps. The service has no other authority: it
    cannot touch an application, an outcome or a score, and it writes a search or policy only by
    calling the same typed method an HTTP route would.
    """

    def __init__(self, proposals: StrategyChangeProposalRepository,
                 executions: StrategyChangeExecutionRepository,
                 onboarding: OnboardingService,
                 policies: ApplicationPolicyService) -> None:
        self._proposals = proposals
        self._executions = executions
        self._onboarding = onboarding
        self._policies = policies

    # --- read surfaces -----------------------------------------------------

    async def proposal(self, user_id: UserId,
                       proposal_id: StrategyChangeProposalId) -> StrategyChangeProposal | None:
        """One of this account's proposals by id, or `None` (including when it is not theirs)."""
        return await self._proposals.get(user_id, proposal_id)

    async def pending(self, user_id: UserId, *,
                      limit: int = DEFAULT_LIMIT) -> tuple[StrategyChangeProposal, ...]:
        """This account's still-open proposals, most recently updated first — the review queue."""
        return await self._proposals.list_for_user(user_id, open_only=True, limit=limit)

    async def history(self, user_id: UserId, *,
                      limit: int = DEFAULT_LIMIT) -> tuple[StrategyChangeProposal, ...]:
        """This account's proposals in every status — a surface to review what was done."""
        return await self._proposals.list_for_user(user_id, limit=limit)

    async def execution(self, user_id: UserId,
                        proposal_id: StrategyChangeProposalId
                        ) -> StrategyChangeExecution | None:
        """The audit recorded for one proposal, or `None` if it was never applied."""
        return await self._executions.get(user_id, proposal_id)

    # --- propose -----------------------------------------------------------

    async def propose(self, user_id: UserId, change: StrategyChange, *,
                      summary: str, now: datetime,
                      source_recommendation_id: CareerRecommendationId | None = None,
                      generator_key: str | None = None,
                      llm_run_id: LLMRunId | None = None,
                      ttl: timedelta = DEFAULT_PROPOSAL_TTL) -> StrategyChangeProposal:
        """Draft a proposal for a typed change, capturing the live target's version (§37-39).

        The target is read once, owner-scoped, to record `target_version` — its `updated_at`
        now, the precondition `approve` revalidates against — so a change drafted against numbers
        the user later edits can never silently overwrite the newer state. A target that does not
        exist for this account raises `STRATEGY_TARGET_NOT_FOUND`: there is nothing to draft
        against. The proposal is born `PROPOSED` and writes nothing to the target; `expires_at`
        follows `created_at` by `ttl` so an unacted suggestion lapses rather than lingering.
        """
        target_version = await self._load_target_version(user_id, change)
        if target_version is None:
            raise CareerError(
                CareerErrorCode.STRATEGY_TARGET_NOT_FOUND,
                f"no such {change.target.value} for this account")
        return await self._proposals.upsert(StrategyChangeProposal(
            id=new_strategy_change_proposal_id(),
            user_id=user_id,
            target=change.target,
            target_id=change.target_ref,
            change=change,
            target_version=target_version,
            summary=summary,
            source_recommendation_id=source_recommendation_id,
            generator_key=generator_key,
            llm_run_id=llm_run_id,
            created_at=now,
            updated_at=now,
            expires_at=now + ttl))

    # --- approve: the guarded executor -------------------------------------

    async def approve(self, user_id: UserId, proposal_id: StrategyChangeProposalId, *,
                      now: datetime,
                      confirm_sensitive: bool = False) -> StrategyChangeExecution:
        """Apply one confirmed proposal through its owning service, recording an audited outcome.

        The whole control-plane rule in one fixed order (§40-45):

        1. the proposal is loaded *scoped by the owner*, so a confirmation naming another
           account's proposal reads as absent and raises `PROPOSAL_NOT_FOUND`;
        2. an execution already recorded for it is returned unchanged — the id derives from the
           proposal, so a double-confirmed proposal collapses onto the one row and the change
           never applies twice;
        3. a proposal no longer open raises `PROPOSAL_NOT_OPEN`; one lapsed past `expires_at` is
           marked `EXPIRED` and raises `PROPOSAL_EXPIRED`;
        4. a change that loosens a safety brake (`is_sensitive`) raises
           `SENSITIVE_CONFIRMATION_REQUIRED` unless the caller passed `confirm_sensitive`, and
           the proposal stays open so a second, confirmed approval can proceed — the acceptance
           rule "never silently expands the user's application policy", enforced here;
        5. the live target is reloaded: a vanished one is a `REJECTED` audit and
           `STRATEGY_TARGET_NOT_FOUND`; a drifted `updated_at` is a `REJECTED` audit and
           `STRATEGY_PROPOSAL_STALE`, so a stale before/after never clobbers newer state;
        6. only then is the typed change applied through its owning service, recorded
           `SUCCEEDED` (or `FAILED` if that service raised) and the proposal moved to match.
        """
        proposal = await self._proposals.get(user_id, proposal_id)
        if proposal is None:
            raise CareerError(CareerErrorCode.PROPOSAL_NOT_FOUND, "no such proposal")
        existing = await self._executions.get(user_id, proposal_id)
        if existing is not None:
            return existing
        if not proposal.is_open:
            raise CareerError(
                CareerErrorCode.PROPOSAL_NOT_OPEN,
                f"a proposal in status {proposal.status.value} cannot be approved")
        if proposal.is_expired(as_of=now):
            await self._proposals.upsert(proposal.expired(at=now))
            raise CareerError(CareerErrorCode.PROPOSAL_EXPIRED, "this proposal has lapsed")
        if proposal.is_sensitive and not confirm_sensitive:
            raise CareerError(
                CareerErrorCode.SENSITIVE_CONFIRMATION_REQUIRED,
                "this change loosens an application-policy brake and needs "
                "an explicit second confirmation")
        current_version = await self._load_target_version(user_id, proposal.change)
        if current_version is None:
            await self._record(proposal, StrategyChangeExecutionOutcome.REJECTED,
                               observed=None, now=now,
                               detail="the target no longer exists for this account")
            raise CareerError(
                CareerErrorCode.STRATEGY_TARGET_NOT_FOUND,
                f"no such {proposal.target.value} for this account")
        if not proposal.matches_target_version(current_version):
            await self._record(proposal, StrategyChangeExecutionOutcome.REJECTED,
                               observed=current_version, now=now,
                               detail="the target changed since this proposal was drafted")
            raise CareerError(
                CareerErrorCode.STRATEGY_PROPOSAL_STALE,
                "the target changed since this proposal was drafted")
        try:
            result_ref = await self._apply(user_id, proposal.change, now=now)
        except Exception:  # cleared re-validation, yet the owning service could not complete
            return await self._record(proposal, StrategyChangeExecutionOutcome.FAILED,
                                     observed=current_version, now=now,
                                     detail=_UNEXPECTED_FAILURE_DETAIL)
        return await self._record(proposal, StrategyChangeExecutionOutcome.SUCCEEDED,
                                 observed=current_version, now=now,
                                 detail="the change was applied to the target",
                                 result_ref=result_ref)

    # --- dismiss and the expiry sweep --------------------------------------

    async def dismiss(self, user_id: UserId, proposal_id: StrategyChangeProposalId, *,
                      now: datetime) -> StrategyChangeProposal:
        """Decline an open proposal without running it — `PROPOSED` → `DISMISSED`.

        Writes no execution because nothing was attempted; it only advances the status so the
        proposal leaves the open set and cannot later be approved. Scoped and guarded like
        `approve`'s opening steps: a foreign or missing id raises `PROPOSAL_NOT_FOUND`, and one
        no longer open raises `PROPOSAL_NOT_OPEN` rather than being dismissed twice.
        """
        proposal = await self._proposals.get(user_id, proposal_id)
        if proposal is None:
            raise CareerError(CareerErrorCode.PROPOSAL_NOT_FOUND, "no such proposal")
        if not proposal.is_open:
            raise CareerError(
                CareerErrorCode.PROPOSAL_NOT_OPEN,
                f"a proposal in status {proposal.status.value} cannot be dismissed")
        return await self._proposals.upsert(proposal.dismissed(at=now))

    async def expire_due(self, user_id: UserId, *,
                         now: datetime) -> tuple[StrategyChangeProposal, ...]:
        """Lapse every open proposal past its `expires_at` — the sweep an unacted queue needs.

        Reads the open set owner-scoped and marks each expired one `EXPIRED`, returning those it
        moved. Idempotent by construction: a second sweep finds them no longer open and moves
        nothing. No execution is written — expiry is the clock passing, not an attempt.
        """
        due = tuple(p for p in await self._proposals.list_for_user(user_id, open_only=True)
                    if p.is_expired(as_of=now))
        return tuple([await self._proposals.upsert(p.expired(at=now)) for p in due])

    # --- routing to the owning service, and the audit ----------------------

    async def _apply(self, user_id: UserId, change: StrategyChange, *, now: datetime) -> str:
        """Route one confirmed change to the service that owns the edit, returning its handle.

        One branch per `StrategyChangeKind`, each calling the same typed method an HTTP route
        would — `OnboardingService` for a search edit, `ApplicationPolicyService` for a policy
        edit. The returned aggregate's new `updated_at`, ISO-formatted, is the `result_ref` the
        audit keeps; `assert_never` makes a change kind added without an executor branch a type
        error here rather than a silent no-op.
        """
        updated: SearchProfile | ApplicationPolicy
        match change:
            case SetSearchRadiusChange():
                updated = await self._onboarding.set_search_radius(
                    user_id, change.search_profile_id, radius_km=change.radius_km, now=now)
            case SetSearchKeywordsChange():
                updated = await self._onboarding.set_search_keywords(
                    user_id, change.search_profile_id,
                    title_keywords=change.title_keywords,
                    excluded_keywords=change.excluded_keywords, now=now)
            case SetSearchSourcesChange():
                updated = await self._onboarding.set_search_sources(
                    user_id, change.search_profile_id,
                    source_keys=change.source_keys, now=now)
            case SetSearchOpportunityTypesChange():
                updated = await self._onboarding.set_search_opportunity_types(
                    user_id, change.search_profile_id,
                    opportunity_types=change.opportunity_types, now=now)
            case SetPolicyOpportunityTypesChange():
                updated = await self._policies.set_allowed_opportunity_types(
                    user_id, change.application_policy_id,
                    allowed_opportunity_types=change.allowed_opportunity_types, now=now)
            case SetApplicationVolumeChange():
                updated = await self._policies.set_application_volume(
                    user_id, change.application_policy_id,
                    max_applications_per_day=change.max_applications_per_day,
                    max_applications_per_week=change.max_applications_per_week, now=now)
            case SetMinimumScoreChange():
                updated = await self._policies.set_minimum_score(
                    user_id, change.application_policy_id,
                    minimum_overall_score=change.minimum_overall_score, now=now)
            case _:
                assert_never(change)
        return updated.updated_at.isoformat()

    async def _load_target_version(self, user_id: UserId,
                                   change: StrategyChange) -> datetime | None:
        """Read the live target's `updated_at`, owner-scoped, or `None` if it is gone.

        The one precondition both `propose` (to capture `target_version`) and `approve` (to
        re-validate it) read. It routes by the change's target to the same owner-scoped read the
        editing service uses, so a foreign or missing target reads as absent exactly as it would
        at edit time — the load is the authorization check here too.
        """
        if change.target is StrategyChangeTarget.SEARCH_PROFILE:
            search = await self._onboarding.search(
                user_id, SearchProfileId(change.target_ref))
            return search.updated_at if search is not None else None
        policy = await self._policies.policy(
            user_id, ApplicationPolicyId(change.target_ref))
        return policy.updated_at if policy is not None else None

    async def _record(self, proposal: StrategyChangeProposal,
                      outcome: StrategyChangeExecutionOutcome, *,
                      observed: datetime | None, now: datetime, detail: str,
                      result_ref: str | None = None) -> StrategyChangeExecution:
        """Write the execution audit and move the proposal to the matching terminal status.

        The id is derived from the proposal alone (`strategy_change_execution_id`), so this is
        write-once by construction: a second attempt on the same proposal collapses onto the one
        row rather than auditing twice. The proposal is then advanced to the status that mirrors
        the outcome — `SUCCEEDED` → `EXECUTED`, `REJECTED` → `REJECTED`, `FAILED` → `FAILED` —
        so it leaves the open set and can never be approved again.
        """
        execution = await self._executions.upsert(StrategyChangeExecution(
            id=strategy_change_execution_id(proposal.id),
            proposal_id=proposal.id,
            user_id=proposal.user_id,
            outcome=outcome,
            observed_target_version=observed,
            detail=detail,
            result_ref=result_ref,
            created_at=now))
        match outcome:
            case StrategyChangeExecutionOutcome.SUCCEEDED:
                moved = proposal.executed(at=now)
            case StrategyChangeExecutionOutcome.REJECTED:
                moved = proposal.rejected(at=now)
            case StrategyChangeExecutionOutcome.FAILED:
                moved = proposal.failed(at=now)
            case _:
                assert_never(outcome)
        await self._proposals.upsert(moved)
        return execution



