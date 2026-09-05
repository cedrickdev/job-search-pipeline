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
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from pydantic import SecretStr
from sqlalchemy import CursorResult, Result, Select, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.common import GeoPoint
from backend.app.domain.company import Company
from backend.app.domain.identifiers import (
    CandidateProfileId,
    CompanyId,
    MatchEvaluationId,
    OpportunityId,
    SearchProfileId,
    UserId,
    UserSessionId,
)
from backend.app.domain.matching import MatchEvaluation
from backend.app.domain.opportunity import Opportunity
from backend.app.domain.search import SearchProfile
from backend.app.domain.user import User, UserSession, normalize_email
from backend.app.infrastructure.database.mappers import (
    candidate_profile_to_domain,
    candidate_profile_to_row,
    company_to_domain,
    company_to_row,
    match_evaluation_to_domain,
    match_evaluation_to_row,
    opportunity_to_domain,
    opportunity_to_row,
    search_profile_to_domain,
    search_profile_to_row,
    user_session_to_domain,
    user_session_to_row,
    user_to_domain,
    user_to_row,
)
from backend.app.infrastructure.database.models import (
    CandidateProfileRow,
    CompanyLocationRow,
    CompanyRow,
    MatchEvaluationRow,
    OpportunityRow,
    OpportunitySourceRecordRow,
    SearchProfileRow,
    UserRow,
    UserSessionRow,
)
from backend.app.infrastructure.database.types import distance_meters, within_radius
from backend.app.repositories.contracts import DEFAULT_LIMIT, OpportunityNearby

if TYPE_CHECKING:  # pragma: no cover - a compile-time assertion, never executed
    from backend.app.repositories.contracts import (
        CandidateProfileRepository,
        CompanyRepository,
        MatchEvaluationRepository,
        OpportunityRepository,
        SearchProfileRepository,
        SessionRepository,
        UserRepository,
    )

    def _implements_contracts(session: AsyncSession) -> tuple[
            "CompanyRepository", "OpportunityRepository", "MatchEvaluationRepository",
            "UserRepository", "SessionRepository", "CandidateProfileRepository",
            "SearchProfileRepository"]:
        """Structural conformance, enforced by `mypy backend`.

        The `Protocol`s in `contracts` are satisfied by shape, so nothing would
        otherwise notice a repository whose signature drifted from the interface
        its callers were written against — until a caller broke. The return type
        of this function is that check, and it costs nothing at runtime.
        """
        return (SqlAlchemyCompanyRepository(session),
                SqlAlchemyOpportunityRepository(session),
                SqlAlchemyMatchEvaluationRepository(session),
                SqlAlchemyUserRepository(session),
                SqlAlchemySessionRepository(session),
                SqlAlchemyCandidateProfileRepository(session),
                SqlAlchemySearchProfileRepository(session))


def _rows_affected(result: Result[Any]) -> int:
    """How many rows a DML statement touched.

    `AsyncSession.execute` is annotated as returning `Result`, which has no
    `rowcount`; an `UPDATE` or a `DELETE` actually returns a `CursorResult`, which
    does. The cast lives here so it appears once instead of at every call site,
    and so the reason is written down next to it.
    """
    return cast("CursorResult[Any]", result).rowcount


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


class SqlAlchemyUserRepository:
    """`UserRepository` over an `AsyncSession`.

    No `selectinload`: `UserRow.sessions` exists for the cascade and for a future
    "my devices" screen, and `user_to_domain` does not read it. Loading every
    session of an account on every request would be the most expensive query in the
    authentication path for data nothing looks at.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, user_id: UserId) -> UserRow | None:
        result = await self._session.execute(
            select(UserRow).where(UserRow.id == user_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId) -> User | None:
        row = await self._row(user_id)
        return None if row is None else user_to_domain(row)

    async def get_by_email(self, email: str) -> User | None:
        # The domain's own normalizer, not `.lower()`: the column is CHECKed to
        # hold `lower(btrim(email))`, so anything else here would look up a form
        # that cannot be stored and report every account as missing.
        result = await self._session.execute(
            select(UserRow).where(UserRow.email == normalize_email(email)))
        row = result.scalar_one_or_none()
        return None if row is None else user_to_domain(row)

    async def upsert(self, user: User) -> User:
        row = user_to_row(user, await self._row(user.id))
        self._session.add(row)
        # A second account on the same address fails here, on `uq_users_email`,
        # which is what makes registration safe without a `SELECT`-then-`INSERT`
        # race: two concurrent registrations both flush, and one loses.
        await self._session.flush()
        return user_to_domain(row)


class SqlAlchemySessionRepository:
    """`SessionRepository` over an `AsyncSession`.

    The two revocation methods are `UPDATE … WHERE revoked_at IS NULL` statements
    rather than load-modify-save. That is not an optimization: it makes revocation
    idempotent in the database, so two logout requests racing on the same cookie
    cannot overwrite each other's `revoked_at`, and "revoke everything" stays one
    round trip however many sessions an account has.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, session_id: UserSessionId) -> UserSessionRow | None:
        result = await self._session.execute(
            select(UserSessionRow).where(UserSessionRow.id == session_id))
        return result.scalar_one_or_none()

    async def get_by_digest(self, token_digest: SecretStr) -> UserSession | None:
        result = await self._session.execute(
            select(UserSessionRow).where(
                UserSessionRow.token_digest == token_digest.get_secret_value()))
        row = result.scalar_one_or_none()
        return None if row is None else user_session_to_domain(row)

    async def upsert(self, session: UserSession) -> UserSession:
        row = user_session_to_row(session, await self._row(session.id))
        self._session.add(row)
        await self._session.flush()
        return user_session_to_domain(row)

    async def revoke(self, user_id: UserId, session_id: UserSessionId,
                     revoked_at: datetime) -> bool:
        result = await self._session.execute(
            update(UserSessionRow)
            .where(UserSessionRow.id == session_id,
                   # Both predicates matter: `user_id` is the authorization check,
                   # and `revoked_at IS NULL` is what makes a repeated logout a
                   # no-op instead of moving the instant forward.
                   UserSessionRow.user_id == user_id,
                   UserSessionRow.revoked_at.is_(None))
            .values(revoked_at=revoked_at))
        return bool(_rows_affected(result))

    async def revoke_all_for_user(self, user_id: UserId,
                                  revoked_at: datetime) -> int:
        result = await self._session.execute(
            update(UserSessionRow)
            .where(UserSessionRow.user_id == user_id,
                   UserSessionRow.revoked_at.is_(None))
            .values(revoked_at=revoked_at))
        return _rows_affected(result)

    async def delete_expired(self, as_of: datetime, *,
                             limit: int = DEFAULT_LIMIT) -> int:
        # A subquery for the cap, because `DELETE … LIMIT` is not PostgreSQL. The
        # cap is what keeps housekeeping from locking the whole table in one
        # statement; the caller loops until it returns zero.
        doomed = (select(UserSessionRow.id)
                  .where(UserSessionRow.expires_at < as_of)
                  .order_by(UserSessionRow.expires_at)
                  .limit(limit))
        result = await self._session.execute(
            delete(UserSessionRow).where(UserSessionRow.id.in_(doomed)))
        return _rows_affected(result)


class SqlAlchemyCandidateProfileRepository:
    """`CandidateProfileRepository` over an `AsyncSession`.

    Every statement carries `user_id`, so another user's profile reads as absent
    and an upsert cannot take one over: the load misses, the insert runs, and the
    primary key rejects it.

    All three child collections are eager-loaded on every read, because they are
    `lazy="raise"` and `candidate_profile_to_row` needs them to reconcile an
    existing row — an upsert that had not loaded them would raise on the first
    child rather than write a profile with none.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_select(self) -> Select[tuple[CandidateProfileRow]]:
        return select(CandidateProfileRow).options(
            selectinload(CandidateProfileRow.languages),
            selectinload(CandidateProfileRow.work_authorizations),
            selectinload(CandidateProfileRow.availability_slots))

    async def _row(self, user_id: UserId,
                   profile_id: CandidateProfileId) -> CandidateProfileRow | None:
        result = await self._session.execute(
            self._base_select().where(CandidateProfileRow.id == profile_id,
                                      CandidateProfileRow.user_id == user_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  profile_id: CandidateProfileId) -> CandidateProfile | None:
        row = await self._row(user_id, profile_id)
        return None if row is None else candidate_profile_to_domain(row)

    async def get_default(self, user_id: UserId) -> CandidateProfile | None:
        result = await self._session.execute(
            self._base_select()
            .where(CandidateProfileRow.user_id == user_id)
            .order_by(CandidateProfileRow.created_at, CandidateProfileRow.id)
            .limit(1))
        row = result.scalar_one_or_none()
        return None if row is None else candidate_profile_to_domain(row)

    async def upsert(self, profile: CandidateProfile) -> CandidateProfile:
        row = candidate_profile_to_row(
            profile, await self._row(profile.user_id, profile.id))
        self._session.add(row)
        await self._session.flush()
        return candidate_profile_to_domain(row)

    async def list_for_user(
            self, user_id: UserId, *,
            limit: int = DEFAULT_LIMIT) -> tuple[CandidateProfile, ...]:
        result = await self._session.execute(
            self._base_select()
            .where(CandidateProfileRow.user_id == user_id)
            .order_by(CandidateProfileRow.created_at, CandidateProfileRow.id)
            .limit(limit))
        return tuple(candidate_profile_to_domain(row) for row in result.scalars())


class SqlAlchemySearchProfileRepository:
    """`SearchProfileRepository` over an `AsyncSession`, `user_id` on every statement."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_select(self) -> Select[tuple[SearchProfileRow]]:
        return select(SearchProfileRow).options(selectinload(SearchProfileRow.areas))

    async def _row(self, user_id: UserId,
                   search_profile_id: SearchProfileId) -> SearchProfileRow | None:
        result = await self._session.execute(
            self._base_select().where(SearchProfileRow.id == search_profile_id,
                                      SearchProfileRow.user_id == user_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  search_profile_id: SearchProfileId) -> SearchProfile | None:
        row = await self._row(user_id, search_profile_id)
        return None if row is None else search_profile_to_domain(row)

    async def upsert(self, profile: SearchProfile) -> SearchProfile:
        row = search_profile_to_row(profile,
                                    await self._row(profile.user_id, profile.id))
        self._session.add(row)
        await self._session.flush()
        return search_profile_to_domain(row)

    async def delete(self, user_id: UserId,
                     search_profile_id: SearchProfileId) -> bool:
        result = await self._session.execute(
            delete(SearchProfileRow).where(SearchProfileRow.id == search_profile_id,
                                           SearchProfileRow.user_id == user_id))
        # The areas go with it through `ON DELETE CASCADE`, which is why this is a
        # bulk `DELETE` and not a load-then-`session.delete`: no children need to
        # be in the identity map for the rows to disappear.
        return bool(_rows_affected(result))

    async def list_for_user(self, user_id: UserId, *, active_only: bool = False,
                            limit: int = DEFAULT_LIMIT) -> tuple[SearchProfile, ...]:
        statement = self._base_select().where(SearchProfileRow.user_id == user_id)
        if active_only:
            statement = statement.where(SearchProfileRow.is_active.is_(True))
        result = await self._session.execute(
            statement
            .order_by(SearchProfileRow.created_at.desc(), SearchProfileRow.id)
            .limit(limit))
        return tuple(search_profile_to_domain(row) for row in result.scalars())



