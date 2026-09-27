"""`ApplicationPolicyService` — the minimal, focused editor an approved strategy change reaches.

Phase 15's spine ends `… → user approves → an existing service executes`, and for a policy
edit *this* is that service: the executor in `backend.app.career.strategy` hands it a typed,
already-confirmed change and it writes the one field, owner-scoped. It is deliberately minimal
— three focused setters mirroring `OnboardingService`'s search editors — because the career
loop only ever tunes three policy levers (which types may be applied to, the submission caps,
the score floor) and never the automation mode or the approval brake: those stay a human's
manual, out-of-band choice (`backend.app.domain.strategy_change` has no member for them).

Every method takes `user_id` from the authenticated session and loads the policy owner-scoped
first — the load is the authorization check, so another account's id reads as absent and
raises `ApplicationPolicyNotFound` rather than writing (docs/ENGINEERING_STANDARDS.md
§Security). No clock in the constructor — each method takes `now` — the convention every V2
service keeps, so a request's `updated_at` stamps agree. Nothing here flips a brake: widening
what the platform may apply to is what `StrategyChangeProposal.is_sensitive` guards, and the
executor demands a second confirmation before it calls the setter at all.
"""
from datetime import datetime

from backend.app.domain.base import Score
from backend.app.domain.identifiers import ApplicationPolicyId, UserId
from backend.app.domain.opportunity import OpportunityType
from backend.app.domain.policy import ApplicationPolicy
from backend.app.repositories.contracts import (
    DEFAULT_LIMIT,
    ApplicationPolicyRepository,
)


class ApplicationPolicyError(Exception):
    """Base class for the ways a policy edit can refuse a request."""


class ApplicationPolicyNotFound(ApplicationPolicyError):
    """No such policy for this user — or it belongs to somebody else.

    One error for both, deliberately: telling the two apart would let a caller enumerate other
    users' policy ids (docs/ENGINEERING_STANDARDS.md §Security). The strategy executor maps this
    to `STRATEGY_TARGET_NOT_FOUND` — the target the proposal named no longer exists for this
    account at approval time.
    """


class ApplicationPolicyService:
    """Read one account's policies, and apply the three focused edits the career loop may make.

    Holds only the policy store, which it reads owner-scoped and writes through. The setters are
    the exact counterparts of the `SET_POLICY_*`/`SET_APPLICATION_VOLUME`/`SET_MINIMUM_SCORE`
    strategy changes, so an approved proposal maps one-to-one onto a method call.
    """

    def __init__(self, policies: ApplicationPolicyRepository) -> None:
        self._policies = policies

    async def policy(self, user_id: UserId,
                     policy_id: ApplicationPolicyId) -> ApplicationPolicy | None:
        """One of this account's policies by id, or `None` (including when it is not theirs)."""
        return await self._policies.get(user_id, policy_id)

    async def default_policy(self, user_id: UserId) -> ApplicationPolicy | None:
        """This account's active default policy, or `None` if it has none yet."""
        return await self._policies.get_default(user_id)

    async def list_for_user(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT) -> tuple[ApplicationPolicy, ...]:
        """This account's policies, most recently updated first — a surface to review."""
        return await self._policies.list_for_user(user_id, limit=limit)

    async def set_allowed_opportunity_types(
            self, user_id: UserId, policy_id: ApplicationPolicyId, *,
            allowed_opportunity_types: tuple[OpportunityType, ...],
            now: datetime) -> ApplicationPolicy:
        """Replace which opportunity types the platform may *apply* to (application authority).

        An empty tuple is the domain's "no restriction" (any type). Widening this list is the
        sensitive expansion `StrategyChangeProposal.is_sensitive` guards; this method assumes the
        caller has already cleared that gate, and only writes the field owner-scoped.
        """
        existing = await self._require_policy(user_id, policy_id)
        return await self._policies.upsert(existing.model_copy(update={
            "allowed_opportunity_types": allowed_opportunity_types, "updated_at": now}))

    async def set_application_volume(
            self, user_id: UserId, policy_id: ApplicationPolicyId, *,
            max_applications_per_day: int | None,
            max_applications_per_week: int | None,
            now: datetime) -> ApplicationPolicy:
        """Set the daily and weekly submission caps together (they carry a coherence rule).

        `None` means *unlimited* — a real and dangerous value, which is why lifting a cap to it
        is a sensitive expansion. Both caps are written in one edit because the policy's own
        invariant requires the daily cap not exceed the weekly one; the `SetApplicationVolume`
        change validates that coherence before this is ever called.
        """
        existing = await self._require_policy(user_id, policy_id)
        return await self._policies.upsert(existing.model_copy(update={
            "max_applications_per_day": max_applications_per_day,
            "max_applications_per_week": max_applications_per_week,
            "updated_at": now}))

    async def set_minimum_score(
            self, user_id: UserId, policy_id: ApplicationPolicyId, *,
            minimum_overall_score: Score | None, now: datetime) -> ApplicationPolicy:
        """Set the overall score floor a match must clear to be applied to.

        `None` is *no floor*, the least selective setting — so lowering or removing it admits
        weaker matches and is the sensitive direction the proposal guards. This method writes
        the field owner-scoped, trusting the caller to have cleared that gate.
        """
        existing = await self._require_policy(user_id, policy_id)
        return await self._policies.upsert(existing.model_copy(update={
            "minimum_overall_score": minimum_overall_score, "updated_at": now}))

    async def _require_policy(self, user_id: UserId,
                              policy_id: ApplicationPolicyId) -> ApplicationPolicy:
        """Load a policy owner-scoped, or refuse — the load is the authorization check."""
        existing = await self._policies.get(user_id, policy_id)
        if existing is None:
            raise ApplicationPolicyNotFound(str(policy_id))
        return existing
