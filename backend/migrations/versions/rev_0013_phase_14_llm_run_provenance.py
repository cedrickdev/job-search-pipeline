"""phase 14 corrective — exact LLM-run provenance on interview artefacts

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-24 21:00:00.000000

The Phase 14 corrective narrows interview provenance from *which strategy* produced an
artefact (`generator_key`/`evaluator_key` = "llm-backed/1") to *which exact run* did: a
nullable `llm_run_id` on the question, the evaluation and the summary, and a
`plan_llm_run_id` on the session for the run that produced its coverage plan. Each points
at `llm_runs.id` — the model, the connection, the prompt version, keyed to the same call —
so an audit can trace a generated question back to the routed request behind it (§27-40).

Every FK is `ON DELETE SET NULL`, never CASCADE: an operator pruning telemetry must never
delete a candidate's practice history as a side effect. The columns are nullable because a
deterministic artefact (a fallback summary keyed `deterministic-summary/1`) has no run to
point at, and because a session created before this revision carries none.

Edited after `alembic revision --autogenerate` the way revisions 0002-0012 document: no
application imports for column types, the FK/column pairs factored into the helpers below,
parents-agnostic (each column stands alone). The schema-drift test compares the result
against `Base.metadata`, which is what keeps that a claim.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UUID = sa.UUID()

# (table, column) for each provenance link this revision adds. The FK to `llm_runs.id` is
# `SET NULL` for every one, so the constraint name is derived the same way `op.f()` would.
_LINKS: tuple[tuple[str, str], ...] = (
    ("interview_sessions", "plan_llm_run_id"),
    ("interview_questions", "llm_run_id"),
    ("interview_answer_evaluations", "llm_run_id"),
    ("interview_session_summaries", "llm_run_id"),
)


def upgrade() -> None:
    """Add the nullable `llm_run_id`/`plan_llm_run_id` columns and their SET NULL FKs."""
    for table, column in _LINKS:
        op.add_column(table, sa.Column(column, _UUID, nullable=True))
        op.create_foreign_key(
            op.f(f"fk_{table}_{column}_llm_runs"),
            table, "llm_runs", [column], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    """Drop the provenance FKs and columns, returning the schema to revision 0012.

    Non-destructive to the interview artefacts themselves: only the link to the run is
    removed, and it was nullable, so nothing that survived depended on it.
    """
    for table, column in reversed(_LINKS):
        op.drop_constraint(op.f(f"fk_{table}_{column}_llm_runs"), table,
                           type_="foreignkey")
        op.drop_column(table, column)
