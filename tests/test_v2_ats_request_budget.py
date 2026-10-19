"""A global, durable Swiss-day request budget guards each reviewed ATS board."""

import asyncio
from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.discovery.adapters.budgeted_ats import BudgetedAtsSourceAdapter
from backend.app.discovery.ats_board_catalog import ReviewedBoard
from backend.app.discovery.bootstrap import build_discovery
from backend.app.discovery.capabilities import SourceCapability
from backend.app.discovery.contracts import SourceFailureCode, SourceHealthStatus
from backend.app.infrastructure.database.models import AtsRequestBudgetRow
from backend.app.repositories.ats_request_budget import SqlAlchemyAtsRequestBudget
from tests.v2_discovery import NOW, a_metadata, a_pack, a_request, frozen_clock


@pytest.mark.asyncio
async def test_two_workers_racing_for_final_slot_persist_exactly_one(db_engine):
    token = f"fixture-{uuid4().hex}"
    day = date(2026, 10, 12)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    budget = SqlAlchemyAtsRequestBudget(factory)
    try:
        allowed = await asyncio.gather(
            budget.reserve("greenhouse", token, day, 1),
            budget.reserve("greenhouse", token, day, 1),
        )
        assert allowed.count(True) == 1
        assert allowed.count(False) == 1
        async with factory() as session:
            rows = (await session.scalars(select(AtsRequestBudgetRow).where(
                AtsRequestBudgetRow.board_token == token))).all()
            assert len(rows) == 1
            assert rows[0].request_count == 1
    finally:
        async with factory.begin() as session:
            await session.execute(delete(AtsRequestBudgetRow).where(
                AtsRequestBudgetRow.board_token == token))


@pytest.mark.asyncio
async def test_next_swiss_day_gets_a_new_bucket_and_invalid_limits_never_spend(db_engine):
    token = f"fixture-{uuid4().hex}"
    day = date(2026, 10, 12)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    budget = SqlAlchemyAtsRequestBudget(factory)
    try:
        assert not await budget.reserve("lever", token, day, 0)
        assert not await budget.reserve("lever", token, day, -1)
        assert await budget.reserve("lever", token, day, 1)
        assert not await budget.reserve("lever", token, day, 1)
        assert await budget.reserve("lever", token, day + timedelta(days=1), 1)
        async with factory() as session:
            rows = (await session.scalars(select(AtsRequestBudgetRow).where(
                AtsRequestBudgetRow.board_token == token))).all()
            assert sorted(row.request_count for row in rows) == [1, 1]
    finally:
        async with factory.begin() as session:
            await session.execute(delete(AtsRequestBudgetRow).where(
                AtsRequestBudgetRow.board_token == token))


def _reviewed_board(token: str) -> ReviewedBoard:
    return ReviewedBoard(
        token=token, company="Example SA", source_key="greenhouse", country="CH",
        provenance_url="https://example.ch/careers",
        conditions_url="https://example.ch/terms", access_decision="approved",
        reviewed_at=date(2026, 10, 12), request_budget_per_day=1,
    )


def test_nonempty_v2_ats_registry_requires_durable_budget():
    with pytest.raises(ValueError, match="budget"):
        build_discovery(boards={"greenhouse": (_reviewed_board("fixture-board"),)})


class FakeBudget:
    def __init__(self, *, allowed: bool = False, fail: bool = False):
        self.allowed = allowed
        self.fail = fail
        self.calls: list[tuple[str, str, date, int]] = []

    async def reserve(self, source_key: str, board_token: str,
                      swiss_date: date, limit: int) -> bool:
        self.calls.append((source_key, board_token, swiss_date, limit))
        if self.fail:
            raise OSError("database unavailable")
        return self.allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(("fail", "reason"), [
    (False, SourceFailureCode.SOURCE_BUDGET_EXHAUSTED),
    (True, SourceFailureCode.SOURCE_BUDGET_UNAVAILABLE),
])
async def test_denied_or_unavailable_budget_makes_no_network_request(fail, reason):
    calls: list[str] = []

    def fetch(token: str, company: str):
        calls.append(token)
        return []

    budget = FakeBudget(fail=fail)
    adapter = BudgetedAtsSourceAdapter(
        metadata=a_metadata("greenhouse", capabilities=frozenset({
            SourceCapability.COMPANY_FILTER, SourceCapability.HEALTHCHECK})),
        fetch=fetch, boards=(_reviewed_board("fixture-board"),), budget=budget,
        packs=lambda _: a_pack("CH"), clock=frozen_clock(NOW),
    )
    result = await adapter.discover(a_request())
    assert calls == []
    assert budget.calls == [("greenhouse", "fixture-board", date(2026, 3, 1), 1)]
    assert result.metrics.requests_made == 0
    assert result.health.status is SourceHealthStatus.UNAVAILABLE
    assert result.health.reason is reason


@pytest.mark.asyncio
async def test_approved_board_is_charged_before_one_fetch_and_healthcheck():
    calls: list[tuple[str, str]] = []

    def fetch(token: str, company: str):
        calls.append((token, company))
        return [{
            "source": "greenhouse", "company": company,
            "title": "Software engineer", "url": "https://example.ch/jobs/1",
            "location": "Zürich", "description": "Build reliable software.",
        }]

    budget = FakeBudget(allowed=True)
    adapter = BudgetedAtsSourceAdapter(
        metadata=a_metadata("greenhouse", capabilities=frozenset({
            SourceCapability.COMPANY_FILTER, SourceCapability.HEALTHCHECK})),
        fetch=fetch, boards=(_reviewed_board("fixture-board"),), budget=budget,
        packs=lambda _: a_pack("CH"), clock=frozen_clock(NOW),
    )
    result = await adapter.discover(a_request())
    assert result.health.status is SourceHealthStatus.HEALTHY
    assert result.metrics.requests_made == 1
    assert len(result.opportunities) == 1
    assert calls == [("fixture-board", "Example SA")]
    assert len(budget.calls) == 1

    await adapter.healthcheck()
    assert len(budget.calls) == 2
    assert calls == [("fixture-board", "Example SA")] * 2


@pytest.mark.asyncio
async def test_one_budget_reservation_never_allows_hidden_http_retries(monkeypatch):
    from pipeline import http_fetch
    from pipeline.sources.greenhouse import fetch_jobs

    attempts: list[str] = []

    def unavailable_request(method, url, **kwargs):
        attempts.append(url)
        raise http_fetch.requests.ConnectionError("fixture upstream unavailable")

    monkeypatch.setattr(http_fetch.requests, "request", unavailable_request)
    monkeypatch.setattr(http_fetch, "RETRY_WAIT", 0)
    budget = FakeBudget(allowed=True)
    adapter = BudgetedAtsSourceAdapter(
        metadata=a_metadata("greenhouse", capabilities=frozenset({
            SourceCapability.COMPANY_FILTER, SourceCapability.HEALTHCHECK})),
        fetch=fetch_jobs, boards=(_reviewed_board("fixture-board"),), budget=budget,
        packs=lambda _: a_pack("CH"), clock=frozen_clock(NOW),
    )
    result = await adapter.discover(a_request())
    assert result.health.status is SourceHealthStatus.UNAVAILABLE
    assert len(budget.calls) == 1
    assert len(attempts) == 1
