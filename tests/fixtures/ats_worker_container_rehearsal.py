"""Fixture-only container rehearsal of the real general-worker entry point.

Mount this file read-only into a disposable Compose project. The two ATS fetch
functions below never open a socket; this file must not ship in the runtime image.
"""

import asyncio
from datetime import UTC, date, datetime
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import text

from backend.app.cli import run_worker
from backend.app.core.settings import DatabaseSettings
from backend.app.discovery import bootstrap
from backend.app.discovery.ats_board_catalog import ReviewedAtsCatalog, ReviewedBoard
from backend.app.domain.task import TaskKind, TaskSpec
from backend.app.infrastructure.database.engine import (
    create_async_database_engine,
    create_session_factory,
    session_scope,
)
from backend.app.repositories.sqlalchemy_repositories import SqlAlchemyTaskRunRepository


OWNER = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")
SEARCH = UUID("33333333-3333-4333-8333-333333333333")
POSTING_URL = "https://boards.greenhouse.io/fixture/jobs/123456789"


def board(source_key: str) -> ReviewedBoard:
    return ReviewedBoard(
        token="fixture-board", company="Fixture SA", source_key=source_key,  # noqa: S106
        country="CH", provenance_url="https://example.invalid/jobs",
        conditions_url="https://example.invalid/terms", access_decision="approved",
        reviewed_at=date.today(), request_budget_per_day=1,
    )


def greenhouse_fixture(token: str, company: str):
    assert token == "fixture-board" and company == "Fixture SA"  # noqa: S105
    return [
        {"source": "greenhouse", "company": company, "title": "Software engineer",
         "url": POSTING_URL, "location": "Zürich, Switzerland",
         "description": "Engineering."},
        {"source": "greenhouse", "company": company, "title": "Software engineer",
         "url": POSTING_URL + "-fr", "location": "Paris, France",
         "description": "Engineering."},
    ]


def lever_fixture(token: str, company: str):
    assert token == "fixture-board" and company == "Fixture SA"  # noqa: S105
    raise TimeoutError("fixture Lever timeout")


async def seed() -> UUID:
    engine = create_async_database_engine(DatabaseSettings.from_env())
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            for owner, label in ((OWNER, "owner"), (OTHER, "other")):
                await session.execute(text(
                    "INSERT INTO users (id, email, password_hash, display_name) "
                    "VALUES (:id, :email, 'fixture-only', :label)"
                ), {"id": owner, "email": f"{label}@example.invalid", "label": label})
            await session.execute(text(
                "INSERT INTO search_profiles (id, user_id, name, queries, source_keys) "
                "VALUES (:id, :owner, 'Fixture CH', ARRAY['engineer'], "
                "ARRAY['greenhouse', 'lever'])"
            ), {"id": SEARCH, "owner": OWNER})
            await session.execute(text(
                "INSERT INTO search_areas (id, search_profile_id, ordinal, kind, country) "
                "VALUES (:id, :search, 0, 'COUNTRY', 'CH')"
            ), {"id": UUID("44444444-4444-4444-8444-444444444444"), "search": SEARCH})
            task = TaskSpec(
                kind=TaskKind.OPPORTUNITY_DISCOVERY,
                idempotency_key="fixture-container-morning",
                user_id=OWNER,
                payload={"search_profile_id": str(SEARCH), "trigger": "MORNING"},
            ).to_queued_run(as_of=datetime.now(UTC))
            await SqlAlchemyTaskRunRepository(session).save(task)
        return task.id
    finally:
        await engine.dispose()


async def verify(task_id: UUID) -> None:
    engine = create_async_database_engine(DatabaseSettings.from_env())
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory, commit=False) as session:
            task = (await session.execute(text(
                "SELECT status, attempts FROM task_runs WHERE id = :id"
            ), {"id": task_id})).one()
            run = (await session.execute(text(
                "SELECT status, matched_count, source_health FROM discovery_runs "
                "WHERE id = :id AND user_id = :owner"
            ), {"id": task_id, "owner": OWNER})).one()
            foreign_run = (await session.execute(text(
                "SELECT count(*) FROM discovery_runs WHERE id = :id AND user_id = :other"
            ), {"id": task_id, "other": OTHER})).scalar_one()
            memberships = (await session.execute(text(
                "SELECT count(*) FROM discovery_memberships "
                "WHERE user_id = :owner AND search_profile_id = :search"
            ), {"owner": OWNER, "search": SEARCH})).scalar_one()
            opportunities = (await session.execute(text(
                "SELECT location_country, location_point IS NULL "
                "FROM opportunities"
            ))).all()
            browser_tasks = (await session.execute(text(
                "SELECT count(*) FROM task_runs WHERE lane = 'BROWSER'"
            ))).scalar_one()
            requests = (await session.execute(text(
                "SELECT source_key, request_count FROM ats_request_budgets "
                "ORDER BY source_key"
            ))).all()
        assert task == ("SUCCEEDED", 1), task
        assert run.status == "PARTIAL" and run.matched_count == 1, run
        assert {item["source_key"]: item["status"] for item in run.source_health} == {
            "greenhouse": "HEALTHY", "lever": "UNAVAILABLE"}, run.source_health
        assert foreign_run == 0 and memberships == 1
        assert opportunities == [("CH", True)], opportunities
        assert browser_tasks == 0
        assert requests == [("greenhouse", 1), ("lever", 1)], requests
        print("Fixture worker: partial CH discovery, owner isolation, list-only map, "
              "two budgeted sources, zero browser tasks")
    finally:
        await engine.dispose()


def main() -> None:
    catalog = ReviewedAtsCatalog(
        version=1,
        boards=MappingProxyType({
            "greenhouse": (board("greenhouse"),),
            "lever": (board("lever"),),
        }),
        reviews=(board("greenhouse"), board("lever")),
    )
    run_worker.load_reviewed_boards = lambda _path: catalog
    bootstrap.ATS_SOURCES = tuple(
        (metadata, greenhouse_fixture if metadata.source_key == "greenhouse"
         else lever_fixture if metadata.source_key == "lever" else fetch)
        for metadata, fetch in bootstrap.ATS_SOURCES
    )
    task_id = asyncio.run(seed())
    assert run_worker.main(["--lane", "general", "--once"]) == 0
    asyncio.run(verify(task_id))


if __name__ == "__main__":
    main()
