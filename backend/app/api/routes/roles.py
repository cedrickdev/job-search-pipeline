"""`/api/v2`: classifying an opportunity by role family, over HTTP.

The "measure" step needs an axis a raw `Opportunity` does not carry — *what kind of role is
this?* — and this surface supplies it without ever calling a provider (§18, §47). A deterministic
title rule proposes a family; a human may overrule it, and the correction outranks every later
backfill. The classification is user-owned even though the posting is a shared fact: two
candidates may reasonably file the same posting under different families, and each sees only their
own.

The owner is never in the path or the body — it is the account resolved from the session. The
opportunity, a shared fact, is named in the path, so a missing one is `OPPORTUNITY_NOT_FOUND`
(404); a read that finds no classification for this account is `role_classification_not_found`
(404) rather than a 500 from rendering nothing. No request body carries a `user_id`, an `id`, a
provenance or a timestamp: those are the service's to assign.
"""
from fastapi import APIRouter

from backend.app.api.dependencies import CurrentSession, Now, RoleClassifications
from backend.app.api.errors import role_classification_not_found
from backend.app.api.schemas import (
    RoleClassificationListResponse,
    RoleClassificationResponse,
    SetRoleClassificationRequest,
)
from backend.app.domain.identifiers import OpportunityId

router = APIRouter(tags=["v2-role-classifications"])


@router.get("/role-classifications", response_model=RoleClassificationListResponse)
async def list_role_classifications(
        current: CurrentSession,
        service: RoleClassifications) -> RoleClassificationListResponse:
    """This account's classifications, most recently updated first — a surface to review.

    Declared before the `/opportunities/{opportunity_id}/role-classification` routes so the
    literal plural path is never confused with a per-opportunity one.
    """
    classifications = await service.list_for_user(current.user.id)
    return RoleClassificationListResponse.of(classifications)


@router.get("/opportunities/{opportunity_id}/role-classification",
            response_model=RoleClassificationResponse)
async def read_role_classification(opportunity_id: OpportunityId, current: CurrentSession,
                                   service: RoleClassifications) -> RoleClassificationResponse:
    """This account's classification of one opportunity, or 404 if it has never classified it.

    The read is keyed by (this account, opportunity), so an empty result means only "not
    classified yet" — there is no foreign row to probe. A 404 (`role_classification_not_found`)
    is returned rather than calling `.of(None)`.
    """
    classification = await service.classification(current.user.id, opportunity_id)
    if classification is None:
        raise role_classification_not_found()
    return RoleClassificationResponse.of(classification)


@router.post("/opportunities/{opportunity_id}/role-classification",
             response_model=RoleClassificationResponse)
async def classify_opportunity(opportunity_id: OpportunityId, current: CurrentSession,
                               service: RoleClassifications,
                               instant: Now) -> RoleClassificationResponse:
    """Record the deterministic family of an opportunity's title, sparing a manual one (§18).

    Idempotent and safe to repeat: the pure title rule assigns a family (or the honest
    unclassified `None`), preserving the first `created_at`. If this account already corrected the
    role by hand, that manual classification is returned unchanged — the backfill never overwrites
    a human's choice. A missing posting is a 404 (`opportunity_not_found`).
    """
    classification = await service.classify(current.user.id, opportunity_id, now=instant)
    return RoleClassificationResponse.of(classification)


@router.put("/opportunities/{opportunity_id}/role-classification",
            response_model=RoleClassificationResponse)
async def set_role_classification(opportunity_id: OpportunityId,
                                  body: SetRoleClassificationRequest, current: CurrentSession,
                                  service: RoleClassifications,
                                  instant: Now) -> RoleClassificationResponse:
    """Record a human's explicit role family, outranking the rule from here on (§18).

    The body names a family — a manual classification cannot be unclassified — and the row is
    stamped `MANUAL`, so a later deterministic backfill leaves it untouched. The first `created_at`
    is preserved across the correction. A missing posting is a 404 (`opportunity_not_found`).
    """
    classification = await service.set_manual(
        current.user.id, opportunity_id, role_family=body.role_family, now=instant)
    return RoleClassificationResponse.of(classification)
