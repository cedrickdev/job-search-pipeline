"""phase 16 background execution: the durable, idempotent queue of background task runs

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-28 10:00:00.000000

Phase 16 §32-40 moves the platform's slow work — discovery, matching, document generation,
analytics, exports, retention and application submission — off the request path and onto a
durable queue. `task_runs` is that queue made persistent: each row is one unit of deferred work
and exactly where it stands between `QUEUED` and a terminal `SUCCEEDED`/`DEAD_LETTERED`, so a
crash loses no work and a redelivery repeats none.

User-owned when a job belongs to an account (`user_id` cascades from `users`, so a deleted
account's queued work vanishes with it, §27); `user_id` is nullable because an operator job like a
retention sweep belongs to no account. The id derives from the `idempotency_key`, so enqueuing the
same job twice collides on the primary key rather than running it twice, and
`uq_task_runs_idempotency_key` is the second half of that guard (§37).

The CHECKs are `TaskRun`'s validator made physical, so a row written by a migration or by psql
cannot assert what the domain layer could never produce: `lane_matches_kind` restates
`lane_for_kind` (a browser job carries the BROWSER lane, §35), `state_coherent` restates the
per-status lease/timing/failure rules, `lease_coherent` and `failure_coherent` restate their
both-or-neither invariants, and the four ordering CHECKs restate the timestamp ordering.

Edited after `alembic revision --autogenerate` the way revisions 0002-0019 document: no
application imports for column types or enum members, the CHECK expressions copied verbatim from
`models.py` so the drift test sees one expression on both sides, and each enum CHECK created and
dropped implicitly with its table. The convention-generated foreign-key name is under PostgreSQL's
63-character limit, so it is not named explicitly. Additive only: no existing table is touched.
"""
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_INTEGER = sa.Integer()
_JSONB = postgresql.JSONB()
_EMPTY_JSON_OBJECT = sa.text("'{}'::jsonb")

# The members of the four enums this revision introduces, written out. They are `StrEnum`s in
# `backend.app.domain.task`; the CHECK behind each column keeps the lists agreeing, and the drift
# test keeps this file and `models.py` agreeing.
_TASK_KINDS = ("OPPORTUNITY_DISCOVERY", "COMPANY_DISCOVERY", "GEOCODING", "MATCHING",
               "DOCUMENT_GENERATION", "CAREER_ANALYTICS", "ACCOUNT_EXPORT", "RETENTION_SWEEP",
               "APPLICATION_SUBMISSION")
_TASK_LANES = ("GENERAL", "BROWSER")
_TASK_STATUSES = ("QUEUED", "RUNNING", "SUCCEEDED", "DEAD_LETTERED")
_TASK_FAILURE_CLASSES = ("TRANSIENT", "PERMANENT")

# Verbatim copies of the CHECK expressions in `models.py`, so the drift test — and a reader — see
# one expression on both sides of each invariant.
_TASK_RUN_LANE_MATCHES_KIND = (
    "(kind = 'APPLICATION_SUBMISSION' AND lane = 'BROWSER')"
    " OR (kind <> 'APPLICATION_SUBMISSION' AND lane = 'GENERAL')"
)
_TASK_RUN_LEASE_COHERENT = (
    "(lease_owner IS NULL) = (lease_expires_at IS NULL)"
)
_TASK_RUN_FAILURE_COHERENT = (
    "((last_failure_class IS NULL) = (failure_reason IS NULL))"
    " AND (failure_detail IS NULL OR failure_reason IS NOT NULL)"
)
_TASK_RUN_STATE_COHERENT = (
    "(status = 'QUEUED' AND lease_owner IS NULL AND lease_expires_at IS NULL"
    " AND finished_at IS NULL)"
    " OR (status = 'RUNNING' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL"
    " AND started_at IS NOT NULL AND finished_at IS NULL"
    " AND last_failure_class IS NULL AND attempts >= 1)"
    " OR (status = 'SUCCEEDED' AND lease_owner IS NULL AND lease_expires_at IS NULL"
    " AND started_at IS NOT NULL AND finished_at IS NOT NULL AND last_failure_class IS NULL)"
    " OR (status = 'DEAD_LETTERED' AND lease_owner IS NULL AND lease_expires_at IS NULL"
    " AND started_at IS NOT NULL AND finished_at IS NOT NULL"
    " AND last_failure_class IS NOT NULL AND attempts >= 1)"
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
    """Create the `task_runs` queue table, referencing the pre-existing `users`."""
    op.create_table(
        "task_runs",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=True),
        sa.Column("kind", _enum("task_kind", *_TASK_KINDS), nullable=False),
        sa.Column("lane", _enum("task_lane", *_TASK_LANES), nullable=False),
        sa.Column("status", _enum("task_status", *_TASK_STATUSES), nullable=False),
        sa.Column("idempotency_key", _TEXT, nullable=False),
        sa.Column("payload", _JSONB, server_default=_EMPTY_JSON_OBJECT, nullable=False),
        sa.Column("max_attempts", _INTEGER, nullable=False),
        sa.Column("attempts", _INTEGER, nullable=False),
        sa.Column("available_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("lease_owner", _TEXT, nullable=True),
        sa.Column("lease_expires_at", _TIMESTAMPTZ, nullable=True),
        sa.Column("last_failure_class",
                  _enum("task_failure_class", *_TASK_FAILURE_CLASSES), nullable=True),
        sa.Column("failure_reason", _TEXT, nullable=True),
        sa.Column("failure_detail", _TEXT, nullable=True),
        sa.Column("enqueued_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("started_at", _TIMESTAMPTZ, nullable=True),
        sa.Column("finished_at", _TIMESTAMPTZ, nullable=True),
        *_timestamps(),
        sa.CheckConstraint("max_attempts >= 1",
                           name=op.f("ck_task_runs_max_attempts_positive")),
        sa.CheckConstraint("attempts >= 0",
                           name=op.f("ck_task_runs_attempts_non_negative")),
        sa.CheckConstraint("attempts <= max_attempts",
                           name=op.f("ck_task_runs_attempts_within_max")),
        sa.CheckConstraint(_TASK_RUN_LANE_MATCHES_KIND,
                           name=op.f("ck_task_runs_lane_matches_kind")),
        sa.CheckConstraint(_TASK_RUN_LEASE_COHERENT,
                           name=op.f("ck_task_runs_lease_coherent")),
        sa.CheckConstraint(_TASK_RUN_FAILURE_COHERENT,
                           name=op.f("ck_task_runs_failure_coherent")),
        sa.CheckConstraint(_TASK_RUN_STATE_COHERENT,
                           name=op.f("ck_task_runs_state_coherent")),
        sa.CheckConstraint("available_at >= enqueued_at",
                           name=op.f("ck_task_runs_available_at_after_enqueued")),
        sa.CheckConstraint("started_at IS NULL OR started_at >= enqueued_at",
                           name=op.f("ck_task_runs_started_at_after_enqueued")),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_task_runs_finished_at_after_started")),
        sa.CheckConstraint("updated_at >= created_at",
                           name=op.f("ck_task_runs_updated_at_after_created_at")),
        sa.UniqueConstraint("idempotency_key",
                            name=op.f("uq_task_runs_idempotency_key")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_task_runs_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_runs")),
    )
    op.create_index("ix_task_runs_lane_status_available_at", "task_runs",
                    ["lane", "status", "available_at"], unique=False)
    op.create_index("ix_task_runs_status_lease_expires_at", "task_runs",
                    ["status", "lease_expires_at"], unique=False)
    op.create_index("ix_task_runs_user_id_created_at", "task_runs",
                    ["user_id", "created_at"], unique=False)


def downgrade() -> None:
    """Return the schema to revision 0019, dropping the queue table.

    Destructive: every queued, running and finished task run is deleted. The explicit indexes are
    dropped explicitly; the four enum CHECKs, the state/lease/failure CHECKs, the unique constraint
    and the foreign key go with the table.
    """
    op.drop_index("ix_task_runs_user_id_created_at", table_name="task_runs")
    op.drop_index("ix_task_runs_status_lease_expires_at", table_name="task_runs")
    op.drop_index("ix_task_runs_lane_status_available_at", table_name="task_runs")
    op.drop_table("task_runs")

