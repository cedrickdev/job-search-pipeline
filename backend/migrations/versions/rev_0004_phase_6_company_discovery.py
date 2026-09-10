"""phase 6 company discovery

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-07 10:12:44.913207

What Phase 6 owes the schema: an employer that is a first-class identity rather
than a string on a posting, the labels other sources published it under, every
careers endpoint it has, and the provenance of how each of those came to be known.

Thirteen columns are added to `companies` and three tables are created —
`company_aliases`, `company_career_sites`, `company_discovery_records`. No column
is dropped and no existing column changes type, so a database at revision 0003
keeps every company, posting and evaluation it had.

**Three of the four candidate tables Phase 6's order lists were not created.**
`company_external_ids` is absent on purpose: the external identity Phase 6
actually produces is an ATS organization, which is a partial unique index on
`companies` (`uq_companies_ats_platform_organization_id`), and a fourth table
holding one kind of row would be a join for nothing. §15 asks for the smallest
normalized schema, not every table the phase order can name.

*`normalized_name` is added nullable, backfilled, and then made NOT NULL.* The
column is the comparison form §3 defines, and no default value can produce it:
`''` violates the CHECK that follows, and `lower(btrim(name))` — the obvious SQL
guess — is **wrong**, because `normalize_company_name` deletes joining
punctuation (`Logitech Europe S.A.` → `logitech europe sa`, not
`logitech europe s.a.`), folds accents and casefolds. A column that disagrees
with the name beside it is worse than a failed migration: every lookup by name
would silently miss the rows it was written for.

So the backfill is a data migration that runs the normalizer. Two functions are
restated here rather than imported, for the reason revision 0003 gives for its
helpers: a revision is an immutable record, and importing
`backend.app.domain.company` would make this file's output depend on whatever
that module does the next time it is edited — a re-run on a restored database
would then write values the original run did not. The cost is stated rather than
hidden: **if `normalize_company_name` ever changes, this revision does not.**
Existing rows keep the form this file computed, and a new revision has to
re-backfill. The round-trip test in `tests/test_v2_persistence_migrations.py` is
what turns that from a hope into a failure.

`normalized_domain` is backfilled in the same statement, from `website` or, when
there is none, `careers_url` — the fallback `Company.normalized_domain` performs,
restated for the same reason.

Edited after `alembic revision --autogenerate` in the two ways revision 0002
documents: no application imports for column *types* (`sa.DateTime(timezone=True)`
is the `TIMESTAMPTZ` `UtcDateTime` emits), and the repetition factored into the
helpers below. The schema-drift test compares the result against `Base.metadata`,
which is what keeps that a claim.

As in revision 0003, the CHECK constraints behind the new enum columns on
`companies` are *not* created explicitly: `op.add_column` attaches the column to a
table object, which is what makes a non-native `Enum` emit its member CHECK. They
are dropped explicitly in `downgrade`, where nothing creates them implicitly.
"""
import re
import unicodedata
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Shared type instances: a SQLAlchemy type object carries no column state, so one
# instance can describe every column that uses it.
_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_JSONB = postgresql.JSONB()
_CODE = sa.String(length=2)
# A provenance key — `^[a-z][a-z0-9_]*$`, at most 40 characters — as the domain
# types it. Width stated rather than left to TEXT because the domain states it.
_PROVENANCE_KEY = sa.String(length=40)
_EMPTY_JSON_ARRAY = sa.text("'[]'::jsonb")
_EMPTY_JSON_OBJECT = sa.text("'{}'::jsonb")

# The members of the five enums this revision introduces, written out. Each is a
# `StrEnum` in `backend.app.domain.company`; the CHECK behind the column is what
# keeps the two lists agreeing, and the schema-drift test is what keeps this file
# and `models.py` agreeing.
_IDENTITY_STATUSES = ("SEEDED", "PROVISIONAL", "VERIFIED")
_DETECTION_STATUSES = ("CONFIRMED", "LIKELY", "UNKNOWN")
_ATS_PLATFORMS = ("GREENHOUSE", "LEVER", "ASHBY")
_CAREER_SITE_KINDS = ("CAREERS_PAGE", "ATS_BOARD", "SPONTANEOUS_APPLICATION")
_SEED_KINDS = ("OPPORTUNITY", "CONFIGURED", "ATS_ORGANIZATION", "WEBSITE", "MANUAL")
_SPONTANEOUS_SUPPORT = ("SUPPORTED", "NOT_SUPPORTED", "UNKNOWN")


# The punctuation split `normalize_company_name` depends on, frozen at this
# revision. A dot or an apostrophe joins a word and is deleted outright — `S.A.`
# becomes `sa`, which is the form the CH pack's legal-suffix list spells its
# entries in — while every other non-word character separates two words and
# becomes a space.
_JOINING_PUNCTUATION = re.compile(r"[.'’ʼ´]", flags=re.UNICODE)
_SEPARATING_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

# `DetectedATS` is all-or-nothing when present, exactly as the value object is: a
# status, a detector and evidence only mean something next to a platform, and §10's
# rule that a `CONFIRMED` detection must name the organization identifier is the
# second clause.
_ATS_COMPLETE_OR_ABSENT = (
    "(ats_platform IS NULL AND ats_organization_id IS NULL"
    " AND ats_status IS NULL AND ats_detected_by IS NULL"
    " AND ats_evidence = '[]'::jsonb)"
    " OR (ats_platform IS NOT NULL AND ats_status IS NOT NULL"
    " AND ats_detected_by IS NOT NULL AND ats_evidence <> '[]'::jsonb"
    " AND (ats_status <> 'CONFIRMED' OR ats_organization_id IS NOT NULL))"
)

# `Company._the_channel_and_the_flag_agree` and
# `SpontaneousApplicationChannel._a_verdict_shows_its_work`, as one expression: the
# boolean the API exposes and the evidence-backed verdict §12 asks for have to give
# the same answer, a decided verdict has to carry evidence and an observer, and
# NOT_SUPPORTED with a URL contradicts itself.
_SPONTANEOUS_SUPPORT_MATCHES_FLAG = (
    "(spontaneous_support IS NULL"
    " AND spontaneous_url IS NULL AND spontaneous_observed_by IS NULL"
    " AND spontaneous_evidence = '[]'::jsonb)"
    " OR (spontaneous_support = 'UNKNOWN'"
    " AND accepts_spontaneous_applications IS NULL)"
    " OR (spontaneous_support = 'SUPPORTED'"
    " AND accepts_spontaneous_applications IS TRUE"
    " AND spontaneous_observed_by IS NOT NULL"
    " AND spontaneous_evidence <> '[]'::jsonb)"
    " OR (spontaneous_support = 'NOT_SUPPORTED'"
    " AND accepts_spontaneous_applications IS FALSE"
    " AND spontaneous_url IS NULL AND spontaneous_observed_by IS NOT NULL"
    " AND spontaneous_evidence <> '[]'::jsonb)"
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
    """`created_at` and `updated_at`, as `TimestampedMixin` declares them.

    The database clock sets both, so two processes inserting concurrently cannot
    disagree about order because one of them had a skewed system clock.
    """
    return (
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
    )


def _fold_accents(value: str) -> str:
    """NFKD, then drop the combining marks: `Sàrl` and `Sarl` compare equal."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _normalize_company_name(value: str) -> str:
    """The comparison form of an employer name, restated from the domain (§3).

    Unicode normalization, accent folding, case folding, punctuation handling and
    whitespace collapse — in that order, and nothing else. No word is removed:
    legal-form handling is country-aware, needs a Country Pack, and is computed on
    demand by `backend.app.companies.identity` rather than stored.
    """
    folded = _fold_accents(_JOINING_PUNCTUATION.sub("", value)).casefold()
    return _WHITESPACE.sub(" ", _SEPARATING_PUNCTUATION.sub(" ", folded)).strip()


def _normalize_domain(value: str) -> str:
    """The comparison form of a URL: host only, lower case, no `www.` (§3)."""
    candidate = value.strip()
    split = urlsplit(candidate if "//" in candidate else f"//{candidate}")
    host = (split.hostname or "").casefold().rstrip(".")
    return host.removeprefix("www.")


def _preferred_domain(website: str | None, careers_url: str | None) -> str | None:
    """`website`'s host, or the careers URL's when there is no website.

    The fallback matters more than it looks: a company discovered from an ATS board
    often has no corporate site on file, and its careers host is then the only
    domain evidence available.
    """
    for candidate in (website, careers_url):
        if candidate is None:
            continue
        host = _normalize_domain(candidate)
        if host:
            return host
    return None


def _backfill_comparison_forms() -> None:
    """Give every existing company the two projections Phase 6 reads.

    A row whose name normalizes to nothing — punctuation only — cannot be given a
    usable comparison form, and the CHECK that follows refuses an empty one, so the
    revision fails here instead of leaving a column the resolver cannot search. The
    message names the rows, because the fix is a `DELETE` or an `UPDATE` an operator
    has to decide on: inventing a name for an employer is exactly the fabrication
    the phase order forbids.
    """
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, name, website, careers_url FROM companies")).all()
    if not rows:
        # A fresh database, which is the case in CI and in every migration test.
        # Guarded rather than left to `executemany`: an empty parameter list gives
        # SQLAlchemy nothing to infer the binds from, and it raises instead of
        # updating zero rows.
        return
    unusable = [name for _id, name, _site, _careers in rows
                if not _normalize_company_name(name)]
    if unusable:
        raise RuntimeError(
            "cannot add companies.normalized_name: these existing rows have a name "
            f"that normalizes to nothing and cannot be compared against anything — "
            f"{unusable}. Rename or delete them, then re-run the migration.")
    bind.execute(
        sa.text("UPDATE companies SET normalized_name = :normalized_name,"
                " normalized_domain = :normalized_domain WHERE id = :id"),
        [{"id": row_id,
          "normalized_name": _normalize_company_name(name),
          "normalized_domain": _preferred_domain(website, careers_url)}
         for row_id, name, website, careers_url in rows])


def _extend_companies() -> None:
    """Turn a name-and-two-URLs employer into an identity Phase 6 can resolve.

    `normalized_name` and `normalized_domain` are projections of the domain
    properties of the same name, which are properties rather than fields so the
    column cannot disagree with the value beside it. The two CHECKs are what keep a
    hand-written `INSERT` from putting a display name in a comparison column.

    Neither is unique. Deciding that `Migros`, `Migros SA` and `MIGROS Vaud` are one
    employer is evidence-based work §2 puts in `companies.resolution`; a unique
    index on the normalized form would be the database making exactly the decision
    that module refuses to make. Identity is the primary key, and these columns
    narrow the shortlist `resolve` then judges.

    The ATS columns are `DetectedATS` flattened, and the CHECK is its
    all-or-nothing rule: no platform means no status, no detector and no evidence,
    and §10's requirement that a `CONFIRMED` detection name the organization
    identifier — without one, nothing can fetch the board it confirmed.

    `spontaneous_*` is `SpontaneousApplicationChannel` flattened beside the boolean
    Phase 1 already had, and its CHECK is the two domain validators as one
    expression: the flag and the verdict must give the same answer, a decided
    verdict must carry an observer and evidence, and `NOT_SUPPORTED` with a URL
    contradicts itself. NULL for the whole group is "nobody has looked", which is
    not the same statement as `UNKNOWN` and round-trips as itself.
    """
    op.add_column("companies", sa.Column("normalized_name", _TEXT, nullable=True))
    op.add_column("companies", sa.Column("normalized_domain", _TEXT, nullable=True))
    op.add_column("companies", sa.Column("country", _CODE, nullable=True))
    op.add_column("companies",
                  sa.Column("identity_status",
                            _enum("company_identity_status", *_IDENTITY_STATUSES),
                            server_default=sa.text("'SEEDED'"), nullable=False))
    op.add_column("companies", sa.Column("ats_platform",
                                         _enum("ats_platform", *_ATS_PLATFORMS),
                                         nullable=True))
    op.add_column("companies", sa.Column("ats_organization_id", _TEXT, nullable=True))
    op.add_column("companies",
                  sa.Column("ats_status",
                            _enum("ats_detection_status", *_DETECTION_STATUSES),
                            nullable=True))
    op.add_column("companies", sa.Column("ats_detected_by", _PROVENANCE_KEY,
                                         nullable=True))
    # JSONB rather than a fourth provenance table: evidence is read whole with the
    # detection it belongs to and never queried by element, and §15 asks for the
    # smallest normalized schema.
    op.add_column("companies", sa.Column("ats_evidence", _JSONB,
                                         server_default=_EMPTY_JSON_ARRAY,
                                         nullable=False))
    op.add_column("companies",
                  sa.Column("spontaneous_support",
                            _enum("spontaneous_application_support",
                                  *_SPONTANEOUS_SUPPORT), nullable=True))
    op.add_column("companies", sa.Column("spontaneous_url", _TEXT, nullable=True))
    op.add_column("companies", sa.Column("spontaneous_observed_by", _PROVENANCE_KEY,
                                         nullable=True))
    op.add_column("companies", sa.Column("spontaneous_evidence", _JSONB,
                                         server_default=_EMPTY_JSON_ARRAY,
                                         nullable=False))

    _backfill_comparison_forms()
    op.alter_column("companies", "normalized_name", existing_type=_TEXT,
                    nullable=False)

    op.create_check_constraint(
        op.f("ck_companies_normalized_name_is_comparison_form"), "companies",
        "normalized_name = lower(normalized_name) AND normalized_name <> ''")
    op.create_check_constraint(
        op.f("ck_companies_normalized_domain_is_comparison_form"), "companies",
        "normalized_domain = lower(normalized_domain) AND normalized_domain <> ''")
    op.create_check_constraint(op.f("ck_companies_country_format"), "companies",
                               "country ~ '^[A-Z]{2}$'")
    op.create_check_constraint(op.f("ck_companies_ats_complete_or_absent"),
                               "companies", _ATS_COMPLETE_OR_ABSENT)
    op.create_check_constraint(
        op.f("ck_companies_spontaneous_support_matches_flag"), "companies",
        _SPONTANEOUS_SUPPORT_MATCHES_FLAG)

    # The two shortlist lookups `resolution.name_lookup_keys` and
    # `strong_lookup_keys` drive. Without them every resolution is a sequential
    # scan of the employer table, which §23's repeated passes would turn into an
    # outage on a real database.
    op.create_index("ix_companies_normalized_name", "companies", ["normalized_name"],
                    unique=False)
    op.create_index("ix_companies_normalized_domain", "companies",
                    ["normalized_domain"], unique=False)
    # The ATS organization as an identity: unique per platform, because
    # `boards.greenhouse.io/acme` is one employer's board and two companies
    # claiming it is the duplicate `resolve` reports as AMBIGUOUS. Partial, so the
    # thousands of companies with no detected ATS do not collide on NULL.
    op.create_index("uq_companies_ats_platform_organization_id", "companies",
                    ["ats_platform", "ats_organization_id"], unique=True,
                    postgresql_where=sa.text("ats_organization_id IS NOT NULL"))


def _create_company_aliases() -> None:
    """Another label the same employer is published under (§4).

    Its own table because an alias has provenance: `LOGITECH` came from a job
    board's own spelling, `Logitech Europe S.A.` from a register, and knowing which
    is which is what lets an operator judge a doubtful merge later. Storing them as
    a list of strings on the company would lose that and would invite the code that
    overwrites the canonical name every time a source spells it differently — which
    §4 forbids.

    `UNIQUE (company_id, normalized_alias)` is what makes §23's repeated pass an
    update rather than an insert: the second sighting of `LOGITECH` moves
    `last_seen_at` and leaves `first_seen_at` alone. The primary key is derived over
    exactly that pair, so the upsert needs no prior SELECT and the unique constraint
    is the backstop rather than the mechanism.

    Both spellings are stored: `alias` is what the source published and
    `normalized_alias` is the comparison form, because showing an operator
    `Logitech Europe S.A.` is only possible if the original survived.
    """
    op.create_table(
        "company_aliases",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("company_id", _UUID, nullable=False),
        sa.Column("alias", _TEXT, nullable=False),
        sa.Column("normalized_alias", _TEXT, nullable=False),
        sa.Column("source_key", _PROVENANCE_KEY, nullable=False),
        sa.Column("first_seen_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("last_seen_at", _TIMESTAMPTZ, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "normalized_alias = lower(normalized_alias) AND normalized_alias <> ''",
            name=op.f("ck_company_aliases_normalized_alias_is_comparison_form")),
        sa.CheckConstraint("last_seen_at >= first_seen_at",
                           name=op.f("ck_company_aliases_seen_window_ordered")),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_company_aliases_company_id_companies"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_aliases")),
        sa.UniqueConstraint(
            "company_id", "normalized_alias",
            name=op.f("uq_company_aliases_company_id_normalized_alias")),
    )
    # "Which companies publish under this label" — the reverse of the lookup an
    # alias row is written for, and the one §13's resolver needs when a posting
    # names an employer by a spelling the canonical name does not use.
    op.create_index("ix_company_aliases_normalized_alias", "company_aliases",
                    ["normalized_alias"], unique=False)


def _create_company_career_sites() -> None:
    """One careers endpoint of one company (§11).

    Several per company is the normal case — a corporate page, an ATS board, a
    spontaneous-application form — which is why §11 asks for records instead of one
    `careers_url` column. That column survives as the *preferred* endpoint, which
    §11 explicitly permits.

    `last_checked_at` is nullable and Phase 6 never writes it: nothing in this phase
    fetches a URL (§9 rules out crawling), so a timestamp here would claim a check
    that never happened. The column exists for the phase that does fetch.
    """
    op.create_table(
        "company_career_sites",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("company_id", _UUID, nullable=False),
        sa.Column("url", _TEXT, nullable=False),
        sa.Column("kind", _enum("career_site_kind", *_CAREER_SITE_KINDS),
                  nullable=False),
        sa.Column("platform", _enum("career_site_platform", *_ATS_PLATFORMS),
                  nullable=True),
        sa.Column("source_key", _PROVENANCE_KEY, nullable=False),
        sa.Column("verification_status",
                  _enum("career_site_verification_status", *_DETECTION_STATUSES),
                  server_default=sa.text("'LIKELY'"), nullable=False),
        sa.Column("discovered_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("last_checked_at", _TIMESTAMPTZ, nullable=True),
        *_timestamps(),
        # A board nothing can identify the platform of is a board no source plugin
        # can read.
        sa.CheckConstraint(
            "kind <> 'ATS_BOARD' OR platform IS NOT NULL",
            name=op.f("ck_company_career_sites_ats_board_names_its_platform")),
        sa.CheckConstraint(
            "last_checked_at IS NULL OR last_checked_at >= discovered_at",
            name=op.f("ck_company_career_sites_checked_after_discovered")),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"],
            name=op.f("fk_company_career_sites_company_id_companies"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_career_sites")),
        sa.UniqueConstraint("company_id", "url",
                            name=op.f("uq_company_career_sites_company_id_url")),
    )
    op.create_index("ix_company_career_sites_company_id", "company_career_sites",
                    ["company_id"], unique=False)


def _create_company_discovery_records() -> None:
    """How one provider came to tell us about one company (§5).

    `UNIQUE (provider_key, external_id)` is §23's external identity uniqueness: the
    same provider reporting the same employer twice is one sighting, updated. The
    primary key is derived over exactly that pair, so the constraint and the key say
    the same thing and neither can drift.

    `company_id` is nullable and the foreign key is `SET NULL`. Both matter. §14
    leaves an `AMBIGUOUS` seed unlinked rather than guessing, and a sighting we keep
    is what stops the next pass rediscovering and re-refusing it; and merging two
    duplicate employers must not delete the provenance that revealed the duplication.

    `raw` is JSONB and the domain refuses credential-shaped keys before a payload
    ever reaches here (§5, §26). No CHECK mirrors that: the forbidden set is a
    substring list that will grow, and a CHECK over JSONB keys would be a second copy
    of it that silently disagrees.
    """
    op.create_table(
        "company_discovery_records",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("provider_key", _PROVENANCE_KEY, nullable=False),
        sa.Column("external_id", _TEXT, nullable=False),
        sa.Column("seed_kind", _enum("company_seed_kind", *_SEED_KINDS),
                  nullable=False),
        sa.Column("company_id", _UUID, nullable=True),
        sa.Column("company_name", _TEXT, nullable=False),
        sa.Column("source_url", _TEXT, nullable=True),
        sa.Column("discovered_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("confidence",
                  _enum("discovery_record_confidence", *_DETECTION_STATUSES),
                  server_default=sa.text("'LIKELY'"), nullable=False),
        sa.Column("raw", _JSONB, server_default=_EMPTY_JSON_OBJECT, nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"],
            name=op.f("fk_company_discovery_records_company_id_companies"),
            ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_discovery_records")),
        sa.UniqueConstraint(
            "provider_key", "external_id",
            name=op.f("uq_company_discovery_records_provider_key_external_id")),
    )
    op.create_index("ix_company_discovery_records_company_id",
                    "company_discovery_records", ["company_id"], unique=False)
    # "What did this provider find, most recent first" — the operator's question on
    # a provenance screen.
    op.create_index("ix_company_discovery_records_provider_key_discovered_at",
                    "company_discovery_records", ["provider_key", "discovered_at"],
                    unique=False)


def upgrade() -> None:
    """Create the Phase 6 schema.

    `companies` is extended first because all three new tables point at it, and the
    backfill runs before the NOT NULL and the CHECKs so that a failure there costs
    nothing but the columns.
    """
    _extend_companies()
    _create_company_aliases()
    _create_company_career_sites()
    _create_company_discovery_records()


def downgrade() -> None:
    """Return the schema to revision 0003, children first.

    Destructive by nature: every alias, careers endpoint and discovery record is
    deleted, and the thirteen columns go with the evidence they held. No company
    row is touched — an employer discovered in Phase 6 survives as the name and URLs
    revision 0002 stored, which is the point of an additive revision.

    Each CHECK is dropped before the column it references, since dropping the column
    would take the constraint with it and the reverse order would fail on a
    constraint that no longer exists.
    """
    op.drop_index("ix_company_discovery_records_provider_key_discovered_at",
                  table_name="company_discovery_records")
    op.drop_index("ix_company_discovery_records_company_id",
                  table_name="company_discovery_records")
    op.drop_table("company_discovery_records")
    op.drop_index("ix_company_career_sites_company_id",
                  table_name="company_career_sites")
    op.drop_table("company_career_sites")
    op.drop_index("ix_company_aliases_normalized_alias",
                  table_name="company_aliases")
    op.drop_table("company_aliases")

    op.drop_index("uq_companies_ats_platform_organization_id",
                  table_name="companies")
    op.drop_index("ix_companies_normalized_domain", table_name="companies")
    op.drop_index("ix_companies_normalized_name", table_name="companies")
    # The five enum CHECKs are in this list and were never created explicitly:
    # `op.add_column` attaches them, and nothing here drops them implicitly.
    for name in ("ck_companies_spontaneous_support_matches_flag",
                 "ck_companies_ats_complete_or_absent",
                 "ck_companies_country_format",
                 "ck_companies_normalized_domain_is_comparison_form",
                 "ck_companies_normalized_name_is_comparison_form",
                 "ck_companies_spontaneous_application_support",
                 "ck_companies_ats_detection_status",
                 "ck_companies_ats_platform",
                 "ck_companies_company_identity_status"):
        op.drop_constraint(op.f(name), "companies", type_="check")
    for column in ("spontaneous_evidence", "spontaneous_observed_by",
                   "spontaneous_url", "spontaneous_support", "ats_evidence",
                   "ats_detected_by", "ats_status", "ats_organization_id",
                   "ats_platform", "identity_status", "country", "normalized_domain",
                   "normalized_name"):
        op.drop_column("companies", column)
