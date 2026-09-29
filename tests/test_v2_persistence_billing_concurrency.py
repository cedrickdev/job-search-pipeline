"""Webhook application ordering, asserted against PostgreSQL under real concurrency (§13-15).

`test_v2_billing.py` proves the webhook service's monotonic guard over fakes, and the fake's
`lock_subscription` is a no-op — a single unit of work has no second delivery to race. That
leaves the one property the fakes *cannot* show: that two deliveries of the *same* subscription's
state, an older and a newer, redelivered out of order and processed concurrently, always leave the
subscription at the newer state — never the older one clobbering it because it committed last.

The `_apply` path is a read-modify-write: `find_by_id → supersedes → upsert`. The two events carry
*different* event ids, so the `subscription_events` unique index never arbitrates between them, and
without a lock both transactions read the same prior row, both pass `supersedes`, and the
later-committing (older) one wins. The transaction-scoped `pg_advisory_xact_lock` keyed on the
subscription is what serializes them: whichever acquires it first applies, the other then reads the
committed state and `supersedes` decides monotonically. Either interleaving converges on the newer.

This cannot be shown over `db_session`'s one savepointed connection (a cross-connection advisory
lock needs two) nor over fakes (whose no-op lock proves nothing), so the tests commit for real over
independent `session_scope` units of work and truncate `users, plans` with CASCADE on the way out.
"""
import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

from backend.app.billing.webhooks import BillingWebhookService
from backend.app.domain.subscription import SubscriptionStatus
from backend.app.domain.subscription_event import SubscriptionEvent, SubscriptionEventOutcome
from backend.app.infrastructure.database.engine import (
    create_session_factory,
    session_scope,
)
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyPlanRepository,
    SqlAlchemySubscriptionEventRepository,
    SqlAlchemySubscriptionRepository,
)
from tests.v2_builders import (
    NOW,
    SUBSCRIPTION,
    a_normalized_event,
    a_plan,
    a_provider_subscription_state,
    a_subscription,
)
from tests.v2_fakes import FakeBillingProvider
from tests.v2_rows import a_user_row

pytestmark = pytest.mark.asyncio

# The base row is seeded at NOW/sequence-1; the two racing deliveries both postdate it, so both
# would supersede it read in isolation. The newer carries the strictly later instant, so whichever
# interleaving occurs the subscription must end at the newer state (CANCELED, sequence 3).
_OLDER_AT = NOW + timedelta(hours=1)
_NEWER_AT = NOW + timedelta(hours=2)


@pytest_asyncio.fixture
async def committed_world(db_engine):
    """A real session factory whose committed rows are truncated on teardown.

    The ordering guarantee can only be shown across two connections that see each other's committed
    writes, so these tests cannot lean on `db_session`'s rollback. This commits for real and, on the
    way out, truncates `users, plans` with CASCADE — which reaches every subscription and
    subscription event — leaving the once-per-session schema clean for whatever runs next.
    """
    factory = create_session_factory(db_engine)
    try:
        yield factory
    finally:
        async with db_engine.begin() as connection:
            await connection.execute(text(
                "TRUNCATE users, plans RESTART IDENTITY CASCADE"))


async def _seed_account_plan_and_base_subscription(factory) -> None:
    """Commit the owner, the `pro` plan its subscription points at, and the base state at NOW/1.

    The base row is `ACTIVE` with `provider_event_at=NOW` and sequence 1, so both racing deliveries
    (each at a later instant) supersede it when read alone — which is exactly the contention the
    lock must resolve to a single, newest winner.
    """
    async with session_scope(factory) as session:
        session.add(a_user_row(display_name="owner"))
        await session.flush()
        await SqlAlchemyPlanRepository(session).upsert(a_plan(slug="pro"))
        await SqlAlchemySubscriptionRepository(session).upsert(a_subscription())


def _older_delivery():
    """The stale delivery: `PAST_DUE` at `_OLDER_AT`, sequence 2, its own event id.

    Postdates the base (so it supersedes it read alone) but predates the newer delivery, so a
    correct apply must never let it be the final state — even when it commits last.
    """
    return a_normalized_event(
        external_event_id="evt_older",
        event_at=_OLDER_AT,
        event_sequence=2,
        subscription=a_provider_subscription_state(status=SubscriptionStatus.PAST_DUE))


def _newer_delivery():
    """The winning delivery: `CANCELED` at `_NEWER_AT`, sequence 3, its own event id."""
    return a_normalized_event(
        external_event_id="evt_newer",
        event_at=_NEWER_AT,
        event_sequence=3,
        subscription=a_provider_subscription_state(status=SubscriptionStatus.CANCELED))


async def _process_in_own_unit_of_work(factory, event):
    """Run one delivery through a real `BillingWebhookService` in its own committed transaction.

    Each caller gets its own connection (its own `session_scope`), which is what lets the
    transaction-scoped advisory lock actually serialize the two — a single connection would
    re-enter the lock rather than block. The fake provider simply hands back the pre-normalized
    event, so no signature or network is involved; the real repositories and the real `_apply`
    read-modify-write are what is under test.
    """
    provider = FakeBillingProvider()
    provider.queue(event)
    async with session_scope(factory) as session:
        service = BillingWebhookService(
            provider,
            SqlAlchemySubscriptionRepository(session),
            SqlAlchemyPlanRepository(session),
            SqlAlchemySubscriptionEventRepository(session))
        return await service.process(
            payload=b"{}", headers={}, received_at=event.event_at)


async def test_out_of_order_deliveries_processed_concurrently_converge_on_the_newer(
        committed_world):
    """Older and newer, delivered at once, always leave the subscription at the newer state (§15).

    The two carry different event ids, so the ledger's unique index never arbitrates between them;
    only the per-subscription advisory lock does. Whichever interleaving the scheduler picks, the
    read-modify-write serializes and `supersedes` decides monotonically, so the stored row ends at
    `CANCELED`/sequence 3 — never rolled back to the older `PAST_DUE`/sequence 2 because it
    committed last. Both deliveries return a recorded ledger fact; neither leaks a raw error.
    """
    await _seed_account_plan_and_base_subscription(committed_world)

    older, newer = await asyncio.gather(
        _process_in_own_unit_of_work(committed_world, _older_delivery()),
        _process_in_own_unit_of_work(committed_world, _newer_delivery()),
        return_exceptions=True)

    # No raw database error (an unguarded race would surface an IntegrityError or a lost update);
    # each delivery is recorded, and the newer one is always applied whatever the interleaving.
    assert isinstance(older, SubscriptionEvent), older
    assert isinstance(newer, SubscriptionEvent), newer
    assert newer.outcome is SubscriptionEventOutcome.APPLIED
    assert older.outcome in {
        SubscriptionEventOutcome.APPLIED, SubscriptionEventOutcome.SUPERSEDED}

    async with session_scope(committed_world) as session:
        stored = await SqlAlchemySubscriptionRepository(session).find_by_id(SUBSCRIPTION)
    assert stored is not None
    assert stored.status is SubscriptionStatus.CANCELED
    assert stored.provider_event_sequence == 3
    assert stored.provider_event_at == _NEWER_AT
