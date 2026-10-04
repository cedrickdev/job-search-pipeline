"""Durable, conservative opportunity identity for Swiss discovery."""

from datetime import datetime
from hashlib import sha256
from uuid import uuid5

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.discovery.catalog import (
    canonical_target as verified_canonical_target,
)
from backend.app.discovery.catalog import (
    normalize_source_url,
)
from backend.app.domain.discovery_run import (
    DiscoveryRun,
    DiscoveryRunStatus,
    SourceHealthSummary,
)
from backend.app.domain.identifiers import (
    SURROGATE_KEY_NAMESPACE,
    DiscoveryRunId,
    OpportunityId,
    SearchProfileId,
    UserId,
)
from backend.app.domain.opportunity import Opportunity, OpportunitySourceRecord
from backend.app.infrastructure.database.mappers import (
    opportunity_to_row,
    source_record_to_domain,
    source_record_to_row,
)
from backend.app.infrastructure.database.models import (
    DiscoveryMembershipRow,
    DiscoveryRunRow,
    OpportunityRow,
    OpportunitySourceRecordRow,
    SearchProfileRow,
)


def _lock_key(identity: str) -> int:
    return int.from_bytes(sha256(identity.encode("utf-8")).digest()[:8],
                          "big", signed=True)


class CatalogOpportunityStore:
    """Persist public postings; the caller owns the surrounding transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def observe(
        self, opportunity: Opportunity, *, canonical_target: str | None,
    ) -> OpportunityId:
        source = opportunity.source
        if (canonical_target is not None
                and canonical_target != verified_canonical_target(opportunity)):
            raise ValueError("canonical_target must be a verified job-detail target")
        normalized_url = (
            normalize_source_url(source.source_url) if source.source_url else None
        )
        if source.external_id is None and normalized_url is None:
            raise ValueError("A stable source identity requires external_id or source_url")

        source_identity = (
            f"id:{source.source_key}:{source.external_id}" if source.external_id
            else f"url:{source.source_key}:{normalized_url}"
        )
        identities = [f"source:{source_identity}"]
        if normalized_url:
            identities.append(f"source:url:{source.source_key}:{normalized_url}")
        if canonical_target:
            identities.append(f"target:{canonical_target}")
        # Lock both possible identities, in a stable order, before lookup. The
        # unique constraints remain the ultimate protection against other writers.
        for identity in sorted(set(identities)):
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key(identity)}
            )

        observation = None
        if source.external_id:
            observation = (await self._session.execute(
                select(OpportunitySourceRecordRow).where(
                    OpportunitySourceRecordRow.source_key == source.source_key,
                    OpportunitySourceRecordRow.external_id == source.external_id,
                )
            )).scalar_one_or_none()
        if observation is None and normalized_url:
            observation = (await self._session.execute(
                select(OpportunitySourceRecordRow).where(
                    OpportunitySourceRecordRow.source_key == source.source_key,
                    OpportunitySourceRecordRow.normalized_url == normalized_url,
                )
            )).scalar_one_or_none()

        row = None
        if observation is not None:
            row = await self._get_row(OpportunityId(observation.opportunity_id))
        elif canonical_target:
            row = (await self._session.execute(
                select(OpportunityRow)
                .options(selectinload(OpportunityRow.sources))
                .where(OpportunityRow.canonical_target == canonical_target)
            )).scalar_one_or_none()

        if row is None:
            # A legacy fingerprint is company+title, not a catalog identity.
            catalog_opportunity = opportunity.model_copy(update={"dedup_fingerprint": None})
            row = opportunity_to_row(catalog_opportunity)
            row.canonical_target = canonical_target
            row.source.normalized_url = normalized_url
            self._session.add(row)
        else:
            # Refresh public fields while keeping the original preferred source,
            # its first-discovered timestamp, and V1's import fingerprint intact.
            preferred = source_record_to_domain(row.source)
            refreshed = opportunity.model_copy(update={
                "id": OpportunityId(row.id), "source": preferred,
                "discovered_at": min(row.discovered_at, opportunity.discovered_at),
                "dedup_fingerprint": row.dedup_fingerprint,
            })
            opportunity_to_row(refreshed, row)
            if canonical_target and row.canonical_target is None:
                row.canonical_target = canonical_target
            if observation is None:
                observation = source_record_to_row(source, OpportunityId(row.id))
                observation.id = uuid5(
                    SURROGATE_KEY_NAMESPACE,
                    f"catalog_source:{source.source_key}:{source.external_id or normalized_url}",
                )
                observation.normalized_url = normalized_url
                row.sources.append(observation)
            else:
                # A changed external ID on the same URL is not enough evidence
                # to erase the identifier previously observed for this record.
                observation.external_id = observation.external_id or source.external_id
                observation.source_url = source.source_url or observation.source_url
                observation.normalized_url = normalized_url or observation.normalized_url
                observation.fetched_at = source.fetched_at
                observation.raw = dict(source.raw)

        await self._session.flush()
        return OpportunityId(row.id)

    async def _get_row(self, opportunity_id: OpportunityId) -> OpportunityRow | None:
        return (await self._session.execute(
            select(OpportunityRow).options(selectinload(OpportunityRow.sources))
            .where(OpportunityRow.id == opportunity_id)
        )).scalar_one_or_none()

    async def provenance(
        self, opportunity_id: OpportunityId,
    ) -> tuple[OpportunitySourceRecord, ...]:
        rows = (await self._session.execute(
            select(OpportunitySourceRecordRow)
            .where(OpportunitySourceRecordRow.opportunity_id == opportunity_id)
            .order_by(OpportunitySourceRecordRow.fetched_at.desc(),
                      OpportunitySourceRecordRow.id)
        )).scalars().all()
        return tuple(source_record_to_domain(row) for row in rows)


def _run_to_domain(row: DiscoveryRunRow) -> DiscoveryRun:
    return DiscoveryRun(
        id=DiscoveryRunId(row.id), user_id=UserId(row.user_id),
        search_profile_id=SearchProfileId(row.search_profile_id),
        country=row.country, trigger=row.trigger, status=row.status,
        started_at=row.started_at, finished_at=row.finished_at,
        seen_count=row.seen_count, matched_count=row.matched_count,
        source_health=tuple(SourceHealthSummary.model_validate(item)
                            for item in row.source_health),
        warning_codes=tuple(row.warning_codes),
    )


class DiscoveryRunStore:
    """Every query and mutation is scoped to the owning candidate."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_run(self, run: DiscoveryRun) -> None:
        self._session.add(DiscoveryRunRow(
            id=run.id, user_id=run.user_id,
            search_profile_id=run.search_profile_id, country=run.country,
            trigger=run.trigger, status=run.status, started_at=run.started_at,
            finished_at=run.finished_at, seen_count=run.seen_count,
            matched_count=run.matched_count,
            source_health=[item.model_dump(mode="json") for item in run.source_health],
            warning_codes=list(run.warning_codes),
        ))
        await self._session.flush()

    async def restart_failed_run(self, run: DiscoveryRun) -> None:
        row = (await self._session.execute(
            select(DiscoveryRunRow).where(
                DiscoveryRunRow.id == run.id,
                DiscoveryRunRow.user_id == run.user_id,
                DiscoveryRunRow.search_profile_id == run.search_profile_id,
                DiscoveryRunRow.status == DiscoveryRunStatus.FAILED,
            ).with_for_update()
        )).scalar_one_or_none()
        if row is None:
            raise LookupError("Failed discovery run not found")
        row.status = DiscoveryRunStatus.RUNNING
        row.started_at = run.started_at
        row.finished_at = None
        row.seen_count = 0
        row.matched_count = 0
        row.source_health = []
        row.warning_codes = []
        await self._session.flush()

    async def get_run(
        self, user_id: UserId, run_id: DiscoveryRunId,
    ) -> DiscoveryRun | None:
        row = (await self._session.execute(
            select(DiscoveryRunRow).join(
                SearchProfileRow, SearchProfileRow.id == DiscoveryRunRow.search_profile_id
            ).where(
                DiscoveryRunRow.id == run_id,
                DiscoveryRunRow.user_id == user_id,
                SearchProfileRow.user_id == user_id,
            )
        )).scalar_one_or_none()
        return None if row is None else _run_to_domain(row)

    async def finish_run(self, run: DiscoveryRun) -> None:
        row = (await self._session.execute(
            select(DiscoveryRunRow).where(
                DiscoveryRunRow.id == run.id, DiscoveryRunRow.user_id == run.user_id,
                DiscoveryRunRow.search_profile_id == run.search_profile_id,
            ).with_for_update()
        )).scalar_one_or_none()
        if row is None:
            raise LookupError("Discovery run no longer belongs to this user")
        if row.status is not DiscoveryRunStatus.RUNNING:
            raise ValueError("Discovery run is already terminal")
        row.status = run.status
        row.finished_at = run.finished_at
        row.seen_count = run.seen_count
        row.matched_count = run.matched_count
        row.source_health = [item.model_dump(mode="json") for item in run.source_health]
        row.warning_codes = list(run.warning_codes)
        await self._session.flush()

    async def add_member(
        self, user_id: UserId, search_profile_id: SearchProfileId,
        opportunity_id: OpportunityId, *, run_id: DiscoveryRunId,
        seen_at: datetime,
    ) -> None:
        statement = pg_insert(DiscoveryMembershipRow).values(
            id=uuid5(SURROGATE_KEY_NAMESPACE,
                     f"discovery_member:{user_id}:{search_profile_id}:{opportunity_id}"),
            user_id=user_id, search_profile_id=search_profile_id,
            opportunity_id=opportunity_id, first_seen_at=seen_at,
            last_seen_at=seen_at, last_run_id=run_id,
        )
        await self._session.execute(statement.on_conflict_do_update(
            index_elements=["user_id", "search_profile_id", "opportunity_id"],
            set_={"last_seen_at": seen_at, "last_run_id": run_id,
                  "updated_at": func.now()},
        ))

    async def list_members(
        self, user_id: UserId, search_profile_id: SearchProfileId,
        *, limit: int, offset: int,
    ) -> tuple[OpportunityId, ...]:
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("Invalid discovery membership page")
        ids = (await self._session.execute(
            select(DiscoveryMembershipRow.opportunity_id)
            .join(SearchProfileRow,
                  SearchProfileRow.id == DiscoveryMembershipRow.search_profile_id)
            .where(DiscoveryMembershipRow.user_id == user_id,
                   DiscoveryMembershipRow.search_profile_id == search_profile_id,
                   SearchProfileRow.user_id == user_id)
            .order_by(DiscoveryMembershipRow.last_seen_at.desc(),
                      DiscoveryMembershipRow.opportunity_id)
            .limit(limit).offset(offset)
        )).scalars().all()
        return tuple(OpportunityId(item) for item in ids)
