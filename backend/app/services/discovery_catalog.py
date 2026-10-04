"""Swiss saved-search collection and durable, owner-scoped catalog reporting."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.discovery.catalog import canonical_target, matching_opportunities
from backend.app.discovery.orchestrator import DiscoveryOrchestrator
from backend.app.discovery.requests import countries_in_scope
from backend.app.domain.discovery_run import (
    DiscoveryRun,
    DiscoveryRunStatus,
    DiscoveryTrigger,
    SourceHealthSummary,
)
from backend.app.domain.identifiers import DiscoveryRunId, OpportunityId, SearchProfileId, UserId
from backend.app.infrastructure.database.engine import session_scope
from backend.app.repositories.discovery_catalog import (
    CatalogOpportunityStore,
    DiscoveryRunStore,
)
from backend.app.repositories.sqlalchemy_repositories import SqlAlchemySearchProfileRepository

Clock = Callable[[], datetime]
MAX_OBSERVATIONS_PER_RUN = 1000


class DiscoveryCatalogService:
    """One run owns two commits: RUNNING first, then all observations and report."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession],
        orchestrator: DiscoveryOrchestrator, *,
        clock: Clock = lambda: datetime.now(UTC),
        max_observations: int = MAX_OBSERVATIONS_PER_RUN,
    ) -> None:
        if not 1 <= max_observations <= MAX_OBSERVATIONS_PER_RUN:
            raise ValueError("max_observations must be between 1 and 1000")
        self._session_factory = session_factory
        self._orchestrator = orchestrator
        self._clock = clock
        self._max_observations = max_observations

    async def run_profile(
        self, user_id: UserId, search_profile_id: SearchProfileId, *,
        trigger: Literal["MANUAL", "MORNING"],
    ) -> DiscoveryRunId:
        started = self._clock()
        run_id = DiscoveryRunId(uuid4())
        async with session_scope(self._session_factory) as session:
            profile = await SqlAlchemySearchProfileRepository(session).get(
                user_id, search_profile_id)
            if profile is None or not profile.is_active or "CH" not in countries_in_scope(profile):
                raise LookupError("Active Swiss saved search not found")
            await DiscoveryRunStore(session).create_run(DiscoveryRun(
                id=run_id, user_id=user_id, search_profile_id=search_profile_id,
                country="CH", trigger=DiscoveryTrigger(trigger),
                status=DiscoveryRunStatus.RUNNING, started_at=started,
                seen_count=0, matched_count=0,
            ))

        try:
            report = await self._orchestrator.sweep(profile, country="CH")
            async with session_scope(self._session_factory) as session:
                # Worker re-entry must not rely on authorization from enqueue time.
                current = await SqlAlchemySearchProfileRepository(session).get(
                    user_id, search_profile_id)
                if current is None or not current.is_active \
                        or "CH" not in countries_in_scope(current):
                    raise LookupError("Active Swiss saved search not found")
                opportunities = CatalogOpportunityStore(session)
                runs = DiscoveryRunStore(session)
                matched: set[OpportunityId] = set()
                selected = matching_opportunities(current, report)
                truncated = len(selected) > self._max_observations
                for opportunity in selected[:self._max_observations]:
                    opportunity_id = await opportunities.observe(
                        opportunity, canonical_target=canonical_target(opportunity))
                    await runs.add_member(
                        user_id, search_profile_id, opportunity_id,
                        run_id=run_id, seen_at=started)
                    matched.add(opportunity_id)
                warning_codes = sorted({
                    str(warning.code.value)
                    for warning in (*report.warnings, *(
                        warning for outcome in report.outcomes
                        for warning in outcome.result.warnings))
                })
                if truncated:
                    warning_codes.append("CATALOG_LIMIT_TRUNCATED")
                await runs.finish_run(DiscoveryRun(
                    id=run_id, user_id=user_id, search_profile_id=search_profile_id,
                    country="CH", trigger=DiscoveryTrigger(trigger),
                    status=(DiscoveryRunStatus.SUCCEEDED if report.is_complete and not truncated
                            else DiscoveryRunStatus.PARTIAL),
                    started_at=started, finished_at=max(started, self._clock()),
                    seen_count=report.metrics.postings_seen,
                    matched_count=len(matched),
                    source_health=tuple(SourceHealthSummary(
                        source_key=health.source_key, status=health.status.value,
                        reason=health.reason.value if health.reason else None,
                    ) for health in report.health),
                    warning_codes=tuple(warning_codes),
                ))
        except Exception:
            async with session_scope(self._session_factory) as session:
                failed_run = await DiscoveryRunStore(session).get_run(user_id, run_id)
                if failed_run is not None and failed_run.status is DiscoveryRunStatus.RUNNING:
                    await DiscoveryRunStore(session).finish_run(failed_run.model_copy(update={
                        "status": DiscoveryRunStatus.FAILED,
                        "finished_at": max(started, self._clock()),
                        "warning_codes": ("RUN_FAILED",),
                    }))
            raise
        return run_id

    async def get_run(
        self, user_id: UserId, run_id: DiscoveryRunId,
    ) -> DiscoveryRun | None:
        async with session_scope(self._session_factory, commit=False) as session:
            return await DiscoveryRunStore(session).get_run(user_id, run_id)

    async def list_members(
        self, user_id: UserId, search_profile_id: SearchProfileId, *,
        limit: int, offset: int,
    ) -> tuple[OpportunityId, ...]:
        async with session_scope(self._session_factory, commit=False) as session:
            return await DiscoveryRunStore(session).list_members(
                user_id, search_profile_id, limit=limit, offset=offset)
