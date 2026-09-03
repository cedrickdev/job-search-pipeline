"""What the application needs from storage, in domain terms.

Three rules shape every signature here:

**No transaction control.** There is no `commit`, no `rollback` and no `begin`. A
repository that commits decides on its caller's behalf that a unit of work is
over, which is how an import ends up half-applied; the boundary is
`backend.app.infrastructure.database.engine.session_scope`.

**User-scoped reads take the user first.** `list_for_user(user_id, …)` cannot be
called without saying whose data is wanted, so an authorization filter is
impossible to forget in Phase 4 (docs/ENGINEERING_STANDARDS.md §Security:
authorization enforced server-side). `Opportunity` and `Company` are shared facts
and deliberately have no such parameter.

**Writes are upserts.** Every persisted entity has an id the domain produced —
uuid5 for anything derived from V1, uuid4 otherwise — so "create" and "update"
are the same operation from the caller's point of view, and retrying a failed
import is safe by construction.

Small on purpose: Phase 2 owes the persistence foundation, and a method nobody
calls yet is a guess about Phase 6 and 8 that would have to be unguessed.
"""
from typing import NamedTuple, Protocol, runtime_checkable

from backend.app.domain.common import GeoPoint
from backend.app.domain.company import Company
from backend.app.domain.identifiers import (
    CandidateProfileId,
    CompanyId,
    MatchEvaluationId,
    OpportunityId,
    UserId,
)
from backend.app.domain.matching import MatchEvaluation
from backend.app.domain.opportunity import Opportunity

# Every list method is capped. An uncapped query is fine against the empty
# development database and is an outage against a real one, and the caller that
# wants everything can say so explicitly.
DEFAULT_LIMIT = 100


class OpportunityNearby(NamedTuple):
    """An opportunity and how far it is from the point that was searched.

    The distance is computed by the database, not recomputed in Python: it comes
    from the same spheroid calculation that decided the row matched, so a result
    can never be shown as 101 km away by a query for everything within 100 km.
    """

    opportunity: Opportunity
    distance_meters: float


@runtime_checkable
class CompanyRepository(Protocol):
    """Employers, with their sites."""

    async def get(self, company_id: CompanyId) -> Company | None:
        """The company and all of its locations, or `None`."""
        ...

    async def upsert(self, company: Company) -> Company:
        """Write the company and reconcile its locations.

        Returns what is now stored, which is how a caller learns that a location
        it did not include has been deleted.
        """
        ...

    async def list_near(self, center: GeoPoint, radius_meters: float, *,
                        limit: int = DEFAULT_LIMIT) -> tuple[Company, ...]:
        """Companies with at least one site within `radius_meters` of `center`.

        The Phase 6 spontaneous-application question ("who is nearby") in its
        minimal form. Phase 7 builds the explorer on top of it.
        """
        ...


@runtime_checkable
class OpportunityRepository(Protocol):
    """Postings — shared facts, so nothing here is user-scoped."""

    async def get(self, opportunity_id: OpportunityId) -> Opportunity | None: ...

    async def get_by_source(self, source_key: str,
                            external_id: str) -> Opportunity | None:
        """The posting a source already published under this identifier.

        The idempotency lookup for any importer or discovery run: it answers
        "have I seen this one before?" without depending on how the id was
        derived.
        """
        ...

    async def get_by_fingerprint(self, fingerprint: str) -> Opportunity | None:
        """The posting carrying this dedup fingerprint, whatever source found it."""
        ...

    async def upsert(self, opportunity: Opportunity) -> Opportunity: ...

    async def list_recent(self, *,
                          limit: int = DEFAULT_LIMIT) -> tuple[Opportunity, ...]:
        """Newest discoveries first — the shape of the feed."""
        ...

    async def list_near(self, center: GeoPoint, radius_meters: float, *,
                        limit: int = DEFAULT_LIMIT) -> tuple[OpportunityNearby, ...]:
        """Postings within `radius_meters` of `center`, closest first."""
        ...


@runtime_checkable
class MatchEvaluationRepository(Protocol):
    """Evaluations — user-owned, so every method names the owner.

    `user_id` is a parameter and not an ambient value on purpose. A repository
    that read the current user from a context variable would let a service forget
    the filter and still compile; here the filter is part of the call.
    """

    async def get(self, user_id: UserId,
                  evaluation_id: MatchEvaluationId) -> MatchEvaluation | None:
        """The evaluation, or `None` — including when it belongs to somebody else.

        Not found and not yours are deliberately indistinguishable: a caller that
        could tell them apart could enumerate another user's rows by id
        (docs/ENGINEERING_STANDARDS.md §Security: no cross-user data leakage).
        """
        ...

    async def get_for_pair(self, user_id: UserId,
                           candidate_profile_id: CandidateProfileId,
                           opportunity_id: OpportunityId) -> MatchEvaluation | None:
        """This profile's verdict on this posting, if it has been evaluated."""
        ...

    async def upsert(self, evaluation: MatchEvaluation) -> MatchEvaluation:
        """Write the evaluation and reconcile its dimension scores.

        The owner comes from `evaluation.user_id`, so there is no signature in
        which the row's owner and the caller's intent can disagree.
        """
        ...

    async def list_for_user(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT) -> tuple[MatchEvaluation, ...]:
        """This user's evaluations, most recently evaluated first."""
        ...


