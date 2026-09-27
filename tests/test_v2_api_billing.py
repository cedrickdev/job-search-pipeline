# tests/test_v2_api_billing.py
"""`/api/v2/billing` over HTTP — the commercial surface a browser and a provider meet.

Phase 16 §17-18, §63-64. The service decisions (what the resolver resolves, what the
metering layer sums, how a webhook is applied idempotently and monotonically) are proved
in `test_v2_billing.py` and `test_v2_billing_webhooks.py`. This file covers what the
*route* layer is responsible for, which is a different set of facts:

- the five read/checkout routes are session-scoped like the rest of the surface, and the
  sixth — the webhook — is the one route on the whole surface with no session, because
  the caller is a provider carrying a signature, not a browser carrying a cookie (§64);
- the catalogue a client reads never carries the provider price handle a checkout needs
  server-side (§17, §61) — there is no field for it to leak through;
- a fresh account resolves to the free tier, and a verified webhook is what moves it onto
  a paid plan (§16) — a browser never gets to;
- the two hosted opens send the provider the *server's* redirect URLs, never the request's
  (§18), so a request naming only a plan slug cannot smuggle an origin into a redirect;
- every read is owner-scoped: one account's subscription and usage never enter another's
  (§63).

The webhook is driven through the harness's `FakeBillingProvider`: a test queues the
`NormalizedWebhookEvent` a *verified* payload would become (or a `BillingError` to drive
the reject-before-trust path), so no HMAC, socket or signing secret is involved — the
route's own job is to read the raw bytes, hand them to the service, and return the
minimal, secret-free acknowledgement, and that is what is asserted here.
"""
from datetime import UTC, datetime, timedelta

import pytest

from backend.app.billing.errors import BillingError, BillingErrorCode
from backend.app.billing.provider import NormalizedWebhookEvent, ProviderSubscriptionState
from backend.app.domain.entitlement import EntitlementKey, EntitlementMeasure
from backend.app.domain.identifiers import plan_id
from backend.app.domain.subscription import SubscriptionStatus
from tests.v2_api import EMAIL, NOW, OTHER_EMAIL, api_harness
from tests.v2_builders import a_subscription, a_usage_event

# A paid window that brackets the harness clock (`NOW`), so a subscription seeded or
# applied with it actually grants entitlements as of the request rather than reading as
# expired. Spelled once here so every "on the paid plan" assertion shares one window.
PERIOD_START = NOW - timedelta(days=1)
PERIOD_END = NOW + timedelta(days=30)


def _active_pro_state(**overrides) -> ProviderSubscriptionState:
    """The provider state a `customer.subscription.created` for the seeded `pro` plan carries.

    Its price is `price_pro` — the handle the harness seeds `pro` under, so it maps to that
    plan — and its window brackets the harness clock, so the resulting subscription grants.
    """
    fields = {
        "external_subscription_id": "sub_api_0001",
        "status": SubscriptionStatus.ACTIVE,
        "external_customer_id": "cus_api_0001",
        "plan_external_price_id": "price_pro",
        "current_period_start": PERIOD_START,
        "current_period_end": PERIOD_END,
        "cancel_at_period_end": False,
    }
    fields.update(overrides)
    return ProviderSubscriptionState(**fields)


def _event(*, user_id, external_event_id="evt_api_0001", event_at=None,
           event_sequence=1, subscription=..., event_type="customer.subscription.created",
           **overrides) -> NormalizedWebhookEvent:
    """A signature-verified webhook, normalized — what a `BillingProvider` hands the service.

    `client_user_id` is the account the provider echoed back from the checkout the platform
    started; it is the only way to attribute the *first* event about a brand-new subscription
    before any row exists to look it up by. Defaults to an active `pro` state; pass
    `subscription=None` for a verified event carrying no state, or a built state to vary it.
    """
    resolved = _active_pro_state() if subscription is ... else subscription
    fields = {
        "provider": "stripe",
        "external_event_id": external_event_id,
        "event_type": event_type,
        "event_at": event_at if event_at is not None else NOW,
        "event_sequence": event_sequence,
        "subscription": resolved,
        "client_user_id": user_id,
    }
    fields.update(overrides)
    return NormalizedWebhookEvent(**fields)


@pytest.mark.asyncio
async def test_the_reads_and_the_two_opens_need_a_session_the_webhook_does_not(tmp_path):
    """Five session-scoped routes answer 401 anonymously; the webhook is reached by signature.

    The five read/checkout routes are ordinary session-scoped routes, so an anonymous caller
    is refused before the request is read — the surface-wide 401 rule. The sixth is the one
    exception: a provider calling in carries no cookie, so the webhook cannot answer 401. It
    is authenticated by the signature the service verifies, and an *unsigned* call is a 400
    (`webhook_signature_invalid`), never a 401 — proof the route is reachable without a
    session and still refuses a payload that does not verify, writing nothing.
    """
    async with api_harness(tmp_path) as api:
        for path in ("/billing/plans", "/billing/subscription", "/billing/usage"):
            response = await api.client.get(api.url(path))
            assert response.status_code == 401, path
            assert response.json()["error"] == "not_authenticated", path
        for path in ("/billing/checkout", "/billing/portal"):
            response = await api.client.post(api.url(path), json={"plan_slug": "pro"})
            assert response.status_code == 401, path
            assert response.json()["error"] == "not_authenticated", path

        # The webhook, with no session and no cookie: the provider signature failed, so the
        # service rejects the payload before trusting a byte of it — a 400, not a 401.
        api.billing_provider.queue(BillingError(
            BillingErrorCode.WEBHOOK_SIGNATURE_INVALID, "the signature did not verify"))
        rejected = await api.client.post(api.url("/billing/webhook"), content=b"{}")
        assert rejected.status_code == 400
        assert rejected.json()["error"] == "webhook_signature_invalid"
        assert api.subscriptions.subscriptions == {}
        assert api.subscription_events.events == {}


@pytest.mark.asyncio
async def test_plans_lists_the_public_catalogue_cheapest_first_without_the_price_handle(
        tmp_path):
    """The catalogue a client reads — free, pro, scale, in that order, and no provider handle.

    Prices, quotas and entitlements are server truth the client only renders (§17, §61), so
    the shape is asserted here: the three public tiers cheapest-first, `is_free` set on the
    free tier alone, and — the rule Phase 16's response walk earns its keep on — no
    `external_price_id` anywhere in the document, because it is the opaque provider token a
    checkout needs server-side and a client acts on none of it.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        response = await api.read("/billing/plans")

        assert response.status_code == 200
        plans = response.json()["plans"]
        assert [p["slug"] for p in plans] == ["free", "pro", "scale"]
        assert [p["is_free"] for p in plans] == [True, False, False]
        assert plans[0]["price_amount_cents"] is None
        assert plans[1]["price_amount_cents"] == 1900
        # The provider price handle is server-only: no plan, and no entitlement inside one,
        # may carry a field naming it — the catalogue is what the type generator reads.
        assert all("external_price_id" not in p for p in plans)
        # `scale` leaves its heavy meters unlimited — a real granted value the surface reads
        # as `is_unlimited` rather than inferring from a null limit.
        scale = plans[2]
        tokens = next(e for e in scale["entitlements"]
                      if e["key"] == EntitlementKey.LLM_TOKENS.value)
        assert tokens["limit"] is None
        assert tokens["is_unlimited"] is True


@pytest.mark.asyncio
async def test_a_fresh_account_resolves_to_the_free_tier(tmp_path):
    """No subscription is the free tier, not an error — the resolver's fallback, over HTTP.

    A brand-new account has never subscribed, so the overview is the free plan with no live
    subscription row behind it: `is_paid` false, no status or window, and no portal to open
    (there is no provider customer to manage). The billing window is still present — the
    calendar month a per-period meter sums against — because usage is metered on the free
    tier too.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        response = await api.read("/billing/subscription")

        assert response.status_code == 200
        body = response.json()
        assert body["plan"]["slug"] == "free"
        assert body["is_paid"] is False
        assert body["status"] is None
        assert body["current_period_start"] is None
        assert body["current_period_end"] is None
        assert body["cancel_at_period_end"] is False
        assert body["can_manage_billing"] is False
        assert body["period"]["label"] == "2026-04"


@pytest.mark.asyncio
async def test_usage_counts_the_concurrent_gauge_live_and_a_meter_by_period(tmp_path):
    """The two enforcement shapes, side by side: a live gauge and a per-period sum (§6).

    Finishing onboarding leaves exactly one *active* search profile, so the concurrent
    `ACTIVE_SEARCH_PROFILES` gauge reads a live count of 1 against the free tier's ceiling of
    1 — no room left. A seeded application-submission usage event of quantity 2 in this
    account's current window is summed by the per-period meter to 2 against the free tier's 5,
    leaving 3. The same split the metering layer enforces, so the numbers a `/billing` panel
    shows are the numbers a quota check reads.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await api.finish_onboarding()
        user_id = (await api.users.get_by_email(EMAIL)).id
        await api.usage_events.add(a_usage_event(
            user_id=user_id, entitlement_key=EntitlementKey.APPLICATION_SUBMISSIONS,
            quantity=2, billing_period="2026-04"))

        response = await api.read("/billing/usage")

        assert response.status_code == 200
        lines = {line["key"]: line for line in response.json()["lines"]}
        gauge = lines[EntitlementKey.ACTIVE_SEARCH_PROFILES.value]
        assert gauge["measure"] == EntitlementMeasure.CONCURRENT.value
        assert (gauge["used"], gauge["limit"], gauge["remaining"]) == (1, 1, 0)
        meter = lines[EntitlementKey.APPLICATION_SUBMISSIONS.value]
        assert meter["measure"] == EntitlementMeasure.PER_PERIOD.value
        assert (meter["used"], meter["limit"], meter["remaining"]) == (2, 5, 3)


@pytest.mark.asyncio
async def test_a_verified_webhook_moves_the_account_onto_the_paid_plan(tmp_path):
    """A subscription changes only when the provider says so — the webhook, not a browser (§16).

    The account signs in but never touches a write that could set a subscription; the paid
    plan arrives as a verified webhook the provider echoed the account id back on. The route
    records one `APPLIED` event and returns the minimal ack — the outcome and the provider's
    raw type, and nothing about the account behind it. Reading the overview afterwards shows
    the account on `pro`, paying, with a portal now openable against its provider customer.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = (await api.users.get_by_email(EMAIL)).id
        api.billing_provider.queue(_event(user_id=user_id))

        ack = await api.client.post(api.url("/billing/webhook"), content=b"{}")

        assert ack.status_code == 200
        assert ack.json() == {"outcome": "APPLIED",
                              "event_type": "customer.subscription.created"}
        # One subscription row, owned by this account and on the pro plan the price mapped to.
        (stored,) = tuple(api.subscriptions.subscriptions.values())
        assert stored.user_id == user_id
        assert stored.plan_id == plan_id("pro")

        overview = (await api.read("/billing/subscription")).json()
        assert overview["plan"]["slug"] == "pro"
        assert overview["is_paid"] is True
        assert overview["status"] == SubscriptionStatus.ACTIVE.value
        assert overview["can_manage_billing"] is True


@pytest.mark.asyncio
async def test_checkout_opens_a_hosted_session_carrying_the_servers_urls_not_the_requests(
        tmp_path):
    """A checkout is attributed to the session and bounced to the *server's* URLs (§18).

    The request names only a plan slug; the account comes from the session and the success and
    cancel URLs are built from `SiteSettings` server-side, so the provider is called with the
    server's origin and the session's account, never anything the request chose. And there is no
    field for a request to choose one through: an extra `success_url` is refused by the closed
    request schema (422) before the provider is ever reached — the smuggle the derived URLs exist
    to prevent cannot even be expressed.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = (await api.users.get_by_email(EMAIL)).id

        created = await api.write("POST", "/billing/checkout", json={"plan_slug": "pro"})

        assert created.status_code == 201
        assert created.json() == {"redirect_url": "https://billing.example/checkout/cs_fake"}
        (call,) = api.billing_provider.open_checkout_calls
        assert call["client_user_id"] == user_id
        assert call["success_url"] == "http://localhost:3000/billing?checkout=success"
        assert call["cancel_url"] == "http://localhost:3000/billing?checkout=cancelled"
        assert call["customer_id"] is None
        assert call["plan"].slug == "pro"

        # A request has no field to name a redirect through: an extra `success_url` is refused by
        # the closed schema, and the provider is never reached — one call still, the first.
        smuggled = await api.write("POST", "/billing/checkout",
                                   json={"plan_slug": "pro",
                                         "success_url": "https://attacker.example/steal"})
        assert smuggled.status_code == 422
        assert smuggled.json()["error"] == "validation_failed"
        assert len(api.billing_provider.open_checkout_calls) == 1


@pytest.mark.asyncio
async def test_checkout_refuses_an_unknown_plan_and_the_free_tier_before_the_provider(tmp_path):
    """Two checkout refusals resolved server-side, never handed to the provider to fail (§18).

    A slug the catalogue does not carry is a 404 (`plan_not_found`); the free tier — a real plan,
    but not one you subscribe to — is a 409 (`plan_not_purchasable`). Both are decided before the
    provider is called, so no hosted session is opened for a purchase that could never complete.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        unknown = await api.write("POST", "/billing/checkout",
                                  json={"plan_slug": "does-not-exist"})
        assert unknown.status_code == 404
        assert unknown.json()["error"] == "plan_not_found"

        free = await api.write("POST", "/billing/checkout", json={"plan_slug": "free"})
        assert free.status_code == 409
        assert free.json()["error"] == "plan_not_purchasable"

        # Neither refusal reached the provider — a checkout that cannot complete opens no session.
        assert api.billing_provider.open_checkout_calls == []


@pytest.mark.asyncio
async def test_the_portal_needs_a_provider_customer_and_returns_to_the_servers_screen(tmp_path):
    """A portal manages an existing customer, so a fresh account has nothing to open (§18).

    An account that never subscribed has no provider customer, which is a 409
    (`billing_customer_missing`) resolved here rather than a provider error. Once a
    provider-backed subscription exists, the portal opens against that account's customer handle
    and returns the browser to the server's `/billing` screen — again a server-built URL, never
    the request's.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = (await api.users.get_by_email(EMAIL)).id

        # Fresh: no customer to manage.
        missing = await api.write("POST", "/billing/portal")
        assert missing.status_code == 409
        assert missing.json()["error"] == "billing_customer_missing"
        assert api.billing_provider.open_portal_calls == []

        # A provider-backed subscription gives the account a customer handle to manage.
        await api.subscriptions.upsert(a_subscription(
            user_id=user_id, current_period_start=PERIOD_START, current_period_end=PERIOD_END))

        opened = await api.write("POST", "/billing/portal")
        assert opened.status_code == 201
        assert opened.json() == {"redirect_url": "https://billing.example/portal/ps_fake"}
        (call,) = api.billing_provider.open_portal_calls
        assert call["customer_id"] == "cus_fixture_0001"
        assert call["return_url"] == "http://localhost:3000/billing"


@pytest.mark.asyncio
async def test_one_accounts_subscription_and_usage_never_enter_anothers(tmp_path):
    """Owner-scoped to the last read: one account's paid plan and usage are invisible to another (§63).

    Account A is put on the paid plan with metered usage in the current window; A reads its pro
    subscription and its usage back. A second account signs in over the same client — replacing the
    session cookie — and reads the *same* two routes: it sees the free tier, no live subscription,
    and a zero meter. Neither read is parameterised by anything but the session, so B can name no
    handle that would reach A's row.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        a_id = (await api.users.get_by_email(EMAIL)).id
        await api.subscriptions.upsert(a_subscription(
            user_id=a_id, current_period_start=PERIOD_START, current_period_end=PERIOD_END))
        # A paid subscription meters against its own window, labelled by its start date — so the
        # seeded event counts in A's snapshot only when its `billing_period` is that same label.
        await api.usage_events.add(a_usage_event(
            user_id=a_id, entitlement_key=EntitlementKey.APPLICATION_SUBMISSIONS,
            quantity=3, billing_period=PERIOD_START.date().isoformat()))

        a_overview = (await api.read("/billing/subscription")).json()
        assert a_overview["plan"]["slug"] == "pro"
        assert a_overview["is_paid"] is True
        a_lines = {line["key"]: line
                   for line in (await api.read("/billing/usage")).json()["lines"]}
        assert a_lines[EntitlementKey.APPLICATION_SUBMISSIONS.value]["used"] == 3

        # A second account, over the same client: the cookie now carries B's identity.
        await api.register(email=OTHER_EMAIL, display_name="Someone Else")
        await api.log_in(email=OTHER_EMAIL)

        b_overview = (await api.read("/billing/subscription")).json()
        assert b_overview["plan"]["slug"] == "free"
        assert b_overview["is_paid"] is False
        assert b_overview["status"] is None
        b_lines = {line["key"]: line
                   for line in (await api.read("/billing/usage")).json()["lines"]}
        assert b_lines[EntitlementKey.APPLICATION_SUBMISSIONS.value]["used"] == 0




