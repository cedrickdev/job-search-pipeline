"""phase 9 eligibility

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-13 10:04:12.882190

Phase 9 adds the *second* axis. Match evaluations already have their two tables
(revision 0002); eligibility is a different question with a different verdict
type, so it gets its own pair rather than a column bolted onto the first.
`eligibility_results` holds one verdict per candidate/opportunity pair,
`eligibility_checks` the gates that verdict is derived from. Nothing existing is
altered — a database at revision 0005 keeps every posting, employer, evaluation
and geocode it had — so this is purely additive, and `alembic upgrade head` on a
fresh database and on a 0005 database reach the same schema.

*Two tables, never one column on `match_evaluations`.* docs/V2_SPECIFICATION.md
§13 and CLAUDE.md make eligibility independent of fit: a pair can score 92% and
be INELIGIBLE, or 61% and ELIGIBLE, and folding the two into one row is the first
step towards averaging a legal gate into a percentage. The schema keeps them
apart so the code cannot.

*The three CHECKs on `eligibility_checks` are the legal-safety rule made
physical.* They restate `EligibilityCheck._verdict_is_accountable`: a non-ELIGIBLE
gate carries a reason, an `LLM_EXTRACTION` gate can only be `INCOMPLETE`, and a
`COUNTRY_PACK_RULE` gate may only reach `INELIGIBLE` when its authority is
`VERIFIED`. That last one is docs/COUNTRY_PACKS.md §Eligibility and Phase 9 §59 —
operator-maintained pack data (the Swiss student-permit hours cap) cannot refuse
an application on its own — enforced by the database, not merely by Python. Each
is written as the implication it is (`NOT antecedent OR consequent`), the form a
CHECK, which fails only on FALSE, evaluates the way the prose reads.

`eligibility_results.status` is denormalized. `EligibilityResult.status` is a
*derived* property (worst-of the checks), but a list that ranks and filters by
verdict must not load every child row to do it, exactly as `match_evaluations`
stores `overall`. The mapper writes the derived value and only that; the CHECKs
above keep the checks it is derived from honest, so the copy cannot assert a pass
over a failed gate.

Edited after `alembic revision --autogenerate` in the two ways revision 0002
documents: no application imports for column *types* (`sa.DateTime(timezone=True)`
is the `TIMESTAMPTZ` `UtcDateTime` emits, `sa.Text()` the `TEXT` a `Mapped[str]`
emits), and the repetition factored into the helpers below. The schema-drift test
compares the result against `Base.metadata`, which is what keeps that a claim.

As in revisions 0002 through 0005, the CHECK constraints backing the enum columns
are *not* created explicitly: `op.create_table` attaches the columns to a table
object, which is what makes a non-native `Enum` emit its member CHECK with the
generated name. They are dropped implicitly too — `op.drop_table` takes them with
the table — so `downgrade` names none of them.
"""
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Shared type instances: a SQLAlchemy type object carries no column state, so one
# instance can describe every column that uses it.
_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_JSONB = postgresql.JSONB()
_EMPTY_JSON_ARRAY = sa.text("'[]'::jsonb")

# The members of the four enums this revision introduces, written out. Each is a
# `StrEnum` in `backend.app.domain.eligibility`; the CHECK behind the column is
# what keeps the two lists agreeing, and the schema-drift test is what keeps this
# file and `models.py` agreeing. The full member set is listed, not only the ones
# the Phase 9 engine emits today: the column admits any valid member, and a gate
# the engine learns to produce later is then not a migration.
_ELIGIBILITY_REQUIREMENTS = ("WORK_AUTHORIZATION", "PERMIT_HOURS_CAP", "MINIMUM_AGE",
                             "LANGUAGE_MINIMUM", "EDUCATION_LEVEL", "CERTIFICATION",
                             "DRIVING_LICENCE", "AVAILABILITY_WINDOW",
                             "LOCATION_REACHABLE")
_ELIGIBILITY_STATUSES = ("ELIGIBLE", "INCOMPLETE", "REVIEW_REQUIRED", "INELIGIBLE")
_DETERMINATION_SOURCES = ("DETERMINISTIC_RULE", "COUNTRY_PACK_RULE",
                          "CANDIDATE_DECLARATION", "HUMAN_REVIEW", "LLM_EXTRACTION")
_RULE_AUTHORITIES = ("VERIFIED", "SOURCE_DECLARED", "OPERATOR_CONFIG", "UNKNOWN")

# `EligibilityCheck._verdict_is_accountable`, as three CHECK expressions. Verbatim
# copies of the `_ELIGIBILITY_*` constants in `models.py`, so the drift test sees
# one expression on both sides. Each is an implication: a CHECK passes unless it
# evaluates to FALSE, so `NOT antecedent OR consequent` reads as "when the
# antecedent holds, the consequent must too".
_ELIGIBILITY_REASONS_PRESENT = (
    "status = 'ELIGIBLE' OR jsonb_array_length(reasons) >= 1"
)
_ELIGIBILITY_LLM_IS_INCOMPLETE = (
    "determined_by <> 'LLM_EXTRACTION' OR status = 'INCOMPLETE'"
)
_ELIGIBILITY_PACK_BLOCKS_ONLY_WHEN_VERIFIED = (
    "determined_by <> 'COUNTRY_PACK_RULE' OR status <> 'INELIGIBLE'"
    " OR authority = 'VERIFIED'"
)


def _enum(name: str, *members: str) -> sa.Enum:
    """A `VARCHAR(32)` plus a CHECK on the permitted values.

    No PostgreSQL `ENUM` type, for the reason revision 0002 gives: adding a member
    to a native enum is DDL that cannot share a transaction with a table rewrite,
    while widening a CHECK is an ordinary migration.
    """
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
    """Create the Phase 9 eligibility schema.

    Parent before child, so the foreign key `eligibility_checks.result_id` has a
    table to point at when it is declared.
    """
    # `user_id` alongside `candidate_profile_id` is denormalized on purpose, as on
    # `match_evaluations`: every authorization-scoped read is `WHERE user_id = ?`,
    # so ownership is one indexed predicate rather than a join a caller could
    # forget. All three foreign keys cascade from their parents, so a deleted
    # account, profile or posting leaves no orphaned verdict.
    op.create_table(
        "eligibility_results",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("candidate_profile_id", _UUID, nullable=False),
        sa.Column("opportunity_id", _UUID, nullable=False),
        # Denormalized copy of the derived `EligibilityResult.status`, written by
        # the mapper so a list can rank and filter without loading every check.
        sa.Column("status", _enum("eligibility_result_status", *_ELIGIBILITY_STATUSES),
                  nullable=False),
        sa.Column("policy_version", _TEXT, nullable=True),
        sa.Column("determined_at", _TIMESTAMPTZ, nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"], ["candidate_profiles.id"],
            name=op.f("fk_eligibility_results_candidate_profile_id_candidate_profiles"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["opportunity_id"], ["opportunities.id"],
            name=op.f("fk_eligibility_results_opportunity_id_opportunities"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_eligibility_results_user_id_users"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eligibility_results")),
        # One verdict per (profile, opportunity). Re-evaluating updates the row,
        # which is what makes a re-scoring run idempotent.
        sa.UniqueConstraint(
            "candidate_profile_id", "opportunity_id",
            name=op.f("uq_eligibility_results_candidate_profile_id_opportunity_id")),
    )
    op.create_index("ix_eligibility_results_user_id_determined_at",
                    "eligibility_results", ["user_id", "determined_at"], unique=False)
    op.create_index("ix_eligibility_results_opportunity_id", "eligibility_results",
                    ["opportunity_id"], unique=False)

    op.create_table(
        "eligibility_checks",
        sa.Column("id", _UUID, nullable=False),
        # Short FK column name (`result_id`, not `eligibility_result_id`) so the
        # generated `fk_eligibility_checks_result_id_eligibility_results` stays
        # under PostgreSQL's 63-character limit, as the candidate child tables do.
        sa.Column("result_id", _UUID, nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column("requirement",
                  _enum("eligibility_requirement", *_ELIGIBILITY_REQUIREMENTS),
                  nullable=False),
        sa.Column("status",
                  _enum("eligibility_check_status", *_ELIGIBILITY_STATUSES),
                  nullable=False),
        sa.Column("determined_by",
                  _enum("eligibility_determination_source", *_DETERMINATION_SOURCES),
                  nullable=False),
        # Defaults to UNKNOWN — the weakest authority — so a check written without
        # one cannot accidentally claim the standing to refuse.
        sa.Column("authority",
                  _enum("eligibility_rule_authority", *_RULE_AUTHORITIES),
                  server_default=sa.text("'UNKNOWN'"), nullable=False),
        sa.Column("detail", _TEXT, nullable=True),
        sa.Column("reasons", _JSONB, server_default=_EMPTY_JSON_ARRAY, nullable=False),
        *_timestamps(),
        # The domain's accountability invariants, one CHECK each, in the order the
        # validator applies them.
        sa.CheckConstraint(
            _ELIGIBILITY_REASONS_PRESENT,
            name=op.f("ck_eligibility_checks_reasons_present_unless_eligible")),
        sa.CheckConstraint(
            _ELIGIBILITY_LLM_IS_INCOMPLETE,
            name=op.f("ck_eligibility_checks_llm_extraction_is_incomplete")),
        sa.CheckConstraint(
            _ELIGIBILITY_PACK_BLOCKS_ONLY_WHEN_VERIFIED,
            name=op.f("ck_eligibility_checks_pack_rule_blocks_only_when_verified")),
        sa.ForeignKeyConstraint(
            ["result_id"], ["eligibility_results.id"],
            name=op.f("fk_eligibility_checks_result_id_eligibility_results"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eligibility_checks")),
        # `ordinal` is the natural key: a requirement may repeat (two required
        # languages are two `LANGUAGE_MINIMUM` gates), so position is what
        # identifies a row and what makes re-evaluation reconcile in place.
        sa.UniqueConstraint("result_id", "ordinal",
                            name=op.f("uq_eligibility_checks_result_id_ordinal")),
    )


def downgrade() -> None:
    """Return the schema to revision 0005.

    Destructive by nature: every eligibility verdict and its checks are deleted.
    Child before parent, so the foreign key never outlives the table it points at.
    The two indexes on `eligibility_results` are dropped explicitly for the same
    reason autogenerate creates them explicitly — they were not declared inline on
    a column — while the enum CHECKs, the unique constraints and the foreign keys
    go with their tables.
    """
    op.drop_table("eligibility_checks")
    op.drop_index("ix_eligibility_results_opportunity_id",
                  table_name="eligibility_results")
    op.drop_index("ix_eligibility_results_user_id_determined_at",
                  table_name="eligibility_results")
    op.drop_table("eligibility_results")
