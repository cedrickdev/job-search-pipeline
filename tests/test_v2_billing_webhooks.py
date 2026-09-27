# tests/test_v2_billing_webhooks.py
"""`BillingWebhookService` — the narrow, auditable path from a signed payload to subscription truth.

The webhook is the *only* thing that changes what an account is subscribed to, so these tests pin
the three guards that make that authority safe and exact (§12-15), and the one boundary it must
never cross (§4, §16):

- **Verify before trust.** A payload the provider adapter refuses never reaches the store.
- **One effect per event.** A redelivery is recognised by its ledger id and its effect is not
  applied a second time — even a *tampered* redelivery carrying a different state is a no-op.
- **Never backwards.** A stale event is recorded `SUPERSEDED` and the stored subscription stands.
- **Capability-only.** A cancellation flips the status and keeps the row: the account falls back
  to the free tier's ceilings, its data and its policy untouched.

The provider is a `FakeBillingProvider` that replays a queued `NormalizedWebhookEvent` (or raises a
queued `BillingError`), so nothing here verifies an HMAC or parses JSON — that is the Stripe
adapter's test. The repositories are the in-memory fakes, whose idempotency mirrors the real
SAVEPOINT convergence. Every assertion is about what a *verified* event becomes.
"""
import pytest

from backend.app.billing.errors import BillingError, BillingErrorCode
from backend.app.billing.webhooks import BillingWebhookService
from backend.app.domain.subscription import SubscriptionStatus
from backend.app.domain.subscription_event import SubscriptionEventOutcome
from tests.v2_builders import (
    LATER,
    NOW,
    PRO_PLAN,
    SUBSCRIPTION,
    SUBSCRIPTION_EVENT,
    USER,
    a_normalized_event,
    a_plan,
    a_provider_subscription_state,
    a_subscription,
)
from tests.v2_fakes import (
    FakeBillingProvider,
    FakePlanRepository,
    FakeSubscriptionEventRepository,
    FakeSubscriptionRepository,
)

pytestmark = pytest.mark.asyncio

# The fake provider ignores payload and headers — it replays the queued event — so the bytes
# passed to `process` are a placeholder that stands in for the verified body.
_PAYLOAD = b"{}"
_HEADERS: dict[str, str] = {}


def _service(provider, subscriptions, plans, events) -> BillingWebhookService:
    return BillingWebhookService(provider, subscriptions, plans, events)


async def _seeded():
    """A provider, the two ledgers and a plan catalogue seeded with the `pro` plan.

    The `pro` plan carries `external_price_id="price_fixture_pro"`, which the default normalized
    event's state names — so the price maps to a plan and the common path applies.
    """
    provider = FakeBillingProvider()
    subscriptions = FakeSubscriptionRepository()
    plans = FakePlanRepository()
    events = FakeSubscriptionEventRepository()
    await plans.upsert(a_plan())
    return provider, subscriptions, plans, events


async def test_the_first_event_creates_and_attributes_a_new_subscription():
    """A brand-new subscription is created, placed on its plan, and attributed to the checkout's account.

    No row exists to look up by id, so the account comes from `client_user_id` — the metadata the
    provider echoes back from the checkout the platform started — and the price maps onto the
    seeded plan. The event is recorded `APPLIED` and the subscription now reads back as the account's.
    """
    provider, subscriptions, plans, events = await _seeded()
    provider.queue(a_normalized_event())

    recorded = await _service(provider, subscriptions, plans, events).process(
        payload=_PAYLOAD, headers=_HEADERS, received_at=LATER)

    assert recorded.outcome is SubscriptionEventOutcome.APPLIED
    assert recorded.id == SUBSCRIPTION_EVENT
    assert recorded.user_id == USER and recorded.subscription_id == SUBSCRIPTION
    stored = await subscriptions.get(USER, SUBSCRIPTION)
    assert stored is not None
    assert stored.plan_id == PRO_PLAN
    assert stored.status is SubscriptionStatus.ACTIVE
    assert stored.user_id == USER


async def test_a_redelivery_is_recognised_by_its_id_and_never_applied_twice():
    """A redelivered event — even one carrying a different state — is a recognised no-op.

    The ledger short-circuits on the event id *before* looking at content, so a second delivery of
    the same event id returns the record already stored and applies nothing. Here the redelivery
    carries a `CANCELED` state; because its id was already recorded, the subscription keeps the
    `ACTIVE` the first delivery applied — the idempotency guard is the event id, not the payload.
    """
    provider, subscriptions, plans, events = await _seeded()
    service = _service(provider, subscriptions, plans, events)
    provider.queue(a_normalized_event())
    first = await service.process(payload=_PAYLOAD, headers=_HEADERS, received_at=LATER)

    # Same event id, a tampered state: a redelivery must not re-open the effect.
    provider.queue(a_normalized_event(subscription=a_provider_subscription_state(
        status=SubscriptionStatus.CANCELED)))
    again = await service.process(payload=_PAYLOAD, headers=_HEADERS, received_at=LATER)

    assert again == first
    stored = await subscriptions.get(USER, SUBSCRIPTION)
    assert stored is not None and stored.status is SubscriptionStatus.ACTIVE


async def test_a_stale_event_is_superseded_and_the_stored_state_stands():
    """An event older than what is recorded is `SUPERSEDED`; the subscription is not rolled back.

    The out-of-order guard: the stored subscription carries a newer provenance (`LATER`/5), and the
    incoming event is dated `NOW`/1 and would cancel it. `Subscription.supersedes` rejects it, so
    the event is recorded `SUPERSEDED` and the `ACTIVE` state stands — a reordered redelivery cannot
    walk a subscription backwards.
    """
    provider, subscriptions, plans, events = await _seeded()
    await subscriptions.upsert(a_subscription(
        status=SubscriptionStatus.ACTIVE, provider_event_at=LATER,
        provider_event_sequence=5, updated_at=LATER))
    provider.queue(a_normalized_event(
        event_at=NOW, event_sequence=1,
        subscription=a_provider_subscription_state(status=SubscriptionStatus.CANCELED)))

    recorded = await _service(provider, subscriptions, plans, events).process(
        payload=_PAYLOAD, headers=_HEADERS, received_at=LATER)

    assert recorded.outcome is SubscriptionEventOutcome.SUPERSEDED
    stored = await subscriptions.get(USER, SUBSCRIPTION)
    assert stored is not None and stored.status is SubscriptionStatus.ACTIVE


async def test_a_new_subscription_whose_price_maps_to_no_plan_is_ignored():
    """A verified event the platform cannot place on the catalogue is `IGNORED`, not guessed.

    A *new* subscription whose provider price maps to no seeded plan cannot be placed on a tier,
    so the event is recorded `IGNORED` (attributed, since the checkout echoed the account) and no
    subscription is created — the platform never invents a plan for an unmapped price.
    """
    provider, subscriptions, plans, events = await _seeded()
    provider.queue(a_normalized_event(subscription=a_provider_subscription_state(
        plan_external_price_id="price_unmapped")))

    recorded = await _service(provider, subscriptions, plans, events).process(
        payload=_PAYLOAD, headers=_HEADERS, received_at=LATER)

    assert recorded.outcome is SubscriptionEventOutcome.IGNORED
    assert recorded.user_id == USER
    assert await subscriptions.get(USER, SUBSCRIPTION) is None


async def test_an_unattributable_event_is_ignored_with_a_null_owner():
    """A first event with no account to attribute it to is `IGNORED` and recorded with a null owner.

    No row exists to look up and the checkout echoed no `client_user_id`, so the platform cannot
    say whose subscription this is. It is recorded `IGNORED` with a null `user_id` — never dropped,
    so a redelivery stays a recognised no-op — and no subscription is created.
    """
    provider, subscriptions, plans, events = await _seeded()
    provider.queue(a_normalized_event(client_user_id=None))

    recorded = await _service(provider, subscriptions, plans, events).process(
        payload=_PAYLOAD, headers=_HEADERS, received_at=LATER)

    assert recorded.outcome is SubscriptionEventOutcome.IGNORED
    assert recorded.user_id is None
    assert await subscriptions.get(USER, SUBSCRIPTION) is None


async def test_a_verified_event_with_no_subscription_state_is_ignored():
    """A well-formed event the platform does not act on is `IGNORED`, attributed where it can be.

    A verified event type that carries no subscription state (the adapter normalized it with
    `subscription=None`) changes nothing, so it is recorded `IGNORED` — carrying the echoed account
    where present — so its redelivery is still a recognised no-op.
    """
    provider, subscriptions, plans, events = await _seeded()
    provider.queue(a_normalized_event(event_type="invoice.paid", subscription=None))

    recorded = await _service(provider, subscriptions, plans, events).process(
        payload=_PAYLOAD, headers=_HEADERS, received_at=LATER)

    assert recorded.outcome is SubscriptionEventOutcome.IGNORED
    assert recorded.user_id == USER


async def test_a_cancellation_is_applied_as_a_capability_only_downgrade():
    """A `deleted` event cancels in place: the row stays, only the status changes (§16).

    A cancellation is webhook-authoritative like any other event, but its effect is capability-only:
    the subscription's status becomes `CANCELED` and the row is kept, so the account simply falls
    back to the free tier's ceilings. Nothing is deleted — the service has no authority to touch a
    candidate's data or an `ApplicationPolicy`, and losing a paid plan never makes the platform less
    safe. The delete event carries no price, so the plan reference is inherited from the stored row.
    """
    provider, subscriptions, plans, events = await _seeded()
    await subscriptions.upsert(a_subscription(
        status=SubscriptionStatus.ACTIVE, provider_event_at=NOW,
        provider_event_sequence=1))
    provider.queue(a_normalized_event(
        event_type="customer.subscription.deleted", event_at=LATER, event_sequence=2,
        client_user_id=None,
        subscription=a_provider_subscription_state(
            status=SubscriptionStatus.CANCELED, plan_external_price_id=None)))

    recorded = await _service(provider, subscriptions, plans, events).process(
        payload=_PAYLOAD, headers=_HEADERS, received_at=LATER)

    assert recorded.outcome is SubscriptionEventOutcome.APPLIED
    stored = await subscriptions.get(USER, SUBSCRIPTION)
    assert stored is not None
    assert stored.status is SubscriptionStatus.CANCELED
    assert stored.plan_id == PRO_PLAN  # the plan reference survived a price-less cancellation


async def test_a_payload_that_fails_verification_is_refused_before_any_write():
    """Verify before trust: a refused payload raises and writes neither a subscription nor a record.

    The provider adapter rejects a forged or replayed payload before a byte is parsed, so the
    service re-raises the `BillingError` and reaches neither store — no subscription is created and
    no event is recorded, so a forged webhook leaves no trace in the ledger it never entered.
    """
    provider, subscriptions, plans, events = await _seeded()
    provider.queue(BillingError(
        BillingErrorCode.WEBHOOK_SIGNATURE_INVALID, "the signature does not match"))
    service = _service(provider, subscriptions, plans, events)

    with pytest.raises(BillingError) as caught:
        await service.process(payload=_PAYLOAD, headers=_HEADERS, received_at=LATER)

    assert caught.value.code is BillingErrorCode.WEBHOOK_SIGNATURE_INVALID
    assert await subscriptions.get(USER, SUBSCRIPTION) is None
    assert await events.get(SUBSCRIPTION_EVENT) is None
