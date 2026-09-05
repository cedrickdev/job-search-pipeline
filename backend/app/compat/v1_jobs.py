"""Map a V1 `jobs` row onto a V2 `Opportunity`.

This module is the proof Phase 1 owes: V1's data fits the new domain language
without touching V1's database. It reads a row, it never writes one, and the V1
`jobs` table keeps its name and its shape (the Phase 1 order forbids renaming or
deleting it).

Two properties matter more than the field-by-field mapping:

**Nothing is invented.** V1 stores `salary` as scraped text ("CHF 25/h",
"80-100%"), and `remote_policy`/`contract_type` as whatever vocabulary each board
happened to use — Lever's `workplaceType`, Ashby's `employmentType`, Welcome to
the Jungle's `remote`, and French prose from Migros and Manpower. Only tokens that
are unambiguous on their own are normalized; everything else stays unclassified.
A `SalaryRange` guessed out of "80-100%" (a workload, not a wage) would be a
fabricated fact about a real employer, which CLAUDE.md forbids outright.

**Nothing is lost.** Every V1 column that is not copied verbatim into a typed
field is preserved as text under a `v1_` key in `source.raw`, so a better parser
in Phase 5 or 6 can re-run over the same input — the reason
docs/LLM_PROVIDER_ARCHITECTURE.md §11 requires `raw` in the first place. A test
asserts that column-by-column.
"""
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, tzinfo
from enum import StrEnum
from hashlib import sha256
from typing import Any, Final
from uuid import UUID, uuid5

from backend.app.domain.base import Score
from backend.app.domain.common import Location
from backend.app.domain.identifiers import OpportunityId
from backend.app.domain.opportunity import (
    ContractType,
    Opportunity,
    OpportunitySourceRecord,
    OpportunityType,
    WorkplaceMode,
)


class V1MappingErrorCode(StrEnum):
    """Why a V1 row could not be mapped, in a form a report can count.

    Added for Phase 2's importer: a migration over thousands of rows has to say
    *what* was wrong with the 12 it skipped, and grouping on a message that
    embeds an id and a value groups nothing. The message stays for a human; the
    code is for the report (docs/PERSISTENCE.md §Import).
    """

    ID_INVALID = "V1_ID_INVALID"
    REQUIRED_COLUMN_MISSING = "V1_REQUIRED_COLUMN_MISSING"
    DISCOVERED_DATE_INVALID = "V1_DISCOVERED_DATE_INVALID"
    SCORE_OUT_OF_RANGE = "V1_SCORE_OUT_OF_RANGE"
    # For a subclass or a caller that raises without classifying.
    UNSPECIFIED = "V1_MAPPING_FAILED"


class V1MappingError(ValueError):
    """A V1 row cannot be represented as an `Opportunity`.

    Raised only for missing identity or provenance (`id`, `source`, `company`,
    `title`, `discovered_date`). Unparseable *descriptive* values never raise:
    they stay in `source.raw` and leave the typed field unset, because dropping
    one malformed date must not cost the whole posting.

    `code` is keyword-only and defaulted, so it is additive: `raise
    V1MappingError("...")` still works and still reads as a `ValueError`.
    """

    def __init__(self, message: str, *,
                 code: V1MappingErrorCode = V1MappingErrorCode.UNSPECIFIED) -> None:
        super().__init__(message)
        self.code = code


# The V1 `jobs` columns this mapper knows about (pipeline/db.py `SCHEMA`).
# The first eleven are exactly `pipeline.jobs.JOB_COLUMNS`, the insertable set,
# which is why they lead; `id`, `discovered_date` and `dedup_hash` are written by
# the insert itself and `track` was added by `_migrate`. A test compares this
# tuple against V1 — and against a live `PRAGMA table_info(jobs)` — so a schema
# change fails loudly instead of silently dropping a column.
V1_JOB_COLUMNS: Final[tuple[str, ...]] = (
    "source", "company", "title", "url", "location", "remote_policy",
    "contract_type", "salary", "description", "language", "posted_date",
    "id", "discovered_date", "dedup_hash", "track",
)

# Columns copied verbatim into a typed field, so `raw` would only duplicate them.
_VERBATIM_COLUMNS: Final[frozenset[str]] = frozenset({
    "source", "company", "title", "description", "dedup_hash",
})

# Fixed namespace so a V1 row always derives the same `OpportunityId`, in this
# process and in Phase 2's import. A random id per run would create duplicates
# the moment the migration is retried.
V1_OPPORTUNITY_NAMESPACE: Final[UUID] = UUID("027137ea-69a3-48cc-8e92-fe489f5f3dec")

_LANGUAGE_CODE_RE = re.compile(r"^[a-z]{2}$")

# `remote_policy` → `WorkplaceMode`. Ashby, Indeed and Indeed CH write the literal
# "remote"; Lever passes its `workplaceType` ("onsite" / "remote" / "hybrid").
# Welcome to the Jungle's values ("fulltime", "partial", "punctual", "no") are
# absent on purpose: "fulltime" reads as a workload, not as full remote, and a
# wrong guess here would put an on-site job in a remote search.
V1_WORKPLACE_MODE_BY_TOKEN: Final[Mapping[str, WorkplaceMode]] = {
    "remote": WorkplaceMode.REMOTE,
    "fully remote": WorkplaceMode.REMOTE,
    "hybrid": WorkplaceMode.HYBRID,
    "on site": WorkplaceMode.ON_SITE,
    "onsite": WorkplaceMode.ON_SITE,
}

# `contract_type` → `OpportunityType`. V1 stores one free-text column for what V2
# splits in two, so the same input feeds this table and the next one. Only
# self-explanatory English/ATS tokens appear: "stage", "alternance", "CDI" and
# "Temporärarbeit" are country vocabulary, and docs/V2_SPECIFICATION.md §5 puts
# those in a Country Pack (Phase 5), not in a compatibility shim.
V1_OPPORTUNITY_TYPE_BY_TOKEN: Final[Mapping[str, OpportunityType]] = {
    "full time": OpportunityType.FULL_TIME,
    "fulltime": OpportunityType.FULL_TIME,
    "part time": OpportunityType.PART_TIME,
    "parttime": OpportunityType.PART_TIME,
    "intern": OpportunityType.INTERNSHIP,
    "internship": OpportunityType.INTERNSHIP,
    "apprenticeship": OpportunityType.APPRENTICESHIP,
    "graduate": OpportunityType.GRADUATE,
    "student job": OpportunityType.STUDENT_JOB,
    "work study": OpportunityType.WORK_STUDY,
    "temporary": OpportunityType.TEMPORARY,
    "freelance": OpportunityType.FREELANCE,
}

# `contract_type` → `ContractType`. Two entries, because these are the only
# tokens V1 can produce whose legal meaning is unambiguous without a country.
# "Contract" is not among them: on one board it means a fixed-term employee and
# on another an external contractor.
V1_CONTRACT_TYPE_BY_TOKEN: Final[Mapping[str, ContractType]] = {
    "permanent": ContractType.PERMANENT,
    "fixed term": ContractType.FIXED_TERM,
}


def opportunity_id_for_v1_job(job_id: int) -> OpportunityId:
    """The stable V2 id of a V1 row.

    Derived with uuid5 so importing the same row twice yields the same
    `Opportunity`, which is what makes Phase 2's migration safe to retry.
    """
    return OpportunityId(uuid5(V1_OPPORTUNITY_NAMESPACE, f"v1:jobs:{job_id}"))


def v1_dedup_fingerprint(company: str, title: str) -> str:
    """V1's cross-source deduplication key, byte-for-byte.

    `pipeline.jobs.dedup_hash` is what V1's `jobs.dedup_hash` column already
    holds, and Phase 2's import copies those values across. A live V2 sweep has to
    compute the *same* hash for the same posting, or the first sweep after the
    migration re-inserts every row V1 already had. Reimplemented rather than
    imported so `backend` keeps no dependency on `pipeline`
    (tests/test_v2_domain_purity.py); a test asserts the two agree on the V1
    fixtures, which is the check that keeps this honest.
    """
    normalized = (re.sub(r"[^a-z0-9]", "", part.lower()) for part in (company, title))
    return sha256("|".join(normalized).encode()).hexdigest()


def v1_score_to_unit_interval(score: int) -> Score:
    """Convert a V1 0-100 score to the unit interval V2 scores use.

    V1's `scores` table constrains the column to 0-100, so a value outside that
    range means the row is corrupt and is worth failing on rather than clamping.
    """
    if not 0 <= score <= 100:
        raise V1MappingError(f"V1 score {score} is outside the 0-100 range",
                             code=V1MappingErrorCode.SCORE_OUT_OF_RANGE)
    return score / 100.0


def v1_token(value: str) -> str:
    """Normalize a vendor token: lower case, one space between words.

    Folds the three spellings the boards actually use — "Full-time" (Lever),
    "FULL_TIME" (Welcome to the Jungle) and "FullTime" (Ashby) — onto one key.
    """
    return re.sub(r"[\s_-]+", " ", value.strip().lower())


def v1_text(row: Mapping[str, Any], column: str) -> str | None:
    """A column as trimmed text, or `None` when absent, NULL or blank."""
    value = row.get(column)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_text(row: Mapping[str, Any], column: str) -> str:
    text = v1_text(row, column)
    if text is None:
        raise V1MappingError(
            f"V1 jobs row is missing required column {column!r}",
            code=V1MappingErrorCode.REQUIRED_COLUMN_MISSING)
    return text


def v1_parse_date(value: str) -> date | None:
    """V1 writes `YYYY-MM-DD`; anything else is left for `raw` to carry."""
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_instant(value: str, default_timezone: tzinfo) -> datetime | None:
    """Parse a V1 timestamp, attaching a zone if the stored value has none.

    `insert_job` writes `datetime.now().date().isoformat()` — a local date, no
    time and no offset — and `migrate_tracker` passes a tracker date through, so
    the zone has to come from outside the data. The caller's `default_timezone`
    is that decision, and the original string stays in `raw` so a later pass can
    revisit it (see the module docstring).
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=default_timezone)


def opportunity_from_v1_job(row: Mapping[str, Any], *,
                            default_timezone: tzinfo = UTC) -> Opportunity:
    """Map one V1 `jobs` row onto an `Opportunity`.

    `row` is plain data — `dict(sqlite3.Row)` from a V1 query — so this module
    never imports `pipeline` or `sqlite3` and V1 needs no change to be read.

    Raises `V1MappingError` when identity or provenance is missing; descriptive
    columns that cannot be parsed are preserved in `source.raw` instead.
    """
    try:
        # Converted through `str`, so a float or a bool cannot be truncated onto
        # the id of a *different* V1 row — `int(1.5)` is 1, and two postings
        # silently merged into one id is worse than a row that refuses to map.
        # Integral text is still accepted: a JSON export of V1 rows is a
        # legitimate input, and `sqlite3` itself returns an `int` here.
        job_id = int(str(row["id"]).strip())
    except (KeyError, TypeError, ValueError) as exc:
        raise V1MappingError(f"V1 jobs row has no usable integer id: {exc}",
                             code=V1MappingErrorCode.ID_INVALID) from exc

    discovered_text = _required_text(row, "discovered_date")
    discovered_at = _parse_instant(discovered_text, default_timezone)
    if discovered_at is None:
        raise V1MappingError(
            f"V1 jobs row {job_id} has an unparseable discovered_date: "
            f"{discovered_text!r}",
            code=V1MappingErrorCode.DISCOVERED_DATE_INVALID)

    url = v1_text(row, "url")
    # Anything that is not http(s) — a `mailto:` or a scraped fragment — is left
    # out of the typed field: `HttpUrlStr` exists so no adapter downstream can be
    # handed a scheme it should not open.
    source_url = url if url is not None and url.startswith(("http://", "https://")) \
        else None

    location_text = v1_text(row, "location")
    remote_token = v1_text(row, "remote_policy")
    contract_token = v1_text(row, "contract_type")
    normalized_contract = v1_token(contract_token) if contract_token is not None else ""
    language = v1_text(row, "language")
    posted_text = v1_text(row, "posted_date")

    return Opportunity(
        id=opportunity_id_for_v1_job(job_id),
        source=OpportunitySourceRecord(
            source_key=_required_text(row, "source"),
            # V1 keeps no per-source identifier, and its row id is not one: it is
            # local to this database, so it travels in `raw` as `v1_id`.
            external_id=None,
            source_url=source_url,
            # V1 records one instant per row, so discovery time is the best
            # available fetch time. V2 stores the two separately, but no V1 row
            # can tell them apart; inventing a different value here would be
            # worse than reusing an honest one.
            fetched_at=discovered_at,
            raw=_raw_snapshot(row),
        ),
        company_name=_required_text(row, "company"),
        title=_required_text(row, "title"),
        description=v1_text(row, "description"),
        opportunity_type=V1_OPPORTUNITY_TYPE_BY_TOKEN.get(normalized_contract),
        contract_type=V1_CONTRACT_TYPE_BY_TOKEN.get(normalized_contract),
        workplace_mode=(V1_WORKPLACE_MODE_BY_TOKEN.get(v1_token(remote_token))
                        if remote_token is not None else None),
        # Free text, unparsed: "Yverdon-les-Bains, Suisse" becomes `Location.raw`
        # and a Phase 7 geocoding pass fills city, country and point.
        location=Location(raw=location_text) if location_text is not None else None,
        posting_language=(language.lower()
                          if language is not None and _LANGUAGE_CODE_RE.match(
                              language.lower()) else None),
        posted_at=v1_parse_date(posted_text) if posted_text is not None else None,
        discovered_at=discovered_at,
        # V1's `url` is the posting page, which is not necessarily where one
        # applies, so it is not promoted to `application_url`.
        application_url=None,
        dedup_fingerprint=v1_text(row, "dedup_hash"),
    )


def _raw_snapshot(row: Mapping[str, Any]) -> dict[str, str]:
    """Every V1 value not copied verbatim into a typed field, as text.

    Keys are prefixed `v1_` so a snapshot of a V1 row can never be confused with
    a snapshot of a live source payload.
    """
    snapshot: dict[str, str] = {}
    for column in V1_JOB_COLUMNS:
        if column in _VERBATIM_COLUMNS:
            continue
        text = v1_text(row, column)
        if text is not None:
            snapshot[f"v1_{column}"] = text
    return snapshot
