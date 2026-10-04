"""Durable, conservative opportunity identity for Swiss discovery."""

from hashlib import sha256
from uuid import uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.discovery.catalog import (
    canonical_target as verified_canonical_target,
)
from backend.app.discovery.catalog import (
    normalize_source_url,
)
from backend.app.domain.identifiers import SURROGATE_KEY_NAMESPACE, OpportunityId
from backend.app.domain.opportunity import Opportunity, OpportunitySourceRecord
from backend.app.infrastructure.database.mappers import (
    opportunity_to_row,
    source_record_to_domain,
    source_record_to_row,
)
from backend.app.infrastructure.database.models import (
    OpportunityRow,
    OpportunitySourceRecordRow,
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
