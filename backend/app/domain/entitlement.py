"""`Entitlement` and `Plan` — the commercial permission layer, and its one hard limit (§2-4).

Phase 16's spine is `commercial entitlement → server-side quota check → existing domain
service → authoritative usage event`, and this module is the *entitlement* end of it. A
`Plan` is a server-authoritative bundle of `Entitlement`s — how many active search profiles,
how many LLM tokens a period, how many document generations — and nothing more. It is the
answer to "what did this account pay for", never "what is this account allowed to do": the
effective permission for any action is `domain permission AND user policy AND commercial
entitlement`, and this layer can only ever *restrict* that conjunction, never widen it.

Four rules make that safe, and the models enforce every one:

- **Only real capabilities are metered (§3).** `EntitlementKey` is closed to the six things
  the platform can actually count — active searches, LLM tokens, document generations,
  application submissions, interview sessions, recommendation generations. A plan cannot grant
  an entitlement to a capability that does not exist, and a provider cannot invent a seventh.
- **An entitlement restricts, it never expands a safety brake (§4).** A limit caps *how much*
  of a capability an account may use; it says nothing about *whether* an action is safe. A paid
  plan may raise the submission quota from 5 to 500, but the `ApplicationPolicy`, the
  eligibility gates, the human-approval brake and the truth/evidence guards still decide every
  single submission. `quota_permits` answers "is there commercial room", one clause of the AND,
  never the whole verdict.
- **`None` is unlimited, a real and deliberate value.** A plan may grant an uncapped
  entitlement (`limit=None`); it is distinct from `0` ("none of this capability") and from an
  absent entitlement ("this plan does not grant this capability at all", which the resolver
  treats as the most restrictive: no allowance).
- **A gauge is not a meter (§6).** `ACTIVE_SEARCH_PROFILES` is a *concurrent* limit — a ceiling
  on how many search profiles may be active at once, checked against a live count — while the
  other five are *per-period* meters checked against a usage sum in the billing window. The
  measure is intrinsic to the key, kept in one table so the metering service and the quota
  check never disagree about which shape a key is.

Pure domain values: `backend.app.domain` imports the standard library and Pydantic only
(docs/ARCHITECTURE.md §1). The frontend defines none of this — prices, quotas and entitlements
are server truth, and a client only ever reads them (§17, §61).
"""
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from backend.app.domain.base import CurrencyCode, DomainModel, NonEmptyStr, UtcDatetime
from backend.app.domain.identifiers import PlanId


class EntitlementKey(StrEnum):
    """The closed set of capabilities a plan may meter — only things the platform counts (§3).

    Every member names a capability that already exists as a real, server-side action, so an
    entitlement is always something the platform can actually measure and enforce, never a
    marketing promise with no meter behind it. A key a provider or a client invents fails to
    parse rather than silently granting an allowance the metering layer never tracks, exactly
    as every other closed vocabulary in the domain.

    - `ACTIVE_SEARCH_PROFILES` — how many search profiles may be *active at once* (a gauge, §6);
    - `LLM_TOKENS` — total LLM tokens per billing period, summed from the Phase 11 `LLMRun`
      telemetry the platform already records — never a second token counter (§5);
    - `DOCUMENT_GENERATIONS` — ATS résumé/cover-letter generations per period;
    - `APPLICATION_SUBMISSIONS` — real application submissions per period (a *commercial* cap
      layered above the `ApplicationPolicy` volume brake, never replacing it, §4);
    - `INTERVIEW_SESSIONS` — adaptive interview-practice sessions per period;
    - `RECOMMENDATION_GENERATIONS` — career-recommendation generation runs per period.
    """

    ACTIVE_SEARCH_PROFILES = "ACTIVE_SEARCH_PROFILES"
    LLM_TOKENS = "LLM_TOKENS"
    DOCUMENT_GENERATIONS = "DOCUMENT_GENERATIONS"
    APPLICATION_SUBMISSIONS = "APPLICATION_SUBMISSIONS"
    INTERVIEW_SESSIONS = "INTERVIEW_SESSIONS"
    RECOMMENDATION_GENERATIONS = "RECOMMENDATION_GENERATIONS"


class EntitlementMeasure(StrEnum):
    """Whether a key is a live gauge or a per-period meter — the shape its quota is checked as (§6).

    Two shapes, because "how many search profiles are active *right now*" and "how many tokens
    have I spent *this month*" are different questions with different enforcement. A `CONCURRENT`
    limit is checked against a count of currently-active resources — creating a resource that
    would exceed it is refused, deleting one frees room again. A `PER_PERIOD` limit is checked
    against the sum of usage events in the current billing window — it resets each period and is
    never freed within one. The metering service and the quota check both read the measure from
    `entitlement_measure` so they can never disagree about which question a key asks.
    """

    CONCURRENT = "CONCURRENT"
    PER_PERIOD = "PER_PERIOD"


# The intrinsic measure of each entitlement key. Kept here, once, so the metering service (which
# decides whether to sum a period or count live resources) and the quota check (which decides
# what "already used" means) read one table rather than each hard-coding the split. Every key is
# present, so `entitlement_measure` is total and a key added to the enum without a measure fails
# a test rather than defaulting silently.
_ENTITLEMENT_MEASURE: dict[EntitlementKey, EntitlementMeasure] = {
    EntitlementKey.ACTIVE_SEARCH_PROFILES: EntitlementMeasure.CONCURRENT,
    EntitlementKey.LLM_TOKENS: EntitlementMeasure.PER_PERIOD,
    EntitlementKey.DOCUMENT_GENERATIONS: EntitlementMeasure.PER_PERIOD,
    EntitlementKey.APPLICATION_SUBMISSIONS: EntitlementMeasure.PER_PERIOD,
    EntitlementKey.INTERVIEW_SESSIONS: EntitlementMeasure.PER_PERIOD,
    EntitlementKey.RECOMMENDATION_GENERATIONS: EntitlementMeasure.PER_PERIOD,
}


def entitlement_measure(key: EntitlementKey) -> EntitlementMeasure:
    """Whether `key` is a live gauge (`CONCURRENT`) or a per-period meter (`PER_PERIOD`) (§6)."""
    return _ENTITLEMENT_MEASURE[key]


class BillingInterval(StrEnum):
    """How often a paid plan renews — the period a per-period meter resets on (§10).

    Closed and coarse on purpose: a plan bills `MONTHLY` or `YEARLY`, and a free plan bills
    on neither (its period is the calendar month the metering layer falls back to). The value
    is provenance the billing adapter maps onto a provider's own interval; nothing in the
    domain dispatches on it beyond describing the plan.
    """

    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"


class Entitlement(DomainModel):
    """One capability a plan grants, and the ceiling it grants it up to (§2-4).

    A value object embedded in a `Plan`, never a row of its own: it is *what the plan is*, and
    it carries exactly two facts — which `key` (which capability) and the `limit` (how much).
    `limit=None` is unlimited, a real value a top plan legitimately grants; a finite `limit` is
    the count or token budget the quota check holds usage against. The measure the limit is
    checked as — a live gauge or a per-period sum — is intrinsic to the key, read via
    `entitlement_measure`, so an entitlement never has to restate it and two entitlements for
    the same key can never disagree about their shape.

    `quota_permits` is the entitlement's whole enforcement surface: given how much is already
    used, does `quantity` more fit. It is one clause of the effective-permission AND (§4) — it
    answers commercial room and nothing about safety — so a service still runs every domain gate
    after it, never instead of it.
    """

    key: EntitlementKey
    limit: Annotated[int, Field(ge=0)] | None = None

    @property
    def measure(self) -> EntitlementMeasure:
        """Whether this entitlement is a live gauge or a per-period meter (derived from `key`)."""
        return entitlement_measure(self.key)

    @property
    def is_unlimited(self) -> bool:
        """Whether the plan grants this capability without a ceiling (`limit=None`)."""
        return self.limit is None

    def remaining(self, already_used: int) -> int | None:
        """How much of this entitlement is left after `already_used`, or `None` if unlimited.

        Pure arithmetic — the caller owns the counting, because a domain object that queried a
        usage sum could not be tested deterministically. Never negative: an account already over
        its ceiling (a limit lowered beneath prior usage) reports `0` room, not a negative one.
        """
        if self.limit is None:
            return None
        return max(0, self.limit - already_used)

    def quota_permits(self, *, already_used: int, quantity: int) -> bool:
        """Whether `quantity` more units fit under this entitlement given `already_used` (§8).

        The commercial half of a quota decision, and only that half: `True` means there is
        room the plan paid for, never that the action is safe or allowed — the domain policy,
        eligibility and approval gates are separate clauses the caller still evaluates. An
        unlimited entitlement always permits. A non-positive `quantity` is a no-op that always
        fits, so a metered action that turns out to consume nothing is never refused for it.
        """
        if quantity <= 0:
            return True
        if self.limit is None:
            return True
        return already_used + quantity <= self.limit


class Plan(DomainModel):
    """One server-authoritative commercial plan — a named bundle of entitlements (§2-4, §17).

    The catalogue entity a `Subscription` points at, and the single source of truth for what an
    account paid for. It carries its stable `slug` (the business key `plan_id` derives its id
    from, so re-seeding is idempotent), a display `name`, its price (`price_amount_cents` with a
    `currency` and a `billing_interval`, all absent together for a free plan), the opaque
    `external_price_id` the configured billing provider needs to open a checkout (provider-
    neutral in shape — the adapter interprets it, the domain never does), and the `entitlements`
    it grants. `is_public` gates whether a plan is offered on the pricing surface; `is_active`
    whether it may still be subscribed to — a retired plan stays in the catalogue so existing
    subscriptions keep resolving, but no new checkout may pick it.

    Two invariants keep it honest: a plan grants each key at most once (two entitlements for the
    same capability would let the quota check pick between disagreeing ceilings), and price is
    all-or-nothing (an amount without a currency, or a paid plan with no interval, is a
    half-specified price a checkout could not act on). The frontend defines none of this; it
    reads the catalogue the backend serves (§61).
    """

    id: PlanId
    slug: NonEmptyStr
    name: NonEmptyStr
    description: NonEmptyStr | None = None
    price_amount_cents: Annotated[int, Field(ge=0)] | None = None
    currency: CurrencyCode | None = None
    billing_interval: BillingInterval | None = None
    external_price_id: NonEmptyStr | None = None
    entitlements: tuple[Entitlement, ...] = ()
    is_public: bool = True
    is_active: bool = True
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @field_validator("entitlements", mode="after")
    @classmethod
    def _entitlements_in_canonical_key_order(
            cls, entitlements: tuple[Entitlement, ...]) -> tuple[Entitlement, ...]:
        """Order the entitlements by key so a plan is a keyed bundle, not an ordered list.

        A plan grants each key at most once (the coherence validator below enforces it), so the
        order they arrive in carries no meaning — two plans granting the same ceilings are the same
        plan. Sorting on the key gives every `Plan` one canonical representation, so equality never
        turns on construction order and a catalogue read round-trips to a value equal to the one
        written: `PlanRow.entitlements` is itself `order_by` its key, and without a matching
        canonical order here that read would only equal the written plan while the pre-`order_by`
        in-memory collection happened to survive in the identity map.
        """
        return tuple(sorted(entitlements, key=lambda entitlement: entitlement.key.value))

    @model_validator(mode="after")
    def _entitlements_and_price_are_coherent(self) -> Self:
        keys = [entitlement.key for entitlement in self.entitlements]
        if len(keys) != len(set(keys)):
            raise ValueError("a Plan must not grant the same EntitlementKey twice")
        priced = self.price_amount_cents is not None
        if priced != (self.currency is not None):
            raise ValueError(
                "a Plan's price is all-or-nothing: price_amount_cents and currency are set "
                "together (a free plan sets neither)")
        if priced and self.price_amount_cents and self.billing_interval is None:
            raise ValueError("a paid Plan must carry a billing_interval")
        if self.updated_at < self.created_at:
            raise ValueError("Plan updated_at must not precede created_at")
        return self

    @property
    def is_free(self) -> bool:
        """Whether this plan costs nothing — no price, or an explicit zero amount."""
        return not self.price_amount_cents

    def entitlement_for(self, key: EntitlementKey) -> Entitlement | None:
        """The entitlement this plan grants for `key`, or `None` if it grants none.

        A `None` is the most restrictive answer, not the most permissive: a plan that grants no
        entitlement for a capability grants *no* allowance of it, so the resolver treats the
        absence as a zero ceiling rather than as "unlimited". Distinguishing "granted, unlimited"
        (`Entitlement(limit=None)`) from "not granted" (`None` here) is exactly why an unlimited
        entitlement is a real value rather than the absence of one.
        """
        for entitlement in self.entitlements:
            if entitlement.key is key:
                return entitlement
        return None
