"""`BillingWebhookService` — turn a verified provider webhook into subscription truth (§12-15).

The webhook is the *only* thing that changes what an account is subscribed to; a browser never
gets to. This service is the narrow, auditable path from "a signed payload arrived" to "the
subscription store reflects it", and it is deliberately the whole of that authority — it can
change *which plan* an account is on and record that it did, and nothing else. It never touches an
`ApplicationPolicy`, never deletes a candidate's data, never widens a safety brake: losing or
changing a subscription narrows or raises a *commercial* quota, one clause of the effective-
permission AND, exactly as §4 and §16 require.

Three guards make processing exactly-once and monotonic, and this service owns all three:

- **Verify before trust (§12).** The provider adapter checks the signature and freshness before a
  byte of the payload is read; a forged or replayed request never reaches the store.
- **One effect per event (§13).** Each processed event is recorded in the `SubscriptionEvent`
  ledger, its id derived from the provider's event id, so a redelivery is recognised as
  already-seen and its effect is not applied twice.
- **Never apply an event backwards (§15).** Before applying a state the service asks
  `Subscription.supersedes`; a stale redelivery of an older state is recorded as `SUPERSEDED` and
  the stored row is left untouched.

A verified event the platform does not act on — a type carrying no subscription state, one it
cannot attribute to an account, or a price that maps to no plan for a *new* subscription — is
recorded as `IGNORED` rather than dropped, so its redelivery is still a recognised no-op.
"""
from datetime import datetime

from backend.app.billing.errors import BillingError, BillingErrorCode
from backend.app.billing.provider import (
    BillingProvider,
    NormalizedWebhookEvent,
    ProviderSubscriptionState,
)
from backend.app.domain.identifiers import (
    PlanId,
    SubscriptionId,
    UserId,
    subscription_event_id,
    subscription_id,
)
from backend.app.domain.subscription import Subscription
from backend.app.domain.subscription_event import SubscriptionEvent, SubscriptionEventOutcome
from backend.app.repositories.contracts import (
    PlanRepository,
    SubscriptionEventRepository,
    SubscriptionRepository,
)


class BillingWebhookService:
    """Apply verified billing webhooks to the subscription store, idempotently and monotonically.

    Holds the provider adapter (which verifies and normalizes), the subscription store, the plan
    catalogue (to map a provider price onto a `Plan`), and the processed-event ledger (idempotency
    and audit). No clock in the constructor — `process` takes `received_at`, the instant it stamps
    on what it writes and checks a signature's freshness against — the convention every V2 service
    keeps.
    """

    def __init__(self, provider: BillingProvider, subscriptions: SubscriptionRepository,
                 plans: PlanRepository, events: SubscriptionEventRepository) -> None:
        self._provider = provider
        self._subscriptions = subscriptions
        self._plans = plans
        self._events = events

    async def process(self, *, payload: bytes, headers: dict[str, str],
                      received_at: datetime) -> SubscriptionEvent:
        """Verify, then apply one inbound webhook, returning the recorded ledger fact (§12-15).

        Raises `BillingError(WEBHOOK_SIGNATURE_INVALID | WEBHOOK_MALFORMED)` before any write when
        the payload does not verify or cannot be normalized. Otherwise records exactly one
        `SubscriptionEvent` — `APPLIED`, `SUPERSEDED` or `IGNORED` — and returns it; a redelivery
        of an already-processed event returns the record already stored without re-applying it.
        """
        event = self._provider.verify_webhook(
            payload=payload, headers=headers, now=received_at)
        event_id = subscription_event_id(event.provider, event.external_event_id)

        already = await self._events.get(event_id)
        if already is not None:
            # A redelivery of an event already handled: the ledger id collided. Its effect was
            # applied the first time (or deliberately not), so recognising it here is the no-op.
            return already

        outcome, user_id, resolved_subscription_id, detail = await self._apply(
            event, received_at=received_at)
        return await self._events.add(SubscriptionEvent(
            id=event_id,
            provider=event.provider,
            external_event_id=event.external_event_id,
            event_type=event.event_type,
            outcome=outcome,
            user_id=user_id,
            subscription_id=resolved_subscription_id,
            event_at=event.event_at,
            received_at=received_at,
            detail=detail))

    async def _apply(self, event: NormalizedWebhookEvent, *, received_at: datetime) -> tuple[
            SubscriptionEventOutcome, UserId | None, SubscriptionId | None, str | None]:
        """Apply the event's subscription state, returning the outcome and what it touched.

        The heart of the three guards: attribution and price mapping decide whether the platform
        can act at all (`IGNORED` when it cannot), `supersedes` guards against writing an older
        state backwards (`SUPERSEDED`), and everything else is a monotonic upsert (`APPLIED`).
        """
        state = event.subscription
        if state is None:
            return (SubscriptionEventOutcome.IGNORED, event.client_user_id, None,
                    f"event type '{event.event_type}' carries no subscription state")

        derived_id = subscription_id(event.provider, state.external_subscription_id)
        existing = await self._subscriptions.find_by_id(derived_id)

        user_id = existing.user_id if existing is not None else event.client_user_id
        if user_id is None:
            return (SubscriptionEventOutcome.IGNORED, None, derived_id,
                    "the subscription could not be attributed to an account")

        plan_id = await self._resolve_plan_id(state.plan_external_price_id, existing)
        if plan_id is None:
            return (SubscriptionEventOutcome.IGNORED, user_id, derived_id,
                    "the provider price maps to no plan and no existing subscription to inherit")

        if existing is not None and not existing.supersedes(
                event_at=event.event_at, event_sequence=event.event_sequence):
            return (SubscriptionEventOutcome.SUPERSEDED, user_id, derived_id,
                    "a newer subscription state is already recorded")

        subscription = self._build_subscription(
            event, state, subscription_key=derived_id, user_id=user_id, plan_id=plan_id,
            existing=existing, received_at=received_at)
        stored = await self._subscriptions.upsert(subscription)
        return (SubscriptionEventOutcome.APPLIED, user_id, stored.id, None)

    async def _resolve_plan_id(self, price_id: str | None,
                               existing: Subscription | None) -> PlanId | None:
        """The plan the event's price maps to, else the existing subscription's, else `None`.

        A `deleted`/`canceled` event need not carry a price, so an existing subscription keeps its
        plan reference (the FK is NOT NULL). A *new* subscription whose price maps to no seeded
        plan cannot be placed on the catalogue, so the event is ignored rather than guessed.
        """
        if price_id is not None:
            plan = await self._plans.get_by_external_price_id(price_id)
            if plan is not None:
                return plan.id
        return existing.plan_id if existing is not None else None

    def _build_subscription(self, event: NormalizedWebhookEvent,
                            state: ProviderSubscriptionState, *,
                            subscription_key: SubscriptionId, user_id: UserId, plan_id: PlanId,
                            existing: Subscription | None,
                            received_at: datetime) -> Subscription:
        """The normalized `Subscription` this event applies, or raise WEBHOOK_MALFORMED.

        `created_at` is preserved from the existing row (a subscription is minted once); a
        brand-new one is minted at `received_at`. A domain `ValueError` here means the verified
        payload described a state the domain forbids (a backwards window), so it is a malformed
        webhook, not a server fault — mapped to 400 rather than crashing the handler.
        """
        try:
            return Subscription(
                id=subscription_key,
                user_id=user_id,
                plan_id=plan_id,
                status=state.status,
                provider=event.provider,
                external_customer_id=state.external_customer_id,
                external_subscription_id=state.external_subscription_id,
                current_period_start=state.current_period_start,
                current_period_end=state.current_period_end,
                cancel_at_period_end=state.cancel_at_period_end,
                provider_event_at=event.event_at,
                provider_event_sequence=event.event_sequence,
                created_at=existing.created_at if existing is not None else received_at,
                updated_at=received_at)
        except ValueError as error:
            raise BillingError(
                BillingErrorCode.WEBHOOK_MALFORMED,
                f"the webhook described an invalid subscription state: {error}") from error
