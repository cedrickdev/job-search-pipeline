"""Catalog identity and multi-source provenance against disposable Postgres."""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.domain.identifiers import OpportunityId
from backend.app.infrastructure.database.models import (
    OpportunityRow,
    OpportunitySourceRecordRow,
)
from backend.app.repositories.discovery_catalog import CatalogOpportunityStore
from tests.v2_builders import a_source_record, an_opportunity

pytestmark = pytest.mark.asyncio


def posting(number: int, *, source_key: str = "board_a", external_id: str | None = None,
            source_url: str | None = None, title: str = "Engineer"):
    return an_opportunity(
        id=OpportunityId(UUID(f"00000000-0000-4000-8000-0000000002{number:02d}")),
        source=a_source_record(
            source_key=source_key, external_id=external_id,
            source_url=source_url),
        title=title, company_name="Example AG", dedup_fingerprint="same-title-hash",
    )


async def count(session, model) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_repeated_source_id_updates_one_observation(db_session):
    store = CatalogOpportunityStore(db_session)
    first = posting(1, external_id="role-1", source_url="https://example.ch/jobs/1")
    changed = posting(2, external_id="role-1", source_url="https://example.ch/jobs/1-new",
                      title="Senior Engineer")
    assert await store.observe(first, canonical_target=None) == first.id
    assert await store.observe(changed, canonical_target=None) == first.id
    assert await count(db_session, OpportunityRow) == 1
    records = await store.provenance(first.id)
    assert len(records) == 1
    assert records[0].source_url == "https://example.ch/jobs/1-new"


async def test_verified_target_groups_two_distinct_source_records(db_session):
    store = CatalogOpportunityStore(db_session)
    target = "https://jobs.lever.co/example/abc"
    first = posting(3, external_id="a-3", source_url="https://a.ch/3").model_copy(
        update={"application_url": target})
    second = posting(4, source_key="board_b", external_id="b-4",
                     source_url="https://b.ch/4").model_copy(
        update={"application_url": target})
    assert await store.observe(first, canonical_target=target) == first.id
    assert await store.observe(second, canonical_target=target) == first.id
    assert await count(db_session, OpportunityRow) == 1
    assert {r.source_key for r in await store.provenance(first.id)} == {"board_a", "board_b"}


async def test_unverified_target_cannot_merge_unrelated_roles(db_session):
    store = CatalogOpportunityStore(db_session)
    with pytest.raises(ValueError, match="verified job-detail target"):
        await store.observe(
            posting(14, external_id="role-14"),
            canonical_target="https://jobs.lever.co/example/abc",
        )


async def test_same_title_and_company_are_not_sufficient_to_merge(db_session):
    store = CatalogOpportunityStore(db_session)
    first = posting(5, external_id="role-5", source_url="https://a.ch/5")
    second = posting(6, external_id="role-6", source_url="https://a.ch/6")
    assert await store.observe(first, canonical_target=None) == first.id
    assert await store.observe(second, canonical_target=None) == second.id
    assert await count(db_session, OpportunityRow) == 2


async def test_missing_url_uses_stable_external_id(db_session):
    store = CatalogOpportunityStore(db_session)
    first = posting(7, external_id="role-7", source_url=None)
    second = posting(8, external_id="role-7", source_url=None)
    assert await store.observe(first, canonical_target=None) == first.id
    assert await store.observe(second, canonical_target=None) == first.id
    assert await count(db_session, OpportunitySourceRecordRow) == 1


async def test_url_fallback_normalizes_tracking_and_fragment(db_session):
    store = CatalogOpportunityStore(db_session)
    first = posting(9, source_url="https://example.ch/jobs/9?utm_source=a#details")
    second = posting(10, source_url="https://EXAMPLE.ch/jobs/9/?utm_source=b")
    assert await store.observe(first, canonical_target=None) == first.id
    assert await store.observe(second, canonical_target=None) == first.id
    assert await count(db_session, OpportunitySourceRecordRow) == 1


async def test_no_stable_identity_is_not_silently_deduplicated(db_session):
    store = CatalogOpportunityStore(db_session)
    with pytest.raises(ValueError, match="stable source identity"):
        await store.observe(posting(11), canonical_target=None)


@pytest.mark.parametrize("second_external_id", ["concurrent-12", "concurrent-13"])
async def test_concurrent_retries_create_one_opportunity(db_engine, second_external_id):
    """Two transactions seeing the same posting cannot both insert it."""
    first = posting(12, external_id="concurrent-12", source_url="https://a.ch/12")
    second = posting(13, external_id=second_external_id, source_url="https://a.ch/12")

    async def insert(item):
        async with AsyncSession(db_engine, expire_on_commit=False) as session:
            async with session.begin():
                return await CatalogOpportunityStore(session).observe(
                    item, canonical_target=None)

    try:
        ids = await asyncio.gather(insert(first), insert(second))
        assert ids[0] == ids[1]
        assert ids[0] in {first.id, second.id}
        async with AsyncSession(db_engine) as session:
            assert await count(session, OpportunityRow) == 1
            assert await count(session, OpportunitySourceRecordRow) == 1
    finally:
        async with AsyncSession(db_engine) as session:
            async with session.begin():
                await session.execute(delete(OpportunitySourceRecordRow).where(
                    OpportunitySourceRecordRow.source_url == "https://a.ch/12"))
                await session.execute(delete(OpportunityRow).where(
                    OpportunityRow.id.in_((first.id, second.id))))
