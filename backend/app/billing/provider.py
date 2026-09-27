"""The billing provider boundary — provider-neutral value objects and the `BillingProvider` port.

A subscription's state is *webhook-authoritative*: the platform never trusts a browser's word
about what an account pays for; the truth arrives as a signature-verified webhook a billing
adapter normalizes. This module is the seam between "a provider's world" (Stripe's JSON, its
signature scheme, its status vocabulary, its HTTP API) and "the platform's world" (the domain's
closed `SubscriptionStatus`, its `Plan`, its `UserId`). Everything Stripe-specific lives behind
the `BillingProvider` port an adapter implements; the webhook service and the billing API depend
only on the value objects here, so a second provider is a second adapter and nothing else moves
(docs/LLM_PROVIDER_ARCHITECTURE.md's discipline, applied to billing).

The value objects are frozen application-layer dataclasses, not domain models: they are the
normalized *shape* a provider's objects take on the way in, carrying the domain's own vocabulary
(`SubscriptionStatus`, `UserId`) but none of a provider's raw strings beyond the opaque handles
the adapter alone interprets. A `NormalizedWebhookEvent` is what a verified webhook becomes; the
webhook service turns it into a `Subscription` and a `SubscriptionEvent` and nothing more — a
webhook can only change *which plan* an account is on, never a safety brake (§4, §16).
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from backend.app.domain.entitlement import Plan
from backend.app.domain.identifiers import UserId
from backend.app.domain.subscription import SubscriptionStatus


@dataclass(frozen=True)
class ProviderSubscriptionState:
    """The subscription state a normalized webhook carries — a provider's object, domain-shaped.

    `external_subscription_id` and `external_customer_id` are the provider's opaque handles (the
    subscription id derives from the first, the second reopens a portal); `plan_external_price_id`
    is the provider price the platform maps to a `Plan` by its `external_price_id`. `status` is
    already the domain's *normalized* lifecycle — the adapter collapsed the provider's own
    vocabulary onto the closed five, or refused the event — so nothing downstream ever meets a
    status it cannot weigh. The window is both-or-neither exactly as `Subscription` requires.
    """

    external_subscription_id: str
    status: SubscriptionStatus
    external_customer_id: str | None = None
    plan_external_price_id: str | None = None
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False


@dataclass(frozen=True)
class NormalizedWebhookEvent:
    """A signature-verified provider webhook, normalized to the platform's vocabulary (§12-15).

    `provider` and `external_event_id` are what the processed-event id derives from, so a
    redelivery is recognised; `event_type` is the provider's raw type, kept for the audit ledger.
    `event_at`/`event_sequence` are the provenance the out-of-order guard compares. `subscription`
    is the state the event carries, or `None` for a verified event the platform parses but does
    not act on (a type it maps to no subscription state). `client_user_id` is the account the
    provider echoed back from the checkout the platform started (Stripe's `client_reference_id` /
    subscription metadata) — the only way to attribute the *first* event about a brand-new
    subscription, before any row exists to look up by its derived id.
    """

    provider: str
    external_event_id: str
    event_type: str
    event_at: datetime
    event_sequence: int | None = None
    subscription: ProviderSubscriptionState | None = None
    client_user_id: UserId | None = None


@dataclass(frozen=True)
class CheckoutSession:
    """A provider-hosted checkout the browser is redirected to — its URL and the provider handle.

    The platform never handles card data: opening a subscription is a redirect to the provider's
    own page. `redirect_url` is where the browser goes; `external_id` is the provider's id for the
    session, kept so a later reconciliation can find it.
    """

    redirect_url: str
    external_id: str


@dataclass(frozen=True)
class PortalSession:
    """A provider-hosted billing portal the account manages its subscription in — just its URL.

    Cancelling, updating a card or changing plan all happen on the provider's page; the platform
    only mints the link and redirects, then learns the result as a webhook.
    """

    redirect_url: str


@runtime_checkable
class BillingProvider(Protocol):
    """The port a billing adapter implements — verify webhooks in, open provider sessions out.

    Provider-neutral by construction: the webhook service and the billing API hold this, never a
    Stripe client, so every provider specific (the signature scheme, the JSON shape, the status
    vocabulary, the HTTP surface) stays inside the adapter. An implementation names *no* provider
    in its constructor beyond its own key; bootstrap wires the concrete adapter, exactly as the
    LLM registry is wired. Nothing here reads a clock — `verify_webhook` takes the instant it
    checks a signature's freshness against — the convention every V2 boundary keeps.
    """

    @property
    def provider_key(self) -> str:
        """The stable key stamped on subscriptions and events this adapter owns (e.g. `stripe`)."""
        ...

    def verify_webhook(self, *, payload: bytes, headers: Mapping[str, str],
                       now: datetime) -> NormalizedWebhookEvent:
        """Verify an inbound webhook's signature and normalize it, or raise (§12-14).

        Checks the payload's signature and freshness against `now` using the adapter's own scheme
        and secret, then parses the verified body into a `NormalizedWebhookEvent` carrying the
        domain's vocabulary. Raises `BillingError(WEBHOOK_SIGNATURE_INVALID)` when the signature
        or timestamp does not check out — *before* trusting a byte of the payload — and
        `BillingError(WEBHOOK_MALFORMED)` when a verified payload cannot be normalized (not JSON, a
        field missing, a status outside the closed set). Never contacts the provider: verification
        is local, keyed on a shared secret.
        """
        ...

    async def open_checkout(self, *, plan: Plan, client_user_id: UserId,
                            success_url: str, cancel_url: str,
                            customer_id: str | None = None) -> CheckoutSession:
        """Open a provider-hosted checkout for `plan`, returning the redirect, or raise.

        Attaches `client_user_id` so the resulting subscription's first webhook can be attributed
        to the account, and reuses `customer_id` when the account already has one so a returning
        subscriber is not duplicated provider-side. Raises `BillingError(PROVIDER_UNAVAILABLE)`
        when the call out fails or the provider answers with an error.
        """
        ...

    async def open_portal(self, *, customer_id: str, return_url: str) -> PortalSession:
        """Open the provider's billing portal for an existing customer, or raise.

        Where an account cancels, updates a card or changes plan — all provider-side, learned
        back as webhooks. Raises `BillingError(PROVIDER_UNAVAILABLE)` on an upstream failure.
        """
        ...
