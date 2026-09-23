"""phase 10 ats documents and candidate evidence store

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-20 09:30:00.000000

Phase 10 gives three long-prepared things a home. The candidate evidence store —
`candidate_evidence` and `candidate_claims`, both children of
`candidate_profiles` — is the substrate the truth guard rests on: every claim and
every generated document line cites the id of an evidence record here, and V1's
"unknown bullet id" gate becomes the CHECK that a claim's `evidence_ids` array is
non-empty. The documents themselves are `candidate_documents` (one per
posting/profile/type triple) and `document_versions` (one attempt each, carrying
content, the guard's verdict and — once rendered — its PDF locator).

Two existing tables gain one column each. `candidate_work_authorizations` and
`eligibility_checks` get an `evidence_ids` `TEXT[]`: the domain always carried the
field, and revisions 0002 and 0006 dropped it only because there was no evidence
store to point at. Both default to `'{}'` — the empty array a
`_text_array_elements_present` CHECK still polices — so a database at revision
0006 keeps every row it had and the added column reads back as the domain's own
default of no citations. Nothing else is altered; `alembic upgrade head` on a
fresh database and on a 0006 database reach the same schema.

*Evidence and claims cite by array, not by foreign key.* PostgreSQL has no
array-of-foreign-keys, and a link table would be joined-across for a citation that
is always read and written whole with the claim that owns it.
`CandidateProfile._claims_rest_on_held_evidence` re-checks on read that every
cited id is one the same profile holds, so a dangling citation fails loudly as a
construction error rather than dangling silently — the trade
`match_evaluations.reasons` already makes for embedded evidence ids.

*The three `document_versions` CHECKs are the lifecycle made physical.* They
restate `DocumentVersion._status_agrees_with_verdict_and_artifact`: a
VALIDATED/RENDERED version carries a passing guard verdict, a REJECTED one a
failing verdict, and only a RENDERED version references an artifact. `guard_ok` is
the boolean the mapper lifts out of the JSONB `guard_report` so a CHECK can read
the verdict without a JSONB path expression. Each is written as the implication it
is, the form a CHECK (which fails only on FALSE) reads as the prose does.

Edited after `alembic revision --autogenerate` in the two ways revisions 0002
through 0006 document: no application imports for column *types*
(`sa.DateTime(timezone=True)` is the `TIMESTAMPTZ` a `Mapped[datetime]` emits,
`postgresql.ARRAY(sa.Text())` the `TEXT[]` a `Mapped[list[str]]` emits), and the
repetition factored into the helpers below. The schema-drift test compares the
result against `Base.metadata`, which is what keeps that a claim. As before, the
enum CHECKs backing the enum columns are created and dropped implicitly with their
tables, so neither `upgrade` nor `downgrade` names them.
"""
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Shared type instances: a SQLAlchemy type object carries no column state, so one
# instance can describe every column that uses it.
_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_JSONB = postgresql.JSONB()
_TEXT_ARRAY = postgresql.ARRAY(sa.Text())
_EMPTY_JSON_OBJECT = sa.text("'{}'::jsonb")
_EMPTY_TEXT_ARRAY = sa.text("'{}'::text[]")

# The members of the five enums this revision introduces, written out. Each is a
# `StrEnum` in `backend.app.domain.candidate` or `.documents`; the CHECK behind the
# column keeps the two lists agreeing, and the schema-drift test keeps this file
# and `models.py` agreeing. The full member set is listed, not only the ones the
# Phase 10 engine emits today.
_EVIDENCE_KINDS = ("CV_BULLET", "CV_SUMMARY", "EMPLOYMENT_RECORD", "DIPLOMA",
                   "CERTIFICATE", "LANGUAGE_ASSESSMENT", "PORTFOLIO_ITEM",
                   "REFERENCE", "PERMIT_DOCUMENT", "SELF_DECLARATION")
_EVIDENCE_PROVENANCES = ("CANDIDATE_PROFILE", "BASE_CV", "MANUAL_USER_INPUT",
                         "IMPORTED_CV", "PROJECT", "EMPLOYMENT_RECORD",
                         "EDUCATION_RECORD", "SYSTEM_DERIVED")
_CLAIM_TYPES = ("SKILL", "EXPERIENCE", "EDUCATION", "CERTIFICATION", "LANGUAGE",
                "AVAILABILITY", "WORK_AUTHORIZATION", "ACHIEVEMENT")
_DOCUMENT_TYPES = ("RESUME", "COVER_LETTER")
_DOCUMENT_STATUSES = ("DRAFT", "VALIDATING", "VALIDATED", "REJECTED", "RENDERED",
                      "ARCHIVED")

# `DocumentVersion._status_agrees_with_verdict_and_artifact`, as CHECK expressions.
# Verbatim copies of the `_DOCUMENT_VERSION_*` constants in `models.py`, so the
# drift test sees one expression on both sides. Each is an implication: a CHECK
# passes unless it evaluates to FALSE.
_DOCUMENT_VERSION_VERDICT_AGREES = (
    "(status NOT IN ('VALIDATED', 'RENDERED') OR guard_ok IS TRUE)"
    " AND (status <> 'REJECTED' OR guard_ok IS FALSE)"
)
_DOCUMENT_VERSION_ARTIFACT_MATCHES_STATUS = (
    "(status = 'RENDERED') = (artifact_storage_key IS NOT NULL)"
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


def _array_elements_present(table: str, column: str) -> sa.CheckConstraint:
    """No NULL and no empty string inside a `TEXT[]` — `models._text_array_elements_present`."""
    return sa.CheckConstraint(
        f"array_position({column}, NULL) IS NULL"
        f" AND array_position({column}, '') IS NULL",
        name=op.f(f"ck_{table}_{column}_elements_present"))


def upgrade() -> None:
    """Create the Phase 10 evidence and document schema, and the two new columns.

    Parents before children, so every foreign key has a table to point at when it
    is declared: `candidate_evidence`/`candidate_claims` after `candidate_profiles`
    (already present), and `document_versions` after `candidate_documents`.
    """
    # --- two additive columns on existing tables ---------------------------
    # The domain always carried these; earlier revisions dropped them only for want
    # of an evidence store. Default `'{}'` so a 0006 row reads back as the domain's
    # own "no citations", and the element-present CHECK still polices the array.
    op.add_column(
        "candidate_work_authorizations",
        sa.Column("evidence_ids", _TEXT_ARRAY, server_default=_EMPTY_TEXT_ARRAY,
                  nullable=False))
    op.create_check_constraint(
        op.f("ck_candidate_work_authorizations_evidence_ids_elements_present"),
        "candidate_work_authorizations",
        "array_position(evidence_ids, NULL) IS NULL"
        " AND array_position(evidence_ids, '') IS NULL")
    op.add_column(
        "eligibility_checks",
        sa.Column("evidence_ids", _TEXT_ARRAY, server_default=_EMPTY_TEXT_ARRAY,
                  nullable=False))
    op.create_check_constraint(
        op.f("ck_eligibility_checks_evidence_ids_elements_present"),
        "eligibility_checks",
        "array_position(evidence_ids, NULL) IS NULL"
        " AND array_position(evidence_ids, '') IS NULL")

    # --- the candidate evidence store --------------------------------------
    # No `user_id`: an evidence record is owned by exactly the profile it hangs
    # from, and `CandidateProfile` refuses another user's records, so the owner is
    # the profile's owner. `ordinal` carries the domain tuple order.
    op.create_table(
        "candidate_evidence",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("profile_id", _UUID, nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column("kind", _enum("evidence_kind", *_EVIDENCE_KINDS), nullable=False),
        sa.Column("provenance", _enum("evidence_provenance", *_EVIDENCE_PROVENANCES),
                  nullable=False),
        sa.Column("reference_key", _TEXT, nullable=True),
        sa.Column("summary", _TEXT, nullable=False),
        sa.Column("detail", _TEXT, nullable=True),
        sa.Column("issued_on", sa.Date(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("source_document", _TEXT, nullable=True),
        sa.Column("recorded_at", _TIMESTAMPTZ, nullable=False),
        *_timestamps(),
        # `CandidateEvidence._validity_window_is_ordered`, as a CHECK.
        sa.CheckConstraint("issued_on <= valid_until",
                           name=op.f("ck_candidate_evidence_validity_window_ordered")),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["candidate_profiles.id"],
            name=op.f("fk_candidate_evidence_profile_id_candidate_profiles"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_evidence")),
        sa.UniqueConstraint("profile_id", "ordinal",
                            name=op.f("uq_candidate_evidence_profile_id_ordinal")),
    )
    op.create_index("ix_candidate_evidence_profile_id", "candidate_evidence",
                    ["profile_id"], unique=False)

    op.create_table(
        "candidate_claims",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("profile_id", _UUID, nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column("claim_type", _enum("claim_type", *_CLAIM_TYPES), nullable=False),
        sa.Column("label", _TEXT, nullable=False),
        sa.Column("detail", _TEXT, nullable=True),
        sa.Column("evidence_ids", _TEXT_ARRAY, server_default=_EMPTY_TEXT_ARRAY,
                  nullable=False),
        *_timestamps(),
        _array_elements_present("candidate_claims", "evidence_ids"),
        # `CandidateClaim.evidence_ids` — `Field(min_length=1)` — as a CHECK: a
        # claim resting on nothing cannot be written even outside the model.
        sa.CheckConstraint("array_length(evidence_ids, 1) >= 1",
                           name=op.f("ck_candidate_claims_evidence_ids_present")),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["candidate_profiles.id"],
            name=op.f("fk_candidate_claims_profile_id_candidate_profiles"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_claims")),
        sa.UniqueConstraint("profile_id", "ordinal",
                            name=op.f("uq_candidate_claims_profile_id_ordinal")),
    )
    op.create_index("ix_candidate_claims_profile_id", "candidate_claims",
                    ["profile_id"], unique=False)

    # --- documents and their versions --------------------------------------
    # `user_id` alongside `candidate_profile_id` is denormalized for the reason
    # every user-owned table states: each scoped read is `WHERE user_id = ?`. All
    # three foreign keys cascade, so a deleted account, profile or posting leaves no
    # document behind. `UNIQUE (profile, opportunity, type)` makes regeneration an
    # upsert on the same document.
    op.create_table(
        "candidate_documents",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("candidate_profile_id", _UUID, nullable=False),
        sa.Column("opportunity_id", _UUID, nullable=False),
        sa.Column("document_type",
                  _enum("candidate_document_type", *_DOCUMENT_TYPES), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"], ["candidate_profiles.id"],
            name=op.f("fk_candidate_documents_candidate_profile_id_candidate_profiles"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["opportunity_id"], ["opportunities.id"],
            name=op.f("fk_candidate_documents_opportunity_id_opportunities"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_candidate_documents_user_id_users"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_documents")),
        # Named explicitly to stay within PostgreSQL's 63-character limit, as the
        # model's `UniqueConstraint` explains: the generated name would be 72.
        sa.UniqueConstraint(
            "candidate_profile_id", "opportunity_id", "document_type",
            name="uq_candidate_documents_profile_opportunity_type"),
    )
    op.create_index("ix_candidate_documents_user_id_updated_at",
                    "candidate_documents", ["user_id", "updated_at"], unique=False)
    op.create_index("ix_candidate_documents_opportunity_id", "candidate_documents",
                    ["opportunity_id"], unique=False)

    # `created_at` is domain-supplied here (the instant the attempt was made) rather
    # than a server default, so it is NOT NULL with no default; `updated_at` stays
    # persistence bookkeeping. The five `artifact_*` columns are the flattened
    # `DocumentArtifactRef`, present exactly when `status = 'RENDERED'`.
    op.create_table(
        "document_versions",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("document_id", _UUID, nullable=False),
        sa.Column("version", sa.SmallInteger(), nullable=False),
        sa.Column("status", _enum("document_status", *_DOCUMENT_STATUSES),
                  nullable=False),
        sa.Column("language", sa.String(length=2), nullable=False),
        sa.Column("content", _JSONB, server_default=_EMPTY_JSON_OBJECT,
                  nullable=False),
        sa.Column("guard_report", _JSONB, nullable=True),
        sa.Column("guard_ok", sa.Boolean(), nullable=True),
        sa.Column("generator_key", _TEXT, nullable=True),
        sa.Column("created_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("artifact_storage_key", _TEXT, nullable=True),
        sa.Column("artifact_media_type", _TEXT, nullable=True),
        sa.Column("artifact_byte_size", sa.Integer(), nullable=True),
        sa.Column("artifact_page_count", sa.SmallInteger(), nullable=True),
        sa.Column("artifact_rendered_at", _TIMESTAMPTZ, nullable=True),
        # `updated_at` from `TimestampedMixin`; `created_at` is declared above with
        # the domain columns because it is domain-supplied, not a server default.
        sa.Column("updated_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("version >= 1",
                           name=op.f("ck_document_versions_version_positive")),
        sa.CheckConstraint(
            "artifact_byte_size >= 0",
            name=op.f("ck_document_versions_artifact_byte_size_non_negative")),
        sa.CheckConstraint(
            "artifact_page_count >= 1",
            name=op.f("ck_document_versions_artifact_page_count_positive")),
        sa.CheckConstraint("language ~ '^[a-z]{2}$'",
                           name=op.f("ck_document_versions_language_format")),
        sa.CheckConstraint(
            _DOCUMENT_VERSION_VERDICT_AGREES,
            name=op.f("ck_document_versions_status_agrees_with_verdict")),
        sa.CheckConstraint(
            _DOCUMENT_VERSION_ARTIFACT_MATCHES_STATUS,
            name=op.f("ck_document_versions_artifact_matches_status")),
        sa.ForeignKeyConstraint(
            ["document_id"], ["candidate_documents.id"],
            name=op.f("fk_document_versions_document_id_candidate_documents"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_versions")),
        sa.UniqueConstraint("document_id", "version",
                            name=op.f("uq_document_versions_document_id_version")),
    )


def downgrade() -> None:
    """Return the schema to revision 0006.

    Destructive by nature: every candidate document, version, evidence record and
    claim is deleted, and the two evidence-id columns are dropped. Children before
    parents, and the two added columns and their CHECKs last, so no foreign key or
    constraint outlives what it points at. The explicit indexes are dropped
    explicitly; the enum CHECKs, unique constraints and foreign keys go with their
    tables.
    """
    op.drop_table("document_versions")
    op.drop_index("ix_candidate_documents_opportunity_id",
                  table_name="candidate_documents")
    op.drop_index("ix_candidate_documents_user_id_updated_at",
                  table_name="candidate_documents")
    op.drop_table("candidate_documents")
    op.drop_index("ix_candidate_claims_profile_id", table_name="candidate_claims")
    op.drop_table("candidate_claims")
    op.drop_index("ix_candidate_evidence_profile_id", table_name="candidate_evidence")
    op.drop_table("candidate_evidence")
    op.drop_constraint(
        op.f("ck_eligibility_checks_evidence_ids_elements_present"),
        "eligibility_checks", type_="check")
    op.drop_column("eligibility_checks", "evidence_ids")
    op.drop_constraint(
        op.f("ck_candidate_work_authorizations_evidence_ids_elements_present"),
        "candidate_work_authorizations", type_="check")
    op.drop_column("candidate_work_authorizations", "evidence_ids")
