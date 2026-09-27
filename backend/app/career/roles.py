"""Grouping the funnel by role family — a deterministic guess a human can always overrule (§18).

The "measure" link needs an axis the raw `Opportunity` does not carry: *what kind of role is
this?* This service is the smallest honest way to supply one. It never invents a classifier and
never calls a provider (§47); it reads a posting's title through the pure `classify_role_family`
keyword rule and records the verdict with its provenance, so a reader can always tell a machine
guess (`DETERMINISTIC_TITLE`) from a human's correction (`MANUAL`).

Two operations, and the precedence between them is the whole point:

- **classify** — the deterministic backfill. It reads the opportunity (a shared fact, so read by
  id and not owner-scoped), applies the rule to the title, and records the family — or the honest
  *unclassified* `None` when nothing matches. It **never overwrites a `MANUAL` classification**:
  a human's judgement outranks the rule and the next analytics run must not silently undo it.
- **set_manual** — a user's explicit correction. It names a family (the domain refuses a manual
  classification that does not) and stamps `MANUAL`, so it survives every later backfill.

Both are idempotent by the id derived from `(user_id, opportunity_id)`, and both preserve the
original `created_at` across a re-classification, so the row records when the classification was
first made rather than when it was last touched. The classification is user-owned even though
the opportunity is not: two candidates may reasonably file the same posting under different
families, and each sees only their own (§18-20).
"""
from datetime import datetime

from backend.app.career.errors import CareerError, CareerErrorCode
from backend.app.domain.identifiers import (
    OpportunityId,
    UserId,
    role_classification_id,
)
from backend.app.domain.opportunity import Opportunity
from backend.app.domain.role import (
    RoleClassification,
    RoleFamily,
    RoleFamilyProvenance,
    classify_role_family,
)
from backend.app.repositories.contracts import (
    DEFAULT_LIMIT,
    OpportunityRepository,
    RoleClassificationRepository,
)


class RoleClassificationService:
    """Classify one account's opportunities by role family, deterministically or by hand.

    Holds the classification store it writes and the opportunity store it reads for the title.
    No clock in the constructor — each mutating method takes `now` — the convention every V2
    service keeps.
    """

    def __init__(self, roles: RoleClassificationRepository,
                 opportunities: OpportunityRepository) -> None:
        self._roles = roles
        self._opportunities = opportunities

    async def classify(self, user_id: UserId, opportunity_id: OpportunityId, *,
                       now: datetime) -> RoleClassification:
        """Record the deterministic family of an opportunity's title, sparing manual ones (§18).

        The opportunity is read by id (a posting is a shared fact with no owner), so a missing
        one raises `OPPORTUNITY_NOT_FOUND`. If this account already corrected the role by hand,
        that `MANUAL` classification is returned unchanged — the backfill never overwrites a
        human's choice. Otherwise the pure rule assigns a family (or the honest `None`), and the
        row is upserted, preserving the first `created_at`.
        """
        opportunity = await self._require_opportunity(opportunity_id)
        existing = await self._roles.get(user_id, opportunity_id)
        if existing is not None and existing.is_manual:
            return existing
        family = classify_role_family(opportunity.title)
        return await self._roles.upsert(RoleClassification(
            id=role_classification_id(user_id, opportunity_id),
            user_id=user_id,
            opportunity_id=opportunity_id,
            role_family=family,
            provenance=RoleFamilyProvenance.DETERMINISTIC_TITLE,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now))

    async def set_manual(self, user_id: UserId, opportunity_id: OpportunityId, *,
                        role_family: RoleFamily, now: datetime) -> RoleClassification:
        """Record a human's explicit role family, outranking the rule from here on (§18).

        The opportunity must exist (`OPPORTUNITY_NOT_FOUND` otherwise). The classification is
        stamped `MANUAL` and names `role_family` — the domain refuses a manual classification
        that does not — so a later `classify` backfill leaves it untouched. The first
        `created_at` is preserved across the correction.
        """
        await self._require_opportunity(opportunity_id)
        existing = await self._roles.get(user_id, opportunity_id)
        return await self._roles.upsert(RoleClassification(
            id=role_classification_id(user_id, opportunity_id),
            user_id=user_id,
            opportunity_id=opportunity_id,
            role_family=role_family,
            provenance=RoleFamilyProvenance.MANUAL,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now))

    async def classification(self, user_id: UserId, opportunity_id: OpportunityId
                            ) -> RoleClassification | None:
        """This account's classification of one opportunity, or `None` if never classified."""
        return await self._roles.get(user_id, opportunity_id)

    async def list_for_user(self, user_id: UserId, *,
                           limit: int = DEFAULT_LIMIT) -> tuple[RoleClassification, ...]:
        """This account's classifications, most recently updated first — a surface to review."""
        return await self._roles.list_for_user(user_id, limit=limit)

    async def _require_opportunity(self, opportunity_id: OpportunityId) -> Opportunity:
        """Load a posting by id, or refuse — a posting is a shared fact, so there is no owner."""
        opportunity = await self._opportunities.get(opportunity_id)
        if opportunity is None:
            raise CareerError(CareerErrorCode.OPPORTUNITY_NOT_FOUND, "no such opportunity")
        return opportunity
