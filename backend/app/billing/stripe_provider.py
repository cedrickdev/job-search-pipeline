"""`StripeBillingProvider` — the one place Stripe's world touches the platform's (§11-13).

Everything Stripe-specific lives here and nowhere else: the `Stripe-Signature` scheme, the event
JSON shape, the mapping from Stripe's dozen subscription statuses onto the domain's closed five,
and the REST calls that open a checkout or a portal. The webhook service and the billing API hold
the `BillingProvider` port, never this class, so a second provider is a second adapter and no
call site changes (docs/LLM_PROVIDER_ARCHITECTURE.md's boundary discipline, applied to billing).

Signature verification is stdlib `hmac` — no Stripe SDK — because the scheme is a documented HMAC
over `f"{timestamp}.{body}"` and pulling in an SDK for one comparison would only widen the trust
surface. The verification is local (keyed on the shared webhook secret) and runs *before* a byte
of the payload is parsed, so a forged or replayed request is rejected unread (§12). The calls out
use `httpx`, form-encoded with a bearer key; the client is injected so a test drives a mock
transport and never touches live Stripe.
"""
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from backend.app.billing.errors import BillingError, BillingErrorCode
from backend.app.billing.provider import (
    CheckoutSession,
    NormalizedWebhookEvent,
    PortalSession,
    ProviderSubscriptionState,
)
from backend.app.core.settings import StripeSettings
from backend.app.domain.entitlement import Plan
from backend.app.domain.identifiers import UserId
from backend.app.domain.subscription import SubscriptionStatus

# The stable key stamped on every subscription and event this adapter owns. The subscription and
# event ids derive from it, so it must never change for a deployment already running.
STRIPE_PROVIDER_KEY = "stripe"

# The metadata key the adapter stamps on the subscription at checkout, so the *first* webhook
# about a brand-new subscription — before any row exists to look up — can be attributed to the
# account that started the checkout.
_CLIENT_USER_ID_METADATA_KEY = "client_user_id"

_SIGNATURE_HEADER = "Stripe-Signature"

# The only event types that carry subscription state the platform acts on. Every other verified
# event normalizes to `subscription=None` and the service records it as IGNORED.
_SUBSCRIPTION_EVENT_TYPES = frozenset({
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
})

# The Stripe subscription status vocabulary collapsed onto the domain's closed five (§14). A
# status absent from this map fails to normalize (`WEBHOOK_MALFORMED`) rather than smuggling an
# unknown state past the access check. `incomplete`/`unpaid`/`paused` grant no access, so they
# degrade to `PAST_DUE` (free-tier fallback, capability-only); `incomplete_expired` has ended.
_STATUS_BY_STRIPE: dict[str, SubscriptionStatus] = {
    "trialing": SubscriptionStatus.TRIALING,
    "active": SubscriptionStatus.ACTIVE,
    "past_due": SubscriptionStatus.PAST_DUE,
    "unpaid": SubscriptionStatus.PAST_DUE,
    "paused": SubscriptionStatus.PAST_DUE,
    "incomplete": SubscriptionStatus.PAST_DUE,
    "incomplete_expired": SubscriptionStatus.CANCELED,
    "canceled": SubscriptionStatus.CANCELED,
}


def _reject_signature(detail: str) -> BillingError:
    return BillingError(BillingErrorCode.WEBHOOK_SIGNATURE_INVALID, detail)


def _reject_malformed(detail: str) -> BillingError:
    return BillingError(BillingErrorCode.WEBHOOK_MALFORMED, detail)


def _provider_unavailable(detail: str) -> BillingError:
    return BillingError(BillingErrorCode.PROVIDER_UNAVAILABLE, detail)


class StripeBillingProvider:
    """The Stripe implementation of `BillingProvider` — verify webhooks in, open sessions out.

    Holds its `StripeSettings` (both credentials excluded from the settings repr) and an injected
    `httpx.AsyncClient` for the calls out; `verify_webhook` needs no client. No clock in the
    constructor — `verify_webhook` takes the instant it checks a signature's freshness against —
    the convention every V2 boundary keeps.
    """

    def __init__(self, settings: StripeSettings, http_client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = http_client

    @property
    def provider_key(self) -> str:
        return STRIPE_PROVIDER_KEY

    # -- calls in: verify and normalize a webhook --------------------------------------------

    def verify_webhook(self, *, payload: bytes, headers: Mapping[str, str],
                       now: datetime) -> NormalizedWebhookEvent:
        secret = self._settings.webhook_secret
        if not secret:
            raise _reject_signature(
                "no Stripe webhook signing secret is configured; the webhook is refused unread")
        header = _header_value(headers, _SIGNATURE_HEADER)
        if header is None:
            raise _reject_signature(f"the {_SIGNATURE_HEADER} header is missing")
        raw_timestamp, timestamp, signatures = _parse_signature_header(header)
        self._verify_signature(
            payload=payload, raw_timestamp=raw_timestamp, timestamp=timestamp,
            signatures=signatures, secret=secret, now=now)
        return _normalize_event(payload)

    def _verify_signature(self, *, payload: bytes, raw_timestamp: str, timestamp: int,
                          signatures: list[str], secret: str, now: datetime) -> None:
        tolerance = self._settings.signature_tolerance_seconds
        if abs(int(now.timestamp()) - timestamp) > tolerance:
            raise _reject_signature(
                "the webhook timestamp is outside the tolerance window; a replayed or stale "
                "payload is refused")
        signed = raw_timestamp.encode("utf-8") + b"." + payload
        expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
            raise _reject_signature("the webhook signature does not match; the payload is refused")

    # -- calls out: open a provider-hosted checkout or portal --------------------------------

    async def open_checkout(self, *, plan: Plan, client_user_id: UserId,
                            success_url: str, cancel_url: str,
                            customer_id: str | None = None) -> CheckoutSession:
        if plan.external_price_id is None:
            raise _provider_unavailable(
                f"plan '{plan.slug}' carries no provider price id; it cannot be checked out")
        data = {
            "mode": "subscription",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items[0][price]": plan.external_price_id,
            "line_items[0][quantity]": "1",
            # Attribute the resulting subscription to the account, twice: `client_reference_id`
            # on the session and the metadata the subscription itself carries, which is what the
            # first `customer.subscription.created` webhook echoes back.
            "client_reference_id": str(client_user_id),
            f"subscription_data[metadata][{_CLIENT_USER_ID_METADATA_KEY}]": str(client_user_id),
        }
        if customer_id is not None:
            data["customer"] = customer_id
        body = await self._post("/v1/checkout/sessions", data)
        return CheckoutSession(
            redirect_url=_require_str(body, "url"), external_id=_require_str(body, "id"))

    async def open_portal(self, *, customer_id: str, return_url: str) -> PortalSession:
        body = await self._post(
            "/v1/billing_portal/sessions", {"customer": customer_id, "return_url": return_url})
        return PortalSession(redirect_url=_require_str(body, "url"))

    async def cancel_subscription(self, *, external_subscription_id: str) -> None:
        """Cancel a Stripe subscription so account deletion leaves nothing paying (§27).

        `DELETE /v1/subscriptions/{id}` is Stripe's immediate cancel. Idempotent by contract: a
        404 means the subscription is already gone provider-side, which is exactly the end state
        the caller wants, so it is a success — a deletion retried after a partial failure
        converges rather than wedging on a subscription that no longer exists. Any other error
        status is `PROVIDER_UNAVAILABLE`, so the caller fails closed and leaves the account intact.
        """
        await self._delete(f"/v1/subscriptions/{quote(external_subscription_id, safe='')}")

    async def _post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        """POST form-encoded to the Stripe API with the bearer key, or raise PROVIDER_UNAVAILABLE.

        Never lets a raw provider error or body escape: the detail carries the HTTP status and a
        fixed sentence, never Stripe's message, so surfacing it echoes nothing sensitive (§54).
        """
        if not self._settings.secret_key:
            raise _provider_unavailable(
                "no Stripe secret key is configured; the billing provider cannot be called")
        url = self._settings.api_base_url.rstrip("/") + path
        try:
            response = await self._client.post(
                url, data=data,
                headers={"Authorization": f"Bearer {self._settings.secret_key}"})
        except httpx.HTTPError as error:
            raise _provider_unavailable(
                f"the billing provider could not be reached ({type(error).__name__})") from error
        if response.status_code >= 400:
            raise _provider_unavailable(
                f"the billing provider answered with status {response.status_code}")
        try:
            body = response.json()
        except ValueError as error:
            raise _provider_unavailable(
                "the billing provider returned a non-JSON response") from error
        if not isinstance(body, dict):
            raise _provider_unavailable("the billing provider returned an unexpected response")
        return body

    async def _delete(self, path: str) -> None:
        """DELETE at the Stripe API with the bearer key; 404 is success, else PROVIDER_UNAVAILABLE.

        The one caller (`cancel_subscription`) needs no response body, only that the resource is
        gone — so a 2xx and a 404 (already gone) are both success, and every other status is a
        provider failure the caller fails closed on. Like `_post`, the detail carries only the
        HTTP status and a fixed sentence, never Stripe's own message (§54).
        """
        if not self._settings.secret_key:
            raise _provider_unavailable(
                "no Stripe secret key is configured; the billing provider cannot be called")
        url = self._settings.api_base_url.rstrip("/") + path
        try:
            response = await self._client.request(
                "DELETE", url,
                headers={"Authorization": f"Bearer {self._settings.secret_key}"})
        except httpx.HTTPError as error:
            raise _provider_unavailable(
                f"the billing provider could not be reached ({type(error).__name__})") from error
        if response.status_code == 404:
            return
        if response.status_code >= 400:
            raise _provider_unavailable(
                f"the billing provider answered with status {response.status_code}")


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    """The header's value, matched case-insensitively — HTTP header names are not case-sensitive."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _parse_signature_header(header: str) -> tuple[str, int, list[str]]:
    """Split a `Stripe-Signature` header into its timestamp and its `v1` signatures.

    The header is `t=<unix>,v1=<hex>,...` and may repeat `v1` when the endpoint has more than one
    active secret. Returns the raw timestamp string (what the signed payload is prefixed with),
    the timestamp as an int (for the freshness check), and every `v1` signature. Raises
    `WEBHOOK_SIGNATURE_INVALID` when the timestamp is missing or non-integer, or no `v1` is present.
    """
    raw_timestamp: str | None = None
    signatures: list[str] = []
    for item in header.split(","):
        key, _, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if key == "t":
            raw_timestamp = value
        elif key == "v1":
            signatures.append(value)
    if raw_timestamp is None or not signatures:
        raise _reject_signature("the Stripe-Signature header is missing a timestamp or signature")
    try:
        timestamp = int(raw_timestamp)
    except ValueError as error:
        raise _reject_signature("the Stripe-Signature timestamp is not an integer") from error
    return raw_timestamp, timestamp, signatures


def _normalize_event(payload: bytes) -> NormalizedWebhookEvent:
    """Parse a verified payload into a `NormalizedWebhookEvent`, or raise WEBHOOK_MALFORMED.

    Runs only after the signature checks out. An event type outside the subscription set
    normalizes with `subscription=None` — a well-formed event the platform does not act on, which
    the service records as IGNORED — so a redelivery of it is still a recognised no-op.
    """
    try:
        parsed = json.loads(payload)
    except ValueError as error:
        raise _reject_malformed("the webhook body is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise _reject_malformed("the webhook body is not a JSON object")
    event_id = _require_str_field(parsed, "id")
    event_type = _require_str_field(parsed, "type")
    event_at = _to_datetime(parsed.get("created"))
    if event_at is None:
        raise _reject_malformed("the webhook is missing a numeric 'created' timestamp")
    if event_type not in _SUBSCRIPTION_EVENT_TYPES:
        return NormalizedWebhookEvent(
            provider=STRIPE_PROVIDER_KEY, external_event_id=event_id,
            event_type=event_type, event_at=event_at)
    obj = _event_object(parsed)
    state, client_user_id = _subscription_state(obj, event_type)
    return NormalizedWebhookEvent(
        provider=STRIPE_PROVIDER_KEY, external_event_id=event_id, event_type=event_type,
        event_at=event_at, subscription=state, client_user_id=client_user_id)


def _event_object(parsed: dict[str, Any]) -> dict[str, Any]:
    data = parsed.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    if not isinstance(obj, dict):
        raise _reject_malformed("the webhook carries no data.object")
    return obj


def _subscription_state(obj: dict[str, Any],
                        event_type: str) -> tuple[ProviderSubscriptionState, UserId | None]:
    """Map a Stripe subscription object onto the domain's vocabulary (§14).

    A `deleted` event is `CANCELED` regardless of the object's own status; otherwise the status is
    mapped through the closed table (an unknown one is malformed), and an `active` subscription
    flagged to cancel at period end is normalized to `CANCEL_AT_PERIOD_END` so the domain's window
    rule holds. The window is both-or-neither: a partial one degrades to unbounded (the calendar
    month) rather than building a state `Subscription`'s validator would reject.
    """
    external_subscription_id = _require_str_field(obj, "id")
    customer = obj.get("customer")
    cancel_flag = bool(obj.get("cancel_at_period_end", False))
    if event_type == "customer.subscription.deleted":
        status = SubscriptionStatus.CANCELED
    else:
        status = _status_for(obj)
        if status is SubscriptionStatus.ACTIVE and cancel_flag:
            status = SubscriptionStatus.CANCEL_AT_PERIOD_END
    start, end = _window(obj)
    state = ProviderSubscriptionState(
        external_subscription_id=external_subscription_id,
        status=status,
        external_customer_id=customer if isinstance(customer, str) else None,
        plan_external_price_id=_price_id(obj),
        current_period_start=start,
        current_period_end=end,
        cancel_at_period_end=cancel_flag)
    return state, _client_user_id(obj)


def _status_for(obj: dict[str, Any]) -> SubscriptionStatus:
    raw = obj.get("status")
    if not isinstance(raw, str):
        raise _reject_malformed("the subscription carries no status")
    mapped = _STATUS_BY_STRIPE.get(raw)
    if mapped is None:
        raise _reject_malformed(f"the subscription status {raw!r} is outside the known set")
    return mapped


def _window(obj: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    start = _to_datetime(obj.get("current_period_start"))
    end = _to_datetime(obj.get("current_period_end"))
    if start is None or end is None:
        item = _first_item(obj)
        if item is not None:
            start = start or _to_datetime(item.get("current_period_start"))
            end = end or _to_datetime(item.get("current_period_end"))
    if start is None or end is None:
        return None, None
    return start, end


def _first_item(obj: dict[str, Any]) -> dict[str, Any] | None:
    items = obj.get("items")
    data = items.get("data") if isinstance(items, dict) else None
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    return first if isinstance(first, dict) else None


def _price_id(obj: dict[str, Any]) -> str | None:
    item = _first_item(obj)
    price = item.get("price") if item is not None else None
    if isinstance(price, dict):
        pid = price.get("id")
        return pid if isinstance(pid, str) else None
    return price if isinstance(price, str) else None


def _client_user_id(obj: dict[str, Any]) -> UserId | None:
    metadata = obj.get("metadata")
    raw = metadata.get(_CLIENT_USER_ID_METADATA_KEY) if isinstance(metadata, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        return UserId(UUID(raw))
    except ValueError:
        return None


def _to_datetime(value: object) -> datetime | None:
    """A Unix-seconds number as a UTC datetime, or `None` — `bool` is rejected (it is an int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def _require_str_field(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise _reject_malformed(f"the webhook is missing a string {key!r}")
    return value


def _require_str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise _provider_unavailable(f"the billing provider response is missing {key!r}")
    return value


