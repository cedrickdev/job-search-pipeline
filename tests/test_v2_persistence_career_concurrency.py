"""The recommendation dedup, asserted against PostgreSQL under real concurrency (§26-33).

The unit suite (`test_v2_career_recommendations.py`) proves the engine's fingerprint gate over
fakes; the persistence round-trip (`test_v2_persistence_repositories.py`) proves one recommendation
and its evidence survive a write and a scoped read. Neither can show the property this file exists
for: that the `find_by_fingerprint`-then-add gate stays correct when two units of work generate the
same suggestion at once.

A read-then-write dedup is a race — both callers read "absent", both insert — and only a
database-level `UNIQUE (user_id, fingerprint)` can close it. Demonstrating the closure needs two
connections that see each other's *committed* rows, so these tests cannot lean on `db_session`'s
one savepointed connection; they commit for real over independent `session_scope` units of work and
truncate on the way out, exactly as the Phase-12 submission-budget concurrency tests do.
"""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from backend.app.domain.recommendation import CareerRecommendation
from backend.app.domain.role import RoleFamily
from backend.app.infrastructure.database.engine import (
    create_session_factory,
    session_scope,
)
from backend.app.infrastructure.database.models import CareerRecommendationRow
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyCareerRecommendationRepository,
)
from tests.v2_builders import (
    OTHER_RECOMMENDATION,
    RECOMMENDATION,
    USER,
    a_career_recommendation,
    a_recommendation_evidence,
)
from tests.v2_rows import a_user_row

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def committed_world(db_engine):
    """A real session factory whose committed rows are truncated on teardown.

    The race can only be shown across two connections that see each other's committed writes, so
    these tests cannot lean on `db_session`'s rollback. This commits for real and, on the way out,
    truncates `users` with CASCADE — which reaches every recommendation and its evidence, all
    owned through `users` — so the once-per-session schema is left clean for whatever runs next.
    """
    factory = create_session_factory(db_engine)
    try:
        yield factory
    finally:
        async with db_engine.begin() as connection:
            await connection.execute(text(
                "TRUNCATE users RESTART IDENTITY CASCADE"))


async def _seed_account(factory) -> None:
    """Commit the one account a recommendation's `user_id` foreign key needs."""
    async with session_scope(factory) as session:
        session.add(a_user_row(display_name="owner"))
        await session.flush()


async def _add_in_own_unit_of_work(factory, recommendation: CareerRecommendation):
    """One worker: add `recommendation` in its own committed transaction.

    Mirrors a real generation — its own session, its own `session_scope` — so each insert races on
    its own connection and the unique index arbitrates between them, not a shared savepoint.
    """
    async with session_scope(factory) as session:
        return await SqlAlchemyCareerRecommendationRepository(session).add(recommendation)


async def test_two_generations_racing_one_fingerprint_write_one_row(committed_world):
    """Two concurrent adds of one logical recommendation converge on a single row (§26-33).

    THE regression this corrective exists for: the same suggestion, generated twice at once, carries
    one `(user_id, fingerprint)` under two random ids. The unique index lets exactly one insert land;
    the other trips it, and the repository rolls back its SAVEPOINT and returns the winner rather than
    surfacing a raw `IntegrityError`. So both callers receive one logical recommendation and the
    store holds one row — never the two a read-then-write gate would leak under the race.
    """
    factory = committed_world
    await _seed_account(factory)
    # Same account, kind, window and cited evidence — one fingerprint — under two random ids.
    rec_a = a_career_recommendation(id=RECOMMENDATION)
    rec_b = a_career_recommendation(
        id=OTHER_RECOMMENDATION,
        evidence=(a_recommendation_evidence(recommendation_id=OTHER_RECOMMENDATION),))
    assert rec_a.fingerprint == rec_b.fingerprint
    assert rec_a.id != rec_b.id

    results = await asyncio.gather(
        _add_in_own_unit_of_work(factory, rec_a),
        _add_in_own_unit_of_work(factory, rec_b),
        return_exceptions=True)

    # No raw IntegrityError reached either caller.
    assert all(isinstance(r, CareerRecommendation) for r in results), results
    # Both were handed the one recommendation that won the race — same id, same fingerprint.
    assert results[0].id == results[1].id
    assert results[0].fingerprint == rec_a.fingerprint

    # And it is durable: exactly one row survived, and it is the winner both callers received.
    async with session_scope(factory, commit=False) as session:
        surviving = (await session.execute(
            select(CareerRecommendationRow.id)
            .where(CareerRecommendationRow.user_id == USER))).scalars().all()
    assert len(surviving) == 1
    assert surviving[0] == results[0].id


async def test_two_generations_with_distinct_fingerprints_both_persist(committed_world):
    """The unique index collapses duplicates only — two genuinely different suggestions coexist.

    The guard is not a wall against a second recommendation: two suggestions that cite different
    slices carry different fingerprints, so both concurrent adds land and the store holds both. The
    dedup is about logical identity, never about capping how many recommendations an account has.
    """
    factory = committed_world
    await _seed_account(factory)
    rec_a = a_career_recommendation(id=RECOMMENDATION)
    rec_b = a_career_recommendation(
        id=OTHER_RECOMMENDATION,
        evidence=(a_recommendation_evidence(
            recommendation_id=OTHER_RECOMMENDATION,
            dimension_key=RoleFamily.SOFTWARE_ENGINEERING.value,
            numerator=3, denominator=40, sample_size=40,
            detail="Software Engineering : 3 réponses sur 40 candidatures."),))
    assert rec_a.fingerprint != rec_b.fingerprint

    results = await asyncio.gather(
        _add_in_own_unit_of_work(factory, rec_a),
        _add_in_own_unit_of_work(factory, rec_b),
        return_exceptions=True)

    assert all(isinstance(r, CareerRecommendation) for r in results), results
    async with session_scope(factory, commit=False) as session:
        ids = set((await session.execute(
            select(CareerRecommendationRow.id)
            .where(CareerRecommendationRow.user_id == USER))).scalars().all())
    assert ids == {RECOMMENDATION, OTHER_RECOMMENDATION}
