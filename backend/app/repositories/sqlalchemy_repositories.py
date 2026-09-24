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
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import (
    ColumnElement,
    CursorResult,
    Result,
    Select,
    and_,
    case,
    delete,
    exists,
    false,
    func,
    null,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.domain.application import Application, ApplicationState
from backend.app.domain.application_event import ApplicationEvent, SubmissionAttempt
from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.chat import (
    ChatActionExecution,
    ChatActionProposal,
    ChatMessage,
    Conversation,
)
from backend.app.domain.common import GeoPoint
from backend.app.domain.company import (
    AtsPlatform,
    CareerSite,
    Company,
    CompanyAlias,
    CompanyDiscoveryRecord,
    normalize_company_name,
)
from backend.app.domain.decision import ApplicationDecision
from backend.app.domain.documents import CandidateDocument, CandidateDocumentType
from backend.app.domain.eligibility import EligibilityResult
from backend.app.domain.geo import (
    GeoSearchQuery,
    GeoStatus,
    RemotePolicy,
    remote_scope_of,
)
from backend.app.domain.identifiers import (
    ApplicationDecisionId,
    ApplicationId,
    ApplicationPolicyId,
    CandidateDocumentId,
    CandidateProfileId,
    ChatActionProposalId,
    CompanyId,
    ConversationId,
    EligibilityResultId,
    InterviewAnswerId,
    InterviewQuestionId,
    InterviewSessionId,
    LLMConnectionId,
    MatchEvaluationId,
    OpportunityId,
    SearchProfileId,
    SubmissionAttemptId,
    UserId,
    UserSessionId,
)
from backend.app.domain.interview import (
    InterviewAnswer,
    InterviewAnswerEvaluation,
    InterviewQuestion,
    InterviewSession,
    InterviewSessionSummary,
)
from backend.app.domain.matching import MatchEvaluation
from backend.app.domain.opportunity import Opportunity, WorkplaceMode
from backend.app.domain.policy import ApplicationPolicy
from backend.app.domain.search import SearchProfile
from backend.app.domain.user import User, UserSession, normalize_email
from backend.app.infrastructure.database.mappers import (
    application_decision_to_domain,
    application_decision_to_row,
    application_event_to_domain,
    application_event_to_row,
    application_policy_to_domain,
    application_policy_to_row,
    application_to_domain,
    application_to_row,
    candidate_document_to_domain,
    candidate_document_to_row,
    candidate_profile_to_domain,
    candidate_profile_to_row,
    career_site_to_domain,
    career_site_to_row,
    chat_action_execution_to_domain,
    chat_action_execution_to_row,
    chat_action_proposal_to_domain,
    chat_action_proposal_to_row,
    chat_message_to_domain,
    chat_message_to_row,
    company_alias_to_domain,
    company_alias_to_row,
    company_location_to_domain,
    company_to_domain,
    company_to_row,
    conversation_to_domain,
    conversation_to_row,
    discovery_record_to_domain,
    discovery_record_to_row,
    eligibility_result_to_domain,
    eligibility_result_to_row,
    interview_answer_evaluation_to_domain,
    interview_answer_evaluation_to_row,
    interview_answer_to_domain,
    interview_answer_to_row,
    interview_question_to_domain,
    interview_question_to_row,
    interview_session_summary_to_domain,
    interview_session_summary_to_row,
    interview_session_to_domain,
    interview_session_to_row,
    llm_connection_to_domain,
    llm_connection_to_row,
    llm_run_to_domain,
    llm_run_to_row,
    match_evaluation_to_domain,
    match_evaluation_to_row,
    opportunity_to_domain,
    opportunity_to_row,
    provider_session_to_domain,
    provider_session_to_row,
    search_profile_to_domain,
    search_profile_to_row,
    submission_attempt_to_domain,
    submission_attempt_to_row,
    user_session_to_domain,
    user_session_to_row,
    user_to_domain,
    user_to_row,
)
from backend.app.infrastructure.database.models import (
    ApplicationDecisionRow,
    ApplicationEventRow,
    ApplicationPolicyRow,
    ApplicationRow,
    CandidateDocumentRow,
    CandidateProfileRow,
    ChatActionExecutionRow,
    ChatActionProposalRow,
    ChatMessageRow,
    CompanyAliasRow,
    CompanyCareerSiteRow,
    CompanyDiscoveryRecordRow,
    CompanyLocationRow,
    CompanyRow,
    ConversationRow,
    EligibilityResultRow,
    InterviewAnswerEvaluationRow,
    InterviewAnswerRow,
    InterviewQuestionRow,
    InterviewSessionRow,
    InterviewSessionSummaryRow,
    LLMConnectionRow,
    LLMRunRow,
    MatchEvaluationRow,
    OpportunityRow,
    OpportunitySourceRecordRow,
    ProviderSessionRow,
    SearchProfileRow,
    SubmissionAttemptRow,
    UserRow,
    UserSessionRow,
)
from backend.app.infrastructure.database.types import (
    distance_meters,
    within_bounds,
    within_radius,
)
from backend.app.llm.connection import LLMConnection
from backend.app.llm.sessions import ProviderSession
from backend.app.llm.telemetry import LLMRun
from backend.app.repositories.contracts import (
    DEFAULT_LIMIT,
    CompanyCandidate,
    CompanyFilter,
    CompanyGeoResult,
    CompanyPage,
    MatchedRadius,
    OpportunityGeoResult,
    OpportunityNearby,
)

if TYPE_CHECKING:  # pragma: no cover - a compile-time assertion, never executed
    from backend.app.repositories.contracts import (
        ApplicationDecisionRepository,
        ApplicationEventRepository,
        ApplicationPolicyRepository,
        ApplicationRepository,
        CandidateDocumentRepository,
        CandidateProfileRepository,
        CareerSiteRepository,
        ChatActionExecutionRepository,
        ChatActionProposalRepository,
        ChatMessageRepository,
        CompanyDiscoveryRepository,
        CompanyRepository,
        ConversationRepository,
        EligibilityResultRepository,
        InterviewAnswerEvaluationRepository,
        InterviewAnswerRepository,
        InterviewQuestionRepository,
        InterviewSessionRepository,
        InterviewSessionSummaryRepository,
        LLMConnectionRepository,
        LLMRunRepository,
        MatchEvaluationRepository,
        OpportunityRepository,
        ProviderSessionRepository,
        SearchProfileRepository,
        SessionRepository,
        SubmissionAttemptRepository,
        UserRepository,
    )

    def _implements_contracts(session: AsyncSession) -> tuple[
            "CompanyRepository", "CareerSiteRepository", "CompanyDiscoveryRepository",
            "OpportunityRepository", "MatchEvaluationRepository",
            "EligibilityResultRepository", "UserRepository",
            "SessionRepository", "CandidateProfileRepository",
            "SearchProfileRepository", "CandidateDocumentRepository",
            "LLMConnectionRepository", "ProviderSessionRepository",
            "LLMRunRepository", "ApplicationPolicyRepository",
            "ApplicationDecisionRepository", "ApplicationRepository",
            "ApplicationEventRepository", "SubmissionAttemptRepository",
            "ConversationRepository", "ChatMessageRepository",
            "ChatActionProposalRepository", "ChatActionExecutionRepository",
            "InterviewSessionRepository", "InterviewQuestionRepository",
            "InterviewAnswerRepository", "InterviewAnswerEvaluationRepository",
            "InterviewSessionSummaryRepository"]:
        """Structural conformance, enforced by `mypy backend`.

        The `Protocol`s in `contracts` are satisfied by shape, so nothing would
        otherwise notice a repository whose signature drifted from the interface
        its callers were written against — until a caller broke. The return type
        of this function is that check, and it costs nothing at runtime.
        """
        return (SqlAlchemyCompanyRepository(session),
                SqlAlchemyCareerSiteRepository(session),
                SqlAlchemyCompanyDiscoveryRepository(session),
                SqlAlchemyOpportunityRepository(session),
                SqlAlchemyMatchEvaluationRepository(session),
                SqlAlchemyEligibilityResultRepository(session),
                SqlAlchemyUserRepository(session),
                SqlAlchemySessionRepository(session),
                SqlAlchemyCandidateProfileRepository(session),
                SqlAlchemySearchProfileRepository(session),
                SqlAlchemyCandidateDocumentRepository(session),
                SqlAlchemyLLMConnectionRepository(session),
                SqlAlchemyProviderSessionRepository(session),
                SqlAlchemyLLMRunRepository(session),
                SqlAlchemyApplicationPolicyRepository(session),
                SqlAlchemyApplicationDecisionRepository(session),
                SqlAlchemyApplicationRepository(session),
                SqlAlchemyApplicationEventRepository(session),
                SqlAlchemySubmissionAttemptRepository(session),
                SqlAlchemyConversationRepository(session),
                SqlAlchemyChatMessageRepository(session),
                SqlAlchemyChatActionProposalRepository(session),
                SqlAlchemyChatActionExecutionRepository(session),
                SqlAlchemyInterviewSessionRepository(session),
                SqlAlchemyInterviewQuestionRepository(session),
                SqlAlchemyInterviewAnswerRepository(session),
                SqlAlchemyInterviewAnswerEvaluationRepository(session),
                SqlAlchemyInterviewSessionSummaryRepository(session))


def _rows_affected(result: Result[Any]) -> int:
    """How many rows a DML statement touched.

    `AsyncSession.execute` is annotated as returning `Result`, which has no
    `rowcount`; an `UPDATE` or a `DELETE` actually returns a `CursorResult`, which
    does. The cast lives here so it appears once instead of at every call site,
    and so the reason is written down next to it.
    """
    return cast("CursorResult[Any]", result).rowcount


def _nearest_distance(column: Any, query: GeoSearchQuery) -> ColumnElement[float | None]:
    """Smallest PostGIS distance to any query centre, or SQL NULL without one."""
    distances = [distance_meters(column, radius.center) for radius in query.radii]
    if not distances:
        return cast("ColumnElement[float | None]", null())
    if len(distances) == 1:
        return cast("ColumnElement[float | None]", distances[0])
    return cast("ColumnElement[float | None]", func.least(*distances))


def _matched_radius_columns(column: Any, query: GeoSearchQuery) -> list[Any]:
    """SQL booleans for each radius, selected beside each result row."""
    return [within_radius(column, radius.center, radius.radius.meters)
            for radius in query.radii]


def _matched_radii(values: Sequence[bool | None],
                   query: GeoSearchQuery) -> tuple[MatchedRadius, ...]:
    return tuple(MatchedRadius(index, query.radii[index].label)
                 for index, matched in enumerate(values) if matched is True)


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

    async def search_geo(self, query: GeoSearchQuery) -> tuple[CompanyGeoResult, ...]:
        """Return each employer once, at its closest matching physical site."""
        if query.remote_policy is RemotePolicy.REMOTE_ONLY:
            return ()
        radius_matches = _matched_radius_columns(
            CompanyLocationRow.location_point, query)
        geographic: list[ColumnElement[bool]] = list(radius_matches)
        if query.countries:
            geographic.append(
                CompanyLocationRow.location_country.in_(query.countries))
        predicates: list[ColumnElement[bool]] = [or_(*geographic)]
        if query.bounds is not None:
            predicates.append(
                within_bounds(CompanyLocationRow.location_point, query.bounds))

        distance = _nearest_distance(CompanyLocationRow.location_point, query)
        # Rank a company's sites by proximity when the query has centres,
        # otherwise by headquarters: `distance` is SQL NULL without radii, and an
        # OVER (ORDER BY NULL) is rejected as a non-integer constant ordering.
        site_order: list[Any] = ([distance.asc().nulls_last()] if query.radii
                                 else [CompanyLocationRow.is_headquarters.desc()])
        rank = func.row_number().over(
            partition_by=CompanyLocationRow.company_id,
            order_by=(*site_order, CompanyLocationRow.id),
        ).label("site_rank")
        sites = (select(
            CompanyLocationRow.id.label("site_id"),
            CompanyLocationRow.company_id.label("company_id"),
            distance.label("distance_meters"),
            *[match.label(f"radius_{index}")
              for index, match in enumerate(radius_matches)],
            rank,
        ).where(*predicates).subquery())
        statement = (select(CompanyRow, CompanyLocationRow,
                            sites.c.distance_meters,
                            *[sites.c[f"radius_{index}"]
                              for index in range(len(radius_matches))])
                     .options(selectinload(CompanyRow.locations))
                     .join(sites, sites.c.company_id == CompanyRow.id)
                     .join(CompanyLocationRow,
                           CompanyLocationRow.id == sites.c.site_id)
                     .where(sites.c.site_rank == 1))
        ordering: list[Any] = []
        if query.radii:
            ordering.append(sites.c.distance_meters.asc().nulls_last())
        ordering.extend((CompanyRow.name, CompanyRow.id))
        result = await self._session.execute(
            statement.order_by(*ordering).limit(query.limit).offset(query.offset))
        found: list[CompanyGeoResult] = []
        for values in result.all():
            company_row, site_row, meters, *matches = values
            found.append(CompanyGeoResult(
                company=company_to_domain(company_row),
                location=company_location_to_domain(site_row),
                distance_meters=None if meters is None else float(meters),
                status=(GeoStatus.RESOLVED if site_row.location_point is not None
                        else GeoStatus.UNRESOLVED),
                matched_radii=_matched_radii(matches, query)))
        return tuple(found)

    async def _aliases_for(
            self,
            company_ids: Sequence[UUID]) -> dict[UUID, tuple[CompanyAlias, ...]]:
        """Every alias of several companies at once, grouped by company.

        One statement for the whole shortlist rather than one per candidate: a
        resolution pass over a few hundred postings would otherwise issue a query
        per company it considered, which is the N+1 that makes the difference
        between a pass that finishes and one that is killed.
        """
        if not company_ids:
            return {}
        result = await self._session.execute(
            select(CompanyAliasRow)
            .where(CompanyAliasRow.company_id.in_(company_ids))
            .order_by(CompanyAliasRow.first_seen_at, CompanyAliasRow.id))
        grouped: dict[UUID, list[CompanyAlias]] = {}
        for row in result.scalars():
            grouped.setdefault(row.company_id, []).append(company_alias_to_domain(row))
        return {company_id: tuple(aliases) for company_id, aliases in grouped.items()}

    async def find_candidates(
            self, *, name_forms: Sequence[str] = (), domain: str | None = None,
            ats_platform: AtsPlatform | None = None,
            ats_organization_id: str | None = None,
            limit: int = DEFAULT_LIMIT) -> tuple[CompanyCandidate, ...]:
        forms = tuple(dict.fromkeys(form for form in name_forms if form))
        matches: list[ColumnElement[bool]] = []
        if forms:
            matches.append(CompanyRow.normalized_name.in_(forms))
            # An alias is a name this employer is known by, so a hit on one puts the
            # company on the shortlist exactly as a hit on `normalized_name` does.
            # `IN (subquery)` rather than a join, so an employer with four matching
            # aliases is still one candidate.
            matches.append(CompanyRow.id.in_(
                select(CompanyAliasRow.company_id)
                .where(CompanyAliasRow.normalized_alias.in_(forms))))
        if domain:
            matches.append(CompanyRow.normalized_domain == domain)
        if ats_platform is not None and ats_organization_id:
            # Both halves or neither: an organization id is only an identity within
            # its platform, and `acme` on Greenhouse is not `acme` on Lever.
            matches.append((CompanyRow.ats_platform == ats_platform)
                           & (CompanyRow.ats_organization_id == ats_organization_id))
        if not matches:
            # No evidence to look up means no shortlist. Returning the first hundred
            # companies instead would hand `resolve` a page of employers nothing
            # connects to the claim, and the first one to share a country would look
            # like a candidate.
            return ()
        result = await self._session.execute(
            self._base_select()
            .where(or_(*matches))
            .order_by(CompanyRow.name, CompanyRow.id)
            .limit(limit))
        rows = tuple(result.scalars())
        aliases = await self._aliases_for(tuple(row.id for row in rows))
        return tuple(CompanyCandidate(company_to_domain(row), aliases.get(row.id, ()))
                     for row in rows)

    def _filters(self, filters: CompanyFilter) -> list[ColumnElement[bool]]:
        """`CompanyFilter` as a conjunction of predicates.

        Shared by the page query and the count so the two cannot disagree — a total
        computed under different predicates than the rows is worse than no total.
        """
        predicates: list[ColumnElement[bool]] = []
        if filters.text is not None:
            # Matched on the comparison form, not the display name: a search for
            # `logitech sa` has to find `Logitech S.A.`, and only the normalizer
            # knows that those are the same characters. `autoescape` because a `%`
            # the user typed is a literal percent sign and not "match anything".
            key = normalize_company_name(filters.text)
            if key:
                predicates.append(or_(
                    CompanyRow.normalized_name.contains(key, autoescape=True),
                    CompanyRow.id.in_(
                        select(CompanyAliasRow.company_id)
                        .where(CompanyAliasRow.normalized_alias.contains(
                            key, autoescape=True)))))
            else:
                # A query that is nothing but punctuation has no comparison form, so
                # nothing can match it. `contains("")` would match every employer,
                # which is the opposite of what was asked.
                predicates.append(false())
        if filters.country is not None:
            predicates.append(CompanyRow.country == filters.country)
        if filters.ats_platform is not None:
            predicates.append(CompanyRow.ats_platform == filters.ats_platform)
        if filters.spontaneous_support is not None:
            predicates.append(
                CompanyRow.spontaneous_support == filters.spontaneous_support)
        if filters.has_opportunities is not None:
            linked = exists().where(OpportunityRow.company_id == CompanyRow.id)
            predicates.append(linked if filters.has_opportunities else ~linked)
        return predicates

    async def search(self, filters: CompanyFilter, *, limit: int = DEFAULT_LIMIT,
                     offset: int = 0) -> CompanyPage:
        predicates = self._filters(filters)
        total = await self._session.execute(
            select(func.count()).select_from(CompanyRow).where(*predicates))
        result = await self._session.execute(
            self._base_select()
            .where(*predicates)
            # `id` breaks the tie on name. Without it two employers called `Migros`
            # could be ordered differently by two queries, which is how a paginated
            # list shows one of them twice and the other not at all.
            .order_by(CompanyRow.name, CompanyRow.id)
            .limit(limit).offset(offset))
        return CompanyPage(
            companies=tuple(company_to_domain(row) for row in result.scalars()),
            total=total.scalar_one())

    async def aliases(self, company_id: CompanyId) -> tuple[CompanyAlias, ...]:
        result = await self._session.execute(
            select(CompanyAliasRow)
            .where(CompanyAliasRow.company_id == company_id)
            .order_by(CompanyAliasRow.first_seen_at, CompanyAliasRow.id))
        return tuple(company_alias_to_domain(row) for row in result.scalars())

    async def upsert_alias(self, alias: CompanyAlias) -> CompanyAlias:
        result = await self._session.execute(
            select(CompanyAliasRow).where(CompanyAliasRow.id == alias.id))
        existing = result.scalar_one_or_none()
        if existing is not None:
            # The stored window and the new sighting, unioned. `min`/`max` rather
            # than "overwrite" because a provider may report an *older* sighting
            # than the one already recorded — a seed file being read for the first
            # time, say — and the answer to "since when have we called it this?"
            # must only ever move backwards.
            alias = alias.model_copy(update={
                "first_seen_at": min(alias.first_seen_at, existing.first_seen_at),
                "last_seen_at": max(alias.last_seen_at, existing.last_seen_at)})
        row = company_alias_to_row(alias, existing)
        self._session.add(row)
        await self._session.flush()
        return company_alias_to_domain(row)


class SqlAlchemyCareerSiteRepository:
    """`CareerSiteRepository` over an `AsyncSession`.

    One row per `(company_id, url)`, and the id is uuid5 over exactly that pair, so
    an upsert needs no `SELECT` to know which row it is about — the load here exists
    only to preserve what the stored row already knew.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_company(self, company_id: CompanyId) -> tuple[CareerSite, ...]:
        result = await self._session.execute(
            select(CompanyCareerSiteRow)
            .where(CompanyCareerSiteRow.company_id == company_id)
            .order_by(CompanyCareerSiteRow.discovered_at, CompanyCareerSiteRow.id))
        return tuple(career_site_to_domain(row) for row in result.scalars())

    async def upsert(self, site: CareerSite) -> CareerSite:
        result = await self._session.execute(
            select(CompanyCareerSiteRow).where(CompanyCareerSiteRow.id == site.id))
        existing = result.scalar_one_or_none()
        if existing is not None:
            checked = [instant for instant
                       in (site.last_checked_at, existing.last_checked_at)
                       if instant is not None]
            site = site.model_copy(update={
                "discovered_at": min(site.discovered_at, existing.discovered_at),
                # A pass that did not check the URL leaves `last_checked_at` unset,
                # and must not erase the instant a pass that did check it recorded.
                "last_checked_at": max(checked) if checked else None})
        row = career_site_to_row(site, existing)
        self._session.add(row)
        await self._session.flush()
        return career_site_to_domain(row)


class SqlAlchemyCompanyDiscoveryRepository:
    """`CompanyDiscoveryRepository` over an `AsyncSession`.

    Provenance, so nothing here deletes: a sighting outlives both the pass that
    produced it and — through `ON DELETE SET NULL` — the company it was linked to.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_external(self, provider_key: str,
                              external_id: str) -> CompanyDiscoveryRecord | None:
        result = await self._session.execute(
            select(CompanyDiscoveryRecordRow).where(
                CompanyDiscoveryRecordRow.provider_key == provider_key,
                CompanyDiscoveryRecordRow.external_id == external_id))
        row = result.scalar_one_or_none()
        return None if row is None else discovery_record_to_domain(row)

    async def upsert(self, record: CompanyDiscoveryRecord) -> CompanyDiscoveryRecord:
        result = await self._session.execute(
            select(CompanyDiscoveryRecordRow).where(
                CompanyDiscoveryRecordRow.id == record.id))
        existing = result.scalar_one_or_none()
        if existing is not None:
            # First sighting wins, for the same reason as an alias: §23's repeated
            # pass must leave "when did we first hear about this employer?" alone.
            record = record.model_copy(update={
                "discovered_at": min(record.discovered_at, existing.discovered_at)})
        row = discovery_record_to_row(record, existing)
        self._session.add(row)
        await self._session.flush()
        return discovery_record_to_domain(row)

    async def list_for_company(
            self, company_id: CompanyId, *,
            limit: int = DEFAULT_LIMIT) -> tuple[CompanyDiscoveryRecord, ...]:
        result = await self._session.execute(
            select(CompanyDiscoveryRecordRow)
            .where(CompanyDiscoveryRecordRow.company_id == company_id)
            .order_by(CompanyDiscoveryRecordRow.discovered_at.desc(),
                      CompanyDiscoveryRecordRow.id)
            .limit(limit))
        return tuple(discovery_record_to_domain(row) for row in result.scalars())


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

    async def search_geo(
            self, query: GeoSearchQuery) -> tuple[OpportunityGeoResult, ...]:
        """Run the typed Phase 7 query, leaving distance arithmetic to PostGIS."""
        opportunity_radius_matches = _matched_radius_columns(
            OpportunityRow.location_point, query)
        fallback_radius_matches = _matched_radius_columns(
            CompanyLocationRow.location_point, query)
        resolved_point = OpportunityRow.location_point.is_not(None)
        radius_predicates = [
            or_(opportunity_match,
                and_(~resolved_point, fallback_match))
            for opportunity_match, fallback_match in zip(
                opportunity_radius_matches, fallback_radius_matches, strict=True)
        ]
        geographic_predicates: list[ColumnElement[bool]] = list(radius_predicates)
        if query.countries:
            geographic_predicates.append(or_(
                OpportunityRow.location_country.in_(query.countries),
                and_(OpportunityRow.location_country.is_(None),
                     CompanyLocationRow.location_country.in_(query.countries))))
        remote = OpportunityRow.workplace_mode == WorkplaceMode.REMOTE
        predicates: list[ColumnElement[bool]] = []
        if query.remote_policy is RemotePolicy.REMOTE_ONLY:
            predicates.append(remote)
        elif query.remote_policy is RemotePolicy.INCLUDE_REMOTE:
            predicates.append(or_(*geographic_predicates, remote))
        else:
            predicates.extend((or_(*geographic_predicates), ~remote))

        if query.remote_countries:
            predicates.append(or_(~remote,
                                  OpportunityRow.location_country.in_(
                                      query.remote_countries)))
        if query.opportunity_types:
            predicates.append(
                OpportunityRow.opportunity_type.in_(query.opportunity_types))
        if query.workplace_modes:
            predicates.append(
                OpportunityRow.workplace_mode.in_(query.workplace_modes))
        if query.bounds is not None:
            predicates.append(or_(
                within_bounds(OpportunityRow.location_point, query.bounds),
                and_(~resolved_point,
                     within_bounds(CompanyLocationRow.location_point,
                                   query.bounds))))

        opportunity_distance = _nearest_distance(
            OpportunityRow.location_point, query)
        fallback_distance = _nearest_distance(
            CompanyLocationRow.location_point, query)
        distance = case(
            (resolved_point, opportunity_distance),
            else_=fallback_distance,
        ) if query.radii else opportunity_distance
        selected_matches = [
            case((resolved_point, opportunity_match), else_=fallback_match)
            .label(f"radius_{index}")
            for index, (opportunity_match, fallback_match) in enumerate(zip(
                opportunity_radius_matches, fallback_radius_matches, strict=True))
        ]
        ordering: list[Any] = []
        if query.radii:
            ordering.append(distance.asc().nulls_last())
        ordering.extend((OpportunityRow.discovered_at.desc(), OpportunityRow.id))
        # Which of a company's sites stands in for a posting with no coordinates:
        # the closest to a search centre when the query has radii, otherwise the
        # headquarters. Ordering by `_nearest_distance` unconditionally would emit
        # `ORDER BY NULL` — a syntax error — on a country- or remote-only search.
        site_order: list[Any] = []
        if query.radii:
            site_order.append(
                _nearest_distance(CompanyLocationRow.location_point, query)
                .asc().nulls_last())
        else:
            site_order.append(CompanyLocationRow.is_headquarters.desc())
        site_order.append(CompanyLocationRow.id)
        nearest_site = (
            select(CompanyLocationRow.id)
            .where(CompanyLocationRow.company_id == OpportunityRow.company_id)
            .order_by(*site_order)
            .limit(1).correlate(OpportunityRow).scalar_subquery())
        result = await self._session.execute(
            select(OpportunityRow, CompanyLocationRow,
                   distance.label("distance_meters"), *selected_matches)
            .options(selectinload(OpportunityRow.source))
            .outerjoin(
                CompanyLocationRow,
                and_(CompanyLocationRow.company_id == OpportunityRow.company_id,
                     CompanyLocationRow.id == nearest_site))
            .where(*predicates)
            .order_by(*ordering)
            .limit(query.limit).offset(query.offset))
        found: list[OpportunityGeoResult] = []
        for values in result.all():
            row, company_location_row, meters, *matches = values
            opportunity = opportunity_to_domain(row)
            matched = _matched_radii(matches, query)
            is_remote = opportunity.is_remote
            has_own_point = (opportunity.location is not None
                             and opportunity.location.point is not None)
            fallback = (not has_own_point and company_location_row is not None
                        and company_location_row.location_point is not None)
            company_location = (company_location_to_domain(company_location_row)
                                if fallback else None)
            result_location = (company_location.location if company_location is not None
                               else opportunity.location)
            found.append(OpportunityGeoResult(
                opportunity=opportunity,
                location=result_location,
                distance_meters=None if meters is None else float(meters),
                status=(GeoStatus.REMOTE if is_remote else
                        GeoStatus.RESOLVED if has_own_point else
                        GeoStatus.COMPANY_FALLBACK if fallback else
                        GeoStatus.UNRESOLVED),
                remote_scope=remote_scope_of(opportunity.workplace_mode,
                                             opportunity.location),
                matched_radii=matched,
                company_location=company_location))
        return tuple(found)

    async def list_unlinked(self, *,
                            limit: int = DEFAULT_LIMIT) -> tuple[Opportunity, ...]:
        result = await self._session.execute(
            self._base_select()
            .where(OpportunityRow.company_id.is_(None),
                   # A posting with no employer string is not a seed: there is
                   # nothing to resolve, and returning it would put an unresolvable
                   # row at the head of every pass forever.
                   OpportunityRow.company_name != "")
            # Oldest first, so repeated bounded passes work through the backlog
            # instead of re-reading the same page of recent postings.
            .order_by(OpportunityRow.discovered_at, OpportunityRow.id)
            .limit(limit))
        return tuple(opportunity_to_domain(row) for row in result.scalars())

    async def link_company(self, opportunity_id: OpportunityId,
                           company_id: CompanyId) -> bool:
        result = await self._session.execute(
            update(OpportunityRow)
            .where(OpportunityRow.id == opportunity_id,
                   # `IS DISTINCT FROM` rather than `!=`, which is NULL for a
                   # posting that has never been linked — i.e. every posting this is
                   # called for. It is also what makes a repeated resolution report
                   # `False` instead of writing the value that is already there.
                   OpportunityRow.company_id.is_distinct_from(company_id))
            # One column. `company_name` is not in this statement and cannot be:
            # §13's "the posting's original company name remains provenance" is a
            # property of the SQL here, not a rule somebody has to remember.
            .values(company_id=company_id))
        return bool(_rows_affected(result))


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


class SqlAlchemyEligibilityResultRepository:
    """`EligibilityResultRepository` over an `AsyncSession`.

    The shape is deliberately the twin of `SqlAlchemyMatchEvaluationRepository`:
    the two axes are stored side by side and read the same way, and a reader
    comparing the two files should find nothing surprising in either. Every
    statement carries `user_id`, so another user's verdict reads as absent and an
    upsert cannot take one over — the load misses, the insert runs, and
    `uq_eligibility_results_candidate_profile_id_opportunity_id` rejects it.

    The checks are eager-loaded on every read because they are `lazy="raise"` and
    `eligibility_result_to_row` needs the stored ones to reconcile an upsert; a
    write that had not loaded them would raise on the first check rather than
    replace the set.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_select(self) -> Select[tuple[EligibilityResultRow]]:
        return select(EligibilityResultRow).options(
            selectinload(EligibilityResultRow.checks))

    async def _row_for_pair(
            self, user_id: UserId, candidate_profile_id: CandidateProfileId,
            opportunity_id: OpportunityId) -> EligibilityResultRow | None:
        result = await self._session.execute(
            self._base_select().where(
                EligibilityResultRow.user_id == user_id,
                EligibilityResultRow.candidate_profile_id == candidate_profile_id,
                EligibilityResultRow.opportunity_id == opportunity_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  result_id: EligibilityResultId) -> EligibilityResult | None:
        result = await self._session.execute(
            self._base_select().where(EligibilityResultRow.id == result_id,
                                      EligibilityResultRow.user_id == user_id))
        row = result.scalar_one_or_none()
        return None if row is None else eligibility_result_to_domain(row)

    async def get_for_pair(self, user_id: UserId,
                           candidate_profile_id: CandidateProfileId,
                           opportunity_id: OpportunityId) -> EligibilityResult | None:
        row = await self._row_for_pair(user_id, candidate_profile_id, opportunity_id)
        return None if row is None else eligibility_result_to_domain(row)

    async def upsert(self, result: EligibilityResult) -> EligibilityResult:
        existing = await self._row_for_pair(result.user_id,
                                            result.candidate_profile_id,
                                            result.opportunity_id)
        row = eligibility_result_to_row(result, existing)
        self._session.add(row)
        await self._session.flush()
        return eligibility_result_to_domain(row)

    async def list_for_user(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT) -> tuple[EligibilityResult, ...]:
        result = await self._session.execute(
            self._base_select()
            .where(EligibilityResultRow.user_id == user_id)
            .order_by(EligibilityResultRow.determined_at.desc(),
                      EligibilityResultRow.id)
            .limit(limit))
        return tuple(eligibility_result_to_domain(row) for row in result.scalars())


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

    All five child collections are eager-loaded on every read, because they are
    `lazy="raise"` and `candidate_profile_to_row` needs them to reconcile an
    existing row — an upsert that had not loaded them would raise on the first
    child rather than write a profile with none. Since Phase 10 that includes the
    evidence and claims the truth guard rests on.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_select(self) -> Select[tuple[CandidateProfileRow]]:
        return select(CandidateProfileRow).options(
            selectinload(CandidateProfileRow.languages),
            selectinload(CandidateProfileRow.work_authorizations),
            selectinload(CandidateProfileRow.availability_slots),
            selectinload(CandidateProfileRow.evidence),
            selectinload(CandidateProfileRow.claims))

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


class SqlAlchemyCandidateDocumentRepository:
    """`CandidateDocumentRepository` over an `AsyncSession`, `user_id` on every read.

    Another user's document reads as absent and an upsert cannot take one over: the
    load misses on the `user_id` predicate, the insert runs, and the primary key
    rejects it. The versions are eager-loaded on every read because they are
    `lazy="raise"` and `candidate_document_to_row` needs the stored ones to
    reconcile an append — a write that had not loaded them would raise on the first
    version rather than add one.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_select(self) -> Select[tuple[CandidateDocumentRow]]:
        return select(CandidateDocumentRow).options(
            selectinload(CandidateDocumentRow.versions))

    async def _row(self, user_id: UserId,
                   document_id: CandidateDocumentId) -> CandidateDocumentRow | None:
        result = await self._session.execute(
            self._base_select().where(CandidateDocumentRow.id == document_id,
                                      CandidateDocumentRow.user_id == user_id))
        return result.scalar_one_or_none()

    async def _row_for_pair(
            self, user_id: UserId, candidate_profile_id: CandidateProfileId,
            opportunity_id: OpportunityId, document_type: CandidateDocumentType
    ) -> CandidateDocumentRow | None:
        result = await self._session.execute(
            self._base_select().where(
                CandidateDocumentRow.user_id == user_id,
                CandidateDocumentRow.candidate_profile_id == candidate_profile_id,
                CandidateDocumentRow.opportunity_id == opportunity_id,
                CandidateDocumentRow.document_type == document_type))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  document_id: CandidateDocumentId) -> CandidateDocument | None:
        row = await self._row(user_id, document_id)
        return None if row is None else candidate_document_to_domain(row)

    async def get_for_pair(self, user_id: UserId,
                           candidate_profile_id: CandidateProfileId,
                           opportunity_id: OpportunityId,
                           document_type: CandidateDocumentType
                           ) -> CandidateDocument | None:
        row = await self._row_for_pair(user_id, candidate_profile_id,
                                       opportunity_id, document_type)
        return None if row is None else candidate_document_to_domain(row)

    async def upsert(self, document: CandidateDocument) -> CandidateDocument:
        existing = await self._row_for_pair(
            document.user_id, document.candidate_profile_id, document.opportunity_id,
            document.document_type)
        row = candidate_document_to_row(document, existing)
        self._session.add(row)
        await self._session.flush()
        return candidate_document_to_domain(row)

    async def list_for_user(
            self, user_id: UserId, *,
            limit: int = DEFAULT_LIMIT) -> tuple[CandidateDocument, ...]:
        result = await self._session.execute(
            self._base_select()
            .where(CandidateDocumentRow.user_id == user_id)
            .order_by(CandidateDocumentRow.updated_at.desc(), CandidateDocumentRow.id)
            .limit(limit))
        return tuple(candidate_document_to_domain(row) for row in result.scalars())


class SqlAlchemyLLMConnectionRepository:
    """`LLMConnectionRepository` over an `AsyncSession`, `user_id` on every statement.

    Another user's connection reads as absent and an upsert cannot take one over: the
    load misses on the `user_id` predicate, the insert runs, and the primary key
    rejects it. No `selectinload`: `llm_connection_to_domain` does not read the
    `sessions` relationship, which exists for the cascade, so loading it on every read
    would be work for data nothing looks at.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, user_id: UserId,
                   connection_id: LLMConnectionId) -> LLMConnectionRow | None:
        result = await self._session.execute(
            select(LLMConnectionRow).where(LLMConnectionRow.id == connection_id,
                                           LLMConnectionRow.user_id == user_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  connection_id: LLMConnectionId) -> LLMConnection | None:
        row = await self._row(user_id, connection_id)
        return None if row is None else llm_connection_to_domain(row)

    async def get_default(self, user_id: UserId) -> LLMConnection | None:
        result = await self._session.execute(
            select(LLMConnectionRow).where(LLMConnectionRow.user_id == user_id,
                                           LLMConnectionRow.is_default.is_(True)))
        row = result.scalar_one_or_none()
        return None if row is None else llm_connection_to_domain(row)

    async def list_for_user(self, user_id: UserId, *, enabled_only: bool = False,
                            limit: int = DEFAULT_LIMIT) -> tuple[LLMConnection, ...]:
        statement = select(LLMConnectionRow).where(
            LLMConnectionRow.user_id == user_id)
        if enabled_only:
            statement = statement.where(LLMConnectionRow.enabled.is_(True))
        result = await self._session.execute(
            statement
            # Priority then id — the router's own order, so the registry a caller
            # builds from this list is tried in the sequence the routing rule expects.
            .order_by(LLMConnectionRow.priority, LLMConnectionRow.id)
            .limit(limit))
        return tuple(llm_connection_to_domain(row) for row in result.scalars())

    async def upsert(self, connection: LLMConnection) -> LLMConnection:
        row = llm_connection_to_row(
            connection, await self._row(connection.user_id, connection.id))
        self._session.add(row)
        # A second default for the same account fails here on
        # `uq_llm_connections_user_id_default` — which is why a service promoting a
        # connection calls `clear_default` first.
        await self._session.flush()
        return llm_connection_to_domain(row)

    async def clear_default(self, user_id: UserId) -> int:
        result = await self._session.execute(
            update(LLMConnectionRow)
            .where(LLMConnectionRow.user_id == user_id,
                   LLMConnectionRow.is_default.is_(True))
            .values(is_default=False))
        return _rows_affected(result)

    async def delete(self, user_id: UserId,
                     connection_id: LLMConnectionId) -> bool:
        result = await self._session.execute(
            delete(LLMConnectionRow).where(LLMConnectionRow.id == connection_id,
                                           LLMConnectionRow.user_id == user_id))
        # Provider sessions go with it by `ON DELETE CASCADE`; telemetry runs stay,
        # their `connection_id` set NULL, so a bulk `DELETE` is right — no children
        # need to be in the identity map for the rows to disappear.
        return bool(_rows_affected(result))


class SqlAlchemyProviderSessionRepository:
    """`ProviderSessionRepository` over an `AsyncSession`, `user_id` on every read.

    The id is uuid5 over `(connection_id, conversation_key)`, so an upsert loads by id
    only to preserve what the stored row knew; the `get` a resume performs is scoped
    by `user_id`, so one account cannot continue another's provider-side conversation.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UserId, connection_id: LLMConnectionId,
                  conversation_key: str) -> ProviderSession | None:
        result = await self._session.execute(
            select(ProviderSessionRow).where(
                ProviderSessionRow.user_id == user_id,
                ProviderSessionRow.connection_id == connection_id,
                ProviderSessionRow.conversation_key == conversation_key))
        row = result.scalar_one_or_none()
        return None if row is None else provider_session_to_domain(row)

    async def upsert(self, session: ProviderSession) -> ProviderSession:
        result = await self._session.execute(
            select(ProviderSessionRow).where(ProviderSessionRow.id == session.id))
        existing = result.scalar_one_or_none()
        row = provider_session_to_row(session, existing)
        self._session.add(row)
        await self._session.flush()
        return provider_session_to_domain(row)


class SqlAlchemyLLMRunRepository:
    """`LLMRunRepository` over an `AsyncSession`.

    The write is not user-scoped, because a run's owner is nullable — a healthcheck
    probe has none — but `list_for_user` is, so a telemetry screen only ever surfaces
    its own account's runs. The upsert loads by the run's own id, so writing the same
    id twice (STARTED, then a terminal state) updates the one row.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, run_id: UUID) -> LLMRunRow | None:
        result = await self._session.execute(
            select(LLMRunRow).where(LLMRunRow.id == run_id))
        return result.scalar_one_or_none()

    async def upsert(self, run: LLMRun) -> LLMRun:
        row = llm_run_to_row(run, await self._row(run.id))
        self._session.add(row)
        await self._session.flush()
        return llm_run_to_domain(row)

    async def list_for_user(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT) -> tuple[LLMRun, ...]:
        result = await self._session.execute(
            select(LLMRunRow)
            .where(LLMRunRow.user_id == user_id)
            .order_by(LLMRunRow.started_at.desc(), LLMRunRow.id)
            .limit(limit))
        return tuple(llm_run_to_domain(row) for row in result.scalars())





class SqlAlchemyApplicationPolicyRepository:
    """`ApplicationPolicyRepository` over an `AsyncSession`.

    Every statement carries `user_id`, so another account's policy reads as absent.
    `get_default` returns the most recently updated *active* policy, the row the
    engine reads before every submission.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, user_id: UserId,
                   policy_id: ApplicationPolicyId) -> ApplicationPolicyRow | None:
        result = await self._session.execute(
            select(ApplicationPolicyRow).where(
                ApplicationPolicyRow.id == policy_id,
                ApplicationPolicyRow.user_id == user_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  policy_id: ApplicationPolicyId) -> ApplicationPolicy | None:
        row = await self._row(user_id, policy_id)
        return None if row is None else application_policy_to_domain(row)

    async def get_default(self, user_id: UserId) -> ApplicationPolicy | None:
        result = await self._session.execute(
            select(ApplicationPolicyRow)
            .where(ApplicationPolicyRow.user_id == user_id,
                   ApplicationPolicyRow.is_active)
            .order_by(ApplicationPolicyRow.updated_at.desc(), ApplicationPolicyRow.id)
            .limit(1))
        row = result.scalar_one_or_none()
        return None if row is None else application_policy_to_domain(row)

    async def list_for_user(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT
                            ) -> tuple[ApplicationPolicy, ...]:
        result = await self._session.execute(
            select(ApplicationPolicyRow)
            .where(ApplicationPolicyRow.user_id == user_id)
            .order_by(ApplicationPolicyRow.updated_at.desc(), ApplicationPolicyRow.id)
            .limit(limit))
        return tuple(application_policy_to_domain(row) for row in result.scalars())

    async def upsert(self, policy: ApplicationPolicy) -> ApplicationPolicy:
        row = application_policy_to_row(
            policy, await self._row(policy.user_id, policy.id))
        self._session.add(row)
        await self._session.flush()
        return application_policy_to_domain(row)


class SqlAlchemyApplicationDecisionRepository:
    """`ApplicationDecisionRepository` over an `AsyncSession`.

    User-scoped everywhere. `get_for_pair` returns the most recent decision for a
    (candidate, opportunity) pair, so a re-decided pair reads its latest intent.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, user_id: UserId,
                   decision_id: ApplicationDecisionId
                   ) -> ApplicationDecisionRow | None:
        result = await self._session.execute(
            select(ApplicationDecisionRow).where(
                ApplicationDecisionRow.id == decision_id,
                ApplicationDecisionRow.user_id == user_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  decision_id: ApplicationDecisionId) -> ApplicationDecision | None:
        row = await self._row(user_id, decision_id)
        return None if row is None else application_decision_to_domain(row)

    async def get_for_pair(self, user_id: UserId,
                           candidate_profile_id: CandidateProfileId,
                           opportunity_id: OpportunityId
                           ) -> ApplicationDecision | None:
        result = await self._session.execute(
            select(ApplicationDecisionRow)
            .where(ApplicationDecisionRow.user_id == user_id,
                   ApplicationDecisionRow.candidate_profile_id == candidate_profile_id,
                   ApplicationDecisionRow.opportunity_id == opportunity_id)
            .order_by(ApplicationDecisionRow.decided_at.desc(),
                      ApplicationDecisionRow.id)
            .limit(1))
        row = result.scalar_one_or_none()
        return None if row is None else application_decision_to_domain(row)

    async def upsert(self, decision: ApplicationDecision) -> ApplicationDecision:
        row = application_decision_to_row(
            decision, await self._row(decision.user_id, decision.id))
        self._session.add(row)
        await self._session.flush()
        return application_decision_to_domain(row)

    async def list_for_user(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT
                            ) -> tuple[ApplicationDecision, ...]:
        result = await self._session.execute(
            select(ApplicationDecisionRow)
            .where(ApplicationDecisionRow.user_id == user_id)
            .order_by(ApplicationDecisionRow.decided_at.desc(),
                      ApplicationDecisionRow.id)
            .limit(limit))
        return tuple(application_decision_to_domain(row) for row in result.scalars())


_SUBMISSION_BUDGET_LOCK_NAMESPACE = 0x4150504C  # "APPL" — namespaces this lock class.


def _submission_budget_lock_key(user_id: UserId) -> int:
    """A stable signed int4 advisory-lock key for one user's submission budget.

    `pg_advisory_xact_lock(int4, int4)` takes two 32-bit integers; the first
    namespaces the lock so it cannot collide with an unrelated advisory lock, the
    second identifies the user. The user is a UUID, so we fold its first four bytes
    into a signed int4. A collision between two *different* users is harmless — it
    only serializes them against each other briefly — while the same user always maps
    to the same key, which is the only guarantee the reservation needs.
    """
    return int.from_bytes(user_id.bytes[:4], "big", signed=True)


class SqlAlchemyApplicationRepository:
    """`ApplicationRepository` over an `AsyncSession`.

    The `events` and `attempts` relationships are never eager-loaded here and never
    written by an application upsert — they are append-only and owned by their own
    repositories. `count_active_submissions_since` and `list_in_flight` are the two
    reads the rate limit (§49) and startup recovery (§88) depend on; the latter is
    deliberately unscoped, because recovery sweeps every account's stuck runs. The
    count is meant to be read under `lock_submission_budget`, whose transaction-scoped
    advisory lock makes the reservation atomic against another worker of the same user.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, user_id: UserId,
                   application_id: ApplicationId) -> ApplicationRow | None:
        result = await self._session.execute(
            select(ApplicationRow).where(ApplicationRow.id == application_id,
                                         ApplicationRow.user_id == user_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  application_id: ApplicationId) -> Application | None:
        row = await self._row(user_id, application_id)
        return None if row is None else application_to_domain(row)

    async def get_by_idempotency_key(self, user_id: UserId,
                                     idempotency_key: str) -> Application | None:
        result = await self._session.execute(
            select(ApplicationRow).where(
                ApplicationRow.idempotency_key == idempotency_key,
                ApplicationRow.user_id == user_id))
        row = result.scalar_one_or_none()
        return None if row is None else application_to_domain(row)

    async def upsert(self, application: Application) -> Application:
        row = application_to_row(
            application, await self._row(application.user_id, application.id))
        self._session.add(row)
        await self._session.flush()
        return application_to_domain(row)

    async def list_for_user(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT) -> tuple[Application, ...]:
        result = await self._session.execute(
            select(ApplicationRow)
            .where(ApplicationRow.user_id == user_id)
            .order_by(ApplicationRow.updated_at.desc(), ApplicationRow.id)
            .limit(limit))
        return tuple(application_to_domain(row) for row in result.scalars())

    async def lock_submission_budget(self, user_id: UserId) -> None:
        await self._session.execute(
            select(func.pg_advisory_xact_lock(
                _SUBMISSION_BUDGET_LOCK_NAMESPACE,
                _submission_budget_lock_key(user_id))))

    async def count_active_submissions_since(self, user_id: UserId,
                                             since: datetime) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(ApplicationRow)
            .where(ApplicationRow.user_id == user_id,
                   ApplicationRow.state.in_((
                       ApplicationState.SUBMITTED.value,
                       ApplicationState.SUBMITTING.value,
                       ApplicationState.SUBMISSION_STATE_UNKNOWN.value)),
                   ApplicationRow.updated_at >= since))
        return int(result.scalar_one())

    async def list_in_flight(self, *,
                             limit: int = DEFAULT_LIMIT) -> tuple[Application, ...]:
        result = await self._session.execute(
            select(ApplicationRow)
            .where(ApplicationRow.state.in_((ApplicationState.SUBMITTING.value,
                                             ApplicationState.PREPARING.value)))
            .order_by(ApplicationRow.updated_at, ApplicationRow.id)
            .limit(limit))
        return tuple(application_to_domain(row) for row in result.scalars())


class SqlAlchemyApplicationEventRepository:
    """`ApplicationEventRepository` over an `AsyncSession`.

    Append-only: there is only `append`, which always inserts (an event id is
    unique). The list read joins to `applications` for the ownership check, so one
    account cannot read another's trail by id.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: ApplicationEvent) -> ApplicationEvent:
        row = application_event_to_row(event)
        self._session.add(row)
        await self._session.flush()
        return application_event_to_domain(row)

    async def list_for_application(self, user_id: UserId,
                                   application_id: ApplicationId, *,
                                   limit: int = DEFAULT_LIMIT
                                   ) -> tuple[ApplicationEvent, ...]:
        result = await self._session.execute(
            select(ApplicationEventRow)
            .join(ApplicationRow,
                  ApplicationRow.id == ApplicationEventRow.application_id)
            .where(ApplicationEventRow.application_id == application_id,
                   ApplicationRow.user_id == user_id)
            .order_by(ApplicationEventRow.occurred_at, ApplicationEventRow.id)
            .limit(limit))
        return tuple(application_event_to_domain(row) for row in result.scalars())


class SqlAlchemySubmissionAttemptRepository:
    """`SubmissionAttemptRepository` over an `AsyncSession`.

    Keyed on the attempt's own id, so the in-flight row and its completion are one
    upsert. The list read joins to `applications` for the ownership check, like the
    event trail.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, attempt_id: SubmissionAttemptId
                   ) -> SubmissionAttemptRow | None:
        result = await self._session.execute(
            select(SubmissionAttemptRow).where(
                SubmissionAttemptRow.id == attempt_id))
        return result.scalar_one_or_none()

    async def upsert(self, attempt: SubmissionAttempt) -> SubmissionAttempt:
        row = submission_attempt_to_row(attempt, await self._row(attempt.id))
        self._session.add(row)
        await self._session.flush()
        return submission_attempt_to_domain(row)

    async def get(self, application_id: ApplicationId,
                  attempt_number: int) -> SubmissionAttempt | None:
        result = await self._session.execute(
            select(SubmissionAttemptRow).where(
                SubmissionAttemptRow.application_id == application_id,
                SubmissionAttemptRow.attempt_number == attempt_number))
        row = result.scalar_one_or_none()
        return None if row is None else submission_attempt_to_domain(row)

    async def list_for_application(self, user_id: UserId,
                                   application_id: ApplicationId, *,
                                   limit: int = DEFAULT_LIMIT
                                   ) -> tuple[SubmissionAttempt, ...]:
        result = await self._session.execute(
            select(SubmissionAttemptRow)
            .join(ApplicationRow,
                  ApplicationRow.id == SubmissionAttemptRow.application_id)
            .where(SubmissionAttemptRow.application_id == application_id,
                   ApplicationRow.user_id == user_id)
            .order_by(SubmissionAttemptRow.attempt_number)
            .limit(limit))
        return tuple(submission_attempt_to_domain(row) for row in result.scalars())


class SqlAlchemyConversationRepository:
    """`ConversationRepository` over an `AsyncSession`, `user_id` on every read.

    The upsert loads by `(user_id, id)` so a write can never reach across accounts —
    exactly as `SqlAlchemyApplicationRepository` does — and `list_for_user` orders by
    the last activity, falling back to `updated_at` for a freshly opened thread that
    has no turn yet.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, user_id: UserId,
                   conversation_id: ConversationId) -> ConversationRow | None:
        result = await self._session.execute(
            select(ConversationRow).where(ConversationRow.id == conversation_id,
                                          ConversationRow.user_id == user_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  conversation_id: ConversationId) -> Conversation | None:
        row = await self._row(user_id, conversation_id)
        return None if row is None else conversation_to_domain(row)

    async def upsert(self, conversation: Conversation) -> Conversation:
        row = conversation_to_row(
            conversation, await self._row(conversation.user_id, conversation.id))
        self._session.add(row)
        await self._session.flush()
        return conversation_to_domain(row)

    async def list_for_user(self, user_id: UserId, *, include_archived: bool = False,
                            limit: int = DEFAULT_LIMIT) -> tuple[Conversation, ...]:
        statement = select(ConversationRow).where(ConversationRow.user_id == user_id)
        if not include_archived:
            statement = statement.where(ConversationRow.is_archived.is_(False))
        statement = statement.order_by(
            func.coalesce(ConversationRow.last_message_at,
                          ConversationRow.updated_at).desc(),
            ConversationRow.id).limit(limit)
        result = await self._session.execute(statement)
        return tuple(conversation_to_domain(row) for row in result.scalars())


class SqlAlchemyChatMessageRepository:
    """`ChatMessageRepository` over an `AsyncSession`.

    A message carries its own `user_id`, so reads scope on it directly rather than
    joining the conversation. The upsert loads by the message's derived id, so
    re-finalizing a turn writes the same row; `latest_sequence` is a `MAX` the service
    numbers the next turn from without loading the thread.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, message_id: UUID) -> ChatMessageRow | None:
        result = await self._session.execute(
            select(ChatMessageRow).where(ChatMessageRow.id == message_id))
        return result.scalar_one_or_none()

    async def upsert(self, message: ChatMessage) -> ChatMessage:
        row = chat_message_to_row(message, await self._row(message.id))
        self._session.add(row)
        await self._session.flush()
        return chat_message_to_domain(row)

    async def latest_sequence(self, user_id: UserId,
                              conversation_id: ConversationId) -> int | None:
        result = await self._session.execute(
            select(func.max(ChatMessageRow.sequence)).where(
                ChatMessageRow.conversation_id == conversation_id,
                ChatMessageRow.user_id == user_id))
        return result.scalar_one()

    async def list_for_conversation(
            self, user_id: UserId, conversation_id: ConversationId, *,
            newest_first: bool = False,
            limit: int = DEFAULT_LIMIT) -> tuple[ChatMessage, ...]:
        order = (ChatMessageRow.sequence.desc() if newest_first
                 else ChatMessageRow.sequence.asc())
        result = await self._session.execute(
            select(ChatMessageRow)
            .where(ChatMessageRow.conversation_id == conversation_id,
                   ChatMessageRow.user_id == user_id)
            .order_by(order).limit(limit))
        return tuple(chat_message_to_domain(row) for row in result.scalars())


class SqlAlchemyChatActionProposalRepository:
    """`ChatActionProposalRepository` over an `AsyncSession`, `user_id` on every read.

    A proposal carries its own `user_id`, so the `get` the executor performs before
    acting scopes on it — a confirmation naming another account's proposal reads as
    absent. The upsert loads by the proposal's derived id, so re-finalizing a turn
    writes the same rows and a confirm or dismiss updates the one row's status.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, user_id: UserId,
                   proposal_id: ChatActionProposalId) -> ChatActionProposalRow | None:
        result = await self._session.execute(
            select(ChatActionProposalRow).where(
                ChatActionProposalRow.id == proposal_id,
                ChatActionProposalRow.user_id == user_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  proposal_id: ChatActionProposalId) -> ChatActionProposal | None:
        row = await self._row(user_id, proposal_id)
        return None if row is None else chat_action_proposal_to_domain(row)

    async def upsert(self, proposal: ChatActionProposal) -> ChatActionProposal:
        row = chat_action_proposal_to_row(
            proposal, await self._row(proposal.user_id, proposal.id))
        self._session.add(row)
        await self._session.flush()
        return chat_action_proposal_to_domain(row)

    async def list_for_conversation(
            self, user_id: UserId, conversation_id: ConversationId, *,
            limit: int = DEFAULT_LIMIT) -> tuple[ChatActionProposal, ...]:
        result = await self._session.execute(
            select(ChatActionProposalRow)
            .where(ChatActionProposalRow.conversation_id == conversation_id,
                   ChatActionProposalRow.user_id == user_id)
            .order_by(ChatActionProposalRow.created_at, ChatActionProposalRow.id)
            .limit(limit))
        return tuple(chat_action_proposal_to_domain(row) for row in result.scalars())


class SqlAlchemyChatActionExecutionRepository:
    """`ChatActionExecutionRepository` over an `AsyncSession`, `user_id` on every read.

    Keyed on the execution's derived id (from the proposal alone), so a double-confirm
    upserts the one row rather than recording two attempts. The `get` the executor uses
    to detect an already-run proposal scopes on the audit's own `user_id`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, execution_id: UUID) -> ChatActionExecutionRow | None:
        result = await self._session.execute(
            select(ChatActionExecutionRow).where(
                ChatActionExecutionRow.id == execution_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  proposal_id: ChatActionProposalId) -> ChatActionExecution | None:
        result = await self._session.execute(
            select(ChatActionExecutionRow).where(
                ChatActionExecutionRow.proposal_id == proposal_id,
                ChatActionExecutionRow.user_id == user_id))
        row = result.scalar_one_or_none()
        return None if row is None else chat_action_execution_to_domain(row)

    async def upsert(self, execution: ChatActionExecution) -> ChatActionExecution:
        row = chat_action_execution_to_row(execution, await self._row(execution.id))
        self._session.add(row)
        await self._session.flush()
        return chat_action_execution_to_domain(row)


class SqlAlchemyInterviewSessionRepository:
    """`InterviewSessionRepository` over an `AsyncSession`, `user_id` on every read.

    The upsert loads by `(user_id, id)`, so a write can never reach across accounts and
    a lifecycle step (start, adapt, complete) re-finalized after a failed flush writes
    the same row. `list_for_user` orders by the last update, so the practice history
    reads most-recent-first.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, user_id: UserId,
                   session_id: InterviewSessionId) -> InterviewSessionRow | None:
        result = await self._session.execute(
            select(InterviewSessionRow).where(
                InterviewSessionRow.id == session_id,
                InterviewSessionRow.user_id == user_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  session_id: InterviewSessionId) -> InterviewSession | None:
        row = await self._row(user_id, session_id)
        return None if row is None else interview_session_to_domain(row)

    async def upsert(self, session: InterviewSession) -> InterviewSession:
        row = interview_session_to_row(
            session, await self._row(session.user_id, session.id))
        self._session.add(row)
        await self._session.flush()
        return interview_session_to_domain(row)

    async def list_for_user(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT) -> tuple[InterviewSession, ...]:
        result = await self._session.execute(
            select(InterviewSessionRow)
            .where(InterviewSessionRow.user_id == user_id)
            .order_by(InterviewSessionRow.updated_at.desc(), InterviewSessionRow.id)
            .limit(limit))
        return tuple(interview_session_to_domain(row) for row in result.scalars())


class SqlAlchemyInterviewQuestionRepository:
    """`InterviewQuestionRepository` over an `AsyncSession`.

    A question carries its own `user_id`, so reads scope on it directly. The upsert loads
    by the question's derived id, so re-finalizing a turn writes the same row;
    `latest_sequence` is a `MAX` the service numbers the next turn from without loading
    the session.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, question_id: InterviewQuestionId) -> InterviewQuestionRow | None:
        result = await self._session.execute(
            select(InterviewQuestionRow).where(InterviewQuestionRow.id == question_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  question_id: InterviewQuestionId) -> InterviewQuestion | None:
        result = await self._session.execute(
            select(InterviewQuestionRow).where(
                InterviewQuestionRow.id == question_id,
                InterviewQuestionRow.user_id == user_id))
        row = result.scalar_one_or_none()
        return None if row is None else interview_question_to_domain(row)

    async def upsert(self, question: InterviewQuestion) -> InterviewQuestion:
        row = interview_question_to_row(question, await self._row(question.id))
        self._session.add(row)
        await self._session.flush()
        return interview_question_to_domain(row)

    async def latest_sequence(self, user_id: UserId,
                              session_id: InterviewSessionId) -> int | None:
        result = await self._session.execute(
            select(func.max(InterviewQuestionRow.sequence)).where(
                InterviewQuestionRow.session_id == session_id,
                InterviewQuestionRow.user_id == user_id))
        return result.scalar_one()

    async def list_for_session(
            self, user_id: UserId, session_id: InterviewSessionId, *,
            limit: int = DEFAULT_LIMIT) -> tuple[InterviewQuestion, ...]:
        result = await self._session.execute(
            select(InterviewQuestionRow)
            .where(InterviewQuestionRow.session_id == session_id,
                   InterviewQuestionRow.user_id == user_id)
            .order_by(InterviewQuestionRow.sequence).limit(limit))
        return tuple(interview_question_to_domain(row) for row in result.scalars())


class SqlAlchemyInterviewAnswerRepository:
    """`InterviewAnswerRepository` over an `AsyncSession`, `user_id` on every read.

    One answer per question — the id derives from the question — so the upsert loads by
    that id and a resubmit lands on the same row. `get_for_question` scopes on the
    answer's own `user_id`, so another account's question reads as unanswered.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, answer_id: UUID) -> InterviewAnswerRow | None:
        result = await self._session.execute(
            select(InterviewAnswerRow).where(InterviewAnswerRow.id == answer_id))
        return result.scalar_one_or_none()

    async def get_for_question(self, user_id: UserId,
                               question_id: InterviewQuestionId) -> InterviewAnswer | None:
        result = await self._session.execute(
            select(InterviewAnswerRow).where(
                InterviewAnswerRow.question_id == question_id,
                InterviewAnswerRow.user_id == user_id))
        row = result.scalar_one_or_none()
        return None if row is None else interview_answer_to_domain(row)

    async def upsert(self, answer: InterviewAnswer) -> InterviewAnswer:
        row = interview_answer_to_row(answer, await self._row(answer.id))
        self._session.add(row)
        await self._session.flush()
        return interview_answer_to_domain(row)

    async def list_for_session(
            self, user_id: UserId, session_id: InterviewSessionId, *,
            limit: int = DEFAULT_LIMIT) -> tuple[InterviewAnswer, ...]:
        result = await self._session.execute(
            select(InterviewAnswerRow)
            .where(InterviewAnswerRow.session_id == session_id,
                   InterviewAnswerRow.user_id == user_id)
            .order_by(InterviewAnswerRow.answered_at, InterviewAnswerRow.id)
            .limit(limit))
        return tuple(interview_answer_to_domain(row) for row in result.scalars())


class SqlAlchemyInterviewAnswerEvaluationRepository:
    """`InterviewAnswerEvaluationRepository` over an `AsyncSession`, `user_id` on reads.

    One evaluation per answer — the id derives from the answer — so a re-grade overwrites
    the one row. `list_for_session` is what `aggregate_session_readiness` reads: readiness
    is computed from these, never authored by a provider (§33-36).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, evaluation_id: UUID) -> InterviewAnswerEvaluationRow | None:
        result = await self._session.execute(
            select(InterviewAnswerEvaluationRow).where(
                InterviewAnswerEvaluationRow.id == evaluation_id))
        return result.scalar_one_or_none()

    async def get_for_answer(
            self, user_id: UserId,
            answer_id: InterviewAnswerId) -> InterviewAnswerEvaluation | None:
        result = await self._session.execute(
            select(InterviewAnswerEvaluationRow).where(
                InterviewAnswerEvaluationRow.answer_id == answer_id,
                InterviewAnswerEvaluationRow.user_id == user_id))
        row = result.scalar_one_or_none()
        return None if row is None else interview_answer_evaluation_to_domain(row)

    async def upsert(self,
                     evaluation: InterviewAnswerEvaluation) -> InterviewAnswerEvaluation:
        row = interview_answer_evaluation_to_row(
            evaluation, await self._row(evaluation.id))
        self._session.add(row)
        await self._session.flush()
        return interview_answer_evaluation_to_domain(row)

    async def list_for_session(
            self, user_id: UserId, session_id: InterviewSessionId, *,
            limit: int = DEFAULT_LIMIT) -> tuple[InterviewAnswerEvaluation, ...]:
        result = await self._session.execute(
            select(InterviewAnswerEvaluationRow)
            .where(InterviewAnswerEvaluationRow.session_id == session_id,
                   InterviewAnswerEvaluationRow.user_id == user_id)
            .order_by(InterviewAnswerEvaluationRow.evaluated_at,
                      InterviewAnswerEvaluationRow.id)
            .limit(limit))
        return tuple(interview_answer_evaluation_to_domain(row)
                     for row in result.scalars())


class SqlAlchemyInterviewSessionSummaryRepository:
    """`InterviewSessionSummaryRepository` over an `AsyncSession`, `user_id` on reads.

    One summary per session — the id derives from the session — so completing it twice
    reuses the row. `list_for_user` is the readiness history a candidate watches over
    repeated practice (§74): the stored summaries, most recent first.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, user_id: UserId,
                   session_id: InterviewSessionId) -> InterviewSessionSummaryRow | None:
        result = await self._session.execute(
            select(InterviewSessionSummaryRow).where(
                InterviewSessionSummaryRow.session_id == session_id,
                InterviewSessionSummaryRow.user_id == user_id))
        return result.scalar_one_or_none()

    async def get(self, user_id: UserId,
                  session_id: InterviewSessionId) -> InterviewSessionSummary | None:
        row = await self._row(user_id, session_id)
        return None if row is None else interview_session_summary_to_domain(row)

    async def upsert(self,
                     summary: InterviewSessionSummary) -> InterviewSessionSummary:
        row = interview_session_summary_to_row(
            summary, await self._row(summary.user_id, summary.session_id))
        self._session.add(row)
        await self._session.flush()
        return interview_session_summary_to_domain(row)

    async def list_for_user(
            self, user_id: UserId, *,
            limit: int = DEFAULT_LIMIT) -> tuple[InterviewSessionSummary, ...]:
        result = await self._session.execute(
            select(InterviewSessionSummaryRow)
            .where(InterviewSessionSummaryRow.user_id == user_id)
            .order_by(InterviewSessionSummaryRow.created_at.desc(),
                      InterviewSessionSummaryRow.id)
            .limit(limit))
        return tuple(interview_session_summary_to_domain(row)
                     for row in result.scalars())


