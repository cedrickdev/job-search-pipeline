"""phase 11 provider-neutral llm platform

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-21 09:30:00.000000

Phase 11 turns the LLM into a replaceable infrastructure dependency, and gives its
three persisted pieces a home. `llm_connections` is a user's configured way to reach
a provider — which type, at which base URL, with which model, and (for a hosted
gateway) an API key kept *encrypted* in `encrypted_api_key` beside the
`secret_version` that names the key which made it. The master key that decrypts it is
never a column (docs/LLM_PROVIDER_ARCHITECTURE.md §21); a database dump holds
ciphertext and a version tag and nothing that reads them. `provider_sessions`
replaces V1's provider-specific `claude_session_id` with a neutral
`external_session_id`, one row per `(connection, conversation)` (§4). `llm_runs` is
the per-call telemetry — provider, model, tokens, cost, latency, outcome, and the
fallback origin when the serving provider was not the first choice (§12, §56).

Two connection CHECKs are the data-layer half of the security model. `secret_pair`
keeps the ciphertext and its version together or absent, so no row carries a
ciphertext no key can read. `transport_shape` refuses a CLI connection that carries a
base URL, a stored credential or custom headers — the §1 rule that the platform never
injects a credential into a self-authenticating CLI — and requires an API connection
to name its endpoint, since §5 forbids assuming the hostname. The three `llm_runs`
CHECKs restate `LLMRun`'s validators: a STARTED run has no finish, a FAILED/TIMEOUT
run carries a code, and a fallback names both its origin and its reason.

Edited after `alembic revision --autogenerate` in the ways revisions 0002 through
0007 document: no application imports for column types, the repetition factored into
the helpers below, and the enum CHECKs created and dropped implicitly with their
tables. The schema-drift test compares the result against `Base.metadata`, which is
what keeps that a claim.
"""
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Shared type instances: a SQLAlchemy type object carries no column state.
_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_JSONB = postgresql.JSONB()
_EMPTY_JSON_OBJECT = sa.text("'{}'::jsonb")

# The members of the enums this revision introduces, written out. Each is a `StrEnum`
# in `backend.app.llm`; the CHECK behind the column keeps the two lists agreeing, and
# the schema-drift test keeps this file and `models.py` agreeing.
_PROVIDER_TYPES = ("CLAUDE_CODE", "CODEX", "OPENAI_COMPATIBLE",
                   "LOCAL_OPENAI_COMPATIBLE")
_TASK_PURPOSES = ("RESUME_TAILORING", "COVER_LETTER", "CAREER_CHAT",
                  "INTERVIEW_PREP", "GENERIC")
_RUN_STATUSES = ("STARTED", "SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT")
_FAILURE_CODES = ("PROVIDER_UNAVAILABLE", "PROVIDER_TIMEOUT",
                  "PROVIDER_AUTH_REQUIRED", "PROVIDER_RATE_LIMITED",
                  "PROVIDER_MISCONFIGURED", "CAPABILITY_NOT_SUPPORTED",
                  "STRUCTURED_OUTPUT_INVALID", "PROVIDER_PROTOCOL_ERROR",
                  "CONTEXT_LENGTH_EXCEEDED", "PROVIDER_CONTENT_FILTERED",
                  "OUTPUT_LIMIT_EXCEEDED", "PROVIDER_CANCELLED",
                  "SESSION_NOT_FOUND", "PROVIDER_INTERNAL_ERROR")

# Verbatim copies of the `_LLM_*` constants in `models.py`, so the drift test sees one
# expression on both sides. Each is an implication or an equivalence a CHECK reads as
# written (it passes unless it evaluates to FALSE).
_LLM_CONNECTION_TRANSPORT_SHAPE = (
    "(provider_type IN ('CLAUDE_CODE', 'CODEX')"
    " AND base_url IS NULL AND encrypted_api_key IS NULL"
    " AND custom_headers = '{}'::jsonb)"
    " OR (provider_type IN ('OPENAI_COMPATIBLE', 'LOCAL_OPENAI_COMPATIBLE')"
    " AND base_url IS NOT NULL)"
)
_LLM_CONNECTION_SECRET_PAIR = "(encrypted_api_key IS NULL) = (secret_version IS NULL)"  # noqa: S105
_LLM_RUN_STARTED_HAS_NO_FINISH = "(status = 'STARTED') = (finished_at IS NULL)"
_LLM_RUN_FAILURE_CARRIES_CODE = (
    "(status IN ('FAILED', 'TIMEOUT')) = (failure_code IS NOT NULL)"
)
_LLM_RUN_FALLBACK_PAIR = "(fallback_from IS NULL) = (fallback_reason IS NULL)"
_PROVIDER_KEY_PATTERN = "^[a-z][a-z0-9_-]*$"


def _enum(name: str, *members: str) -> sa.Enum:
    """A `VARCHAR(32)` plus a CHECK on the permitted values, as revision 0002 gives."""
    return sa.Enum(*members, name=name, native_enum=False, create_constraint=True,
                   length=32)


def _timestamps() -> tuple[sa.Column[Any], ...]:
    """`created_at` and `updated_at`, as `TimestampedMixin` declares them."""
    return (
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
    )


def upgrade() -> None:
    """Create the Phase 11 LLM schema, parents before children.

    `llm_connections` first, so `provider_sessions` and `llm_runs` have a table to
    point at. `provider_sessions` cascades from its connection; `llm_runs` sets its
    `connection_id` NULL on delete, because a run is provenance and outlives the
    connection it used.
    """
    op.create_table(
        "llm_connections",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("provider_type", _enum("llm_provider_type", *_PROVIDER_TYPES),
                  nullable=False),
        sa.Column("display_name", _TEXT, nullable=False),
        sa.Column("base_url", _TEXT, nullable=True),
        sa.Column("model", _TEXT, nullable=True),
        sa.Column("encrypted_api_key", _TEXT, nullable=True),
        sa.Column("secret_version", sa.SmallInteger(), nullable=True),
        sa.Column("custom_headers", _JSONB, server_default=_EMPTY_JSON_OBJECT,
                  nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"),
                  nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"),
                  nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("100"),
                  nullable=False),
        *_timestamps(),
        sa.CheckConstraint(_LLM_CONNECTION_SECRET_PAIR,
                           name=op.f("ck_llm_connections_secret_pair_complete")),
        sa.CheckConstraint(_LLM_CONNECTION_TRANSPORT_SHAPE,
                           name=op.f("ck_llm_connections_transport_shape_coherent")),
        sa.CheckConstraint("secret_version >= 1",
                           name=op.f("ck_llm_connections_secret_version_positive")),
        sa.CheckConstraint("priority >= 0",
                           name=op.f("ck_llm_connections_priority_non_negative")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_llm_connections_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_connections")),
    )
    op.create_index("ix_llm_connections_user_id", "llm_connections", ["user_id"],
                    unique=False)
    # At most one default connection per account: a partial unique index, free on the
    # non-default rows, the connection twin of the single-headquarters rule.
    op.create_index("uq_llm_connections_user_id_default", "llm_connections",
                    ["user_id"], unique=True,
                    postgresql_where=sa.text("is_default"))

    op.create_table(
        "provider_sessions",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("connection_id", _UUID, nullable=False),
        sa.Column("conversation_key", _TEXT, nullable=False),
        sa.Column("purpose", _enum("provider_session_purpose", *_TASK_PURPOSES),
                  server_default=sa.text("'GENERIC'"), nullable=False),
        sa.Column("external_session_id", _TEXT, nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["llm_connections.id"],
            name=op.f("fk_provider_sessions_connection_id_llm_connections"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_provider_sessions_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_sessions")),
        sa.UniqueConstraint(
            "connection_id", "conversation_key",
            name=op.f("uq_provider_sessions_connection_id_conversation_key")),
    )
    op.create_index("ix_provider_sessions_user_id", "provider_sessions", ["user_id"],
                    unique=False)
    op.create_index("ix_provider_sessions_connection_id", "provider_sessions",
                    ["connection_id"], unique=False)

    # `started_at` is domain-supplied (the instant the call began) and so has no
    # server default; `finished_at` is NULL until the call returns. The token, cost
    # and latency columns are nullable because the unknown is null, never zero (§58).
    op.create_table(
        "llm_runs",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=True),
        sa.Column("connection_id", _UUID, nullable=True),
        sa.Column("provider_key", _TEXT, nullable=False),
        sa.Column("provider_type", _enum("llm_run_provider_type", *_PROVIDER_TYPES),
                  nullable=True),
        sa.Column("model", _TEXT, nullable=True),
        sa.Column("purpose", _enum("llm_run_purpose", *_TASK_PURPOSES),
                  server_default=sa.text("'GENERIC'"), nullable=False),
        sa.Column("status", _enum("llm_run_status", *_RUN_STATUSES), nullable=False),
        sa.Column("prompt_name", _TEXT, nullable=True),
        sa.Column("prompt_version", _TEXT, nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Double(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("failure_code", _enum("llm_run_failure_code", *_FAILURE_CODES),
                  nullable=True),
        sa.Column("failure_detail", _TEXT, nullable=True),
        sa.Column("fallback_from", _TEXT, nullable=True),
        sa.Column("fallback_reason",
                  _enum("llm_run_fallback_reason", *_FAILURE_CODES), nullable=True),
        sa.Column("started_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("finished_at", _TIMESTAMPTZ, nullable=True),
        *_timestamps(),
        sa.CheckConstraint(_LLM_RUN_STARTED_HAS_NO_FINISH,
                           name=op.f("ck_llm_runs_started_has_no_finish")),
        sa.CheckConstraint(_LLM_RUN_FAILURE_CARRIES_CODE,
                           name=op.f("ck_llm_runs_failure_carries_code")),
        sa.CheckConstraint(_LLM_RUN_FALLBACK_PAIR,
                           name=op.f("ck_llm_runs_fallback_pair_complete")),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0",
            name=op.f("ck_llm_runs_token_counts_non_negative")),
        sa.CheckConstraint("cost_usd >= 0.0",
                           name=op.f("ck_llm_runs_cost_non_negative")),
        sa.CheckConstraint("latency_ms >= 0",
                           name=op.f("ck_llm_runs_latency_non_negative")),
        sa.CheckConstraint(f"provider_key ~ '{_PROVIDER_KEY_PATTERN}'",
                           name=op.f("ck_llm_runs_provider_key_format")),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["llm_connections.id"],
            name=op.f("fk_llm_runs_connection_id_llm_connections"),
            ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_llm_runs_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_runs")),
    )
    op.create_index("ix_llm_runs_user_id_started_at", "llm_runs",
                    ["user_id", "started_at"], unique=False)
    op.create_index("ix_llm_runs_provider_key_started_at", "llm_runs",
                    ["provider_key", "started_at"], unique=False)
    op.create_index("ix_llm_runs_connection_id", "llm_runs", ["connection_id"],
                    unique=False)


def downgrade() -> None:
    """Return the schema to revision 0007.

    Destructive by nature: every stored connection, provider session and telemetry
    run is deleted. Children before parents, so no foreign key outlives what it points
    at. Explicit indexes are dropped explicitly; the enum CHECKs, unique constraints
    and foreign keys go with their tables.
    """
    op.drop_index("ix_llm_runs_connection_id", table_name="llm_runs")
    op.drop_index("ix_llm_runs_provider_key_started_at", table_name="llm_runs")
    op.drop_index("ix_llm_runs_user_id_started_at", table_name="llm_runs")
    op.drop_table("llm_runs")
    op.drop_index("ix_provider_sessions_connection_id",
                  table_name="provider_sessions")
    op.drop_index("ix_provider_sessions_user_id", table_name="provider_sessions")
    op.drop_table("provider_sessions")
    op.drop_index("uq_llm_connections_user_id_default", table_name="llm_connections")
    op.drop_index("ix_llm_connections_user_id", table_name="llm_connections")
    op.drop_table("llm_connections")
