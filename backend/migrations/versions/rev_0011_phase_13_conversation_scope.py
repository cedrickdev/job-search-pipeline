"""phase 13 corrective: conversation domain scope

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-23 21:30:00.000000

The Phase 13 corrective gives a conversation a real domain scope: a thread is either
`GLOBAL` (about the account's whole search) or anchored to one application, opportunity,
company, or saved search. Two additive columns on `conversations` carry it — `scope`
(the closed `ConversationScope` enum) and `scope_id` (the anchored resource, NULL for a
GLOBAL thread) — and a CHECK makes the shape invariant physical: exactly the GLOBAL
threads carry no anchor, and every other scope carries one.

`scope_id` deliberately has **no** cross-table foreign key: it addresses one of four
different tables depending on `scope`, and ownership is re-checked by reading through a
`user_id`-scoped repository at use time, not by a constraint. The CHECK enforces only the
shape, exactly as `Conversation._scope_id_matches_scope` does in the domain and
`_CONVERSATION_SCOPE_ID_MATCHES_SCOPE` does in `models.py`.

Additive and backwards-compatible: `scope` takes a server default of `'GLOBAL'`, so every
row written by revision 0010 reads back as a GLOBAL thread with a NULL `scope_id` — which
already satisfies the CHECK. The enum's own member CHECK is not created explicitly:
`op.add_column` attaches the column to a table object, which is what makes a non-native
`Enum` emit its member CHECK, exactly as revisions 0003 and 0005 document. The
schema-drift test compares the result against `Base.metadata`, which keeps that a claim.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UUID = sa.UUID()

# The members of `ConversationScope` (`backend.app.domain.chat`), written out. The CHECK
# behind the column keeps the two lists agreeing; the schema-drift test keeps this file
# and `models.py` agreeing.
_SCOPES = ("GLOBAL", "OPPORTUNITY", "APPLICATION", "COMPANY", "SEARCH_PROFILE")

# Verbatim copy of `_CONVERSATION_SCOPE_ID_MATCHES_SCOPE` in `models.py`, so the drift
# test sees one expression on both sides: a GLOBAL thread carries no anchor id, and every
# other scope carries exactly one.
_SCOPE_ID_MATCHES_SCOPE = (
    "(scope = 'GLOBAL' AND scope_id IS NULL)"
    " OR (scope <> 'GLOBAL' AND scope_id IS NOT NULL)")


def _enum(name: str, *members: str, length: int = 32) -> sa.Enum:
    """A `VARCHAR(length)` plus a CHECK on the permitted values, as revision 0002 gives."""
    return sa.Enum(*members, name=name, native_enum=False, create_constraint=True,
                   length=length)


def upgrade() -> None:
    """Add the conversation scope columns and the shape CHECK.

    `scope` defaults to `'GLOBAL'` so existing rows backfill to a GLOBAL thread, and
    `scope_id` is nullable and defaults to NULL — the pair a GLOBAL thread must hold, so
    the shape CHECK is satisfiable the instant it is added.
    """
    op.add_column(
        "conversations",
        sa.Column("scope", _enum("conversation_scope", *_SCOPES),
                  server_default=sa.text("'GLOBAL'"), nullable=False))
    op.add_column(
        "conversations",
        sa.Column("scope_id", _UUID, nullable=True))
    op.create_check_constraint(
        op.f("ck_conversations_scope_id_matches_scope"),
        "conversations", _SCOPE_ID_MATCHES_SCOPE)


def downgrade() -> None:
    """Drop the scope columns and their constraints, back to revision 0010.

    The shape CHECK is dropped explicitly; the enum's own member CHECK goes with its
    column when `scope` is dropped.
    """
    op.drop_constraint(op.f("ck_conversations_scope_id_matches_scope"),
                       "conversations", type_="check")
    op.drop_column("conversations", "scope_id")
    op.drop_column("conversations", "scope")
