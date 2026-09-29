"""The server-authoritative plan catalogue and its idempotent seed (§2-4, §17, §61).

What the platform *sells*, defined in code so it is one source of truth a deployment writes into
the `plans` table rather than something an operator hand-edits row by row — and something the
frontend only ever *reads*, never defines (§17, §61). Three tiers:

- `free` — the tier every account falls back to (no subscription, or a lapsed one, §16). It costs
  nothing, so it carries no price, currency or interval; its ceilings are deliberately modest but
  usable, enough to try every capability once or twice per month.
- `pro` — the paid monthly tier, raising every ceiling well above the free tier's.
- `scale` — the top monthly tier, leaving the heavy meters *unlimited* (`limit=None`, a real
  granted value distinct from a large number) and keeping only a generous concurrent-search cap.

What a paid tier *costs* is **not** baked into the catalogue as production truth (§17, §61).
A `_PlanDefinition` fixes a tier's identity, its `billing_interval` (that it is monthly-recurring)
and the ceilings it grants — never an amount. Both the price (`prices`) and the provider-side
`external_price_id` a checkout needs are supplied at seed time by the deployment, so the same
catalogue definition serves every deployment, a fixture never carries a real provider id, and no
approved product price is hard-coded here. A tier seeded with no price is coherent but not
sellable (`is_free` is true and there is no `external_price_id`), which is exactly what an
operator who has not yet configured pricing should get — never an invented amount.
`DEMO_PLAN_PRICES` is the one explicitly-labelled non-production price set, for fixtures, local
runs and `--demo`.

`seed_plan_catalogue` is idempotent by construction: `plan_id` derives from the slug, so an
`upsert` lands on the one `free`/`pro`/`scale` row and reconciles its entitlements rather than
minting a second tier. A re-seed preserves each plan's original `created_at` (read back before
writing) and advances `updated_at` to the reconciliation instant — the catalogue records when it
was last written, and the row's identity never moves.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from backend.app.domain.entitlement import (
    BillingInterval,
    Entitlement,
    EntitlementKey,
    Plan,
)
from backend.app.domain.identifiers import plan_id
from backend.app.repositories.contracts import PlanRepository

# The stable business key of the free tier — the slug the entitlement resolver falls back to and
# the seed writes. Named once here so the resolver and the catalogue agree on one spelling.
FREE_PLAN_SLUG = "free"


@dataclass(frozen=True)
class PlanPrice:
    """A deployment-supplied price for one paid tier: an amount and its currency.

    `amount_cents` is in the smallest currency unit (e.g. Rappen for CHF), the shape
    `Plan.price_amount_cents` holds and a provider expects. Kept out of `_PlanDefinition` on
    purpose: what a tier *costs* is a deployment decision (or an explicitly-labelled demo value),
    never a constant baked into the catalogue as production truth (§17, §61). The server stays
    authoritative — a client only ever renders the price the seed wrote.
    """

    amount_cents: int
    currency: str


@dataclass(frozen=True)
class _PlanDefinition:
    """One tier's product truth: its identity, its billing cadence and the ceilings it grants.

    A plain application-layer value, turned into a domain `Plan` by `_build_plan`. It carries no
    price amount — that is supplied at seed time (`prices`) — only `billing_interval`, which is
    product structure (a monthly-recurring tier) rather than an approved amount. `entitlements`
    is a `(key, limit)` mapping where `None` is unlimited — every key a tier grants appears once,
    and a key it omits is simply not granted (the resolver treats that absence as no allowance).
    """

    slug: str
    name: str
    description: str
    billing_interval: BillingInterval | None
    entitlements: Mapping[EntitlementKey, int | None]


# The catalogue, cheapest first. A tier carries no price amount here — only its billing cadence
# (a paid tier is monthly-recurring) and the ceilings it grants. `scale` leaves its heavy meters
# unlimited. Prices are applied at seed time from a deployment's config, never baked in.
_PLAN_DEFINITIONS: tuple[_PlanDefinition, ...] = (
    _PlanDefinition(
        slug=FREE_PLAN_SLUG, name="Free", billing_interval=None,
        description="Try every capability with modest monthly ceilings.",
        entitlements={
            EntitlementKey.ACTIVE_SEARCH_PROFILES: 1,
            EntitlementKey.LLM_TOKENS: 50_000,
            EntitlementKey.DOCUMENT_GENERATIONS: 3,
            EntitlementKey.APPLICATION_SUBMISSIONS: 5,
            EntitlementKey.INTERVIEW_SESSIONS: 1,
            EntitlementKey.RECOMMENDATION_GENERATIONS: 3,
        }),
    _PlanDefinition(
        slug="pro", name="Pro", billing_interval=BillingInterval.MONTHLY,
        description="Higher monthly ceilings for an active search.",
        entitlements={
            EntitlementKey.ACTIVE_SEARCH_PROFILES: 10,
            EntitlementKey.LLM_TOKENS: 2_000_000,
            EntitlementKey.DOCUMENT_GENERATIONS: 100,
            EntitlementKey.APPLICATION_SUBMISSIONS: 200,
            EntitlementKey.INTERVIEW_SESSIONS: 50,
            EntitlementKey.RECOMMENDATION_GENERATIONS: 100,
        }),
    _PlanDefinition(
        slug="scale", name="Scale", billing_interval=BillingInterval.MONTHLY,
        description="Unlimited generation and submission for a full-time search.",
        entitlements={
            EntitlementKey.ACTIVE_SEARCH_PROFILES: 100,
            EntitlementKey.LLM_TOKENS: None,
            EntitlementKey.DOCUMENT_GENERATIONS: None,
            EntitlementKey.APPLICATION_SUBMISSIONS: None,
            EntitlementKey.INTERVIEW_SESSIONS: None,
            EntitlementKey.RECOMMENDATION_GENERATIONS: None,
        }),
)


# Non-production demo pricing. These CHF amounts are **not** approved product pricing (§17): they
# exist only so fixtures, local runs and the `--demo` seed have a concrete, clearly-labelled price
# to render. A real deployment supplies its own prices at seed time (see
# `backend/app/cli/seed_plans.py`); this constant is never the source of a production catalogue.
DEMO_PLAN_PRICES: Mapping[str, PlanPrice] = {
    "pro": PlanPrice(amount_cents=1900, currency="CHF"),
    "scale": PlanPrice(amount_cents=4900, currency="CHF"),
}


def _build_plan(definition: _PlanDefinition, *, external_price_id: str | None,
                price: PlanPrice | None, created_at: datetime, updated_at: datetime) -> Plan:
    """One `_PlanDefinition` into a domain `Plan`, with the deployment's price and handle applied.

    The id derives from the slug (so the write is an idempotent upsert). The amount and currency
    ride along only when a `PlanPrice` is supplied for this tier — a tier with no configured price
    is seeded unpriced (both `None`) but keeps its `billing_interval`, which the domain accepts as
    a coherent, simply-not-yet-sellable plan. The entitlements become value objects sorted by key
    so the seeded order is stable.
    """
    price_amount_cents = price.amount_cents if price is not None else None
    currency = price.currency if price is not None else None
    entitlements = tuple(
        Entitlement(key=key, limit=definition.entitlements[key])
        for key in sorted(definition.entitlements, key=lambda k: k.value))
    return Plan(
        id=plan_id(definition.slug), slug=definition.slug, name=definition.name,
        description=definition.description, price_amount_cents=price_amount_cents,
        currency=currency, billing_interval=definition.billing_interval,
        external_price_id=external_price_id, entitlements=entitlements,
        is_public=True, is_active=True, created_at=created_at, updated_at=updated_at)


async def seed_plan_catalogue(
        plans: PlanRepository, *, now: datetime,
        external_price_ids: Mapping[str, str] | None = None,
        prices: Mapping[str, PlanPrice] | None = None) -> tuple[Plan, ...]:
    """Write (or reconcile) the `free`/`pro`/`scale` catalogue, idempotently (§2, §17).

    Called at deployment time by `backend.app.cli.seed_plans`, never implicitly on startup.
    `prices` maps a slug to the amount+currency a deployment (or the demo set) assigns that tier;
    a tier absent from it is seeded unpriced — coherent, but not sellable until priced, never an
    invented amount. `external_price_ids` maps a slug to the provider-side price handle a checkout
    for that tier needs; a tier absent from it is seeded with no handle, so a checkout is simply
    not offered for it. Each plan keeps its original `created_at` on a re-seed (read back first)
    and advances `updated_at` to `now`, and the returned tuple is what is now stored, so a caller
    sees the reconciled catalogue.
    """
    price_ids = external_price_ids or {}
    price_by_slug = prices or {}
    written: list[Plan] = []
    for definition in _PLAN_DEFINITIONS:
        existing = await plans.get_by_slug(definition.slug)
        created_at = existing.created_at if existing is not None else now
        plan = _build_plan(
            definition, external_price_id=price_ids.get(definition.slug),
            price=price_by_slug.get(definition.slug), created_at=created_at, updated_at=now)
        written.append(await plans.upsert(plan))
    return tuple(written)
