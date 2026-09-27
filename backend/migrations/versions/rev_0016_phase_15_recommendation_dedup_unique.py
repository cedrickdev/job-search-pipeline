"""phase 15 corrective: make a recommendation's (user_id, fingerprint) identity unique

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-27 14:00:00.000000

Revision 0015 gave `career_recommendations` a `fingerprint` — the deterministic identity of a
suggestion's logical content — and a *non-unique* index on `(user_id, fingerprint)` that the engine
used as a dedup lookup: it read `find_by_fingerprint` before inserting, so regenerating over
unchanged analytics reused the existing row instead of writing a duplicate. That read-then-write is
not safe under concurrency. Two units of work generating the same suggestion at once both find
nothing, both insert, and the store ends with two rows for one logical recommendation.

This revision closes the race at the only layer that can — the database — by promoting that index
to UNIQUE. With `(user_id, fingerprint)` unique, the second concurrent insert trips the index and
raises rather than duplicating; the repository catches that, rolls back its SAVEPOINT and returns
the recommendation that won the race, so both callers receive one logical recommendation and no raw
error escapes. The identity a recommendation's fingerprint always described is now a fact the
database keeps, not merely a convention the application hopes to honour.

A unique index cannot be created over a table that already holds duplicate `(user_id, fingerprint)`
pairs, so any that a pre-corrective run left behind are collapsed first. A duplicate is by
definition the *same* logical recommendation — same account, kind, analytics recipe, window,
horizon and cited evidence — so keeping the most recent (`created_at`, then `id` as a stable
tie-break) and deleting the older copies loses no distinct suggestion; the evidence of a deleted row
cascades with it. On the empty database CI and every migration test build from, the delete is a
no-op. Legacy rows carry `legacy:`-prefixed fingerprints keyed on their own id (0015), so they are
unique per row and are never touched.

The index keeps its name, so it is still the lookup `find_by_fingerprint` runs — now doing double
duty as the lookup and the uniqueness guarantee. Additive to the schema the way 0015 was; the
downgrade restores the non-unique index but cannot resurrect a collapsed duplicate, which is honest:
those rows were exact logical copies, so no unique recommendation is lost either way. Do not rewrite
revisions 0001-0015; this revision only re-shapes the one index on the one table.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "ix_career_recommendations_user_id_fingerprint"


def _collapse_duplicate_fingerprints() -> None:
    """Delete the older copies of any `(user_id, fingerprint)` duplicate, keeping the newest.

    A row `a` goes when some row `b` in the same account-and-fingerprint group is more recent
    (`(created_at, id)` greater), so exactly the group's maximum survives. Duplicates share their
    whole logical content by construction, so this collapses copies of one suggestion, never two
    distinct ones; the surviving row's evidence is untouched and a deleted row's cascades. A no-op
    on the empty database CI starts from — written set-based so it is one statement regardless.
    """
    op.execute(sa.text(
        "DELETE FROM career_recommendations a"
        " USING career_recommendations b"
        " WHERE a.user_id = b.user_id"
        " AND a.fingerprint = b.fingerprint"
        " AND (a.created_at, a.id) < (b.created_at, b.id)"))


def upgrade() -> None:
    """Collapse any duplicates, then promote the fingerprint index to UNIQUE."""
    _collapse_duplicate_fingerprints()
    op.drop_index(_INDEX_NAME, table_name="career_recommendations")
    op.create_index(_INDEX_NAME, "career_recommendations",
                    ["user_id", "fingerprint"], unique=True)


def downgrade() -> None:
    """Restore the non-unique fingerprint index.

    Reversible for the schema, not for the data: a duplicate collapsed on the way up stays
    collapsed, which loses no distinct recommendation — the removed rows were exact logical copies
    of the one kept.
    """
    op.drop_index(_INDEX_NAME, table_name="career_recommendations")
    op.create_index(_INDEX_NAME, "career_recommendations",
                    ["user_id", "fingerprint"], unique=False)
