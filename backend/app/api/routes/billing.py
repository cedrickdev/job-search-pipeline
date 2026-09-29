"""`/api/v2/billing`: the commercial surface — read the catalogue, pay, manage, receive events.

Six routes over two authorities kept deliberately apart. Five are the *read/checkout* side, and
they are ordinary session-scoped routes: they read the plan catalogue, the account's effective
plan and its usage, and they open the two provider-hosted sessions that do the paying. None of
them writes a subscription or a usage event — they are readers of the entitlement resolver and the
metering ledger, never a second authority over them (docs/IMPLEMENTATION_PLAN.md §Phase 16).

The sixth, `POST /billing/webhook`, is the *write* side, and it is the one route on the whole V2
surface with no session. A subscription changes only when the provider says it did, and the
provider is a server calling in with a signed body, not a browser carrying a cookie — so the
webhook is authenticated by the provider's signature inside `BillingWebhookService`, never by
`current_session`. It reads the raw request bytes (a parsed body would already have discarded the
exact bytes the signature covers) and returns a minimal, secret-free acknowledgement: the caller
is the provider, and it needs to know the event was received and how it resolved, nothing about
the account behind it (§64).

The redirect URLs a checkout and a portal return to are the server's, built from `SiteSettings`
inside the service — a request names only a plan slug and can never smuggle an origin into a
provider redirect (§18).
"""
from fastapi import APIRouter, Request, status

from backend.app.api.dependencies import (
    Billing,
    BillingWebhooks,
    CheckoutRateLimit,
    CurrentSession,
    Now,
)
from backend.app.api.schemas import (
    CheckoutRequest,
    CheckoutResponse,
    PlanListResponse,
    PortalResponse,
    SubscriptionOverviewResponse,
    UsageSnapshotResponse,
    WebhookAckResponse,
)

router = APIRouter(prefix="/billing", tags=["v2-billing"])


@router.get("/plans", response_model=PlanListResponse)
async def list_plans(current: CurrentSession, service: Billing) -> PlanListResponse:
    """The purchasable catalogue — active, public plans, cheapest first (§17, §61).

    Server truth: prices, quotas and entitlements are defined here, and the client only reads
    what this serves. Session-scoped like the rest of the surface, though the catalogue is the
    same for every account — an upgrade screen is an in-app view.
    """
    plans = await service.list_plans()
    return PlanListResponse.of(plans)


@router.get("/subscription", response_model=SubscriptionOverviewResponse)
async def subscription_overview(current: CurrentSession, service: Billing,
                                instant: Now) -> SubscriptionOverviewResponse:
    """This account's effective plan, live subscription state and billing window as of now.

    Straight from the resolver: the paid plan while it grants, the free tier otherwise. A lapsed
    payer still sees the `PAST_DUE` or expired row that explains the free-tier fallback, and
    whether a portal can be opened for it.
    """
    overview = await service.subscription_overview(current.user.id, as_of=instant)
    return SubscriptionOverviewResponse.of(overview)


@router.get("/usage", response_model=UsageSnapshotResponse)
async def usage_snapshot(current: CurrentSession, service: Billing,
                         instant: Now) -> UsageSnapshotResponse:
    """This account's consumption against its effective plan's ceilings, in the current window.

    One line per entitlement the plan grants — a period sum for a meter, a live count for the
    concurrent gauge — the same split the metering layer enforces, so the numbers shown are the
    numbers a quota check reads (§6).
    """
    snapshot = await service.usage_snapshot(current.user.id, as_of=instant)
    return UsageSnapshotResponse.of(snapshot)


@router.post("/checkout", response_model=CheckoutResponse,
             status_code=status.HTTP_201_CREATED,
             dependencies=[CheckoutRateLimit])
async def open_checkout(body: CheckoutRequest, current: CurrentSession,
                        service: Billing) -> CheckoutResponse:
    """Open a provider-hosted checkout for a purchasable plan, attributed to this account (§18).

    201, because a new hosted session is created. The service refuses a slug that names no plan
    (404) or a plan that cannot be bought — free, retired, or price-less (409) — before the
    provider is ever called. The success and cancel URLs are the server's, never the request's.
    """
    session = await service.open_checkout(current.user.id, plan_slug=body.plan_slug)
    return CheckoutResponse.of(session)


@router.post("/portal", response_model=PortalResponse,
             status_code=status.HTTP_201_CREATED)
async def open_portal(current: CurrentSession, service: Billing) -> PortalResponse:
    """Open the provider's billing portal for this account, or refuse if it has no customer.

    201 for the same reason as checkout — a hosted session is created. An account that never
    subscribed has no provider customer to manage, which is a 409, not a provider error. The
    return URL is the server's `/billing` screen (§18).
    """
    session = await service.open_portal(current.user.id)
    return PortalResponse.of(session)


@router.post("/webhook", response_model=WebhookAckResponse)
async def receive_webhook(request: Request, service: BillingWebhooks,
                          instant: Now) -> WebhookAckResponse:
    """Verify and apply one provider webhook — the only route that changes a subscription (§12).

    No session: a webhook is a server calling in, authenticated by the provider signature the
    service verifies before it trusts a byte of the payload, never by a browser cookie (§64). The
    raw request bytes are read here — the signature covers the exact bytes, so a parsed body would
    already have lost them — and the headers carry the signature. The service records exactly one
    `SubscriptionEvent` (applied, superseded or ignored) and reconciles idempotently and
    monotonically; a redelivery of an already-processed event returns the record already stored
    without re-applying it. An invalid signature or unparseable payload is a `BillingError` the
    API maps to 400, never a 401.
    """
    payload = await request.body()
    headers = dict(request.headers)
    event = await service.process(payload=payload, headers=headers, received_at=instant)
    return WebhookAckResponse.of(event)
