"""phase 15 corrective: pin the analytics snapshot and the dedup fingerprint on a recommendation

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-27 12:00:00.000000

The Phase 15 corrective gives `career_recommendations` two things revision 0014 did not persist.
First, the exact analytics snapshot behind a suggestion — `analytics_computed_at`, the
`observation_horizon_days` it was measured under, and the `window_start`/`window_end` of the
population it observed — so a stored recommendation can be read back against the precise report
that produced it, not merely the `analytics_version` recipe. Second, a `fingerprint`: the
deterministic identity of a recommendation's logical content (its evidence and snapshot window,
never its wording), which the engine gates on before inserting so that regenerating over unchanged
analytics is idempotent rather than a flood of near-duplicate rows.

Additive and non-destructive, the way revisions 0004, 0005 and 0013 document. The three columns a
recommendation cannot be without — `fingerprint`, `analytics_computed_at` and
`observation_horizon_days` — are added nullable, backfilled for any pre-existing row, then set
`NOT NULL`; the window pair stays nullable because a report over an empty population genuinely has
no window. No server default
survives the upgrade: the model declares none, and the schema-drift test compares this result
against `Base.metadata`, so a lingering default would be reported as drift. A row written before
this corrective gets a `legacy:`-prefixed fingerprint (its own id, so it stays distinct and never
collides with a real content fingerprint), its `created_at` as the computed-at instant, and the
default 30-day horizon — an honest best effort for a suggestion drawn before the snapshot was kept,
never a fabricated window.

The CHECK restates the domain's `observation_horizon_days >= 1`, and the composite index on
`(user_id, fingerprint)` is the lookup the engine's dedup gate runs. Do not rewrite revisions
0001-0014; this revision only extends the one table.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_SMALLINT = sa.SmallInteger()

# The horizon a recommendation drawn before this corrective is backfilled with, restated from
# `backend.app.domain.analytics.DEFAULT_OBSERVATION_HORIZON_DAYS`. Written out rather than imported,
# the way this file avoids application imports; the CHECK behind the column and the drift test keep
# the two agreeing.
_DEFAULT_OBSERVATION_HORIZON_DAYS = 30


def _backfill_snapshot() -> None:
    """Give every pre-existing recommendation the snapshot and fingerprint this corrective pins.

    A row written under revision 0014 kept no snapshot, so there is nothing to recover: it gets a
    `legacy:`-prefixed fingerprint built from its own id (distinct per row, and namespaced so it can
    never collide with a real content fingerprint the engine computes), its `created_at` as the
    best available computed-at instant, and the default horizon. The window stays NULL — an honest
    "unknown", never a fabricated span. A single set-based UPDATE, a no-op on the empty database CI
    and every migration test start from.
    """
    op.execute(sa.text(
        "UPDATE career_recommendations SET"
        " fingerprint = 'legacy:' || id::text,"
        " analytics_computed_at = created_at,"
        " observation_horizon_days = :horizon"
        " WHERE fingerprint IS NULL").bindparams(
            horizon=_DEFAULT_OBSERVATION_HORIZON_DAYS))


def upgrade() -> None:
    """Add the snapshot columns and the dedup fingerprint to `career_recommendations`."""
    op.add_column("career_recommendations",
                  sa.Column("fingerprint", _TEXT, nullable=True))
    op.add_column("career_recommendations",
                  sa.Column("analytics_computed_at", _TIMESTAMPTZ, nullable=True))
    op.add_column("career_recommendations",
                  sa.Column("observation_horizon_days", _SMALLINT, nullable=True))
    op.add_column("career_recommendations",
                  sa.Column("window_start", _TIMESTAMPTZ, nullable=True))
    op.add_column("career_recommendations",
                  sa.Column("window_end", _TIMESTAMPTZ, nullable=True))

    _backfill_snapshot()

    op.alter_column("career_recommendations", "fingerprint",
                    existing_type=_TEXT, nullable=False)
    op.alter_column("career_recommendations", "analytics_computed_at",
                    existing_type=_TIMESTAMPTZ, nullable=False)
    op.alter_column("career_recommendations", "observation_horizon_days",
                    existing_type=_SMALLINT, nullable=False)

    op.create_check_constraint(
        op.f("ck_career_recommendations_observation_horizon_days_positive"),
        "career_recommendations", "observation_horizon_days >= 1")
    op.create_index("ix_career_recommendations_user_id_fingerprint",
                    "career_recommendations", ["user_id", "fingerprint"], unique=False)


def downgrade() -> None:
    """Drop the fingerprint, the snapshot columns and the objects that guarded them.

    Reversible without loss of a recommendation: the five columns and their CHECK and index go, but
    every suggestion revision 0014 could hold — its kind, version, prose and evidence — survives, so
    an operator who has to roll the corrective back keeps the recommendations themselves.
    """
    op.drop_index("ix_career_recommendations_user_id_fingerprint",
                  table_name="career_recommendations")
    op.drop_constraint(
        op.f("ck_career_recommendations_observation_horizon_days_positive"),
        "career_recommendations", type_="check")
    op.drop_column("career_recommendations", "window_end")
    op.drop_column("career_recommendations", "window_start")
    op.drop_column("career_recommendations", "observation_horizon_days")
    op.drop_column("career_recommendations", "analytics_computed_at")
    op.drop_column("career_recommendations", "fingerprint")
