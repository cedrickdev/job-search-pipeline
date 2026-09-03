"""SQLAlchemy implementations of the repository contracts.

Each class holds an `AsyncSession` it did not create and does not own. It reads,
it writes, it flushes when a caller needs the write to be visible to the next
query in the same transaction — and it never commits. The unit of work is the
caller's (`session_scope`), which is what makes "import 400 rows or none of them"
expressible.

Every read eager-loads the children the mapper is about to touch. The
relationships are `lazy="raise"`, so a forgotten `selectinload` is an immediate
error here rather than an implicit query — under asyncio a lazy load is not a
performance footnote, it is an exception at the worst possible moment.
"""
from typing import TYPE_CHECKING

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
from backend.app.infrastructure.database.mappers import (
    company_to_domain,
    company_to_row,
    match_evaluation_to_domain,
    match_evaluation_to_row,
    opportunity_to_domain,
    opportunity_to_row,
)
from backend.app.infrastructure.database.models import (
    CompanyLocationRow,
    CompanyRow,
    MatchEvaluationRow,
    OpportunityRow,
    OpportunitySourceRecordRow,
)
from backend.app.infrastructure.database.types import distance_meters, within_radius
from backend.app.repositories.contracts import DEFAULT_LIMIT, OpportunityNearby

if TYPE_CHECKING:  # pragma: no cover - a compile-time assertion, never executed
    from backend.app.repositories.contracts import (
        CompanyRepository,
        MatchEvaluationRepository,
        OpportunityRepository,
    )

    def _implements_contracts(session: AsyncSession) -> tuple[
            "CompanyRepository", "OpportunityRepository", "MatchEvaluationRepository"]:
        """Structural conformance, enforced by `mypy backend`.

        The `Protocol`s in `contracts` are satisfied by shape, so nothing would
        otherwise notice a repository whose signature drifted from the interface
        its callers were written against — until a caller broke. The return type
        of this function is that check, and it costs nothing at runtime.
        """
        return (SqlAlchemyCompanyRepository(session),
                SqlAlchemyOpportunityRepository(session),
                SqlAlchemyMatchEvaluationRepository(session))


class SqlAlchemyCompanyRepository:
    """`CompanyRepository` over an `AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_select(self) -> Select[tuple[CompanyRow]]:
        return select(CompanyRow).options(selectinload(CompanyRow.locations))

    async def _row(self, company_id: CompanyId) -> CompanyRow | None:
        result = await self._session.execute(
            self._base_select().where(CompanyRow.id == company_id))
        return result.scalar_one_or_none()

    async def get(self, company_id: CompanyId) -> Company | None:
        row = await self._row(company_id)
        return None if row is None else company_to_domain(row)

    async def upsert(self, company: Company) -> Company:
        row = company_to_row(company, await self._row(company.id))
        self._session.add(row)
        # Flush, not commit: the caller's transaction stays open, but the INSERT
        # has run, so a unique-constraint violation surfaces here — attached to
        # the company that caused it — instead of at commit time with no context.
        await self._session.flush()
        return company_to_domain(row)

    async def list_near(self, center: GeoPoint, radius_meters: float, *,
                        limit: int = DEFAULT_LIMIT) -> tuple[Company, ...]:
        # A subquery on the sites rather than a join: a chain with three branches
        # inside the radius must appear once, and `SELECT DISTINCT` over an
        # entity's columns is both slower and fragile as columns are added.
        nearby = (select(CompanyLocationRow.company_id)
                  .where(within_radius(CompanyLocationRow.location_point, center,
                                       radius_meters)))
        result = await self._session.execute(
            self._base_select()
            .where(CompanyRow.id.in_(nearby))
            # Ordering by name and then id keeps the page stable across calls; a
            # company has several sites, so "nearest company" would need a
            # per-company minimum this contract does not promise.
            .order_by(CompanyRow.name, CompanyRow.id)
            .limit(limit))
        return tuple(company_to_domain(row) for row in result.scalars())


class SqlAlchemyOpportunityRepository:
    """`OpportunityRepository` over an `AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_select(self) -> Select[tuple[OpportunityRow]]:
        return select(OpportunityRow).options(selectinload(OpportunityRow.source))

    async def _one(self, statement: Select[tuple[OpportunityRow]]) -> Opportunity | None:
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        return None if row is None else opportunity_to_domain(row)

    async def _row(self, opportunity_id: OpportunityId) -> OpportunityRow | None:
        result = await self._session.execute(
            self._base_select().where(OpportunityRow.id == opportunity_id))
        return result.scalar_one_or_none()

    async def get(self, opportunity_id: OpportunityId) -> Opportunity | None:
        return await self._one(
            self._base_select().where(OpportunityRow.id == opportunity_id))

    async def get_by_source(self, source_key: str,
                            external_id: str) -> Opportunity | None:
        return await self._one(
            self._base_select()
            .join(OpportunityRow.source.of_type(OpportunitySourceRecordRow))
            .where(OpportunitySourceRecordRow.source_key == source_key,
                   OpportunitySourceRecordRow.external_id == external_id))

    async def get_by_fingerprint(self, fingerprint: str) -> Opportunity | None:
        return await self._one(
            self._base_select()
            .where(OpportunityRow.dedup_fingerprint == fingerprint))

    async def upsert(self, opportunity: Opportunity) -> Opportunity:
        row = opportunity_to_row(opportunity, await self._row(opportunity.id))
        self._session.add(row)
        # A different posting already holding this `dedup_fingerprint` raises
        # here. That is the intended outcome: two ids for one posting is the
        # duplicate the fingerprint exists to prevent, and the importer reports it
        # per row rather than guessing which one to keep.
        await self._session.flush()
        return opportunity_to_domain(row)

    async def list_recent(self, *,
                          limit: int = DEFAULT_LIMIT) -> tuple[Opportunity, ...]:
        result = await self._session.execute(
            self._base_select()
            .order_by(OpportunityRow.discovered_at.desc(), OpportunityRow.id)
            .limit(limit))
        return tuple(opportunity_to_domain(row) for row in result.scalars())

    async def list_near(self, center: GeoPoint, radius_meters: float, *,
                        limit: int = DEFAULT_LIMIT) -> tuple[OpportunityNearby, ...]:
        distance = distance_meters(OpportunityRow.location_point, center)
        result = await self._session.execute(
            select(OpportunityRow, distance.label("distance_meters"))
            .options(selectinload(OpportunityRow.source))
            # `ST_DWithin` is NULL for a row with no point and `WHERE` keeps only
            # TRUE, so unlocated postings drop out without a second predicate —
            # and the GiST index still does the work.
            .where(within_radius(OpportunityRow.location_point, center, radius_meters))
            .order_by(distance, OpportunityRow.id)
            .limit(limit))
        return tuple(OpportunityNearby(opportunity_to_domain(row), float(meters))
                     for row, meters in result.all())


class SqlAlchemyMatchEvaluationRepository:
    """`MatchEvaluationRepository` over an `AsyncSession`.

    Every statement carries `user_id`. A row that exists but belongs to somebody
    else is therefore reported as absent, and an upsert cannot take over another
    user's row: the load misses, the insert runs, and
    `uq_match_evaluations_candidate_profile_id_opportunity_id` rejects it. Loud
    and safe beats silent and wrong.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_select(self) -> Select[tuple[MatchEvaluationRow]]:
        return select(MatchEvaluationRow).options(
            selectinload(MatchEvaluationRow.dimensions))

    async def _row_for_pair(
            self, user_id: UserId, candidate_profile_id: CandidateProfileId,
            opportunity_id: OpportunityId) -> MatchEvaluationRow | None:
        result = await self._session.execute(
            self._base_select().where(
                MatchEvaluationRow.user_id == user_id,
                MatchEvaluationRow.candidate_profile_id == candidate_profile_id,
                MatchEvaluationRow.opportunity_id == opportunity_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  evaluation_id: MatchEvaluationId) -> MatchEvaluation | None:
        result = await self._session.execute(
            self._base_select().where(MatchEvaluationRow.id == evaluation_id,
                                      MatchEvaluationRow.user_id == user_id))
        row = result.scalar_one_or_none()
        return None if row is None else match_evaluation_to_domain(row)

    async def get_for_pair(self, user_id: UserId,
                           candidate_profile_id: CandidateProfileId,
                           opportunity_id: OpportunityId) -> MatchEvaluation | None:
        row = await self._row_for_pair(user_id, candidate_profile_id, opportunity_id)
        return None if row is None else match_evaluation_to_domain(row)

    async def upsert(self, evaluation: MatchEvaluation) -> MatchEvaluation:
        existing = await self._row_for_pair(evaluation.user_id,
                                            evaluation.candidate_profile_id,
                                            evaluation.opportunity_id)
        row = match_evaluation_to_row(evaluation, existing)
        self._session.add(row)
        await self._session.flush()
        return match_evaluation_to_domain(row)

    async def list_for_user(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT) -> tuple[MatchEvaluation, ...]:
        result = await self._session.execute(
            self._base_select()
            .where(MatchEvaluationRow.user_id == user_id)
            .order_by(MatchEvaluationRow.evaluated_at.desc(), MatchEvaluationRow.id)
            .limit(limit))
        return tuple(match_evaluation_to_domain(row) for row in result.scalars())



