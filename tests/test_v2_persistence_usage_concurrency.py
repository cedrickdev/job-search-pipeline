"""The usage-budget reservation, asserted against PostgreSQL under real concurrency (§5-9, §49-51).

`test_v2_billing.py` proves the metering logic over fakes, and the fake's `lock_usage_budget` is a
no-op — a single unit of work has no second worker to serialize against. That leaves the one
property the fakes *cannot* show: that `authorize`'s advisory lock actually serializes the
count → decide → record of two workers of the same account racing the last unit of a period's
allowance, so they cannot both read the budget as free and both consume it.

It is the exact twin of the Phase-12 submission-budget concurrency test, in its own lock namespace.
A per-period meter is a read-then-write reservation — sum the window, decide, append an event — and
the two events carry *different* idempotency keys (different sources), so no unique index arbitrates
between them: only the transaction-scoped `pg_advisory_xact_lock` does. Without it, both workers sum
zero against a ceiling of one and both append, leaving two events over a one-unit budget. With it,
the loser blocks until the winner commits, then sums one, and `QUOTA_EXCEEDED` refuses it.

This cannot be shown over `db_session`'s one savepointed connection (a cross-connection advisory
lock needs two) nor over fakes (whose no-op lock proves nothing), so the tests commit for real over
independent `session_scope` units of work and truncate `users` with CASCADE on the way out.

The `LLM_TOKENS` reservation is the same property one meter over: two concurrent `recorder.route`
calls racing the last of a token budget must not both slip through. The recorder reserves a
per-request estimate before the provider runs and records the measured actual after, all inside the
one locked unit of work — so the last test here drives two real routed calls at a ceiling only one
reservation can fit, and the advisory lock lets exactly one meter while the other is refused.
"""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from backend.app.billing.entitlements import EntitlementResolver
from backend.app.billing.errors import BillingError, BillingErrorCode
from backend.app.billing.metering import MeteringService
from backend.app.domain.entitlement import EntitlementKey
from backend.app.domain.usage import UsageEvent, UsageSourceType
from backend.app.infrastructure.database.engine import (
    create_session_factory,
    session_scope,
)
from backend.app.infrastructure.database.models import UsageEventRow
from backend.app.llm.contracts import LLMMessage, TokenUsage
from backend.app.llm.recorder import LLMTelemetryRecorder
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyLLMRunRepository,
    SqlAlchemyPlanRepository,
    SqlAlchemySubscriptionRepository,
    SqlAlchemyUsageEventRepository,
)
from tests.test_v2_llm_recorder import _EXTERNAL, _request, _router
from tests.v2_builders import (
    NOW,
    USER,
    a_plan,
    an_entitlement,
)
from tests.v2_llm import FakeProvider
from tests.v2_rows import a_user_row

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def committed_world(db_engine):
    """A real session factory whose committed rows are truncated on teardown.

    The reservation can only be shown across two connections that see each other's committed
    writes, so these tests cannot lean on `db_session`'s rollback. This commits for real and, on
    the way out, truncates `users` with CASCADE — which reaches every usage event, owned through
    `users` — leaving the once-per-session schema clean for whatever runs next. The `plans`
    catalogue carries no `user_id`, so it is truncated explicitly alongside.
    """
    factory = create_session_factory(db_engine)
    try:
        yield factory
    finally:
        async with db_engine.begin() as connection:
            await connection.execute(text(
                "TRUNCATE users, plans RESTART IDENTITY CASCADE"))


async def _seed_account_and_a_one_submission_free_plan(factory) -> None:
    """Commit the account and a free plan whose submission allowance is exactly one.

    A ceiling of one is what forces contention: with it, two concurrent metered submissions cannot
    both fit, so the reservation must let exactly one through. The account has no subscription, so
    the resolver falls back to this free plan and meters against the calendar month.
    """
    async with session_scope(factory) as session:
        session.add(a_user_row(display_name="owner"))
        await session.flush()
        await SqlAlchemyPlanRepository(session).upsert(a_plan(
            slug="free", price_amount_cents=None, currency=None, billing_interval=None,
            external_price_id=None,
            entitlements=(an_entitlement(
                key=EntitlementKey.APPLICATION_SUBMISSIONS, limit=1),)))


async def _meter_one_submission_in_own_unit_of_work(factory, *, source_id: str):
    """One worker: reserve then record one submission in its own committed transaction.

    Mirrors a real worker process — its own session, its own `session_scope` — so the advisory lock
    `authorize` takes is held on its own connection from the count through the write to the commit
    that releases it. The two workers pass *different* `source_id`s, so their events carry different
    idempotency keys and only the lock, never a unique index, can keep the second out.
    """
    async with session_scope(factory) as session:
        resolver = EntitlementResolver(
            SqlAlchemyPlanRepository(session), SqlAlchemySubscriptionRepository(session))
        metering = MeteringService(resolver, SqlAlchemyUsageEventRepository(session))
        resolved = await metering.authorize(
            USER, EntitlementKey.APPLICATION_SUBMISSIONS, quantity=1, as_of=NOW)
        return await metering.record(
            USER, EntitlementKey.APPLICATION_SUBMISSIONS,
            source_type=UsageSourceType.APPLICATION_SUBMISSION, source_id=source_id,
            quantity=1, occurred_at=NOW, billing_period=resolved.period.label)


async def test_two_workers_racing_the_last_period_slot_meter_once(committed_world):
    """Two concurrent metered submissions for a one-unit period consume exactly one (§8, §49-51).

    THE property the fakes cannot show: the same account, two workers, one slot left. The advisory
    lock serializes their count → decide → record, so the winner reserves and records while the
    loser blocks; when the loser's lock is granted it sums the winner's committed event, finds the
    ceiling met, and is refused with `QUOTA_EXCEEDED`. No raw error reaches either caller, and the
    ledger holds exactly one event — never the two an unserialized read-then-write would leak.
    """
    factory = committed_world
    await _seed_account_and_a_one_submission_free_plan(factory)

    results = await asyncio.gather(
        _meter_one_submission_in_own_unit_of_work(factory, source_id="submission-a"),
        _meter_one_submission_in_own_unit_of_work(factory, source_id="submission-b"),
        return_exceptions=True)

    # No raw IntegrityError reached either caller — every result is a recorded event or the
    # commercial refusal, nothing lower-level leaked through the reservation.
    recorded = [r for r in results if isinstance(r, UsageEvent)]
    refused = [r for r in results
               if isinstance(r, BillingError) and r.code is BillingErrorCode.QUOTA_EXCEEDED]
    assert len(recorded) == 1, results
    assert len(refused) == 1, results

    # And it is durable: exactly one usage event survived for the account, the winner's.
    async with session_scope(factory, commit=False) as session:
        surviving = (await session.execute(
            select(func.count()).select_from(UsageEventRow)
            .where(UsageEventRow.user_id == USER))).scalar_one()
    assert surviving == 1


async def test_a_second_period_slot_is_refused_without_leaking_a_row(committed_world):
    """The loser's unit of work rolls back cleanly — a refusal writes no event (§5).

    A sequential companion to the race: once the one slot is spent, a second metered submission is
    refused, and because the refusal is raised inside `authorize` before any write, the ledger is
    untouched. The honest record of a refused action is *no* event, exactly as an unmeasured one is.
    """
    factory = committed_world
    await _seed_account_and_a_one_submission_free_plan(factory)

    first = await _meter_one_submission_in_own_unit_of_work(factory, source_id="submission-a")
    assert isinstance(first, UsageEvent)

    with pytest.raises(BillingError) as caught:
        await _meter_one_submission_in_own_unit_of_work(factory, source_id="submission-b")
    assert caught.value.code is BillingErrorCode.QUOTA_EXCEEDED

    async with session_scope(factory, commit=False) as session:
        surviving = (await session.execute(
            select(func.count()).select_from(UsageEventRow)
            .where(UsageEventRow.user_id == USER))).scalar_one()
    assert surviving == 1


# --------------------------------------------------------- LLM_TOKENS reserve/finalize (Fix #2)


async def _seed_account_and_a_two_thousand_token_free_plan(factory) -> None:
    """Commit the account and a free plan whose LLM-token allowance is exactly two thousand.

    The ceiling is set equal to a single call's reservation below, so the two racing calls cannot
    both reserve it: once either records any positive actual, the other's reservation no longer
    fits. The account has no subscription, so the resolver falls back to this free plan and meters
    against the calendar month.
    """
    async with session_scope(factory) as session:
        session.add(a_user_row(display_name="owner"))
        await session.flush()
        await SqlAlchemyPlanRepository(session).upsert(a_plan(
            slug="free", price_amount_cents=None, currency=None, billing_interval=None,
            external_price_id=None,
            entitlements=(an_entitlement(key=EntitlementKey.LLM_TOKENS, limit=2000),)))


async def _route_one_llm_call_in_own_unit_of_work(factory, *, provider_key: str):
    """One worker: reserve, route a fake provider, and record the measured usage, all under the lock.

    Mirrors a real serving process — its own session, its own `session_scope` — so the advisory lock
    `authorize` takes is held on its own connection from the reservation through the recorded event
    to the commit that releases it. The request pins `max_output_tokens=2000` over empty content, so
    its reservation is exactly the plan's whole ceiling; the fake provider reports a positive
    measured total, so whichever call commits first leaves no room for the other's reservation.
    """
    async with session_scope(factory) as session:
        resolver = EntitlementResolver(
            SqlAlchemyPlanRepository(session), SqlAlchemySubscriptionRepository(session))
        metering = MeteringService(resolver, SqlAlchemyUsageEventRepository(session))
        recorder = LLMTelemetryRecorder(
            runs=SqlAlchemyLLMRunRepository(session), metering=metering)
        provider = FakeProvider(
            provider_key=provider_key, text="ok", usage=TokenUsage(total_tokens=1800))
        request = _request(messages=(LLMMessage.user(""),), max_output_tokens=2000)
        return await recorder.route(_router(provider), request, _EXTERNAL, user_id=USER)


async def test_two_workers_racing_the_last_token_budget_meter_once(committed_world):
    """Two concurrent routed calls for a one-reservation budget consume exactly one (§5-9, §49-51).

    THE property the fakes cannot show for `LLM_TOKENS`: the same account, two calls, only room for
    one reservation. The advisory lock serializes their reserve → route → record, so the winner
    reserves, spends a provider and records its measured usage while the loser blocks; when the
    loser's lock is granted it sums the winner's committed usage, finds no room for its reservation,
    and is refused with `QUOTA_EXCEEDED` — before its provider is ever asked. No raw error reaches
    either caller, and the ledger holds exactly one measured event, never two over the one budget.
    """
    factory = committed_world
    await _seed_account_and_a_two_thousand_token_free_plan(factory)

    results = await asyncio.gather(
        _route_one_llm_call_in_own_unit_of_work(factory, provider_key="conn_gateway_a"),
        _route_one_llm_call_in_own_unit_of_work(factory, provider_key="conn_gateway_b"),
        return_exceptions=True)

    # Exactly one call routed and metered; the other was refused with the commercial code, and
    # nothing lower-level (an IntegrityError, say) leaked through the reservation to either caller.
    refused = [r for r in results
               if isinstance(r, BillingError) and r.code is BillingErrorCode.QUOTA_EXCEEDED]
    succeeded = [r for r in results if not isinstance(r, Exception)]
    assert len(succeeded) == 1, results
    assert len(refused) == 1, results

    # Durable: exactly one measured usage event survived, the winner's, within the 2,000 ceiling.
    async with session_scope(factory, commit=False) as session:
        quantities = (await session.execute(
            select(UsageEventRow.quantity).where(UsageEventRow.user_id == USER))).scalars().all()
    assert quantities == [1800]
