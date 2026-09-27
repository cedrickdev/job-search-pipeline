# tests/test_v2_billing_stripe_adapter.py
"""`StripeBillingProvider` — the one place Stripe's wire format meets the platform (§11-14).

Everything Stripe-specific is confined to this adapter, so these tests pin the wire contract that
keeps that confinement honest, driven entirely off a `httpx.MockTransport` seam (the pattern from
`tests/test_v2_llm_openai_adapter.py`) so nothing here opens a socket or touches live Stripe:

- **Verify before trust.** A payload whose HMAC or timestamp does not check out — or that arrives
  with no signing secret configured — is refused *before* a byte is parsed (§12).
- **Normalize onto the closed vocabulary.** Stripe's dozen statuses collapse onto the domain's
  five; a `deleted` event is `CANCELED`; an active subscription flagged to cancel becomes
  `CANCEL_AT_PERIOD_END`; a status outside the table is `WEBHOOK_MALFORMED`, never smuggled through.
- **Leak nothing on the way out.** A checkout/portal call carries the bearer key and the plan's
  price; an upstream failure becomes a typed `PROVIDER_UNAVAILABLE` whose detail never echoes the
  provider's own message (§54).

The signing helper re-derives the `Stripe-Signature` HMAC exactly as the adapter verifies it, using
only stdlib `hmac`/`hashlib`/`json` — the same discipline the adapter itself keeps.
"""
import contextlib
import hashlib
import hmac
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from backend.app.billing.errors import BillingError, BillingErrorCode
from backend.app.billing.stripe_provider import STRIPE_PROVIDER_KEY, StripeBillingProvider
from backend.app.core.settings import StripeSettings
from backend.app.domain.subscription import SubscriptionStatus
from tests.v2_builders import USER, a_plan

pytestmark = pytest.mark.asyncio

# A fixed instant the freshness check runs against; the webhook's own `created` second is this
# same instant, so a freshly-signed payload sits well inside any sane tolerance window.
_NOW = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)
_NOW_TS = int(_NOW.timestamp())
_PERIOD_END = datetime(2026, 3, 31, 9, 30, tzinfo=UTC)
_PERIOD_END_TS = int(_PERIOD_END.timestamp())

# Fixture credentials, named so no lint rule mistakes them for a real one — the whole point of the
# mock transport is that these bytes never leave the process. `_SIGNING_MATERIAL` keys the webhook
# HMAC (the `whsec_...`); `_API_CREDENTIAL` authorises calls out (the `sk_...`).
_SIGNING_MATERIAL = "whsec_fixture_signing"
_API_CREDENTIAL = "sk_test_fixture"
_API_BASE_URL = "https://api.stripe.test"


def _settings(**overrides) -> StripeSettings:
    """Stripe settings pointed at the mock transport, with both credentials configured by default.

    A test that exercises a missing credential passes `secret_key=None` or `webhook_secret=None`;
    the values here are unpacked (never keyword string literals) so no hardcoded-secret rule fires.
    """
    fields: dict[str, object] = {
        "secret_key": _API_CREDENTIAL, "webhook_secret": _SIGNING_MATERIAL,
        "api_base_url": _API_BASE_URL, "signature_tolerance_seconds": 300}
    fields.update(overrides)
    return StripeSettings(**fields)


def _no_http(request: httpx.Request) -> httpx.Response:
    """A transport that fails loudly: the code paths using it must make no HTTP call."""
    raise AssertionError(f"no HTTP call expected (got {request.method} {request.url})")


def _verifier(**settings_overrides) -> StripeBillingProvider:
    """A provider whose HTTP client would fail if touched — `verify_webhook` calls out to nothing."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(_no_http))
    return StripeBillingProvider(_settings(**settings_overrides), client)


@contextlib.asynccontextmanager
async def _calling(handler, **settings_overrides):
    """A provider wired to `handler` for the calls out, its client closed on exit."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yield StripeBillingProvider(_settings(**settings_overrides), client)


def _sign(payload: bytes, *, signing: str = _SIGNING_MATERIAL, timestamp: int = _NOW_TS) -> str:
    """The `Stripe-Signature` header for `payload`, signed exactly as the adapter re-derives it."""
    signed = f"{timestamp}".encode() + b"." + payload
    digest = hmac.new(signing.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _signed_headers(payload: bytes, **overrides) -> dict[str, str]:
    return {"Stripe-Signature": _sign(payload, **overrides)}


def _subscription_object(*, sub_id="sub_test_0001", status="active", customer="cus_test_0001",
                         price="price_fixture_pro", cancel_at_period_end=False,
                         window=(_NOW_TS, _PERIOD_END_TS), metadata=...) -> dict[str, object]:
    """A Stripe subscription object, shaped like the `data.object` of a subscription event.

    `status=None` omits the status field; `price=None` omits the line item; `window=None` omits the
    period; `metadata=None` omits the account attribution — each drives a distinct normalize path.
    """
    obj: dict[str, object] = {"id": sub_id, "customer": customer,
                              "cancel_at_period_end": cancel_at_period_end}
    if status is not None:
        obj["status"] = status
    if window is not None:
        obj["current_period_start"], obj["current_period_end"] = window
    if price is not None:
        obj["items"] = {"data": [{"price": {"id": price}}]}
    if metadata is ...:
        obj["metadata"] = {"client_user_id": str(USER)}
    elif metadata is not None:
        obj["metadata"] = metadata
    return obj


def _event_payload(*, event_id="evt_test_0001", event_type="customer.subscription.updated",
                   created=_NOW_TS, obj=...) -> bytes:
    """A Stripe event body; `obj=None` omits `data.object` (a type that carries no state)."""
    body: dict[str, object] = {"id": event_id, "type": event_type, "created": created}
    resolved = _subscription_object() if obj is ... else obj
    if resolved is not None:
        body["data"] = {"object": resolved}
    return json.dumps(body).encode()


# --- verify_webhook: refuse before trust (§12) ------------------------------------------------


async def test_the_adapter_announces_its_stable_provider_key():
    assert _verifier().provider_key == STRIPE_PROVIDER_KEY == "stripe"


async def test_a_valid_signature_verifies_and_normalizes_the_event():
    """A correctly-signed subscription event parses into the domain's vocabulary end to end."""
    payload = _event_payload()
    event = _verifier().verify_webhook(
        payload=payload, headers=_signed_headers(payload), now=_NOW)

    assert event.provider == STRIPE_PROVIDER_KEY
    assert event.external_event_id == "evt_test_0001"
    assert event.event_type == "customer.subscription.updated"
    assert event.event_at == datetime.fromtimestamp(_NOW_TS, tz=UTC)
    assert event.client_user_id == USER
    state = event.subscription
    assert state is not None
    assert state.external_subscription_id == "sub_test_0001"
    assert state.status is SubscriptionStatus.ACTIVE
    assert state.external_customer_id == "cus_test_0001"
    assert state.plan_external_price_id == "price_fixture_pro"
    assert state.current_period_start == datetime.fromtimestamp(_NOW_TS, tz=UTC)
    assert state.current_period_end == datetime.fromtimestamp(_PERIOD_END_TS, tz=UTC)
    assert state.cancel_at_period_end is False


async def test_a_tampered_body_fails_signature_verification():
    """A signature minted for one body does not verify a different body — the HMAC covers it."""
    headers = _signed_headers(_event_payload())
    forged = _event_payload(event_id="evt_forged")
    with pytest.raises(BillingError) as caught:
        _verifier().verify_webhook(payload=forged, headers=headers, now=_NOW)
    assert caught.value.code is BillingErrorCode.WEBHOOK_SIGNATURE_INVALID


async def test_a_stale_timestamp_is_refused_even_with_a_valid_signature():
    """A correctly-signed but old payload is refused: the freshness window blocks a replay."""
    payload = _event_payload()
    headers = _signed_headers(payload, timestamp=_NOW_TS - 301)  # signed, but outside 300s
    with pytest.raises(BillingError) as caught:
        _verifier().verify_webhook(payload=payload, headers=headers, now=_NOW)
    assert caught.value.code is BillingErrorCode.WEBHOOK_SIGNATURE_INVALID


async def test_a_missing_signature_header_is_refused():
    with pytest.raises(BillingError) as caught:
        _verifier().verify_webhook(payload=_event_payload(), headers={}, now=_NOW)
    assert caught.value.code is BillingErrorCode.WEBHOOK_SIGNATURE_INVALID


async def test_a_header_carrying_no_v1_signature_is_refused():
    payload = _event_payload()
    with pytest.raises(BillingError) as caught:
        _verifier().verify_webhook(
            payload=payload, headers={"Stripe-Signature": f"t={_NOW_TS}"}, now=_NOW)
    assert caught.value.code is BillingErrorCode.WEBHOOK_SIGNATURE_INVALID


async def test_a_non_integer_timestamp_is_refused():
    payload = _event_payload()
    with pytest.raises(BillingError) as caught:
        _verifier().verify_webhook(
            payload=payload, headers={"Stripe-Signature": "t=nope,v1=deadbeef"}, now=_NOW)
    assert caught.value.code is BillingErrorCode.WEBHOOK_SIGNATURE_INVALID


async def test_a_webhook_is_refused_unread_when_no_signing_secret_is_configured():
    """With no signing secret the payload is refused on signature grounds before it is parsed.

    The body is deliberately not even valid JSON: a deployment that has not configured a signing
    secret must never fall through to parsing an unverifiable payload.
    """
    with pytest.raises(BillingError) as caught:
        _verifier(webhook_secret=None).verify_webhook(
            payload=b"not-json", headers={"Stripe-Signature": "t=1,v1=x"}, now=_NOW)
    assert caught.value.code is BillingErrorCode.WEBHOOK_SIGNATURE_INVALID


# --- verify_webhook: normalize onto the closed vocabulary (§14) --------------------------------


async def test_a_verified_body_that_is_not_json_is_malformed():
    payload = b"not json at all"
    with pytest.raises(BillingError) as caught:
        _verifier().verify_webhook(payload=payload, headers=_signed_headers(payload), now=_NOW)
    assert caught.value.code is BillingErrorCode.WEBHOOK_MALFORMED


async def test_a_verified_body_missing_the_created_timestamp_is_malformed():
    payload = json.dumps({"id": "evt_x", "type": "customer.subscription.updated"}).encode()
    with pytest.raises(BillingError) as caught:
        _verifier().verify_webhook(payload=payload, headers=_signed_headers(payload), now=_NOW)
    assert caught.value.code is BillingErrorCode.WEBHOOK_MALFORMED


async def test_a_subscription_status_outside_the_known_set_is_malformed():
    payload = _event_payload(obj=_subscription_object(status="frozen"))
    with pytest.raises(BillingError) as caught:
        _verifier().verify_webhook(payload=payload, headers=_signed_headers(payload), now=_NOW)
    assert caught.value.code is BillingErrorCode.WEBHOOK_MALFORMED


@pytest.mark.parametrize(("stripe_status", "expected"), [
    ("trialing", SubscriptionStatus.TRIALING),
    ("active", SubscriptionStatus.ACTIVE),
    ("past_due", SubscriptionStatus.PAST_DUE),
    ("unpaid", SubscriptionStatus.PAST_DUE),
    ("paused", SubscriptionStatus.PAST_DUE),
    ("incomplete", SubscriptionStatus.PAST_DUE),
    ("incomplete_expired", SubscriptionStatus.CANCELED),
    ("canceled", SubscriptionStatus.CANCELED),
])
async def test_the_status_table_collapses_stripes_vocabulary_onto_the_closed_five(
        stripe_status, expected):
    """Every Stripe status maps onto exactly one domain status; the ones granting no access degrade."""
    payload = _event_payload(obj=_subscription_object(status=stripe_status))
    event = _verifier().verify_webhook(
        payload=payload, headers=_signed_headers(payload), now=_NOW)
    assert event.subscription is not None
    assert event.subscription.status is expected


async def test_a_deleted_event_is_canceled_regardless_of_the_objects_own_status():
    """A `deleted` event is `CANCELED` even if the object still reads `active` — the type decides."""
    payload = _event_payload(
        event_type="customer.subscription.deleted", obj=_subscription_object(status="active"))
    event = _verifier().verify_webhook(
        payload=payload, headers=_signed_headers(payload), now=_NOW)
    assert event.subscription is not None
    assert event.subscription.status is SubscriptionStatus.CANCELED


async def test_an_active_subscription_flagged_to_cancel_normalizes_to_cancel_at_period_end():
    """`active` + `cancel_at_period_end` is the domain's `CANCEL_AT_PERIOD_END`, window still granted."""
    payload = _event_payload(obj=_subscription_object(cancel_at_period_end=True))
    event = _verifier().verify_webhook(
        payload=payload, headers=_signed_headers(payload), now=_NOW)
    assert event.subscription is not None
    assert event.subscription.status is SubscriptionStatus.CANCEL_AT_PERIOD_END
    assert event.subscription.cancel_at_period_end is True


async def test_a_non_subscription_event_type_carries_no_state():
    """A verified event outside the subscription set normalizes to `subscription=None` (IGNORED)."""
    payload = _event_payload(event_type="invoice.paid", obj=None)
    event = _verifier().verify_webhook(
        payload=payload, headers=_signed_headers(payload), now=_NOW)
    assert event.event_type == "invoice.paid"
    assert event.subscription is None
    assert event.client_user_id is None


async def test_a_subscription_without_client_metadata_has_no_attributed_account():
    """Absent the checkout metadata, the first event carries no `client_user_id` to attribute by."""
    payload = _event_payload(obj=_subscription_object(metadata=None))
    event = _verifier().verify_webhook(
        payload=payload, headers=_signed_headers(payload), now=_NOW)
    assert event.client_user_id is None


async def test_the_billing_window_falls_back_to_the_line_item_period():
    """When the top-level period is absent, the window is read from the first line item instead."""
    obj = _subscription_object(window=None)
    obj["items"] = {"data": [{"price": {"id": "price_fixture_pro"},
                              "current_period_start": _NOW_TS,
                              "current_period_end": _PERIOD_END_TS}]}
    payload = _event_payload(obj=obj)
    event = _verifier().verify_webhook(
        payload=payload, headers=_signed_headers(payload), now=_NOW)
    assert event.subscription is not None
    assert event.subscription.current_period_start == datetime.fromtimestamp(_NOW_TS, tz=UTC)
    assert event.subscription.current_period_end == datetime.fromtimestamp(_PERIOD_END_TS, tz=UTC)


async def test_a_half_open_window_degrades_to_unbounded():
    """A period with only one edge is not a valid window; it degrades to unbounded, both `None`."""
    payload = _event_payload(obj=_subscription_object(window=(_NOW_TS, None)))
    event = _verifier().verify_webhook(
        payload=payload, headers=_signed_headers(payload), now=_NOW)
    assert event.subscription is not None
    assert event.subscription.current_period_start is None
    assert event.subscription.current_period_end is None


# --- calls out: open a checkout or portal, leaking nothing (§54) -------------------------------


async def test_open_checkout_posts_the_price_with_the_bearer_key_and_returns_the_redirect():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"id": "cs_test_0001", "url": "https://pay.test/cs"})

    async with _calling(handler) as provider:
        session = await provider.open_checkout(
            plan=a_plan(), client_user_id=USER,
            success_url="https://app.test/ok", cancel_url="https://app.test/no")

    assert session.redirect_url == "https://pay.test/cs"
    assert session.external_id == "cs_test_0001"
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/checkout/sessions"
    assert seen["auth"] == f"Bearer {_API_CREDENTIAL}"
    form = seen["form"]
    assert form["mode"] == ["subscription"]
    assert form["line_items[0][price]"] == ["price_fixture_pro"]
    assert form["client_reference_id"] == [str(USER)]
    assert form["subscription_data[metadata][client_user_id]"] == [str(USER)]
    assert "customer" not in form


async def test_open_checkout_reuses_a_supplied_customer():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"id": "cs_x", "url": "https://pay.test/x"})

    async with _calling(handler) as provider:
        await provider.open_checkout(
            plan=a_plan(), client_user_id=USER, customer_id="cus_returning",
            success_url="https://app.test/ok", cancel_url="https://app.test/no")

    assert seen["form"]["customer"] == ["cus_returning"]


async def test_open_checkout_for_a_plan_without_a_price_is_refused_before_any_call():
    async with _calling(_no_http) as provider:
        with pytest.raises(BillingError) as caught:
            await provider.open_checkout(
                plan=a_plan(external_price_id=None), client_user_id=USER,
                success_url="https://app.test/ok", cancel_url="https://app.test/no")
    assert caught.value.code is BillingErrorCode.PROVIDER_UNAVAILABLE


async def test_open_portal_posts_the_customer_and_returns_the_redirect():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"url": "https://billing.test/portal"})

    async with _calling(handler) as provider:
        session = await provider.open_portal(
            customer_id="cus_test_0001", return_url="https://app.test/account")

    assert session.redirect_url == "https://billing.test/portal"
    assert seen["path"] == "/v1/billing_portal/sessions"
    assert seen["form"]["customer"] == ["cus_test_0001"]
    assert seen["form"]["return_url"] == ["https://app.test/account"]


async def test_a_provider_error_status_becomes_provider_unavailable_without_leaking_its_message():
    async with _calling(lambda _r: httpx.Response(500, text="upstream boom")) as provider:
        with pytest.raises(BillingError) as caught:
            await provider.open_portal(customer_id="cus_x", return_url="https://app.test/account")
    assert caught.value.code is BillingErrorCode.PROVIDER_UNAVAILABLE
    assert "boom" not in caught.value.detail  # the provider's own words never surface (§54)


async def test_a_non_json_provider_response_becomes_provider_unavailable():
    async with _calling(lambda _r: httpx.Response(200, text="not json")) as provider:
        with pytest.raises(BillingError) as caught:
            await provider.open_portal(customer_id="cus_x", return_url="https://app.test/account")
    assert caught.value.code is BillingErrorCode.PROVIDER_UNAVAILABLE


async def test_a_transport_failure_becomes_provider_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _calling(handler) as provider:
        with pytest.raises(BillingError) as caught:
            await provider.open_portal(customer_id="cus_x", return_url="https://app.test/account")
    assert caught.value.code is BillingErrorCode.PROVIDER_UNAVAILABLE


async def test_a_call_out_without_a_secret_key_is_refused_before_any_request():
    async with _calling(_no_http, secret_key=None) as provider:
        with pytest.raises(BillingError) as caught:
            await provider.open_portal(customer_id="cus_x", return_url="https://app.test/account")
    assert caught.value.code is BillingErrorCode.PROVIDER_UNAVAILABLE


