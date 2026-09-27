"""phase 16 saas subscriptions, metering and the commercial spine

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-27 12:30:00.000000

Phase 16 turns the platform into a SaaS and gives its commercial pieces a home, without
handing any of them authority over the domain's safety rules. `plans` is the
server-authoritative catalogue of what the platform sells — shared, not user-owned, so two
accounts on the same tier reference one row — and `plan_entitlements` is its child collection
of capability ceilings, one `Entitlement` value object per row, matched by `(plan_id, key)`.
`subscriptions` is one account's normalized, webhook-authoritative relationship with a billing
provider; losing it narrows an account's quotas and never deletes data or touches an
`ApplicationPolicy`. `usage_events` is the append-only, idempotent metering ledger — the
authoritative-usage-event end of the spine `commercial entitlement -> server-side quota check
-> existing domain service -> authoritative usage event`, whose per-period sum a quota check
reads under an advisory lock.

The CHECKs are domain validators made physical, so a row written by a migration or by psql
cannot assert what the domain layer could never have produced: `price_all_or_nothing` and
`paid_carries_an_interval` restate `Plan`'s price validator (a price needs a currency, a *paid*
price needs a renewal interval, a free plan sets neither), `window_both_or_neither` /
`window_runs_forward` / `cancel_at_period_end_carries_the_flag` restate `Subscription`'s window
validator, and `quantity_positive` restates that a usage event exists only for real, positive,
measured consumption — an unmeasurable call writes no event rather than a fabricated `0`.

Edited after `alembic revision --autogenerate` the way revisions 0002-0016 document: no
application imports for column types or enum members, the repetition factored into the helpers
below, and the enum CHECKs created and dropped implicitly with their tables. Every
convention-generated foreign-key name here is under PostgreSQL's 63-character identifier limit,
so none is named explicitly. The schema-drift test compares the result against `Base.metadata`,
which is what keeps that a claim. Additive only: no existing table is touched.
"""
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_INTEGER = sa.Integer()
_BOOLEAN = sa.Boolean()
_CURRENCY = sa.String(length=3)

# The internal free-tier provider sentinel, restated from
# `backend.app.domain.subscription.INTERNAL_BILLING_PROVIDER`. Written out rather than
# imported, the way this file avoids application imports; the column default and the drift
# test keep the two agreeing.
_INTERNAL_BILLING_PROVIDER = "internal"

# CHECK_MEMBERS_PLACEHOLDER

# The members of the enums this revision introduces, written out. Each is a `StrEnum` in
# `backend.app.domain.{entitlement,subscription,usage}`; the CHECK behind the column keeps the
# two lists agreeing, and the drift test keeps this file and `models.py` agreeing.
_BILLING_INTERVALS = ("MONTHLY", "YEARLY")
_ENTITLEMENT_KEYS = ("ACTIVE_SEARCH_PROFILES", "LLM_TOKENS", "DOCUMENT_GENERATIONS",
                     "APPLICATION_SUBMISSIONS", "INTERVIEW_SESSIONS",
                     "RECOMMENDATION_GENERATIONS")
_SUBSCRIPTION_STATUSES = ("TRIALING", "ACTIVE", "PAST_DUE", "CANCEL_AT_PERIOD_END",
                          "CANCELED")
_USAGE_SOURCE_TYPES = ("LLM_RUN", "DOCUMENT_VERSION", "APPLICATION_SUBMISSION",
                       "INTERVIEW_SESSION", "CAREER_RECOMMENDATION")

# Verbatim copies of the CHECK expressions in `models.py`, so the drift test — and a reader —
# see one expression on both sides of each invariant.
_PLAN_PRICE_ALL_OR_NOTHING = (
    "(price_amount_cents IS NULL AND currency IS NULL)"
    " OR (price_amount_cents IS NOT NULL AND currency IS NOT NULL)")
_PLAN_PAID_CARRIES_AN_INTERVAL = (
    "price_amount_cents IS NULL OR price_amount_cents = 0 OR billing_interval IS NOT NULL")
_SUBSCRIPTION_WINDOW_BOTH_OR_NEITHER = (
    "(current_period_start IS NULL AND current_period_end IS NULL)"
    " OR (current_period_start IS NOT NULL AND current_period_end IS NOT NULL)")


def _enum(name: str, *members: str, length: int = 32) -> sa.Enum:
    """A `VARCHAR(length)` plus a CHECK on the permitted values, as revision 0002 gives."""
    return sa.Enum(*members, name=name, native_enum=False, create_constraint=True,
                   length=length)


def _timestamps() -> tuple[sa.Column[Any], ...]:
    """`created_at` and `updated_at`, as `TimestampedMixin` declares them."""
    return (
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
    )


def upgrade() -> None:
    """Create the Phase 16 SaaS schema, parents before children.

    `plans` first (the catalogue), then `plan_entitlements` (its children) and `subscriptions`
    (which reference both `plans` and the pre-existing `users`), then `usage_events`.
    """
    op.create_table(
        "plans",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("slug", _TEXT, nullable=False),
        sa.Column("name", _TEXT, nullable=False),
        sa.Column("description", _TEXT, nullable=True),
        sa.Column("price_amount_cents", _INTEGER, nullable=True),
        sa.Column("currency", _CURRENCY, nullable=True),
        sa.Column("billing_interval", _enum("billing_interval", *_BILLING_INTERVALS),
                  nullable=True),
        sa.Column("external_price_id", _TEXT, nullable=True),
        sa.Column("is_public", _BOOLEAN, server_default=sa.text("true"), nullable=False),
        sa.Column("is_active", _BOOLEAN, server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("price_amount_cents IS NULL OR price_amount_cents >= 0",
                           name=op.f("ck_plans_price_amount_cents_non_negative")),
        sa.CheckConstraint(_PLAN_PRICE_ALL_OR_NOTHING,
                           name=op.f("ck_plans_price_all_or_nothing")),
        sa.CheckConstraint(_PLAN_PAID_CARRIES_AN_INTERVAL,
                           name=op.f("ck_plans_paid_carries_an_interval")),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'",
                           name=op.f("ck_plans_currency_format")),
        sa.CheckConstraint("updated_at >= created_at",
                           name=op.f("ck_plans_updated_at_after_created_at")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plans")),
        sa.UniqueConstraint("slug", name=op.f("uq_plans_slug")),
    )

    op.create_table(
        "plan_entitlements",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("plan_id", _UUID, nullable=False),
        sa.Column("key", _enum("entitlement_key", *_ENTITLEMENT_KEYS), nullable=False),
        sa.Column("entitlement_limit", _INTEGER, nullable=True),
        *_timestamps(),
        sa.CheckConstraint("entitlement_limit IS NULL OR entitlement_limit >= 0",
                           name=op.f("ck_plan_entitlements_entitlement_limit_non_negative")),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"],
            name=op.f("fk_plan_entitlements_plan_id_plans"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_entitlements")),
        sa.UniqueConstraint("plan_id", "key",
                            name=op.f("uq_plan_entitlements_plan_id_key")),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("plan_id", _UUID, nullable=False),
        sa.Column("status", _enum("subscription_status", *_SUBSCRIPTION_STATUSES),
                  nullable=False),
        sa.Column("provider", _TEXT,
                  server_default=sa.text(f"'{_INTERNAL_BILLING_PROVIDER}'"),
                  nullable=False),
        sa.Column("external_customer_id", _TEXT, nullable=True),
        sa.Column("external_subscription_id", _TEXT, nullable=True),
        sa.Column("current_period_start", _TIMESTAMPTZ, nullable=True),
        sa.Column("current_period_end", _TIMESTAMPTZ, nullable=True),
        sa.Column("cancel_at_period_end", _BOOLEAN, server_default=sa.text("false"),
                  nullable=False),
        sa.Column("provider_event_at", _TIMESTAMPTZ, nullable=True),
        sa.Column("provider_event_sequence", _INTEGER, nullable=True),
        *_timestamps(),
        sa.CheckConstraint(_SUBSCRIPTION_WINDOW_BOTH_OR_NEITHER,
                           name=op.f("ck_subscriptions_window_both_or_neither")),
        sa.CheckConstraint(
            "current_period_start IS NULL OR current_period_end > current_period_start",
            name=op.f("ck_subscriptions_window_runs_forward")),
        sa.CheckConstraint(
            "status <> 'CANCEL_AT_PERIOD_END' OR cancel_at_period_end",
            name=op.f("ck_subscriptions_cancel_at_period_end_carries_the_flag")),
        sa.CheckConstraint(
            "provider_event_sequence IS NULL OR provider_event_sequence >= 0",
            name=op.f("ck_subscriptions_provider_event_sequence_non_negative")),
        sa.CheckConstraint("updated_at >= created_at",
                           name=op.f("ck_subscriptions_updated_at_after_created_at")),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"],
            name=op.f("fk_subscriptions_plan_id_plans"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_subscriptions_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
        sa.UniqueConstraint(
            "provider", "external_subscription_id",
            name=op.f("uq_subscriptions_provider_external_subscription_id")),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"],
                    unique=False)
    op.create_index("ix_subscriptions_plan_id", "subscriptions", ["plan_id"],
                    unique=False)

    op.create_table(
        "usage_events",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("entitlement_key", _enum("entitlement_key", *_ENTITLEMENT_KEYS),
                  nullable=False),
        sa.Column("quantity", _INTEGER, nullable=False),
        sa.Column("source_type", _enum("usage_source_type", *_USAGE_SOURCE_TYPES),
                  nullable=False),
        sa.Column("source_id", _TEXT, nullable=False),
        sa.Column("occurred_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("billing_period", _TEXT, nullable=False),
        sa.Column("idempotency_key", _TEXT, nullable=False),
        sa.Column("detail", _TEXT, nullable=True),
        *_timestamps(),
        sa.CheckConstraint("quantity >= 1", name=op.f("ck_usage_events_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_usage_events_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_events")),
        sa.UniqueConstraint("idempotency_key",
                            name=op.f("uq_usage_events_idempotency_key")),
    )
    op.create_index("ix_usage_events_user_id_entitlement_key_billing_period",
                    "usage_events", ["user_id", "entitlement_key", "billing_period"],
                    unique=False)


def downgrade() -> None:
    """Return the schema to revision 0016, children before parents.

    Destructive: every plan, entitlement, subscription and metered usage event is deleted.
    Explicit indexes are dropped explicitly; the enum CHECKs, unique constraints and foreign
    keys go with their tables.
    """
    op.drop_index("ix_usage_events_user_id_entitlement_key_billing_period",
                  table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_index("ix_subscriptions_plan_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("plan_entitlements")
    op.drop_table("plans")
