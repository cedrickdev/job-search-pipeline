"""phase 16 subscription-event ledger: idempotent, auditable billing webhooks

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-27 15:00:00.000000

Phase 16's billing webhook path needs a place to record what it did with each verified event, and
that record is the whole of its idempotency and its audit. `subscription_events` is that ledger:
one row per processed webhook, its id derived from the provider's own event id, so a redelivery
composes the same primary key and is recognised as already-seen rather than applied twice.
`UNIQUE (provider, external_event_id)` is the second half of that guard, catching a row whose id
was written by hand.

The ledger is keyed for recognition by *event*, not by owner — a webhook arrives with no session —
so `user_id` and `subscription_id` are nullable: an event the platform cannot attribute (an
unmapped price on a new subscription, a customer it does not know) is recorded as `IGNORED` rather
than dropped, so its redelivery stays a recognised no-op. `user_id` is `ON DELETE SET NULL` so a
closed account's audit trail survives it rather than cascading away, and `subscription_id` is a
plain column, not a foreign key, so an audit fact never depends on the row it describes still
existing. `outcome` is the closed `{APPLIED, SUPERSEDED, IGNORED}` set the service writes.

Append-only, so — like `usage_events` — there is no `updated_at >= created_at` CHECK: the row is
written once when the event is handled and never mutated.

Edited after `alembic revision --autogenerate` the way revisions 0002-0017 document: no
application imports for the enum members, the enum CHECK created and dropped implicitly with its
table, and the convention-generated foreign-key name kept under PostgreSQL's 63-character limit so
it is not named explicitly. The schema-drift test compares the result against `Base.metadata`,
which is what keeps that a claim. Additive only: no existing table is touched.
"""
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)

# The members of the enum this revision introduces, written out. It is a `StrEnum` in
# `backend.app.domain.subscription_event`; the CHECK behind the column keeps the two lists
# agreeing, and the drift test keeps this file and `models.py` agreeing.
_SUBSCRIPTION_EVENT_OUTCOMES = ("APPLIED", "SUPERSEDED", "IGNORED")


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
    """Create the append-only `subscription_events` ledger, referencing the pre-existing `users`."""
    op.create_table(
        "subscription_events",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("provider", _TEXT, nullable=False),
        sa.Column("external_event_id", _TEXT, nullable=False),
        sa.Column("event_type", _TEXT, nullable=False),
        sa.Column("outcome",
                  _enum("subscription_event_outcome", *_SUBSCRIPTION_EVENT_OUTCOMES),
                  nullable=False),
        sa.Column("user_id", _UUID, nullable=True),
        sa.Column("subscription_id", _UUID, nullable=True),
        sa.Column("event_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("received_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("detail", _TEXT, nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_subscription_events_user_id_users"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription_events")),
        sa.UniqueConstraint(
            "provider", "external_event_id",
            name=op.f("uq_subscription_events_provider_external_event_id")),
    )
    op.create_index("ix_subscription_events_user_id", "subscription_events",
                    ["user_id"], unique=False)


def downgrade() -> None:
    """Return the schema to revision 0017, dropping the ledger.

    Destructive: every processed-webhook audit record is deleted. The explicit index is dropped
    explicitly; the enum CHECK, unique constraint and foreign key go with the table.
    """
    op.drop_index("ix_subscription_events_user_id", table_name="subscription_events")
    op.drop_table("subscription_events")
