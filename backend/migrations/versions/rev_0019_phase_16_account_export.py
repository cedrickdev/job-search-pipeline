"""phase 16 account data export: portable, secret-free, expiring copies of a user's own data

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-28 09:00:00.000000

Phase 16 §23-25 gives a user a portable copy of everything the platform holds about them.
`account_exports` is the lifecycle record of one such request: it never holds the data, only
where the produced archive lives (`storage_key`), how big it is (`byte_size`), when it stops
being downloadable (`expires_at`), and — for a failed one — the machine `failure_reason`. The
archive bytes themselves live in an export store behind an abstraction, so this table survives a
switch from the local store to object storage untouched (§25).

User-owned, read `WHERE user_id = ?`, cascading from `users` — so account deletion (§27-28)
removes the *row* by the same FK cascade every user-owned table relies on, while the archive
*bytes* are deleted explicitly by the export store, never orphaned. The id is random, so each
request is its own row.

The CHECKs are `AccountExport`'s validator made physical, so a row written by a migration or by
psql cannot assert what the domain layer could never produce: `state_coherent` restates the
per-status field rules (a `READY` export has an artifact and an expiry and no failure; a `FAILED`
one has a reason and no artifact; a `PENDING` one has none of them; an `EXPIRED` one keeps its
provenance but has had its artifact purged), `expiry_after_completion` restates the forward-running
retention window, `schema_version_positive` and `byte_size_non_negative` restate the field bounds.
`byte_size` is `BIGINT` — an archive is measured in bytes and need not fit `INT`.

Edited after `alembic revision --autogenerate` the way revisions 0002-0018 document: no
application imports for column types or enum members, the state CHECK copied verbatim from
`models.py` so the drift test sees one expression on both sides, and the enum CHECK created and
dropped implicitly with its table. The convention-generated foreign-key name is under
PostgreSQL's 63-character limit, so it is not named explicitly. The schema-drift test compares the
result against `Base.metadata`, which is what keeps that a claim. Additive only: no existing table
is touched.
"""
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_INTEGER = sa.Integer()
_BIGINT = sa.BigInteger()

# The members of the enum this revision introduces, written out. It is a `StrEnum` in
# `backend.app.domain.account_export`; the CHECK behind the column keeps the two lists agreeing,
# and the drift test keeps this file and `models.py` agreeing.
_ACCOUNT_EXPORT_STATUSES = ("PENDING", "READY", "FAILED", "EXPIRED")

# Verbatim copy of the CHECK expression in `models.py` (`_ACCOUNT_EXPORT_STATE_COHERENT`), so the
# drift test — and a reader — see one expression on both sides of the state-machine invariant.
_ACCOUNT_EXPORT_STATE_COHERENT = (
    "(status = 'PENDING' AND storage_key IS NULL AND byte_size IS NULL"
    " AND completed_at IS NULL AND expires_at IS NULL AND failure_reason IS NULL)"
    " OR (status = 'READY' AND storage_key IS NOT NULL AND byte_size IS NOT NULL"
    " AND completed_at IS NOT NULL AND expires_at IS NOT NULL AND failure_reason IS NULL)"
    " OR (status = 'FAILED' AND storage_key IS NULL AND byte_size IS NULL"
    " AND completed_at IS NOT NULL AND expires_at IS NULL AND failure_reason IS NOT NULL)"
    " OR (status = 'EXPIRED' AND storage_key IS NULL AND byte_size IS NULL"
    " AND completed_at IS NOT NULL AND expires_at IS NOT NULL AND failure_reason IS NULL)"
)


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
    """Create the `account_exports` lifecycle table, referencing the pre-existing `users`."""
    op.create_table(
        "account_exports",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("status", _enum("account_export_status", *_ACCOUNT_EXPORT_STATUSES),
                  nullable=False),
        sa.Column("schema_version", _INTEGER, nullable=False),
        sa.Column("storage_key", _TEXT, nullable=True),
        sa.Column("byte_size", _BIGINT, nullable=True),
        sa.Column("completed_at", _TIMESTAMPTZ, nullable=True),
        sa.Column("expires_at", _TIMESTAMPTZ, nullable=True),
        sa.Column("failure_reason", _TEXT, nullable=True),
        *_timestamps(),
        sa.CheckConstraint("schema_version >= 1",
                           name=op.f("ck_account_exports_schema_version_positive")),
        sa.CheckConstraint("byte_size IS NULL OR byte_size >= 0",
                           name=op.f("ck_account_exports_byte_size_non_negative")),
        sa.CheckConstraint(_ACCOUNT_EXPORT_STATE_COHERENT,
                           name=op.f("ck_account_exports_state_coherent")),
        sa.CheckConstraint(
            "expires_at IS NULL OR completed_at IS NULL OR expires_at > completed_at",
            name=op.f("ck_account_exports_expiry_after_completion")),
        sa.CheckConstraint("updated_at >= created_at",
                           name=op.f("ck_account_exports_updated_at_after_created_at")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_account_exports_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_exports")),
    )
    op.create_index("ix_account_exports_user_id_created_at", "account_exports",
                    ["user_id", "created_at"], unique=False)


def downgrade() -> None:
    """Return the schema to revision 0018, dropping the export table.

    Destructive: every export lifecycle record is deleted (the archive bytes in the export store
    are not this migration's to remove). The explicit index is dropped explicitly; the enum CHECK,
    the state CHECKs and the foreign key go with the table.
    """
    op.drop_index("ix_account_exports_user_id_created_at", table_name="account_exports")
    op.drop_table("account_exports")
