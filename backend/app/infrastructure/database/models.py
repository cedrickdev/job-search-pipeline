"""The V2 tables.

The schema is a flattening of the Phase 1 domain, not a second model of the
problem. Value objects (`Location`, `SalaryRange`, `WorkloadRange`) become column
groups on the entity that owns them, because a `location_city` on `opportunities`
is one row read where a joined `locations` table is two, and nothing else ever
refers to an opportunity's location by identity. Entities the specification names
in their own right (`OpportunitySourceRecord`, `CompanyLocation`,
`DimensionScore`) stay separate tables.

Three rules the tables are built to hold:

**The database refuses what the domain refuses.** Every `model_validator` in
`backend/app/domain` that a column group can express is also a named CHECK — a
salary range with no bound, a workload whose minimum exceeds its maximum, a score
outside the unit interval. A domain invariant enforced only in Python survives
exactly until the first `INSERT` that does not go through Python, which the V1
importer and any future backfill script are.

**Ownership is prepared, not invented.** `companies` and `opportunities` are
shared facts and carry no `user_id`; `match_evaluations` is user-scoped and
carries one, and its foreign keys cascade from `users` so that Phase 4 can add
authentication without a schema migration on the tables that matter.

**Enums are TEXT plus a CHECK, not PostgreSQL `ENUM` types.** Adding a member to
a native enum is a DDL statement that cannot run inside a transaction with a
table rewrite, and removing one is nearly impossible; a CHECK is one
`DROP CONSTRAINT`/`ADD CONSTRAINT` pair in a forward-only migration
(docs/ENGINEERING_STANDARDS.md §Database rules).
"""
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import TextClause

from backend.app.domain.application import ApplicationState, SubmissionOutcome
from backend.app.domain.application_channel import (
    ApplicationChannel,
    HumanRequiredReason,
)
from backend.app.domain.application_event import (
    ApplicationEventActor,
    ApplicationEventType,
)
from backend.app.domain.application_failure import ApplicationFailureCode
from backend.app.domain.candidate import (
    ClaimType,
    EvidenceKind,
    EvidenceProvenance,
    WorkAuthorizationStatus,
)
from backend.app.domain.chat import (
    ChatActionExecutionOutcome,
    ChatActionKind,
    ChatActionProposalStatus,
    ChatMessageRole,
    ConversationScope,
)
from backend.app.domain.common import (
    GeocodingConfidence,
    GeoPoint,
    LanguageLevel,
    LocationPrecision,
    LocationProvenance,
    SalaryPeriod,
    Weekday,
)
from backend.app.domain.company import (
    AtsPlatform,
    CareerSiteKind,
    CompanyIdentityStatus,
    CompanySeedKind,
    DetectionStatus,
    SpontaneousApplicationSupport,
)
from backend.app.domain.decision import ApplicationDecisionKind
from backend.app.domain.documents import (
    CandidateDocumentType,
    DocumentStatus,
)
from backend.app.domain.eligibility import (
    DeterminationSource,
    EligibilityRequirement,
    EligibilityStatus,
    RuleAuthority,
)
from backend.app.domain.geo import GeocodingOutcome
from backend.app.domain.interview import (
    InterviewAnswerFormat,
    InterviewDifficulty,
    InterviewMode,
    InterviewQuestionType,
    InterviewSessionStatus,
    SessionStyle,
)
from backend.app.domain.matching import MatchDimension
from backend.app.domain.opportunity import ContractType, OpportunityType, WorkplaceMode
from backend.app.domain.policy import AutomationMode
from backend.app.domain.search import SearchAreaKind
from backend.app.domain.user import UserStatus
from backend.app.infrastructure.database.base import Base, TimestampedMixin
from backend.app.llm.connection import LLMProviderType
from backend.app.llm.contracts import TaskPurpose
from backend.app.llm.failures import LLMFailureCode
from backend.app.llm.telemetry import LLMRunStatus

# Advertised pay. `NUMERIC`, never a float: 4500.10 has to come back as 4500.10.
# Two decimals is what postings quote, and 12 integer digits covers a yearly
# salary in any currency this application is likely to see.
_MONEY: Final[Numeric[Decimal]] = Numeric(14, 2)

# JSONB rather than JSON: it is indexable and it does not re-parse text on every
# read. The empty literals are server-side defaults so a row inserted by a
# migration or by psql is as valid as one inserted by SQLAlchemy.
_EMPTY_JSON_OBJECT: Final[TextClause] = text("'{}'::jsonb")
_EMPTY_JSON_ARRAY: Final[TextClause] = text("'[]'::jsonb")

# A saved search's allow-lists are `TEXT[]`, not child tables and not JSONB. They
# are read and written whole, they are never joined, and PostgreSQL can still
# index and containment-query them (`<@`, `&&`) — which JSONB would also allow but
# with no element type at all. An array of TEXT keeps the CHECKs below expressible.
_EMPTY_TEXT_ARRAY: Final[TextClause] = text("'{}'::text[]")

_LOCATION_COMPONENTS: Final[tuple[str, ...]] = (
    "location_country", "location_region", "location_city", "location_postal_code",
    "location_point", "location_raw",
)

# Every enum column is `VARCHAR(32)`, not `VARCHAR(<longest member>)`. Left to
# itself SQLAlchemy sizes the column to the longest current value, which makes
# adding a longer member two DDL changes (the CHECK *and* the column type)
# instead of one. 32 characters is beyond any member name in the domain.
_ENUM_LENGTH: Final[int] = 32


def enum_column(enum_class: type[StrEnum], name: str) -> Enum:
    """A `StrEnum` as a TEXT column with a named CHECK on its members.

    `values_callable` stores `member.value`; SQLAlchemy's default is
    `member.name`. For today's enums the two coincide, which is exactly why it
    has to be explicit — the first member whose name and value differ would
    otherwise silently change what is written.

    `validate_strings` makes a raw string that is not a member fail in Python as
    well, so a bad value surfaces where it was written rather than as a CHECK
    violation from the driver.
    """
    return Enum(enum_class, name=name, native_enum=False, create_constraint=True,
                validate_strings=True, length=_ENUM_LENGTH,
                values_callable=lambda members: [str(m.value) for m in members])



def _unit_interval(column: str) -> CheckConstraint:
    """`Score` — `Annotated[float, Field(ge=0.0, le=1.0)]` — as a CHECK.

    `BETWEEN` evaluates to NULL for a NULL column, and a CHECK only fails on
    FALSE, so this is correct for nullable scores without a special case.
    """
    return CheckConstraint(f"{column} BETWEEN 0.0 AND 1.0",
                           name=f"{column}_in_unit_interval")


def _code_format(column: str, pattern: str) -> CheckConstraint:
    """An ISO code column constrained to the shape the domain validates."""
    return CheckConstraint(f"{column} ~ '{pattern}'", name=f"{column}_format")


def _location_provenance_coherent() -> CheckConstraint:
    """`Location._the_provenance_describes_coordinates_that_exist`, as a CHECK.

    Three clauses, one per rule the domain validator states. A precision without
    coordinates would claim an accuracy for nothing; a `GEOCODED` row with no
    point is a resolution that did not resolve; and geocoding metadata on a row
    the geocoder never touched would misreport who is responsible for the value —
    which matters here specifically, because §7 lets that metadata *veto* a later
    write.

    Restated on each of the three tables that embed `LocationColumnsMixin`
    (a mixin cannot carry `__table_args__`), which is why it is a function.
    """
    return CheckConstraint(
        "(location_precision = 'UNKNOWN' OR location_point IS NOT NULL)"
        " AND (location_provenance <> 'GEOCODED'"
        "      OR (location_point IS NOT NULL AND location_geocoder IS NOT NULL))"
        " AND (location_provenance = 'GEOCODED'"
        "      OR (location_confidence IS NULL AND location_geocoder IS NULL"
        "          AND location_geocoded_at IS NULL))",
        name="location_provenance_coherent")


def _text_array_elements_present(column: str) -> CheckConstraint:
    """No NULL and no empty string inside a `TEXT[]`.

    Every array here holds `NonEmptyStr` or an enum value in the domain, and an
    array is the one column type where PostgreSQL's NOT NULL says nothing about
    the elements: `ARRAY[NULL]::text[]` is a perfectly non-null array of one null.
    `array_position` is the containment test that works for NULL — `= ANY` would
    evaluate to NULL and pass.

    A whitespace-only element is *not* caught, and that is stated rather than
    faked: `btrim` inside a per-element test needs `unnest`, and a CHECK may not
    contain a subquery. The domain's `NonEmptyStr` is what rejects "  ".
    """
    return CheckConstraint(
        f"array_position({column}, NULL) IS NULL"
        f" AND array_position({column}, '') IS NULL",
        name=f"{column}_elements_present")


def _enum_array_members(column: str, enum_class: type[StrEnum]) -> CheckConstraint:
    """A `TEXT[]` whose every element is a member of `enum_class`.

    `<@` is array containment, which is exactly "every element of the left array
    appears in the right one" — the array form of the CHECK `enum_column` writes
    for a scalar. An empty array is contained in anything, which matches the
    domain's convention that an empty allow-list restricts nothing.
    """
    members = ", ".join(f"'{member.value}'" for member in enum_class)
    return CheckConstraint(f"{column} <@ ARRAY[{members}]::text[]",
                           name=f"{column}_members")


def _code_array_format(column: str, pattern: str) -> CheckConstraint:
    """A `TEXT[]` whose every element matches `pattern`.

    `unnest` would need a subquery, which a CHECK may not contain, so the array is
    joined and the joined form is matched: a separator that cannot appear inside
    the pattern turns "every element matches" into one regular expression.
    `array_to_string` is IMMUTABLE, which is what makes it legal here.
    """
    return CheckConstraint(
        f"array_to_string({column}, ',') ~ '^({pattern}(,{pattern})*)?$'",
        name=f"{column}_format")


def _sha256_hex(column: str) -> CheckConstraint:
    """A stored token digest, constrained to the shape `SessionTokenDigest` accepts.

    The database repeating the domain's validator is the point: a code path that
    wrote a raw 43-character `token_urlsafe` value into a digest column would be
    storing a live credential in the clear, and this is the layer that would still
    refuse it if the write did not go through the model.
    """
    return CheckConstraint(f"{column} ~ '^[0-9a-f]{{64}}$'", name=f"{column}_format")


# `WorkloadRange` bounds, shared by `opportunities` (what the posting offers) and
# `search_profiles` (what the candidate wants). One pair of expressions for both,
# because the two column groups mean the same thing and a saved search whose
# workload the database accepted but an opportunity's would refuse would be a
# filter that can never match anything.
_WORKLOAD_PERCENT_RANGE: Final[str] = (
    "workload_min_percent BETWEEN 1 AND 100"
    " AND workload_max_percent BETWEEN 1 AND 100"
)

_WORKLOAD_HOURS_RANGE: Final[str] = (
    "workload_min_weekly_hours > 0 AND workload_min_weekly_hours <= 168"
    " AND workload_max_weekly_hours > 0 AND workload_max_weekly_hours <= 168"
)




class LocationColumnsMixin:
    """`Location` flattened, with one definition for both tables that embed it.

    The `location_` prefix is redundant on `company_locations` and load-bearing
    on `opportunities`, where a bare `raw` column would be confused with the
    source record's raw payload and a bare `country` with a future country of
    incorporation. One prefixed mixin, used twice, is worth the redundancy: the
    two tables cannot drift, and the geography column is declared identically in
    both — which is what makes a single radius query work over either.

    Every component is nullable because discovery is incremental: a posting that
    says only "Lausanne" stores `location_city` and nothing else, and the
    geocoding pass of Phase 7 fills `location_point` later.

    Phase 7 added the five `location_provenance`/`location_precision`/… columns.
    They describe the *coordinates*, not the place, which is why they sit outside
    `_LOCATION_COMPONENTS`: a row with a provenance and no components still
    locates nothing. Their job is to make the enrichment pass re-runnable — it can
    read that a point came from the employer itself and leave it alone (§7, §39) —
    and to stop a city centroid being drawn as a street address (§32).

    The two defaults say what an un-geocoded row means: the source provided
    whatever is there, and nothing claims a precision. They are server-side so
    that revision 0005 can backfill the existing rows with the same statement that
    adds the column, and so a row written by psql is as honest as one written here.
    """

    location_country: Mapped[str | None] = mapped_column(String(2))
    location_region: Mapped[str | None]
    location_city: Mapped[str | None]
    location_postal_code: Mapped[str | None]
    location_point: Mapped[GeoPoint | None]
    location_raw: Mapped[str | None]

    location_provenance: Mapped[LocationProvenance] = mapped_column(
        enum_column(LocationProvenance, "location_provenance"),
        default=LocationProvenance.SOURCE_PROVIDED,
        server_default=text(f"'{LocationProvenance.SOURCE_PROVIDED.value}'"))
    location_precision: Mapped[LocationPrecision] = mapped_column(
        enum_column(LocationPrecision, "location_precision"),
        default=LocationPrecision.UNKNOWN,
        server_default=text(f"'{LocationPrecision.UNKNOWN.value}'"))
    location_confidence: Mapped[GeocodingConfidence | None] = mapped_column(
        enum_column(GeocodingConfidence, "location_confidence"))
    location_geocoder: Mapped[str | None]
    location_geocoded_at: Mapped[datetime | None]


# A SHA-256 rendered as lower-case hex is always 64 characters, so the column is
# fixed-width by nature. `String(64)` rather than TEXT because the width is a real
# constraint here and stating it lets the database reject a wrong-length value
# without consulting the CHECK.
_DIGEST_LENGTH: Final[int] = 64


class UserRow(TimestampedMixin, Base):
    """One account: the login identifier, the credential, and the login counters.

    Phase 2 created this table with a display name and nothing else, so that
    user-owned rows could carry a real foreign key before anything could
    authenticate. Phase 4 fills it in.

    Two columns are worth explaining. `email` is unique *and* CHECKed to be its own
    normalized form: the unique index alone would let `Ada@x.com` and `ada@x.com`
    both register, since PostgreSQL compares TEXT case-sensitively, and the whole
    point of `normalize_email` in the domain is that one person has one account.
    Enforcing the normalized form here is what makes the index mean what it says.

    `password_hash` is TEXT with no width. Argon2id's encoded form is 97 characters
    today, and a parameter bump — which `check_needs_rehash` exists to make routine
    — changes that length. A `VARCHAR(97)` would turn the next cost increase into a
    migration.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email"),
        # Trimmed, lower-cased, and containing an `@` that is neither first nor
        # last. Not an attempt to validate an address — `EmailStr` does that with
        # the `email-validator` package — but enough that a row written by psql or
        # by a future backfill cannot be a login identifier nobody can ever match.
        CheckConstraint(
            "email = lower(btrim(email))"
            " AND position('@' in email) > 1"
            " AND position('@' in email) < length(email)",
            name="email_normalized"),
        CheckConstraint("failed_login_attempts >= 0",
                        name="failed_login_attempts_non_negative"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    email: Mapped[str]
    password_hash: Mapped[str]
    status: Mapped[UserStatus] = mapped_column(
        enum_column(UserStatus, "user_status"),
        server_default=text(f"'{UserStatus.ACTIVE.value}'"))
    display_name: Mapped[str | None]
    # NULL for every account in Phase 4: nothing sends mail yet. The column exists
    # so the authorization layer has somewhere truthful to look later, and it is
    # deliberately not defaulted to `now()` — a verified-at that nothing verified
    # is worse than a NULL.
    email_verified_at: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]
    failed_login_attempts: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("0"))
    locked_until: Mapped[datetime | None]
    onboarding_completed_at: Mapped[datetime | None]

    sessions: Mapped[list["UserSessionRow"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="UserSessionRow.issued_at")


class UserSessionRow(TimestampedMixin, Base):
    """One authenticated browser, as a revocable server-side row.

    What is stored is a digest, never a token: the value the browser holds exists
    only in the response that set the cookie (docs/AUTHENTICATION.md §Sessions). So
    a leaked dump of this table is not a set of working credentials, and the CHECKs
    below are what keep it that way — a 43-character `token_urlsafe` value written
    here by mistake violates `token_digest_format` instead of persisting.

    There is no IP address and no user-agent column. Both are personal data with
    retention rules of their own, neither is used by any decision in this phase,
    and a session table is the easiest place to accumulate a request log nobody
    asked for (docs/ENGINEERING_STANDARDS.md §Security).

    `csrf_token_digest` lives here rather than in its own table because a CSRF
    token has exactly the lifetime of the session it protects. Keeping it beside
    the session is also what turns the double-submit cookie into a comparison
    against server-side state: a token planted in the cookie jar by a sibling host
    matches the cookie it was planted in, and nothing else.
    """

    __tablename__ = "user_sessions"
    __table_args__ = (
        # The lookup every authenticated request performs, so it is the constraint
        # that also serves as its index. Unique because two sessions sharing a
        # token digest would be two browsers holding one credential.
        UniqueConstraint("token_digest"),
        _sha256_hex("token_digest"),
        _sha256_hex("csrf_token_digest"),
        # `UserSession._the_two_digests_differ`: issuing one secret twice would
        # hand the session token to any script that can read the CSRF cookie.
        CheckConstraint("token_digest <> csrf_token_digest",
                        name="digests_are_independent"),
        CheckConstraint("expires_at > issued_at", name="window_is_forward"),
        CheckConstraint("last_seen_at >= issued_at", name="last_seen_after_issued"),
        # "Revoke my other sessions" and the expiry sweep both read by user; the
        # sweep also reads by expiry, hence the second column.
        Index("ix_user_sessions_user_id_expires_at", "user_id", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    token_digest: Mapped[str] = mapped_column(String(_DIGEST_LENGTH))
    csrf_token_digest: Mapped[str] = mapped_column(String(_DIGEST_LENGTH))
    issued_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    # NULL means live. A `revoked` boolean would lose *when*, which is the column a
    # security question ("was this session active at 14:05?") actually needs.
    revoked_at: Mapped[datetime | None]

    user: Mapped["UserRow"] = relationship(back_populates="sessions", lazy="raise")



class CandidateProfileRow(LocationColumnsMixin, TimestampedMixin, Base):
    """What the platform knows about one candidate, minus the evidence store.

    Separate from `users` because one person legitimately searches under more than
    one profile (a student job and a graduate role weigh education differently),
    and an evaluation is against a profile, not against an account. Onboarding
    creates one profile per account and derives its id from the account's, so a
    double-submitted onboarding collides on the primary key instead of producing
    two profiles; a second profile would be an explicit act with a fresh id.

    `evidence` and `claims` are child tables since Phase 10: the evidence store
    the truth guard rests on. Each `candidate_claims` row cites the evidence it
    rests on as a `TEXT[]` of evidence ids rather than a link table — the citation
    is read and written whole with the claim, never joined across, and
    `CandidateProfile._claims_rest_on_held_evidence` re-checks on the way back out
    that every cited id is one this profile holds, so a dangling citation surfaces
    as a loud construction error rather than a silent orphan.

    Availability is flattened into five columns plus a child table for the weekly
    slots, and all five are nullable: an all-NULL group with no slots reads back as
    `None`, the same convention `_read_location` follows. `Availability()` with
    nothing set is therefore indistinguishable from "not stated", which is what it
    means anyway.
    """

    __tablename__ = "candidate_profiles"
    __table_args__ = (
        _code_format("location_country", "^[A-Z]{2}$"),
        _location_provenance_coherent(),
        CheckConstraint(
            "availability_earliest_start <= availability_latest_end",
            name="availability_window_ordered"),
        CheckConstraint(
            "availability_min_weekly_hours <= availability_max_weekly_hours",
            name="availability_hours_ordered"),
        CheckConstraint(
            "availability_min_weekly_hours BETWEEN 0 AND 168"
            " AND availability_max_weekly_hours > 0"
            " AND availability_max_weekly_hours <= 168",
            name="availability_hours_range"),
        CheckConstraint("availability_notice_period_days >= 0",
                        name="availability_notice_non_negative"),
        Index("ix_candidate_profiles_user_id", "user_id"),
        # The profile's own coordinates, so Phase 7 can answer "how far is this
        # posting from home?" with the same index type the postings use.
        Index("ix_candidate_profiles_location_point", "location_point",
              postgresql_using="gist"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    display_name: Mapped[str]
    headline: Mapped[str | None]

    availability_earliest_start: Mapped[date | None]
    availability_latest_end: Mapped[date | None]
    availability_min_weekly_hours: Mapped[float | None]
    availability_max_weekly_hours: Mapped[float | None]
    availability_notice_period_days: Mapped[int | None] = mapped_column(SmallInteger)

    languages: Mapped[list["CandidateLanguageRow"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="CandidateLanguageRow.ordinal")
    work_authorizations: Mapped[list["CandidateWorkAuthorizationRow"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="CandidateWorkAuthorizationRow.ordinal")
    availability_slots: Mapped[list["CandidateAvailabilitySlotRow"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="CandidateAvailabilitySlotRow.ordinal")
    evidence: Mapped[list["CandidateEvidenceRow"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="CandidateEvidenceRow.ordinal")
    claims: Mapped[list["CandidateClaimRow"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="CandidateClaimRow.ordinal")


# The domain's collections are tuples, and tuple order is information the candidate
# supplied — the first language listed is the one they lead with. A child table has
# no inherent order, so every one of them carries an explicit `ordinal` and every
# relationship sorts by it. Without that, a profile read back would be equal to the
# one written only by luck, and the round-trip tests would be asserting nothing.
_ORDINAL_DOC: Final[str] = "position in the domain tuple, so order survives a round trip"

# The three child tables below say `profile_id`, not `candidate_profile_id` like
# `match_evaluations` does. The qualifier is redundant inside a table already named
# `candidate_…`, and it is also unaffordable: the naming convention derives
# `fk_candidate_work_authorizations_candidate_profile_id_candidate_profiles`, which
# is 72 characters, and PostgreSQL truncates at 63 — a silently truncated name with
# a hash suffix is one a migration cannot reliably drop.
_PROFILE_FK: Final[str] = "candidate_profiles.id"


class CandidateLanguageRow(TimestampedMixin, Base):
    """One language the candidate speaks, at one CEFR level.

    A child table rather than JSONB because docs/V2_SPECIFICATION.md §9 asks for
    "candidates who read German at B2 or better", which is a comparison across
    rows, and because `LanguageLevel` is an enum the database can then police.
    """

    __tablename__ = "candidate_languages"
    __table_args__ = (
        # `CandidateProfile._one_entry_per_language_and_country`, as a constraint.
        # It also indexes the foreign key, which is why there is no separate index.
        UniqueConstraint("profile_id", "language"),
        _code_format("language", "^[a-z]{2}$"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey(_PROFILE_FK, ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(SmallInteger, doc=_ORDINAL_DOC)
    language: Mapped[str] = mapped_column(String(2))
    level: Mapped[LanguageLevel] = mapped_column(
        enum_column(LanguageLevel, "language_level"))

    profile: Mapped["CandidateProfileRow"] = relationship(
        back_populates="languages", lazy="raise")


class CandidateWorkAuthorizationRow(TimestampedMixin, Base):
    """The candidate's right to work in one country.

    `permit_hours_cap` is the column that makes a class of eligibility
    deterministic: a student permit capped at 15h/week makes a 20h/week job
    *ineligible* rather than a poor schedule fit. It is a legal fact a Country Pack
    supplies in Phase 5; this table only carries it.

    `evidence_ids` is a `TEXT[]` of the evidence records that attest the permit
    (Phase 10 gave the store a home). It is not a foreign-key array — PostgreSQL
    has none — but the ids all belong to the same profile's `candidate_evidence`,
    and `CandidateProfile._claims_rest_on_held_evidence` re-checks that on read, so
    a citation the profile does not hold fails loudly rather than dangling. Empty is
    the honest default: a status can be recorded before its permit document is.
    """

    __tablename__ = "candidate_work_authorizations"
    __table_args__ = (
        UniqueConstraint("profile_id", "country"),
        _code_format("country", "^[A-Z]{2}$"),
        CheckConstraint("permit_hours_cap > 0 AND permit_hours_cap <= 168",
                        name="permit_hours_cap_range"),
        _text_array_elements_present("evidence_ids"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey(_PROFILE_FK, ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(SmallInteger, doc=_ORDINAL_DOC)
    country: Mapped[str] = mapped_column(String(2))
    status: Mapped[WorkAuthorizationStatus] = mapped_column(
        enum_column(WorkAuthorizationStatus, "work_authorization_status"))
    permit_label: Mapped[str | None]
    valid_until: Mapped[date | None]
    permit_hours_cap: Mapped[float | None]
    evidence_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)

    profile: Mapped["CandidateProfileRow"] = relationship(
        back_populates="work_authorizations", lazy="raise")


class CandidateAvailabilitySlotRow(TimestampedMixin, Base):
    """A recurring window the candidate can work, in whole local hours.

    No timezone, matching `WeeklyAvailabilitySlot`: "Saturday mornings" means it in
    the shop's local time, and storing an instant would invent precision nobody
    supplied.

    Overlap between two slots on the same day is a domain invariant that no CHECK
    can express — a row constraint cannot see another row, and an exclusion
    constraint would need a range type the domain does not use. The exact-duplicate
    case *is* expressible, and is refused.
    """

    __tablename__ = "candidate_availability_slots"
    __table_args__ = (
        UniqueConstraint("profile_id", "weekday", "start_hour"),
        CheckConstraint("start_hour BETWEEN 0 AND 23 AND end_hour BETWEEN 1 AND 24",
                        name="slot_hours_range"),
        CheckConstraint("start_hour < end_hour", name="slot_hours_ordered"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey(_PROFILE_FK, ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(SmallInteger, doc=_ORDINAL_DOC)
    weekday: Mapped[Weekday] = mapped_column(enum_column(Weekday, "weekday"))
    start_hour: Mapped[int] = mapped_column(SmallInteger)
    end_hour: Mapped[int] = mapped_column(SmallInteger)

    profile: Mapped["CandidateProfileRow"] = relationship(
        back_populates="availability_slots", lazy="raise")


class CandidateEvidenceRow(TimestampedMixin, Base):
    """One record attesting something about the candidate (Phase 10).

    The V2 form of a V1 base-library bullet, and the store the truth guard rests
    on: every claim and every generated document line cites the id of a row here.
    `reference_key` keeps the V1 bullet id ("acme-checkout"), so importing the base
    CV loses no link back to the YAML.

    No `user_id` column: an evidence record is owned by exactly the profile it
    hangs from, and `CandidateProfile` refuses to aggregate another user's records,
    so the owner is the profile's owner and a second copy on the row could only
    disagree. `source_document` is a label (a path, a URL), never file bytes — the
    domain describes where proof lives, it does not carry it.
    """

    __tablename__ = "candidate_evidence"
    __table_args__ = (
        UniqueConstraint("profile_id", "ordinal"),
        CheckConstraint("issued_on <= valid_until", name="validity_window_ordered"),
        Index("ix_candidate_evidence_profile_id", "profile_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey(_PROFILE_FK, ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(SmallInteger, doc=_ORDINAL_DOC)
    kind: Mapped[EvidenceKind] = mapped_column(
        enum_column(EvidenceKind, "evidence_kind"))
    provenance: Mapped[EvidenceProvenance] = mapped_column(
        enum_column(EvidenceProvenance, "evidence_provenance"))
    reference_key: Mapped[str | None]
    summary: Mapped[str]
    detail: Mapped[str | None]
    issued_on: Mapped[date | None]
    valid_until: Mapped[date | None]
    source_document: Mapped[str | None]
    recorded_at: Mapped[datetime]

    profile: Mapped["CandidateProfileRow"] = relationship(
        back_populates="evidence", lazy="raise")


class CandidateClaimRow(TimestampedMixin, Base):
    """Something the platform will say on the candidate's behalf, and its backing.

    `evidence_ids` is a `TEXT[]` and is CHECK-constrained to at least one element,
    which is `CandidateClaim.evidence_ids`' `min_length=1` made physical: an
    unsupported claim cannot reach the table any more than it can be constructed.
    The ids point into the same profile's `candidate_evidence`; that they are held
    is re-checked by `CandidateProfile` on read rather than by a foreign key, for
    the reason the column comment on `candidate_work_authorizations.evidence_ids`
    gives.

    No `user_id` column, for the same reason `candidate_evidence` has none.
    """

    __tablename__ = "candidate_claims"
    __table_args__ = (
        UniqueConstraint("profile_id", "ordinal"),
        _text_array_elements_present("evidence_ids"),
        # `CandidateClaim.evidence_ids` — `Field(min_length=1)` — as a CHECK, so a
        # claim resting on nothing cannot be written even outside the model.
        CheckConstraint("array_length(evidence_ids, 1) >= 1",
                        name="evidence_ids_present"),
        Index("ix_candidate_claims_profile_id", "profile_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey(_PROFILE_FK, ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(SmallInteger, doc=_ORDINAL_DOC)
    claim_type: Mapped[ClaimType] = mapped_column(
        enum_column(ClaimType, "claim_type"))
    label: Mapped[str]
    detail: Mapped[str | None]
    evidence_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)

    profile: Mapped["CandidateProfileRow"] = relationship(
        back_populates="claims", lazy="raise")



class SearchProfileRow(TimestampedMixin, Base):
    """One saved search belonging to one user.

    This is what replaces V1's `config/searches.yaml`, which is a single-user file
    of free-text `locations` and a keyword blacklist. Two differences carry the
    phase: the areas are a child table with real geometry rather than strings, and
    the filters are allow-lists of typed values rather than excluded words — V1
    excludes "stage" and "apprenti", which are exactly the opportunity types
    docs/V2_SPECIFICATION.md §7 makes first-class.

    **An empty array means "no restriction", not "match nothing".** The domain
    states the convention once and every filter here inherits it, which is why the
    server defaults are `'{}'` rather than NULL: a nullable array would give the
    same intent two representations, and the first query written with `= ANY` on
    the NULL one would silently match nothing.
    """

    __tablename__ = "search_profiles"
    __table_args__ = (
        CheckConstraint("workload_min_percent <= workload_max_percent",
                        name="workload_percent_ordered"),
        CheckConstraint("workload_min_weekly_hours <= workload_max_weekly_hours",
                        name="workload_hours_ordered"),
        CheckConstraint(_WORKLOAD_PERCENT_RANGE, name="workload_percent_range"),
        CheckConstraint(_WORKLOAD_HOURS_RANGE, name="workload_hours_range"),
        _text_array_elements_present("queries"),
        _text_array_elements_present("title_keywords"),
        _text_array_elements_present("excluded_keywords"),
        _text_array_elements_present("source_keys"),
        _enum_array_members("opportunity_types", OpportunityType),
        _enum_array_members("contract_types", ContractType),
        _enum_array_members("workplace_modes", WorkplaceMode),
        _code_array_format("posting_languages", "[a-z]{2}"),
        # Discovery reads "every active search", and the account page reads "my
        # searches". One index serves both, because a partial index on `is_active`
        # could not answer the second.
        Index("ix_search_profiles_user_id_is_active", "user_id", "is_active"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str]
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))

    queries: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)
    title_keywords: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)
    excluded_keywords: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)
    opportunity_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)
    contract_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)
    workplace_modes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)
    posting_languages: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)
    source_keys: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)

    workload_min_percent: Mapped[int | None] = mapped_column(SmallInteger)
    workload_max_percent: Mapped[int | None] = mapped_column(SmallInteger)
    workload_min_weekly_hours: Mapped[float | None]
    workload_max_weekly_hours: Mapped[float | None]

    areas: Mapped[list["SearchAreaRow"]] = relationship(
        back_populates="search_profile", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise", order_by="SearchAreaRow.ordinal")


# `SearchArea` is a discriminated union of three shapes, and a single table with
# nullable columns is the flattening every ORM reaches for. What makes it safe here
# is that the union's exhaustiveness is written down as a constraint: each kind
# names exactly which columns must be present and which must be absent, so a row
# cannot be a RADIUS area with no centre or a COUNTRY area with a stray radius.
# Without this, the discriminator would be a label and the coherence the domain
# guarantees would be lost at the first `INSERT` that did not go through Pydantic.
_AREA_SHAPE_MATCHES_KIND: Final[str] = (
    f"(kind = '{SearchAreaKind.COUNTRY.value}'"
    " AND country IS NOT NULL AND center IS NULL AND radius_km IS NULL)"
    f" OR (kind = '{SearchAreaKind.RADIUS.value}'"
    " AND country IS NULL AND center IS NOT NULL AND radius_km IS NOT NULL)"
    f" OR (kind = '{SearchAreaKind.REMOTE_ONLY.value}'"
    " AND center IS NULL AND radius_km IS NULL)"
)


class SearchAreaRow(TimestampedMixin, Base):
    """Where one saved search looks: a country, a radius, or nowhere in particular.

    `ordinal` is the natural key rather than a convenience: two radius areas can
    differ only by their radius, so there is nothing else to identify a row by, and
    `UNIQUE (search_profile_id, ordinal)` is what makes re-saving a search an update
    of the same rows instead of a delete-and-reinsert.
    """

    __tablename__ = "search_areas"
    __table_args__ = (
        UniqueConstraint("search_profile_id", "ordinal"),
        CheckConstraint(_AREA_SHAPE_MATCHES_KIND, name="shape_matches_kind"),
        CheckConstraint("radius_km > 0 AND radius_km <= 500", name="radius_km_range"),
        _code_format("country", "^[A-Z]{2}$"),
        # The same GiST index the opportunities carry, so Phase 7's `ST_DWithin`
        # can start from either end of the question.
        Index("ix_search_areas_center", "center", postgresql_using="gist"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    search_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("search_profiles.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(SmallInteger, doc=_ORDINAL_DOC)
    kind: Mapped[SearchAreaKind] = mapped_column(
        enum_column(SearchAreaKind, "search_area_kind"))
    country: Mapped[str | None] = mapped_column(String(2))
    center: Mapped[GeoPoint | None]
    radius_km: Mapped[float | None]
    label: Mapped[str | None]

    search_profile: Mapped["SearchProfileRow"] = relationship(
        back_populates="areas", lazy="raise")






# A provenance key — a source or provider key — as `ProvenanceKey` validates it:
# `^[a-z][a-z0-9_]*$`, at most 40 characters. Width stated rather than left to TEXT
# because the domain states it, and a 200-character value in this column would be a
# bug somewhere upstream rather than a long name.
_PROVENANCE_KEY_LENGTH: Final[int] = 40

# `DetectedATS` is all-or-nothing when present, exactly as the value object is: a
# status, a detector and evidence only mean something next to a platform, and §10's
# rule that a `CONFIRMED` detection must name the organization identifier is the
# second clause. Without this the flattening would allow a status asserting
# confidence about a platform nobody detected.
_ATS_COMPLETE_OR_ABSENT: Final[str] = (
    "(ats_platform IS NULL AND ats_organization_id IS NULL"
    " AND ats_status IS NULL AND ats_detected_by IS NULL"
    " AND ats_evidence = '[]'::jsonb)"
    " OR (ats_platform IS NOT NULL AND ats_status IS NOT NULL"
    " AND ats_detected_by IS NOT NULL AND ats_evidence <> '[]'::jsonb"
    f" AND (ats_status <> '{DetectionStatus.CONFIRMED.value}'"
    " OR ats_organization_id IS NOT NULL))"
)

# `Company._the_channel_and_the_flag_agree` and
# `SpontaneousApplicationChannel._a_verdict_shows_its_work`, as one expression: the
# boolean the API exposes and the evidence-backed verdict §12 asks for have to give
# the same answer, a decided verdict has to carry evidence and an observer, and
# NOT_SUPPORTED with a URL contradicts itself.
_SPONTANEOUS_SUPPORT_MATCHES_FLAG: Final[str] = (
    "(spontaneous_support IS NULL"
    " AND spontaneous_url IS NULL AND spontaneous_observed_by IS NULL"
    " AND spontaneous_evidence = '[]'::jsonb)"
    f" OR (spontaneous_support = '{SpontaneousApplicationSupport.UNKNOWN.value}'"
    " AND accepts_spontaneous_applications IS NULL)"
    f" OR (spontaneous_support = '{SpontaneousApplicationSupport.SUPPORTED.value}'"
    " AND accepts_spontaneous_applications IS TRUE"
    " AND spontaneous_observed_by IS NOT NULL"
    " AND spontaneous_evidence <> '[]'::jsonb)"
    f" OR (spontaneous_support = '{SpontaneousApplicationSupport.NOT_SUPPORTED.value}'"
    " AND accepts_spontaneous_applications IS FALSE"
    " AND spontaneous_url IS NULL AND spontaneous_observed_by IS NOT NULL"
    " AND spontaneous_evidence <> '[]'::jsonb)"
)


class CompanyRow(TimestampedMixin, Base):
    """A canonical employer.

    `name` is indexed but not unique, and `normalized_name` is indexed and not
    unique either. Two legally distinct companies share a name often enough that a
    unique constraint would reject real data — and, more to the point, deciding that
    "Migros", "Migros SA" and "MIGROS Vaud" are one employer is evidence-based work
    the database cannot do: §2 forbids merging on name similarity, so a unique index
    on the normalized form would be the database silently making exactly the decision
    `backend.app.companies.resolution` refuses to make. Identity comes from the
    primary key, and `normalized_name` is what narrows the shortlist that `resolve`
    then judges.

    Phase 6 added thirteen columns and every one of them is read off the domain
    object rather than computed here. `normalized_name` and `normalized_domain` are
    projections of `Company.normalized_name`/`normalized_domain` — properties, not
    fields, so the column cannot disagree with the name beside it — and the CHECK
    that each equals its own lower-cased form is what keeps a hand-written `INSERT`
    from putting a display name in the comparison column.

    `accepts_spontaneous_applications` is nullable because the domain models it as
    three-valued — NULL is "nobody has looked", which an application strategy must
    not read as "no" — and `spontaneous_*` beside it is the evidence §12 requires
    for a decided answer. `ck_companies_spontaneous_support_matches_flag` is
    `Company._the_channel_and_the_flag_agree` as a constraint: the two
    representations exist because Phase 1's boolean is what the API exposes, and a
    row where they contradict each other would make the answer depend on which
    column the reader picked.

    The ATS columns are `DetectedATS` flattened, with the same all-or-nothing rule
    the value object has: no platform means no status, no evidence and no
    organization, and a `CONFIRMED` detection must name the organization or nothing
    can fetch the board.
    """

    __tablename__ = "companies"
    __table_args__ = (
        # `Company._a_name_has_a_comparison_form`, and the reason the column is NOT
        # NULL: a company whose name normalizes to nothing can never be compared
        # against anything, so it is not a usable identity.
        CheckConstraint("normalized_name = lower(normalized_name)"
                        " AND normalized_name <> ''",
                        name="normalized_name_is_comparison_form"),
        CheckConstraint("normalized_domain = lower(normalized_domain)"
                        " AND normalized_domain <> ''",
                        name="normalized_domain_is_comparison_form"),
        _code_format("country", "^[A-Z]{2}$"),
        CheckConstraint(_ATS_COMPLETE_OR_ABSENT, name="ats_complete_or_absent"),
        CheckConstraint(_SPONTANEOUS_SUPPORT_MATCHES_FLAG,
                        name="spontaneous_support_matches_flag"),
        Index("ix_companies_name", "name"),
        # The two shortlist lookups `resolution.name_lookup_keys` and
        # `strong_lookup_keys` drive. Without them every resolution is a sequential
        # scan of the whole employer table, which is fine on an empty development
        # database and is the outage §23's repeated passes would cause on a real one.
        Index("ix_companies_normalized_name", "normalized_name"),
        Index("ix_companies_normalized_domain", "normalized_domain"),
        # The ATS organization as an identity: unique per platform, because
        # `boards.greenhouse.io/acme` is one employer's board and two companies
        # claiming it is the duplicate `resolve` reports as AMBIGUOUS. Partial, so
        # the thousands of companies with no detected ATS do not collide on NULL.
        Index("uq_companies_ats_platform_organization_id",
              "ats_platform", "ats_organization_id", unique=True,
              postgresql_where=text("ats_organization_id IS NOT NULL")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str]
    # A projection of `Company.normalized_name`. NOT NULL: every company has one by
    # construction, and a nullable column would invite a query that misses rows.
    normalized_name: Mapped[str]
    website: Mapped[str | None]
    careers_url: Mapped[str | None]
    # A projection of `Company.normalized_domain`, which falls back to the careers
    # host when there is no website. Nullable because plenty of employers arrive
    # with neither.
    normalized_domain: Mapped[str | None]
    country: Mapped[str | None] = mapped_column(String(2))
    identity_status: Mapped[CompanyIdentityStatus] = mapped_column(
        enum_column(CompanyIdentityStatus, "company_identity_status"),
        server_default=text(f"'{CompanyIdentityStatus.SEEDED.value}'"))
    ats_platform: Mapped[AtsPlatform | None] = mapped_column(
        enum_column(AtsPlatform, "ats_platform"))
    ats_organization_id: Mapped[str | None]
    ats_status: Mapped[DetectionStatus | None] = mapped_column(
        enum_column(DetectionStatus, "ats_detection_status"))
    ats_detected_by: Mapped[str | None] = mapped_column(
        String(_PROVENANCE_KEY_LENGTH))
    # `tuple[Evidence, ...]` as JSONB, the same shape `reasons_to_json` writes for a
    # match evaluation. A child table would be the third provenance table in this
    # phase for a collection that is never queried by its elements — §15 asks for the
    # smallest normalized schema, and evidence is read whole with its detection.
    ats_evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=_EMPTY_JSON_ARRAY)
    accepts_spontaneous_applications: Mapped[bool | None]
    spontaneous_support: Mapped[SpontaneousApplicationSupport | None] = mapped_column(
        enum_column(SpontaneousApplicationSupport, "spontaneous_application_support"))
    spontaneous_url: Mapped[str | None]
    spontaneous_observed_by: Mapped[str | None] = mapped_column(
        String(_PROVENANCE_KEY_LENGTH))
    spontaneous_evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=_EMPTY_JSON_ARRAY)

    locations: Mapped[list["CompanyLocationRow"]] = relationship(
        back_populates="company", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        # Deterministic order, so a company read twice produces an equal domain
        # object and a round-trip test can compare tuples directly.
        order_by="CompanyLocationRow.id")


class CompanyAliasRow(TimestampedMixin, Base):
    """Another label the same employer is published under (§4).

    Its own table because an alias has provenance, and `UNIQUE (company_id,
    normalized_alias)` is what makes §23's repeated pass an update rather than an
    insert: the second sighting of `LOGITECH` moves `last_seen_at` and leaves
    `first_seen_at` alone. The primary key is derived (`company_alias_id`, uuid5 over
    the same pair), so the upsert can be written without a prior SELECT and the
    unique constraint is the backstop rather than the mechanism.

    `alias` keeps the spelling the source used and `normalized_alias` is the
    comparison form. Both, because §4's rule is that another source's label must not
    overwrite the canonical name — showing an operator `Logitech Europe S.A.` is only
    possible if the original spelling survived.
    """

    __tablename__ = "company_aliases"
    __table_args__ = (
        UniqueConstraint("company_id", "normalized_alias"),
        CheckConstraint("normalized_alias = lower(normalized_alias)"
                        " AND normalized_alias <> ''",
                        name="normalized_alias_is_comparison_form"),
        CheckConstraint("last_seen_at >= first_seen_at", name="seen_window_ordered"),
        Index("ix_company_aliases_normalized_alias", "normalized_alias"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"))
    alias: Mapped[str]
    normalized_alias: Mapped[str]
    source_key: Mapped[str] = mapped_column(String(_PROVENANCE_KEY_LENGTH))
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]


class CompanyCareerSiteRow(TimestampedMixin, Base):
    """One careers endpoint of one company (§11).

    Several per company is the normal case — a corporate page, an ATS board, a
    spontaneous-application form — which is why §11 asks for records instead of one
    `careers_url` column. `Company.careers_url` survives as the *preferred* endpoint,
    which §11 explicitly permits.

    `UNIQUE (company_id, url)` with a derived primary key, for the same idempotence
    reason as the aliases: rediscovering the same board updates one row.

    `last_checked_at` is nullable and Phase 6 never sets it: nothing in this phase
    fetches a URL, so a timestamp here would claim a check that never happened.
    """

    __tablename__ = "company_career_sites"
    __table_args__ = (
        UniqueConstraint("company_id", "url"),
        # `CareerSite._an_ats_board_names_its_platform`: a board nothing can identify
        # the platform of is a board no source plugin can read.
        CheckConstraint(
            f"kind <> '{CareerSiteKind.ATS_BOARD.value}' OR platform IS NOT NULL",
            name="ats_board_names_its_platform"),
        CheckConstraint("last_checked_at IS NULL OR last_checked_at >= discovered_at",
                        name="checked_after_discovered"),
        Index("ix_company_career_sites_company_id", "company_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"))
    url: Mapped[str]
    kind: Mapped[CareerSiteKind] = mapped_column(
        enum_column(CareerSiteKind, "career_site_kind"))
    platform: Mapped[AtsPlatform | None] = mapped_column(
        enum_column(AtsPlatform, "career_site_platform"))
    source_key: Mapped[str] = mapped_column(String(_PROVENANCE_KEY_LENGTH))
    verification_status: Mapped[DetectionStatus] = mapped_column(
        enum_column(DetectionStatus, "career_site_verification_status"),
        server_default=text(f"'{DetectionStatus.LIKELY.value}'"))
    discovered_at: Mapped[datetime]
    last_checked_at: Mapped[datetime | None]


class CompanyDiscoveryRecordRow(TimestampedMixin, Base):
    """How one provider came to tell us about one company (§5).

    `UNIQUE (provider_key, external_id)` is §23's external identity uniqueness: the
    same provider reporting the same employer twice is one sighting, updated. The
    primary key is `company_discovery_record_id`, uuid5 over exactly that pair, so
    the constraint and the key say the same thing and neither can drift.

    `company_id` is nullable and the foreign key is `ON DELETE SET NULL`. Both matter:
    §14 leaves an `AMBIGUOUS` seed unlinked rather than guessing, and a sighting we
    keep is what stops the next pass rediscovering and re-refusing it; and merging two
    duplicate employers must not delete the provenance that revealed the duplication.

    `raw` is JSONB and the domain refuses credential-shaped keys before it ever gets
    here (`CompanyDiscoveryRecord._raw_carries_no_secrets`, §5, §26). No CHECK
    mirrors that: the forbidden set is a substring list that will grow, and a CHECK
    over JSONB keys would be a second copy of it that silently disagrees.
    """

    __tablename__ = "company_discovery_records"
    __table_args__ = (
        UniqueConstraint("provider_key", "external_id"),
        Index("ix_company_discovery_records_company_id", "company_id"),
        # "What did this provider find, most recent first" — the operator's question
        # on a provenance screen.
        Index("ix_company_discovery_records_provider_key_discovered_at",
              "provider_key", "discovered_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(_PROVENANCE_KEY_LENGTH))
    external_id: Mapped[str]
    seed_kind: Mapped[CompanySeedKind] = mapped_column(
        enum_column(CompanySeedKind, "company_seed_kind"))
    company_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"))
    company_name: Mapped[str]
    source_url: Mapped[str | None]
    discovered_at: Mapped[datetime]
    confidence: Mapped[DetectionStatus] = mapped_column(
        enum_column(DetectionStatus, "discovery_record_confidence"),
        server_default=text(f"'{DetectionStatus.LIKELY.value}'"))
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=_EMPTY_JSON_OBJECT)


class CompanyLocationRow(LocationColumnsMixin, TimestampedMixin, Base):
    """One physical site of a company — a marker on the Phase 8 map, once the
    Phase 7 geocoding pass gives it coordinates.

    Unlike an opportunity's location, this one is required to say something:
    `Location` is non-optional on the domain entity, so `location_not_empty`
    mirrors `Location._must_locate_something` as a CHECK. A row with six NULLs
    would be a site nobody can find.
    """

    __tablename__ = "company_locations"
    __table_args__ = (
        CheckConstraint(
            " OR ".join(f"{column} IS NOT NULL" for column in _LOCATION_COMPONENTS),
            name="location_not_empty"),
        _code_format("location_country", "^[A-Z]{2}$"),
        _location_provenance_coherent(),
        Index("ix_company_locations_company_id", "company_id"),
        # GiST is what makes `ST_DWithin` an index lookup instead of a scan of
        # every site in the country.
        Index("ix_company_locations_location_point", "location_point",
              postgresql_using="gist"),
        # §13's whole-country branch, which is not a spatial predicate and which
        # GiST therefore does not serve: `location_country = 'CH'`, against the
        # canonical code rather than a substring of a display string.
        Index("ix_company_locations_location_country", "location_country"),
        # `Company._locations_belong_here` allows at most one headquarters. A
        # partial unique index says the same thing to the database, and costs
        # nothing on the rows that are not headquarters.
        Index("uq_company_locations_company_id_headquarters", "company_id",
              unique=True, postgresql_where=text("is_headquarters")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"))
    is_headquarters: Mapped[bool] = mapped_column(server_default=text("false"))

    company: Mapped["CompanyRow"] = relationship(back_populates="locations",
                                                 lazy="raise")


# `SalaryRange` is optional on an opportunity but is all-or-nothing when present:
# a currency without a bound cannot be displayed, and a bound without a currency
# cannot be compared. The domain expresses this by making the value object
# indivisible; a flattened column group needs it spelled out.
_SALARY_COMPLETE_OR_ABSENT: Final[str] = (
    "(salary_currency IS NULL AND salary_period IS NULL"
    " AND salary_minimum IS NULL AND salary_maximum IS NULL)"
    " OR (salary_currency IS NOT NULL AND salary_period IS NOT NULL"
    " AND (salary_minimum IS NOT NULL OR salary_maximum IS NOT NULL))"
)


class OpportunityRow(LocationColumnsMixin, TimestampedMixin, Base):
    """A normalized opportunity — the central V2 table.

    No `user_id`: a posting is a shared fact, and what a particular candidate
    thinks of it lives in `match_evaluations`. This is the decision
    docs/ARCHITECTURE.md §5 makes, and it is why two candidates searching the
    same market do not store the same posting twice.

    `company_name` sits beside a nullable `company_id` because that is the order
    the facts arrive in: discovery has a string, and Phase 6 resolves it to a
    `companies` row without overwriting what the posting claimed. The foreign key
    is `ON DELETE SET NULL` for the same reason — merging two duplicate company
    records must not delete their postings.
    """

    __tablename__ = "opportunities"
    __table_args__ = (
        CheckConstraint("workload_min_percent <= workload_max_percent",
                        name="workload_percent_ordered"),
        CheckConstraint("workload_min_weekly_hours <= workload_max_weekly_hours",
                        name="workload_hours_ordered"),
        CheckConstraint(_WORKLOAD_PERCENT_RANGE, name="workload_percent_range"),
        CheckConstraint(_WORKLOAD_HOURS_RANGE, name="workload_hours_range"),
        CheckConstraint(_SALARY_COMPLETE_OR_ABSENT, name="salary_complete_or_absent"),
        CheckConstraint("salary_minimum <= salary_maximum",
                        name="salary_bounds_ordered"),
        CheckConstraint("salary_minimum >= 0 AND salary_maximum >= 0",
                        name="salary_non_negative"),
        _code_format("location_country", "^[A-Z]{2}$"),
        _code_format("posting_language", "^[a-z]{2}$"),
        _code_format("salary_currency", "^[A-Z]{3}$"),
        _location_provenance_coherent(),
        Index("ix_opportunities_company_id", "company_id"),
        # The feed is "what is new", so this is the ordering every list query
        # uses. PostgreSQL does not index a foreign key or a sort column for you.
        Index("ix_opportunities_discovered_at", "discovered_at"),
        Index("ix_opportunities_location_point", "location_point",
              postgresql_using="gist"),
        # §13's whole-country branch — see `ix_company_locations_location_country`.
        Index("ix_opportunities_location_country", "location_country"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    company_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"))
    company_name: Mapped[str]
    title: Mapped[str]
    description: Mapped[str | None]

    opportunity_type: Mapped[OpportunityType | None] = mapped_column(
        enum_column(OpportunityType, "opportunity_type"))
    contract_type: Mapped[ContractType | None] = mapped_column(
        enum_column(ContractType, "contract_type"))
    workplace_mode: Mapped[WorkplaceMode | None] = mapped_column(
        enum_column(WorkplaceMode, "workplace_mode"))

    # Both workload scales are stored as the source gave them. Converting a
    # percentage into hours needs the local full-time week, which is Country Pack
    # knowledge (Phase 5); a column that guessed it would be wrong in one country
    # out of two and unfixable afterwards.
    workload_min_percent: Mapped[int | None] = mapped_column(SmallInteger)
    workload_max_percent: Mapped[int | None] = mapped_column(SmallInteger)
    workload_min_weekly_hours: Mapped[float | None]
    workload_max_weekly_hours: Mapped[float | None]

    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_period: Mapped[SalaryPeriod | None] = mapped_column(
        enum_column(SalaryPeriod, "salary_period"))
    salary_minimum: Mapped[Decimal | None] = mapped_column(_MONEY)
    salary_maximum: Mapped[Decimal | None] = mapped_column(_MONEY)

    posting_language: Mapped[str | None] = mapped_column(String(2))
    # A list of `LanguageRequirement` objects. JSONB rather than a child table:
    # the requirements are always read with the opportunity, never queried on
    # their own in Phase 2, and a `?` containment index can be added later without
    # a schema change if Phase 9 needs "postings that require German".
    language_requirements: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)

    posted_at: Mapped[date | None]
    discovered_at: Mapped[datetime]
    application_url: Mapped[str | None]
    # V1's `dedup_hash`, carried through unchanged. Unique so a second import of
    # the same posting collides instead of duplicating — and nullable, because
    # PostgreSQL treats NULLs as distinct, so postings whose source never provided
    # a fingerprint coexist freely.
    dedup_fingerprint: Mapped[str | None] = mapped_column(unique=True)

    source: Mapped["OpportunitySourceRecordRow"] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise", uselist=False)


class OpportunitySourceRecordRow(TimestampedMixin, Base):
    """Where an opportunity came from, and what the source literally said.

    Its own table, not a column group, for two reasons the specification gives:
    provenance is an entity in docs/ARCHITECTURE.md §3, and `raw` is an unbounded
    JSONB payload that would be read on every opportunity list query if it lived
    on the same row.

    `UNIQUE (source_key, external_id)` is the import idempotency key
    (docs/ENGINEERING_STANDARDS.md §Database rules): running the V1 import twice
    conflicts on the second run instead of inserting a second copy. NULL
    `external_id` values do not conflict with each other in PostgreSQL, which is
    the behaviour wanted here — a source that publishes no stable id cannot claim
    two postings are the same.

    `UNIQUE (opportunity_id)` holds the one-to-one the domain models today. It is
    the constraint to drop, not a table to create, if a posting later needs to be
    traceable to several boards.
    """

    __tablename__ = "opportunity_source_records"
    __table_args__ = (
        # Unnamed on purpose: the `uq` naming convention derives
        # `uq_opportunity_source_records_source_key_external_id` from the columns,
        # so the name in a migration is predictable from the definition here.
        UniqueConstraint("opportunity_id"),
        UniqueConstraint("source_key", "external_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    opportunity_id: Mapped[UUID] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"))
    source_key: Mapped[str]
    external_id: Mapped[str | None]
    source_url: Mapped[str | None]
    fetched_at: Mapped[datetime]
    # Public posting metadata only. Credentials, cookies and session tokens must
    # never reach this column (docs/ENGINEERING_STANDARDS.md §Security); the
    # domain model carries the same warning, and the importer only ever copies
    # V1's own job columns into it.
    raw: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON_OBJECT)

    opportunity: Mapped["OpportunityRow"] = relationship(
        back_populates="source", lazy="raise")


class MatchEvaluationRow(TimestampedMixin, Base):
    """How one candidate fits one opportunity — the user-scoped half of the pair.

    `user_id` is stored even though it is reachable through
    `candidate_profiles.user_id`. The duplication is deliberate: every
    authorization filter in Phase 4 and every list query is `WHERE user_id = ?`,
    and a policy that needs a join to know who owns a row is a policy that will
    eventually be written without the join. The foreign keys cascade, so a deleted
    account leaves nothing behind.

    `overall` is stored rather than recomputed from the dimension rows, matching
    `MatchEvaluation`: a matcher may apply penalties the mean does not express,
    and a database view that recomputed it would quietly disagree with the engine.

    `UNIQUE (candidate_profile_id, opportunity_id)` makes re-evaluation an upsert.
    """

    __tablename__ = "match_evaluations"
    __table_args__ = (
        UniqueConstraint("candidate_profile_id", "opportunity_id"),
        _unit_interval("overall"),
        _unit_interval("evidence_confidence"),
        # The shape of every authorization-scoped read: this user's evaluations,
        # newest first.
        Index("ix_match_evaluations_user_id_evaluated_at", "user_id", "evaluated_at"),
        Index("ix_match_evaluations_opportunity_id", "opportunity_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    candidate_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"))
    opportunity_id: Mapped[UUID] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"))
    overall: Mapped[float]
    evidence_confidence: Mapped[float | None]
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    evaluator_key: Mapped[str | None]
    evaluated_at: Mapped[datetime]

    dimensions: Mapped[list["MatchDimensionScoreRow"]] = relationship(
        back_populates="evaluation", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="MatchDimensionScoreRow.dimension")


class MatchDimensionScoreRow(TimestampedMixin, Base):
    """One axis of one evaluation, with the reasons behind it.

    A table rather than a JSONB array on the evaluation, unlike
    `reasons`: docs/V2_SPECIFICATION.md §9 asks which axis is weak across a whole
    market, and "average LOCATION_FIT for this user's evaluations" is an
    aggregate, which is what rows are for. The `UNIQUE (match_evaluation_id,
    dimension)` pair enforces `MatchEvaluation._one_score_per_dimension`.

    A `weight` of exactly 0 is legal and means "computed, deliberately ignored" —
    different from the absence of a row, which means "not computed". Only the
    domain's `weighted_dimension_mean()` interprets that difference.
    """

    __tablename__ = "match_dimension_scores"
    __table_args__ = (
        UniqueConstraint("match_evaluation_id", "dimension"),
        _unit_interval("score"),
        _unit_interval("weight"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    match_evaluation_id: Mapped[UUID] = mapped_column(
        ForeignKey("match_evaluations.id", ondelete="CASCADE"))
    dimension: Mapped[MatchDimension] = mapped_column(
        enum_column(MatchDimension, "match_dimension"))
    score: Mapped[float]
    weight: Mapped[float] = mapped_column(server_default=text("1.0"))
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)

    evaluation: Mapped["MatchEvaluationRow"] = relationship(
        back_populates="dimensions", lazy="raise")


# `GeocodingResult._the_payload_matches_the_outcome`, as a CHECK. The reason it is
# worth restating in the database is the reason the validator exists: an adapter is
# written against one example response, and `AMBIGUOUS` with a single candidate is
# the bug that would quietly retire §6's prohibition on turning the first hit into
# truth. `'{}'::jsonb` is "no place" and `'[]'::jsonb` is "no alternatives", the
# same absent-means-empty convention `ats_evidence` uses.
_GEOCODING_PAYLOAD_MATCHES_OUTCOME: Final[str] = (
    f"(outcome = '{GeocodingOutcome.MATCHED.value}' AND place <> '{{}}'::jsonb"
    " AND alternatives = '[]'::jsonb)"
    f" OR (outcome = '{GeocodingOutcome.AMBIGUOUS.value}' AND place = '{{}}'::jsonb"
    " AND jsonb_array_length(alternatives) >= 2)"
    f" OR (outcome IN ('{GeocodingOutcome.NOT_FOUND.value}',"
    f" '{GeocodingOutcome.FAILED.value}')"
    " AND place = '{}'::jsonb AND alternatives = '[]'::jsonb)"
)


class GeocodingCacheRow(TimestampedMixin, Base):
    """One provider's answer to one normalized question (Phase 7 §23).

    A table rather than an in-process dictionary because the enrichment pass is a
    CLI invocation (§22) — the process exits, and a cache that died with it would
    let a nightly run ask a rate-limited public geocoder the same thousand
    questions every night. Idempotence by construction is the point: the primary
    key is `geocoding_cache_entry_id`, uuid5 over exactly the three columns the
    unique constraint covers, so re-asking updates the row rather than adding one.

    **The key is provider-aware and country-aware.** Two geocoders answer the same
    string differently, so caching one under the other's name would attribute
    provenance to the wrong service; and "Neuchâtel" resolves differently
    depending on the hint that accompanied it. `country_hint` is NOT NULL with `''`
    meaning "no hint", rather than nullable, for a reason worth stating: in
    PostgreSQL two NULLs are distinct in a unique constraint, so a nullable column
    here would let the unhinted question — the common one — be stored twice with
    nothing to stop it.

    **A failure is not an answer.** `expires_at` is NULL for `MATCHED`,
    `AMBIGUOUS` and `NOT_FOUND`: those are what the provider knows, and re-asking
    tomorrow gives the same reply. It is set for `FAILED`, so a timeout or a 502
    is remembered only long enough to stop a retry storm — caching one provider
    outage forever would turn it into a permanently unresolvable address.

    **No user data reaches this table.** It is shared and unscoped, so §33 applies
    with full force: the enrichment service geocodes `opportunities` and
    `company_locations` and nothing else. A candidate's home address is private,
    and normalizing it into a shared cache key would be precisely the leak §33
    names — which is why "geocode a candidate profile" is not an operation this
    phase offers.

    `place` and `alternatives` hold `GeocodedPlace` documents rather than flattened
    columns. Nothing queries the cache geographically — it is read by key and only
    by key — and a `GeocodedPlace` has nowhere to put a credential by
    construction, so the JSONB carries no provider blob (§6).
    """

    __tablename__ = "geocoding_cache"
    __table_args__ = (
        UniqueConstraint("provider", "country_hint", "normalized_query"),
        _code_format("country_hint", "^([A-Z]{2})?$"),
        _code_format("provider", "^[a-z][a-z0-9_]*$"),
        CheckConstraint(_GEOCODING_PAYLOAD_MATCHES_OUTCOME,
                        name="payload_matches_outcome"),
        # Only a failure may expire, and it must: the two halves of §23's rule,
        # stated so a provider outage cannot be recorded as permanent.
        CheckConstraint(
            f"(outcome = '{GeocodingOutcome.FAILED.value}')"
            " = (expires_at IS NOT NULL)",
            name="only_a_failure_expires"),
        CheckConstraint("normalized_query <> ''", name="normalized_query_present"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(_PROVENANCE_KEY_LENGTH))
    country_hint: Mapped[str] = mapped_column(String(2), server_default=text("''"))
    normalized_query: Mapped[str]
    outcome: Mapped[GeocodingOutcome] = mapped_column(
        enum_column(GeocodingOutcome, "geocoding_outcome"))
    place: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON_OBJECT)
    alternatives: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    # Composed by the adapter from a fixed vocabulary, never a forwarded provider
    # message and never a formatted exception: a URL in an exception carries its
    # query string, and a query string carries the API key
    # (`backend/app/discovery/failures.py`).
    detail: Mapped[str | None]
    # When the provider was actually asked. `created_at` is when the row was
    # written, which stops being the same thing the first time an entry is
    # refreshed in place.
    checked_at: Mapped[datetime]
    expires_at: Mapped[datetime | None]


# `EligibilityCheck._verdict_is_accountable`, as three CHECKs. The domain refuses
# to *construct* a check that trips any of them; restating them here is what keeps
# a row written by a migration, a backfill or psql from asserting a verdict the
# engine could never have produced — most consequentially the legal-safety rule,
# below, that operator-maintained pack data may not refuse an application on its
# own (docs/COUNTRY_PACKS.md §Eligibility, §59). Each is written as the implication
# it is — `NOT antecedent OR consequent` — because that is the form a CHECK, which
# fails only on FALSE, evaluates the way the prose reads.
_ELIGIBILITY_REASONS_PRESENT: Final[str] = (
    f"status = '{EligibilityStatus.ELIGIBLE.value}'"
    " OR jsonb_array_length(reasons) >= 1"
)

_ELIGIBILITY_LLM_IS_INCOMPLETE: Final[str] = (
    f"determined_by <> '{DeterminationSource.LLM_EXTRACTION.value}'"
    f" OR status = '{EligibilityStatus.INCOMPLETE.value}'"
)

_ELIGIBILITY_PACK_BLOCKS_ONLY_WHEN_VERIFIED: Final[str] = (
    f"determined_by <> '{DeterminationSource.COUNTRY_PACK_RULE.value}'"
    f" OR status <> '{EligibilityStatus.INELIGIBLE.value}'"
    f" OR authority = '{RuleAuthority.VERIFIED.value}'"
)


class EligibilityResultRow(TimestampedMixin, Base):
    """Whether one candidate may apply to one opportunity — the second, separate axis.

    Deliberately its own table beside `match_evaluations`, never a column on it:
    docs/V2_SPECIFICATION.md §13 and CLAUDE.md make eligibility a different question
    from fit, with a different verdict type, and a schema that folded the two would
    invite exactly the averaging the phase order forbids. A pair can score 92% and
    be INELIGIBLE, or 61% and ELIGIBLE; two tables is what keeps those independent.

    `status` is stored even though `EligibilityResult.status` is *derived* from the
    checks. The duplication is the same trade `MatchEvaluationRow.overall` makes: a
    list that ranks and filters by verdict must not load every child row of every
    result to do it. The mapper writes the derived value and never a second opinion,
    and `ck_eligibility_checks_*` keep the checks it is derived from honest, so the
    denormalized copy cannot assert a pass over a failed gate.

    `UNIQUE (candidate_profile_id, opportunity_id)` makes re-evaluation an upsert,
    and the foreign keys cascade from `users` so a deleted account leaves no verdict
    behind. `user_id` is carried for the same authorization reason it is on
    `match_evaluations`: every scoped read is `WHERE user_id = ?`, and a policy that
    needed a join to know the owner is one that will be written without it.
    """

    __tablename__ = "eligibility_results"
    __table_args__ = (
        UniqueConstraint("candidate_profile_id", "opportunity_id"),
        # The shape of every authorization-scoped read: this user's verdicts,
        # newest first.
        Index("ix_eligibility_results_user_id_determined_at",
              "user_id", "determined_at"),
        Index("ix_eligibility_results_opportunity_id", "opportunity_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    candidate_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"))
    opportunity_id: Mapped[UUID] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"))
    status: Mapped[EligibilityStatus] = mapped_column(
        enum_column(EligibilityStatus, "eligibility_result_status"))
    # The engine and policy that produced this verdict, for audit — the eligibility
    # twin of `match_evaluations.evaluator_key`, kept a plain string for the same
    # provider-neutral reason.
    policy_version: Mapped[str | None]
    determined_at: Mapped[datetime]

    checks: Mapped[list["EligibilityCheckRow"]] = relationship(
        back_populates="result", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="EligibilityCheckRow.ordinal")


class EligibilityCheckRow(TimestampedMixin, Base):
    """One gate of one eligibility result, with the reasons that closed it.

    A child table rather than a JSONB array on the result, and for a sharper reason
    than `match_dimension_scores` has: the three CHECKs below are the domain's
    accountability invariants, and PostgreSQL can only police them per row. Folding
    the checks into a document column would move `EligibilityCheck._verdict_is_
    accountable` back into Python alone, where the first write that skipped the model
    would be free to store an unexplained refusal or an LLM-decided one.

    `ordinal` is the natural key. Unlike a dimension score, a requirement may repeat
    — two required languages are two `LANGUAGE_MINIMUM` gates — so the requirement
    cannot identify a row and position is what is left. `UNIQUE (result_id, ordinal)`
    is therefore what makes re-evaluating a pair update the rows already there, and a
    gate that a re-run no longer emits is deleted by the `delete-orphan` cascade.

    `evidence_ids` is a `TEXT[]` of the candidate evidence a gate rested on, stored
    since Phase 10 gave the evidence store a home. Not a foreign-key array —
    PostgreSQL has none — and unlike a claim's citation it is not re-checked against
    a held set here, because an eligibility check belongs to a verdict, not to the
    profile whose evidence it names; it is provenance for why the gate closed the
    way it did. Empty is the default: a deterministic gate often rests on a permit
    field rather than on a discrete evidence record.
    """

    __tablename__ = "eligibility_checks"
    __table_args__ = (
        UniqueConstraint("result_id", "ordinal"),
        # `EligibilityCheck._verdict_is_accountable`, restated so a non-model write
        # cannot slip past it. Order matches the domain validator.
        CheckConstraint(_ELIGIBILITY_REASONS_PRESENT,
                        name="reasons_present_unless_eligible"),
        CheckConstraint(_ELIGIBILITY_LLM_IS_INCOMPLETE,
                        name="llm_extraction_is_incomplete"),
        CheckConstraint(_ELIGIBILITY_PACK_BLOCKS_ONLY_WHEN_VERIFIED,
                        name="pack_rule_blocks_only_when_verified"),
        _text_array_elements_present("evidence_ids"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    # Short FK column name on purpose: the convention derives
    # `fk_eligibility_checks_result_id_eligibility_results` (51 chars) from it, and
    # `eligibility_result_id` would push that past PostgreSQL's 63-char limit into a
    # silently truncated name a migration cannot reliably drop — the same reason the
    # candidate child tables say `profile_id`.
    result_id: Mapped[UUID] = mapped_column(
        ForeignKey("eligibility_results.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(SmallInteger, doc=_ORDINAL_DOC)
    requirement: Mapped[EligibilityRequirement] = mapped_column(
        enum_column(EligibilityRequirement, "eligibility_requirement"))
    status: Mapped[EligibilityStatus] = mapped_column(
        enum_column(EligibilityStatus, "eligibility_check_status"))
    determined_by: Mapped[DeterminationSource] = mapped_column(
        enum_column(DeterminationSource, "eligibility_determination_source"))
    authority: Mapped[RuleAuthority] = mapped_column(
        enum_column(RuleAuthority, "eligibility_rule_authority"),
        server_default=text(f"'{RuleAuthority.UNKNOWN.value}'"))
    detail: Mapped[str | None]
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    evidence_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)

    result: Mapped["EligibilityResultRow"] = relationship(
        back_populates="checks", lazy="raise")


# `DocumentVersion._status_agrees_with_verdict_and_artifact`, as CHECK expressions.
# The status is a summary of the guard's verdict and of whether a PDF was rendered,
# and a row where the three disagree would let a rejected version be served as
# usable — exactly the silent failure the guard exists to prevent. Each clause is
# the implication the domain validator states, in the `NOT antecedent OR
# consequent` form a CHECK (which fails only on FALSE) reads as written. `guard_ok`
# is the extracted boolean the mapper writes from `guard_report.ok`, because a
# CHECK cannot reach inside a JSONB document and stay legible.
_DOCUMENT_VERSION_VERDICT_AGREES: Final[str] = (
    "(status NOT IN ('VALIDATED', 'RENDERED') OR guard_ok IS TRUE)"
    " AND (status <> 'REJECTED' OR guard_ok IS FALSE)"
)
# A RENDERED version references its artifact; no other status carries one. Written
# against `artifact_storage_key` as the presence witness, the column the artifact
# group cannot be missing when it exists.
_DOCUMENT_VERSION_ARTIFACT_MATCHES_STATUS: Final[str] = (
    "(status = 'RENDERED') = (artifact_storage_key IS NOT NULL)"
)


class CandidateDocumentRow(TimestampedMixin, Base):
    """A document a candidate keeps for one posting, across its versions (Phase 10).

    User-owned and scoped to a `(candidate_profile_id, opportunity_id,
    document_type)` triple — the triple `candidate_document_id` derives from — so
    regenerating a résumé for a posting reuses this row and appends a *version*
    rather than leaving a second, orphaned document behind. `UNIQUE` on that triple
    is what makes the regeneration an upsert.

    `user_id` is carried alongside `candidate_profile_id` for the authorization
    reason every user-owned table states: each scoped read is `WHERE user_id = ?`,
    one indexed predicate rather than a join a policy could forget. All three
    foreign keys cascade from their parents, so a deleted account, profile or
    posting leaves no document behind.
    """

    __tablename__ = "candidate_documents"
    __table_args__ = (
        # Named explicitly: the convention would derive
        # `uq_candidate_documents_candidate_profile_id_opportunity_id_document_type`
        # at 72 characters, which PostgreSQL silently truncates to 63 — a name a
        # migration then cannot address. The short name is a pure function of the
        # columns just as the generated one is, only within the limit.
        UniqueConstraint("candidate_profile_id", "opportunity_id", "document_type",
                         name="uq_candidate_documents_profile_opportunity_type"),
        Index("ix_candidate_documents_user_id_updated_at", "user_id", "updated_at"),
        Index("ix_candidate_documents_opportunity_id", "opportunity_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    candidate_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"))
    opportunity_id: Mapped[UUID] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"))
    document_type: Mapped[CandidateDocumentType] = mapped_column(
        enum_column(CandidateDocumentType, "candidate_document_type"))

    versions: Mapped[list["DocumentVersionRow"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="DocumentVersionRow.version")


class DocumentVersionRow(TimestampedMixin, Base):
    """One attempt at a document: content, the guard's verdict, and its artifact.

    `content` and `guard_report` are JSONB documents validated back through the
    domain on read, the same trade `match_evaluations.reasons` makes: they are read
    and written whole and never queried into, so a child table per bullet would buy
    nothing. `guard_ok` is the one field lifted out of `guard_report`, because the
    three status/verdict CHECKs must be able to read the verdict without reaching
    inside a JSONB document.

    The artifact reference is flattened into five nullable columns, present exactly
    when `status = 'RENDERED'` (`ck_document_versions_artifact_matches_status`). The
    bytes themselves live in a `DocumentArtifactStore`, not here — the table carries
    the locator, for the reason `CandidateEvidence.source_document` is a label.

    `created_at` from `TimestampedMixin` is the row-write instant; the domain's own
    `DocumentVersion.created_at` is written into it by the mapper and preserved
    across the in-place status transitions a version goes through (DRAFT →
    VALIDATED → RENDERED), so it keeps meaning "when this attempt was made".
    """

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("artifact_byte_size >= 0", name="artifact_byte_size_non_negative"),
        CheckConstraint("artifact_page_count >= 1", name="artifact_page_count_positive"),
        _code_format("language", "^[a-z]{2}$"),
        CheckConstraint(_DOCUMENT_VERSION_VERDICT_AGREES,
                        name="status_agrees_with_verdict"),
        CheckConstraint(_DOCUMENT_VERSION_ARTIFACT_MATCHES_STATUS,
                        name="artifact_matches_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_documents.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[DocumentStatus] = mapped_column(
        enum_column(DocumentStatus, "document_status"))
    language: Mapped[str] = mapped_column(String(2))
    content: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON_OBJECT)
    guard_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Lifted out of `guard_report` so the status CHECKs can read the verdict
    # without a JSONB path expression. NULL when the version has not been guarded
    # yet (a DRAFT); the mapper keeps it in step with `guard_report.ok`.
    guard_ok: Mapped[bool | None]
    generator_key: Mapped[str | None]
    created_at: Mapped[datetime]

    artifact_storage_key: Mapped[str | None]
    artifact_media_type: Mapped[str | None]
    artifact_byte_size: Mapped[int | None]
    artifact_page_count: Mapped[int | None] = mapped_column(SmallInteger)
    artifact_rendered_at: Mapped[datetime | None]

    document: Mapped["CandidateDocumentRow"] = relationship(
        back_populates="versions", lazy="raise")


# `LLMConnection._transport_shape_is_coherent`, as a CHECK. A CLI connection carries
# no base URL, no stored credential and no custom headers — the data-layer half of
# the §1 rule that the platform never injects a credential into a self-authenticating
# CLI — while an API connection must name the endpoint it speaks to (§5 forbids
# assuming the hostname, so it is required, never defaulted). The two provider-type
# lists are the `_CLI_TYPES` split in `backend.app.llm.connection`, restated so a
# non-model write cannot store a connection the factory could not build.
_LLM_CONNECTION_TRANSPORT_SHAPE: Final[str] = (
    f"(provider_type IN ('{LLMProviderType.CLAUDE_CODE.value}',"
    f" '{LLMProviderType.CODEX.value}')"
    " AND base_url IS NULL AND encrypted_api_key IS NULL"
    " AND custom_headers = '{}'::jsonb)"
    f" OR (provider_type IN ('{LLMProviderType.OPENAI_COMPATIBLE.value}',"
    f" '{LLMProviderType.LOCAL_OPENAI_COMPATIBLE.value}')"
    " AND base_url IS NOT NULL)"
)

# `LLMConnection._secret_pair_is_complete`: the ciphertext and the version tag naming
# the key that made it are stored together or not at all, so a row can never carry a
# ciphertext no version can decrypt or a version with nothing to decrypt.
_LLM_CONNECTION_SECRET_PAIR: Final[str] = (
    "(encrypted_api_key IS NULL) = (secret_version IS NULL)"  # noqa: S105 — a SQL CHECK
)

# `LLMRun._status_agrees_with_shape`, as two CHECKs. A STARTED run is in flight and
# has no `finished_at`; a terminal run has one. A failure (FAILED or TIMEOUT) carries
# a code; every other status carries none — so a stored row cannot claim a success
# with a failure code or a failure with none.
_LLM_RUN_STARTED_HAS_NO_FINISH: Final[str] = (
    f"(status = '{LLMRunStatus.STARTED.value}') = (finished_at IS NULL)"
)
_LLM_RUN_FAILURE_CARRIES_CODE: Final[str] = (
    f"(status IN ('{LLMRunStatus.FAILED.value}', '{LLMRunStatus.TIMEOUT.value}'))"
    " = (failure_code IS NOT NULL)"
)
# `LLMRun._fallback_pair_is_complete`: the provider given way from and the reason it
# gave are recorded together, so a run cannot name a fallback origin with no reason.
_LLM_RUN_FALLBACK_PAIR: Final[str] = (
    "(fallback_from IS NULL) = (fallback_reason IS NULL)"
)

# A provider key as `LLMProviderMetadata.provider_key` validates it. Distinct from
# `_PROVENANCE_KEY_LENGTH`'s `^[a-z][a-z0-9_]*$` because a provider key may carry a
# hyphen; the trailing `-` in the class is a literal.
_PROVIDER_KEY_PATTERN: Final[str] = "^[a-z][a-z0-9_-]*$"


class LLMConnectionRow(TimestampedMixin, Base):
    """A user's stored connection to an LLM provider (Phase 11 §4).

    User-owned: `user_id` cascades from `users`, and every read is `WHERE user_id = ?`
    so one account cannot see another's connections or the keys they hold. The
    credential is `encrypted_api_key` — the ciphertext `SecretCipher` produced, never
    the plaintext — beside `secret_version`, the tag naming the key that made it; the
    two are both-or-neither (`ck_llm_connections_secret_pair`), and the master key that
    decrypts them is never a column (§21).

    `is_default` marks the connection a task uses when the user stated no preference,
    and a partial unique index allows at most one per account — the connection twin of
    `company_locations`' single-headquarters rule. `custom_headers` is JSONB rather
    than a child table for the reason `ats_evidence` is: it is read and written whole
    with the connection and never queried into.
    """

    __tablename__ = "llm_connections"
    __table_args__ = (
        CheckConstraint(_LLM_CONNECTION_SECRET_PAIR, name="secret_pair_complete"),
        CheckConstraint(_LLM_CONNECTION_TRANSPORT_SHAPE,
                        name="transport_shape_coherent"),
        CheckConstraint("secret_version >= 1", name="secret_version_positive"),
        CheckConstraint("priority >= 0", name="priority_non_negative"),
        # Scoped reads list a user's connections by priority; one index serves them.
        Index("ix_llm_connections_user_id", "user_id"),
        # At most one default per account, said to the database the way the single
        # headquarters is: a partial unique index, free on the non-default rows.
        Index("uq_llm_connections_user_id_default", "user_id",
              unique=True, postgresql_where=text("is_default")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    provider_type: Mapped[LLMProviderType] = mapped_column(
        enum_column(LLMProviderType, "llm_provider_type"))
    display_name: Mapped[str]
    base_url: Mapped[str | None]
    model: Mapped[str | None]
    encrypted_api_key: Mapped[str | None]
    secret_version: Mapped[int | None] = mapped_column(SmallInteger)
    custom_headers: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON_OBJECT)
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    is_default: Mapped[bool] = mapped_column(server_default=text("false"))
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=text("100"))

    sessions: Mapped[list["ProviderSessionRow"]] = relationship(
        back_populates="connection", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="ProviderSessionRow.conversation_key")


class ProviderSessionRow(TimestampedMixin, Base):
    """The provider-side handle for one conversation on one connection (§4).

    Replaces V1's provider-specific `claude_session_id` column with a neutral
    `external_session_id`, so a resumable exchange keeps its handle without a generic
    table learning a provider's vocabulary. `UNIQUE (connection_id, conversation_key)`
    is the natural key `provider_session_id` derives the primary key from, so resuming
    a conversation refreshes the one row rather than inserting a second.

    `user_id` is carried for the authorization reason every user-owned table states —
    a session is read for its owner — and cascades from `users`; the connection
    cascade takes a session with the connection it belongs to.
    """

    __tablename__ = "provider_sessions"
    __table_args__ = (
        UniqueConstraint("connection_id", "conversation_key"),
        Index("ix_provider_sessions_user_id", "user_id"),
        Index("ix_provider_sessions_connection_id", "connection_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("llm_connections.id", ondelete="CASCADE"))
    conversation_key: Mapped[str]
    purpose: Mapped[TaskPurpose] = mapped_column(
        enum_column(TaskPurpose, "provider_session_purpose"),
        server_default=text(f"'{TaskPurpose.GENERIC.value}'"))
    external_session_id: Mapped[str | None]

    connection: Mapped["LLMConnectionRow"] = relationship(
        back_populates="sessions", lazy="raise")


class LLMRunRow(TimestampedMixin, Base):
    """One telemetry record of one LLM call (§12, §56).

    `user_id` and `connection_id` are nullable: a healthcheck probe has no user, and a
    provider built by `bootstrap` rather than from a stored connection has no
    connection. `connection_id` is `ON DELETE SET NULL`, not CASCADE — a run is
    provenance and outlives the connection it used, the same trade
    `company_discovery_records` makes — while `user_id` cascades, so deleting an
    account takes its runs with it.

    The token, cost and latency columns are all nullable because the unknown is null,
    never zero (§58): a CLI that reports no usage leaves them NULL, and a telemetry sum
    skips them rather than counting a fabricated 0. The three CHECKs restate
    `LLMRun`'s validators, so a row written outside the model still cannot claim a
    success with a failure code, a STARTED run that finished, or a fallback with no
    reason.
    """

    __tablename__ = "llm_runs"
    __table_args__ = (
        CheckConstraint(_LLM_RUN_STARTED_HAS_NO_FINISH,
                        name="started_has_no_finish"),
        CheckConstraint(_LLM_RUN_FAILURE_CARRIES_CODE, name="failure_carries_code"),
        CheckConstraint(_LLM_RUN_FALLBACK_PAIR, name="fallback_pair_complete"),
        CheckConstraint("prompt_tokens >= 0 AND completion_tokens >= 0"
                        " AND total_tokens >= 0", name="token_counts_non_negative"),
        CheckConstraint("cost_usd >= 0.0", name="cost_non_negative"),
        CheckConstraint("latency_ms >= 0", name="latency_non_negative"),
        CheckConstraint(f"provider_key ~ '{_PROVIDER_KEY_PATTERN}'",
                        name="provider_key_format"),
        # "My runs, newest first" and "this provider's runs, newest first" — the two
        # questions a telemetry screen and a status page ask.
        Index("ix_llm_runs_user_id_started_at", "user_id", "started_at"),
        Index("ix_llm_runs_provider_key_started_at", "provider_key", "started_at"),
        Index("ix_llm_runs_connection_id", "connection_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    connection_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("llm_connections.id", ondelete="SET NULL"))
    provider_key: Mapped[str]
    provider_type: Mapped[LLMProviderType | None] = mapped_column(
        enum_column(LLMProviderType, "llm_run_provider_type"))
    model: Mapped[str | None]
    purpose: Mapped[TaskPurpose] = mapped_column(
        enum_column(TaskPurpose, "llm_run_purpose"),
        server_default=text(f"'{TaskPurpose.GENERIC.value}'"))
    status: Mapped[LLMRunStatus] = mapped_column(
        enum_column(LLMRunStatus, "llm_run_status"))

    prompt_name: Mapped[str | None]
    prompt_version: Mapped[str | None]

    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None]
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    failure_code: Mapped[LLMFailureCode | None] = mapped_column(
        enum_column(LLMFailureCode, "llm_run_failure_code"))
    failure_detail: Mapped[str | None]

    fallback_from: Mapped[str | None]
    fallback_reason: Mapped[LLMFailureCode | None] = mapped_column(
        enum_column(LLMFailureCode, "llm_run_fallback_reason"))

    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]


# ---------------------------------------------------------------------------
# Phase 12 — the application engine. Five tables: the user's policy, the
# decision of intent, the execution aggregate, its append-only event trail, and
# its submission attempts. Every CHECK below restates a domain validator, so a
# row written by a migration or by psql cannot assert a state the engine could
# never have produced (§17-18, §36-41).
# ---------------------------------------------------------------------------

# `ApplicationPolicy._brakes_and_limits_are_coherent`: only AUTOPILOT may switch
# the approval brake off. Written as the implication it is, so the CHECK reads the
# way the prose does.
_POLICY_BRAKE_COHERENT: Final[str] = (
    f"mode = '{AutomationMode.AUTOPILOT.value}'"
    " OR require_approval_before_submission"
)

# `ApplicationDecision._target_matches_the_kind`: a spontaneous application names a
# company, everything else names an opportunity.
_DECISION_TARGET_MATCHES_KIND: Final[str] = (
    f"(kind = '{ApplicationDecisionKind.SPONTANEOUS_APPLICATION.value}'"
    " AND company_id IS NOT NULL)"
    f" OR (kind <> '{ApplicationDecisionKind.SPONTANEOUS_APPLICATION.value}'"
    " AND opportunity_id IS NOT NULL)"
)

# `Application._target_is_singular_and_keyed`: exactly one of the two targets.
_APPLICATION_TARGET_SINGULAR: Final[str] = (
    "(opportunity_id IS NULL) <> (company_id IS NULL)"
)

# `SubmissionResult._fields_match_the_outcome`, applied only once an outcome is
# recorded — an in-flight attempt (outcome NULL) carries none of these yet (§88).
_ATTEMPT_REQUIRES_HUMAN_HAS_REASON: Final[str] = (
    f"outcome IS NULL OR outcome <> '{SubmissionOutcome.REQUIRES_HUMAN.value}'"
    " OR human_required_reason IS NOT NULL"
)
_ATTEMPT_HUMAN_REASON_ONLY_ON_HUMAN: Final[str] = (
    "human_required_reason IS NULL"
    f" OR outcome = '{SubmissionOutcome.REQUIRES_HUMAN.value}'"
)
_ATTEMPT_FAILED_HAS_CODE: Final[str] = (
    f"outcome IS NULL OR outcome <> '{SubmissionOutcome.FAILED.value}'"
    " OR failure_code IS NOT NULL"
)
_ATTEMPT_CODE_ONLY_ON_FAILED: Final[str] = (
    f"failure_code IS NULL OR outcome = '{SubmissionOutcome.FAILED.value}'"
)
_ATTEMPT_CONFIRMATION_ONLY_ON_SUBMITTED: Final[str] = (
    "confirmation_reference IS NULL"
    f" OR outcome = '{SubmissionOutcome.SUBMITTED.value}'"
)


class ApplicationPolicyRow(TimestampedMixin, Base):
    """A user's standing rules about applying (§2, §70).

    User-owned; `created_at`/`updated_at` are domain-supplied and written by the
    mapper, as on `users`. The brake CHECK is the data-layer half of "only AUTOPILOT
    may submit unattended" — a lesser mode with the approval brake off is a row the
    engine could never have built, and the CHECK refuses it.
    """

    __tablename__ = "application_policies"
    __table_args__ = (
        _unit_interval("minimum_overall_score"),
        _enum_array_members("allowed_opportunity_types", OpportunityType),
        CheckConstraint(_POLICY_BRAKE_COHERENT, name="brake_coherent"),
        CheckConstraint(
            "max_applications_per_day IS NULL OR max_applications_per_day >= 0",
            name="max_per_day_non_negative"),
        CheckConstraint(
            "max_applications_per_week IS NULL OR max_applications_per_week >= 0",
            name="max_per_week_non_negative"),
        CheckConstraint(
            "max_applications_per_day IS NULL OR max_applications_per_week IS NULL"
            " OR max_applications_per_day <= max_applications_per_week",
            name="day_within_week"),
        Index("ix_application_policies_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str]
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    mode: Mapped[AutomationMode] = mapped_column(
        enum_column(AutomationMode, "application_automation_mode"))
    require_approval_before_submission: Mapped[bool] = mapped_column(
        server_default=text("true"))
    allowed_opportunity_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), server_default=_EMPTY_TEXT_ARRAY)
    minimum_overall_score: Mapped[float | None]
    dimension_thresholds: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    allow_incomplete_eligibility: Mapped[bool] = mapped_column(
        server_default=text("false"))
    allow_spontaneous_applications: Mapped[bool] = mapped_column(
        server_default=text("false"))
    max_applications_per_day: Mapped[int | None] = mapped_column(Integer)
    max_applications_per_week: Mapped[int | None] = mapped_column(Integer)


class ApplicationDecisionRow(TimestampedMixin, Base):
    """One decision of intent about one target (§2-3).

    `created_at`/`updated_at` are the row's own bookkeeping (server default);
    `decided_at` is the domain fact. The embedded match and eligibility snapshots are
    *not* stored here — they live in their own tables and the engine re-reads them at
    gate time, so a decision row is the intent plus the ids it is about. The target
    CHECK mirrors the domain: a spontaneous decision names a company, all others an
    opportunity.
    """

    __tablename__ = "application_decisions"
    __table_args__ = (
        _unit_interval("confidence"),
        CheckConstraint(_DECISION_TARGET_MATCHES_KIND, name="target_matches_kind"),
        CheckConstraint("jsonb_array_length(reasons) >= 1", name="reasons_present"),
        Index("ix_application_decisions_user_id_decided_at", "user_id", "decided_at"),
        Index("ix_application_decisions_candidate_profile_id_opportunity_id",
              "candidate_profile_id", "opportunity_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    # Explicitly named: the convention's
    # `fk_application_decisions_candidate_profile_id_candidate_profiles` is 64
    # characters, one past PostgreSQL's 63-char limit, so it is shortened here the
    # way `eligibility_checks.result_id` shortens its column for the same reason.
    candidate_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE",
                   name="fk_application_decisions_candidate_profile"))
    opportunity_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"))
    company_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"))
    policy_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("application_policies.id", ondelete="SET NULL"))
    kind: Mapped[ApplicationDecisionKind] = mapped_column(
        enum_column(ApplicationDecisionKind, "application_decision_kind"))
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    confidence: Mapped[float | None]
    requires_human_review: Mapped[bool] = mapped_column(server_default=text("false"))
    decided_by: Mapped[str | None]
    decided_at: Mapped[datetime]


class ApplicationRow(TimestampedMixin, Base):
    """The execution aggregate for one (candidate, target, channel) application (§17).

    `created_at`/`updated_at` are domain-supplied and written by the mapper. The
    UNIQUE on `idempotency_key` is the data-layer half of the duplicate-prevention
    story (§36): the id is derived from the key, so a second application for the same
    target collides on the key even if it bypassed the derivation. `pinned_documents`
    and `answers` are JSONB — read and written whole with the aggregate, never joined.
    """

    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        CheckConstraint(_APPLICATION_TARGET_SINGULAR, name="target_singular"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        Index("ix_applications_user_id_updated_at", "user_id", "updated_at"),
        Index("ix_applications_state", "state"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    candidate_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"))
    decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("application_decisions.id", ondelete="CASCADE"))
    channel: Mapped[ApplicationChannel] = mapped_column(
        enum_column(ApplicationChannel, "application_channel"))
    state: Mapped[ApplicationState] = mapped_column(
        enum_column(ApplicationState, "application_state"))
    idempotency_key: Mapped[str]
    opportunity_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"))
    company_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"))
    policy_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("application_policies.id", ondelete="SET NULL"))
    pinned_documents: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    answers: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    form_fingerprint: Mapped[str | None]
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    correlation_id: Mapped[str | None]

    events: Mapped[list["ApplicationEventRow"]] = relationship(
        back_populates="application", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="ApplicationEventRow.occurred_at")
    attempts: Mapped[list["SubmissionAttemptRow"]] = relationship(
        back_populates="application", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="SubmissionAttemptRow.attempt_number")


class ApplicationEventRow(TimestampedMixin, Base):
    """One immutable entry in an application's history (§41).

    Append-only: nothing updates a row here, so `updated_at` never moves off its
    server default. `from_state`/`to_state` are nullable because a `GATE_EVALUATED`
    or `DUPLICATE_BLOCKED` event changes no state. `occurred_at` is the domain fact;
    `created_at` is when the row was written.
    """

    __tablename__ = "application_events"
    __table_args__ = (
        Index("ix_application_events_application_id_occurred_at",
              "application_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    application_id: Mapped[UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"))
    event_type: Mapped[ApplicationEventType] = mapped_column(
        enum_column(ApplicationEventType, "application_event_type"))
    actor: Mapped[ApplicationEventActor] = mapped_column(
        enum_column(ApplicationEventActor, "application_event_actor"),
        server_default=text(f"'{ApplicationEventActor.SYSTEM.value}'"))
    from_state: Mapped[ApplicationState | None] = mapped_column(
        enum_column(ApplicationState, "application_event_from_state"))
    to_state: Mapped[ApplicationState | None] = mapped_column(
        enum_column(ApplicationState, "application_event_to_state"))
    detail: Mapped[str | None]
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    correlation_id: Mapped[str | None]
    occurred_at: Mapped[datetime]

    application: Mapped["ApplicationRow"] = relationship(
        back_populates="events", lazy="raise")


class SubmissionAttemptRow(TimestampedMixin, Base):
    """One try at the irreversible act, recorded whole (§39, §88).

    `UNIQUE (application_id, attempt_number)` matches the pair the attempt's id
    derives from, so the in-flight row written at SUBMISSION_STARTED and its
    completion are one row. The qualifier CHECKs restate `SubmissionResult`'s
    validators, but each passes while `outcome` is NULL — the in-flight row that is
    the crash evidence §88 wants.
    """

    __tablename__ = "submission_attempts"
    __table_args__ = (
        UniqueConstraint("application_id", "attempt_number"),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint(_ATTEMPT_REQUIRES_HUMAN_HAS_REASON,
                        name="requires_human_has_reason"),
        CheckConstraint(_ATTEMPT_HUMAN_REASON_ONLY_ON_HUMAN,
                        name="human_reason_only_on_human"),
        CheckConstraint(_ATTEMPT_FAILED_HAS_CODE, name="failed_has_code"),
        CheckConstraint(_ATTEMPT_CODE_ONLY_ON_FAILED, name="code_only_on_failed"),
        CheckConstraint(_ATTEMPT_CONFIRMATION_ONLY_ON_SUBMITTED,
                        name="confirmation_only_on_submitted"),
        CheckConstraint("finished_at IS NULL OR finished_at >= started_at",
                        name="finished_after_started"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    application_id: Mapped[UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"))
    attempt_number: Mapped[int] = mapped_column(SmallInteger)
    adapter_key: Mapped[str]
    outcome: Mapped[SubmissionOutcome | None] = mapped_column(
        enum_column(SubmissionOutcome, "submission_attempt_outcome"))
    detail: Mapped[str | None]
    confirmation_reference: Mapped[str | None]
    human_required_reason: Mapped[HumanRequiredReason | None] = mapped_column(
        enum_column(HumanRequiredReason, "submission_attempt_human_reason"))
    failure_code: Mapped[ApplicationFailureCode | None] = mapped_column(
        enum_column(ApplicationFailureCode, "submission_attempt_failure_code"))
    correlation_id: Mapped[str | None]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]

    application: Mapped["ApplicationRow"] = relationship(
        back_populates="attempts", lazy="raise")


# A `USER` turn is the candidate's own prose: it never carries the telemetry of an
# LLM call, because the account, not a provider, produced it. The CHECK restates
# `ChatMessage`'s provenance rule so a row written outside the mapper cannot attribute
# a run to a human turn (docs/CAREER_CHAT.md §…).
_CHAT_MESSAGE_USER_HAS_NO_RUN: Final[str] = (
    "role <> 'USER' OR (llm_run_id IS NULL AND provider_key IS NULL)")

# The conversation-scope invariant as a CHECK, the DB half of
# `Conversation._scope_id_matches_scope`: a GLOBAL thread carries no anchor id, and every
# other scope carries exactly one. Restated here in SQL so a row written outside the
# mapper cannot claim to be about an application it never names (docs/CAREER_CHAT.md §Scope).
_CONVERSATION_SCOPE_ID_MATCHES_SCOPE: Final[str] = (
    "(scope = 'GLOBAL' AND scope_id IS NULL)"
    " OR (scope <> 'GLOBAL' AND scope_id IS NOT NULL)")


class ConversationRow(TimestampedMixin, Base):
    """One career-chat thread, owned by exactly one account, bound to one scope (§Scope).

    User-owned like every Phase 4+ entity: `user_id` cascades from `users`, so deleting
    an account takes its conversations — and, through the cascades below, their messages,
    proposals and executions — with it. `last_message_at` is nullable (a freshly opened
    thread has no turn yet) and pairs with `user_id` in the one index the conversation
    list reads: "my threads, most recently active first".

    `scope`/`scope_id` anchor the thread to a domain surface. `scope_id` carries no
    cross-table foreign key on purpose — it addresses one of four different tables
    depending on `scope`, and ownership is re-checked by reading through a `user_id`-scoped
    repository at use time, not by a constraint. The CHECK enforces only the shape
    invariant (`_CONVERSATION_SCOPE_ID_MATCHES_SCOPE`).
    """

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_user_id_last_message_at",
              "user_id", "last_message_at"),
        CheckConstraint(_CONVERSATION_SCOPE_ID_MATCHES_SCOPE,
                        name="scope_id_matches_scope"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str]
    scope: Mapped[ConversationScope] = mapped_column(
        enum_column(ConversationScope, "conversation_scope"),
        server_default=text(f"'{ConversationScope.GLOBAL.value}'"))
    scope_id: Mapped[UUID | None]
    is_archived: Mapped[bool] = mapped_column(server_default=text("false"))
    last_message_at: Mapped[datetime | None]

    messages: Mapped[list["ChatMessageRow"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise", order_by="ChatMessageRow.sequence")
    proposals: Mapped[list["ChatActionProposalRow"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise", order_by="ChatActionProposalRow.created_at")


class ChatMessageRow(TimestampedMixin, Base):
    """One turn in a conversation — the prose and the provenance of who produced it (§…).

    `UNIQUE (conversation_id, sequence)` is the natural key `chat_message_id` derives the
    primary key from, so re-finalizing a turn writes the same row rather than duplicating
    the exchange. `content` is the prose only: the fenced proposal block is parsed out
    into `chat_action_proposals` and never stored here. `llm_run_id` is `SET NULL` — a
    message outlives the telemetry row it points at, the same trade `llm_runs` makes with
    its connection — and the CHECK forbids a `USER` turn from carrying one.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence"),
        CheckConstraint("sequence >= 0", name="sequence_non_negative"),
        CheckConstraint(_CHAT_MESSAGE_USER_HAS_NO_RUN, name="user_has_no_run"),
        Index("ix_chat_messages_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"))
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[ChatMessageRole] = mapped_column(
        enum_column(ChatMessageRole, "chat_message_role"))
    content: Mapped[str]
    sequence: Mapped[int] = mapped_column(Integer)
    llm_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("llm_runs.id", ondelete="SET NULL"))
    provider_key: Mapped[str | None]

    conversation: Mapped["ConversationRow"] = relationship(
        back_populates="messages", lazy="raise")


class ChatActionProposalRow(TimestampedMixin, Base):
    """One typed action the model proposed in a turn, awaiting a human's confirmation (§…).

    The persisted heart of "prose has zero authority": a row here is a *request* to act,
    parsed and validated out of the assistant's fenced block, that changes nothing until a
    human confirms it and the executor re-authorizes it. `UNIQUE (message_id, ordinal)` is
    the natural key `chat_action_proposal_id` derives from, so re-finalizing the turn lands
    on the same proposals. `kind` is stored as its own column so "my open submit proposals"
    is one indexed query, while `action` holds the whole validated `ChatAction` as JSONB —
    read back through `CHAT_ACTION_ADAPTER`, never trusted as free-form.
    """

    __tablename__ = "chat_action_proposals"
    __table_args__ = (
        UniqueConstraint("message_id", "ordinal"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        Index("ix_chat_action_proposals_user_id_status", "user_id", "status"),
        Index("ix_chat_action_proposals_conversation_id", "conversation_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"))
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"))
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)
    kind: Mapped[ChatActionKind] = mapped_column(
        enum_column(ChatActionKind, "chat_action_kind"))
    action: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON_OBJECT)
    status: Mapped[ChatActionProposalStatus] = mapped_column(
        enum_column(ChatActionProposalStatus, "chat_action_proposal_status"),
        server_default=text(f"'{ChatActionProposalStatus.PROPOSED.value}'"))
    summary: Mapped[str]

    conversation: Mapped["ConversationRow"] = relationship(
        back_populates="proposals", lazy="raise")


class ChatActionExecutionRow(TimestampedMixin, Base):
    """The record of one attempt to execute a confirmed proposal — the executor's audit (§…).

    `UNIQUE (proposal_id)` matches the derivation `chat_action_execution_id` performs from
    the proposal alone, so a double-confirmed proposal collides on this row rather than
    running the underlying service action twice — the idempotency the executor rests on.
    `outcome` records whether the action was refused at validation (`REJECTED`), permitted
    but failed (`FAILED`), or ran (`SUCCEEDED`); `result_ref` and `detail` are secret-free
    handles the chat shows without re-deriving what happened.
    """

    __tablename__ = "chat_action_executions"
    __table_args__ = (
        UniqueConstraint("proposal_id"),
        Index("ix_chat_action_executions_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_action_proposals.id", ondelete="CASCADE"))
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    outcome: Mapped[ChatActionExecutionOutcome] = mapped_column(
        enum_column(ChatActionExecutionOutcome, "chat_action_execution_outcome"))
    detail: Mapped[str | None]
    result_ref: Mapped[str | None]


# ---------------------------------------------------------------------------
# Phase 14 — the adaptive interview simulator. A session, the questions its engine
# asked, the candidate's one answer per question, the structured evaluation of each
# answer, and the coaching summary a completed session produces. The CHECKs restate
# the domain invariants that a column group can express, so a row written outside the
# mapper is refused exactly as a `model_validator` would refuse it — most pointedly
# the follow-up shape and the "readiness is never on an evaluation" absence.
# ---------------------------------------------------------------------------

# `InterviewSession._ended_at_matches_terminal_status`, as a CHECK: a session carries
# an `ended_at` exactly when its status is terminal, and never otherwise.
_INTERVIEW_SESSION_ENDED_AT_MATCHES_STATUS: Final[str] = (
    "(status IN ('COMPLETED', 'ABANDONED')) = (ended_at IS NOT NULL)")

# `InterviewQuestion._follow_up_shape_is_coherent`, as a CHECK: a primary question
# (depth 0) follows nothing, and a follow-up (depth > 0) names an earlier question.
_INTERVIEW_QUESTION_FOLLOW_UP_SHAPE: Final[str] = (
    "(depth = 0 AND follows_sequence IS NULL)"
    " OR (depth > 0 AND follows_sequence IS NOT NULL AND follows_sequence < sequence)")

# `InterviewAnswer._transcript_confidence_only_for_voice`, as a CHECK: only a VOICE
# answer has a transcription to be unsure about; a TEXT answer carries no confidence.
_INTERVIEW_ANSWER_CONFIDENCE_ONLY_FOR_VOICE: Final[str] = (
    "format <> 'TEXT' OR transcript_confidence IS NULL")


class InterviewSessionRow(TimestampedMixin, Base):
    """One adaptive interview-practice session, owned by exactly one account (§1-6).

    User-owned like every Phase 4+ entity: `user_id` cascades from `users`, so deleting
    an account takes its sessions — and, through the cascades below, their questions,
    answers, evaluations and summary — with it. `candidate_profile_id` and
    `opportunity_id` cascade too (the practice is meaningless without the profile it
    rehearses and the role it targets), but `application_id` is `SET NULL`: a session
    may be *for* an application, yet it outlives one that is later withdrawn. `plan` is
    the frozen coverage plan as JSONB; the CHECK enforces the terminal/`ended_at`
    invariant, and the index serves the "my sessions, most recent first" list.
    """

    __tablename__ = "interview_sessions"
    __table_args__ = (
        CheckConstraint(_INTERVIEW_SESSION_ENDED_AT_MATCHES_STATUS,
                        name="ended_at_matches_status"),
        _code_format("language", "^[a-z]{2}$"),
        Index("ix_interview_sessions_user_id_updated_at", "user_id", "updated_at"),
        Index("ix_interview_sessions_opportunity_id", "opportunity_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    candidate_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"))
    opportunity_id: Mapped[UUID] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"))
    application_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"))
    mode: Mapped[InterviewMode] = mapped_column(
        enum_column(InterviewMode, "interview_mode"))
    style: Mapped[SessionStyle] = mapped_column(
        enum_column(SessionStyle, "interview_session_style"),
        server_default=text(f"'{SessionStyle.COACHING.value}'"))
    difficulty: Mapped[InterviewDifficulty] = mapped_column(
        enum_column(InterviewDifficulty, "interview_difficulty"),
        server_default=text(f"'{InterviewDifficulty.INTERMEDIATE.value}'"))
    status: Mapped[InterviewSessionStatus] = mapped_column(
        enum_column(InterviewSessionStatus, "interview_session_status"),
        server_default=text(f"'{InterviewSessionStatus.CREATED.value}'"))
    language: Mapped[str | None] = mapped_column(String(2))
    plan: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON_OBJECT)
    title: Mapped[str]
    ended_at: Mapped[datetime | None]

    questions: Mapped[list["InterviewQuestionRow"]] = relationship(
        back_populates="session", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise", order_by="InterviewQuestionRow.sequence")


class InterviewQuestionRow(TimestampedMixin, Base):
    """One question the engine asked, at one position in a session (§8-9).

    `UNIQUE (session_id, sequence)` is the natural key `interview_question_id` derives
    the primary key from, so re-finalizing a turn writes the same row rather than asking
    twice. `depth` and `follows_sequence` reconstruct the adaptive follow-up chain
    without a self-referential id; the CHECK restates
    `InterviewQuestion._follow_up_shape_is_coherent` so a primary can never claim to
    follow anything and a follow-up can never dangle. Written once and never mutated, so
    `asked_at` is the domain fact and the mixin timestamps are row bookkeeping.
    """

    __tablename__ = "interview_questions"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence"),
        CheckConstraint("sequence >= 0", name="sequence_non_negative"),
        CheckConstraint("depth BETWEEN 0 AND 2", name="depth_within_bounds"),
        CheckConstraint(_INTERVIEW_QUESTION_FOLLOW_UP_SHAPE,
                        name="follow_up_shape_coherent"),
        Index("ix_interview_questions_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"))
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    question_type: Mapped[InterviewQuestionType] = mapped_column(
        enum_column(InterviewQuestionType, "interview_question_type"))
    difficulty: Mapped[InterviewDifficulty] = mapped_column(
        enum_column(InterviewDifficulty, "interview_difficulty"))
    prompt: Mapped[str]
    topic_label: Mapped[str | None]
    follows_sequence: Mapped[int | None] = mapped_column(Integer)
    depth: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    generator_key: Mapped[str | None]
    asked_at: Mapped[datetime]

    session: Mapped["InterviewSessionRow"] = relationship(
        back_populates="questions", lazy="raise")


class InterviewAnswerRow(TimestampedMixin, Base):
    """The candidate's one, immutable answer to one question (§13, §52-53).

    `UNIQUE (question_id)` matches `interview_answer_id`'s derivation from the question
    alone, so a resubmit after a failed flush lands on the same row rather than recording
    the reply twice. `content` is the answer as text — for a VOICE answer the transcript,
    because the raw audio is discarded (§17). `transcript_confidence` exists only for a
    voice answer, which the CHECK enforces. Written once and never mutated.
    """

    __tablename__ = "interview_answers"
    __table_args__ = (
        UniqueConstraint("question_id"),
        _unit_interval("transcript_confidence"),
        CheckConstraint(_INTERVIEW_ANSWER_CONFIDENCE_ONLY_FOR_VOICE,
                        name="transcript_confidence_only_for_voice"),
        Index("ix_interview_answers_session_id", "session_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    question_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_questions.id", ondelete="CASCADE"))
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"))
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    format: Mapped[InterviewAnswerFormat] = mapped_column(
        enum_column(InterviewAnswerFormat, "interview_answer_format"))
    content: Mapped[str]
    transcript_confidence: Mapped[float | None]
    answered_at: Mapped[datetime]


class InterviewAnswerEvaluationRow(TimestampedMixin, Base):
    """The structured grade of one answer — and, pointedly, no readiness (§19-24, §33).

    The persisted half of "coaching, not prediction, enforced by absence": there is no
    readiness, probability or verdict column here, because the platform computes
    readiness later from a whole session's evaluations, never a provider per answer.
    `UNIQUE (answer_id)` matches `interview_answer_evaluation_id`'s derivation, so
    re-grading an answer overwrites its one evaluation rather than accreting a second —
    the idempotency `aggregate_session_readiness` rests on so a pair is never counted
    twice. `dimensions` is the per-axis grade as a JSONB array, re-validated on read.
    """

    __tablename__ = "interview_answer_evaluations"
    __table_args__ = (
        UniqueConstraint("answer_id"),
        _unit_interval("confidence"),
        Index("ix_interview_answer_evaluations_session_id", "session_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    answer_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_answers.id", ondelete="CASCADE"))
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"))
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    dimensions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    confidence: Mapped[float | None]
    strengths: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    improvements: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    suggested_answer: Mapped[str | None]
    evaluator_key: Mapped[str | None]
    evaluated_at: Mapped[datetime]


class InterviewSessionSummaryRow(TimestampedMixin, Base):
    """The coaching artefact produced when a session completes (§37-39, §73-74).

    `UNIQUE (session_id)` matches `interview_session_summary_id`'s derivation, so
    completing a session twice reuses the row rather than appending a second report.
    `readiness` is the deterministic `SessionReadiness` as JSONB — computed by the
    platform, re-validated on read — paired with the coaching prose (`headline`,
    `strengths`, `focus_areas`) the evidence guard cleared before persistence. The index
    serves the readiness history a candidate watches over repeated practice (§74);
    `created_at` is the domain fact (it carries no `onupdate`) and `updated_at` is the
    row-write bookkeeping the server default fills.
    """

    __tablename__ = "interview_session_summaries"
    __table_args__ = (
        UniqueConstraint("session_id"),
        CheckConstraint("questions_asked >= 0", name="questions_asked_non_negative"),
        CheckConstraint("answers_evaluated >= 0", name="answers_evaluated_non_negative"),
        Index("ix_interview_session_summaries_user_id_created_at",
              "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"))
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    readiness: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON_OBJECT)
    headline: Mapped[str]
    strengths: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    focus_areas: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    questions_asked: Mapped[int] = mapped_column(Integer)
    answers_evaluated: Mapped[int] = mapped_column(Integer)
    generator_key: Mapped[str | None]





