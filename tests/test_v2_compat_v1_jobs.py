# tests/test_v2_compat_v1_jobs.py
"""V1 `jobs` → V2 `Opportunity`: the proof Phase 1 owes, without a migration.

Two claims are under test here, and both are refusals as much as conversions.

*Nothing is lost*: every V1 column ends up either in a typed field or under a
`v1_` key in `source.raw`, asserted column by column against a row that a real
`pipeline.jobs.insert_job` wrote into a real SQLite database.

*Nothing is invented*: V1's `salary` is scraped text and its `contract_type` is
whatever vocabulary a board used, so "CHF 25/h" must not become a `SalaryRange`
and "CDI" must not become a `ContractType`. Those cases are the majority of this
file, because a fabricated fact about a real employer is the expensive failure —
a missing one only costs a re-parse in Phase 5.
"""
from datetime import UTC, datetime, timedelta, timezone

import pytest

from backend.app.compat.v1_jobs import (
    V1_JOB_COLUMNS,
    V1MappingError,
    opportunity_from_v1_job,
    opportunity_id_for_v1_job,
    v1_score_to_unit_interval,
)
from backend.app.domain.opportunity import ContractType, OpportunityType, WorkplaceMode
from pipeline.jobs import JOB_COLUMNS, insert_job

# A plausible V1 row, deliberately messy: the values are the shapes the V1
# adapters actually store (see docs/V1_BASELINE.md), not tidy ones.


def a_row(**overrides):
    fields = {
        "id": 7,
        "source": "wtj",
        "company": "Migros",
        "title": "Vendeur en boulangerie",
        "url": "https://boards.example.com/jobs/7",
        "location": "Yverdon-les-Bains, Suisse",
        "remote_policy": "onsite",
        "contract_type": "Part-time",
        "salary": "CHF 25/h",
        "description": "Vente et encaissement.",
        "language": "fr",
        "posted_date": "2026-06-15",
        "discovered_date": "2026-06-18T08:30:00",
        "dedup_hash": "b0a1c2d3",
        "track": "job",
    }
    fields.update(overrides)
    return fields


def test_the_mapper_knows_every_column_v1_actually_has(conn):
    """A tripwire: V1 growing a column must fail here, not lose data silently.

    `JOB_COLUMNS` is the insertable set; `id`, `discovered_date`, `dedup_hash`
    and `track` are filled by `insert_job` and `_migrate` instead, so the mapper
    lists all fifteen and this test checks the split is still the real one.
    """
    assert V1_JOB_COLUMNS[:len(JOB_COLUMNS)] == tuple(JOB_COLUMNS)
    assert set(V1_JOB_COLUMNS) - set(JOB_COLUMNS) == {
        "id", "discovered_date", "dedup_hash", "track"}
    live = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert live == set(V1_JOB_COLUMNS)


def test_a_row_v1_wrote_itself_maps_onto_an_opportunity(conn):
    """The round trip, through V1's own insert rather than a handwritten dict."""
    job_id, created = insert_job(conn, a_row(id=None))
    assert created
    row = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    opportunity = opportunity_from_v1_job(row)
    assert opportunity.id == opportunity_id_for_v1_job(job_id)
    assert opportunity.company_name == "Migros"
    assert opportunity.title == "Vendeur en boulangerie"
    assert opportunity.source.source_key == "wtj"
    assert opportunity.opportunity_type is OpportunityType.PART_TIME
    assert opportunity.workplace_mode is WorkplaceMode.ON_SITE
    assert opportunity.location.raw == "Yverdon-les-Bains, Suisse"
    assert opportunity.posting_language == "fr"
    assert opportunity.dedup_fingerprint == row["dedup_hash"]


def test_mapping_a_row_writes_nothing_back_to_v1(conn):
    """The compatibility layer reads V1; Phase 1 changes no V1 behaviour."""
    job_id, _ = insert_job(conn, a_row(id=None))
    before = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    opportunity_from_v1_job(before)
    after = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    assert after == before
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 1

# Where each V1 column lands as a typed value. Spelled out here rather than
# imported from the mapper, so the test states the contract independently.
_TYPED_DESTINATION = {
    "source": lambda o: o.source.source_key,
    "company": lambda o: o.company_name,
    "title": lambda o: o.title,
    "description": lambda o: o.description,
    "dedup_hash": lambda o: o.dedup_fingerprint,
}


def test_every_v1_column_survives_typed_or_verbatim_in_raw(conn):
    """Column by column: normalization is lossy, so `raw` carries the original."""
    job_id, _ = insert_job(conn, a_row(id=None))
    row = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    opportunity = opportunity_from_v1_job(row)
    raw = opportunity.source.raw
    for column in V1_JOB_COLUMNS:
        text = str(row[column]).strip()
        assert text, f"the fixture row should exercise {column}"
        if column in _TYPED_DESTINATION:
            assert _TYPED_DESTINATION[column](opportunity) == text
            assert f"v1_{column}" not in raw, "a typed field is not duplicated in raw"
        else:
            assert raw[f"v1_{column}"] == text, f"{column} would be lost"


def test_an_empty_v1_column_is_omitted_rather_than_stored_as_a_blank():
    """`""` in `raw` would read as "the source said nothing", which it did not."""
    opportunity = opportunity_from_v1_job(a_row(salary=None, posted_date="",
                                                location="   ", language=None))
    assert "v1_salary" not in opportunity.source.raw
    assert "v1_posted_date" not in opportunity.source.raw
    assert "v1_location" not in opportunity.source.raw
    assert opportunity.location is None
    assert opportunity.posted_at is None
    assert opportunity.posting_language is None
    assert opportunity.salary is None


def test_the_same_v1_row_always_derives_the_same_id():
    """Pinned, because Phase 2's import has to be safe to retry.

    A random id per run would duplicate every posting on the second attempt.
    """
    assert str(opportunity_id_for_v1_job(7)) == "120355f4-18b1-50f4-8412-c36f580ced1d"
    assert str(opportunity_id_for_v1_job(1)) == "495a7c3a-d696-5fd5-8305-41e9706be804"
    assert opportunity_from_v1_job(a_row()).id == opportunity_id_for_v1_job(7)
    assert opportunity_id_for_v1_job(7) != opportunity_id_for_v1_job(8)


@pytest.mark.parametrize("token,expected", [
    ("Full-time", OpportunityType.FULL_TIME),      # Lever
    ("FULL_TIME", OpportunityType.FULL_TIME),      # Welcome to the Jungle
    ("FullTime", OpportunityType.FULL_TIME),       # Ashby
    ("  full time  ", OpportunityType.FULL_TIME),
    ("Part-time", OpportunityType.PART_TIME),
    ("Internship", OpportunityType.INTERNSHIP),
    ("INTERN", OpportunityType.INTERNSHIP),
    ("Apprenticeship", OpportunityType.APPRENTICESHIP),
    ("Graduate", OpportunityType.GRADUATE),
    ("Temporary", OpportunityType.TEMPORARY),
    ("Freelance", OpportunityType.FREELANCE),
])
def test_one_ats_token_may_be_spelled_three_ways(token, expected):
    """Three boards, three spellings, one meaning — folded, not guessed."""
    assert opportunity_from_v1_job(a_row(contract_type=token)).opportunity_type is expected


@pytest.mark.parametrize("token,expected", [
    ("remote", WorkplaceMode.REMOTE),
    ("Fully Remote", WorkplaceMode.REMOTE),
    ("hybrid", WorkplaceMode.HYBRID),
    ("on-site", WorkplaceMode.ON_SITE),
    ("onsite", WorkplaceMode.ON_SITE),
])
def test_an_unambiguous_remote_policy_is_normalized(token, expected):
    assert opportunity_from_v1_job(a_row(remote_policy=token)).workplace_mode is expected


@pytest.mark.parametrize("token,expected", [
    ("Permanent", ContractType.PERMANENT),
    ("Fixed-term", ContractType.FIXED_TERM),
])
def test_the_two_country_neutral_contract_words_are_normalized(token, expected):
    assert opportunity_from_v1_job(a_row(contract_type=token)).contract_type is expected


@pytest.mark.parametrize("salary", ["CHF 25/h", "80-100%", "selon expérience",
                                    "à discuter", "competitive", "25"])
def test_a_scraped_salary_is_never_guessed_into_a_range(salary):
    """"80-100%" is a workload; a `SalaryRange` from it would invent a wage.

    CLAUDE.md forbids fabricating facts, and this is the one V1 column where a
    plausible-looking parse would attribute a number to a real employer.
    """
    opportunity = opportunity_from_v1_job(a_row(salary=salary))
    assert opportunity.salary is None
    assert opportunity.workload is None
    assert opportunity.source.raw["v1_salary"] == salary


@pytest.mark.parametrize("remote_policy", ["fulltime", "partial", "punctual", "no"])
def test_welcome_to_the_jungles_remote_vocabulary_stays_unclassified(remote_policy):
    """Its `remote` field answers "how much", not "from where".

    Reading "fulltime" as fully remote would put an on-site job in a remote
    search — the failure that costs a candidate an application, so the value
    waits in `raw` for a Phase 5 source-specific parser.
    """
    opportunity = opportunity_from_v1_job(a_row(remote_policy=remote_policy))
    assert opportunity.workplace_mode is None
    assert opportunity.is_remote is False
    assert opportunity.source.raw["v1_remote_policy"] == remote_policy


@pytest.mark.parametrize("contract_type", [
    "CDI", "CDD", "CDI 80-100%", "Temporärarbeit", "stage", "alternance",
    "Ausbildung", "Minijob", "apprenti", "Contract", "Autre",
])
def test_country_vocabulary_is_left_to_a_country_pack(contract_type):
    """The compatibility shim is not the place for local employment law.

    "Contract" is in this list too: on one board it is a fixed-term employee and
    on another an external contractor, so it is ambiguous even in English.
    """
    opportunity = opportunity_from_v1_job(a_row(contract_type=contract_type))
    assert opportunity.opportunity_type is None
    assert opportunity.contract_type is None
    assert opportunity.source.raw["v1_contract_type"] == contract_type


def test_the_v1_track_column_does_not_infer_an_opportunity_type():
    """V1's `track` sorts a search, it does not describe the posting.

    `track='travail'` means "found by a dev-track search", not "full time", and
    guessing otherwise would relabel every row of one whole V1 search.
    """
    opportunity = opportunity_from_v1_job(a_row(track="travail", contract_type=None))
    assert opportunity.opportunity_type is None
    assert opportunity.source.raw["v1_track"] == "travail"


def test_a_url_that_is_not_http_is_kept_but_never_typed():
    """`HttpUrlStr` exists so no adapter downstream is handed a scheme to open."""
    opportunity = opportunity_from_v1_job(a_row(url="mailto:jobs@example.com"))
    assert opportunity.source.source_url is None
    assert opportunity.source.raw["v1_url"] == "mailto:jobs@example.com"
    assert opportunity.application_url is None, "the posting page is not the apply page"


@pytest.mark.parametrize("posted_date", ["18.06.2026", "18/06/2026", "hier",
                                         "Publié il y a 3 jours", "2026-13-01"])
def test_an_unparseable_posting_date_costs_only_the_date(posted_date):
    """Dropping the whole posting over one malformed field would be the bug."""
    opportunity = opportunity_from_v1_job(a_row(posted_date=posted_date))
    assert opportunity.posted_at is None
    assert opportunity.title == "Vendeur en boulangerie"
    assert opportunity.source.raw["v1_posted_date"] == posted_date


def test_a_well_formed_posting_date_is_a_date_not_an_instant():
    """A board publishes on a day; pretending to know the hour invents precision."""
    opportunity = opportunity_from_v1_job(a_row(posted_date="2026-06-15"))
    assert opportunity.posted_at.isoformat() == "2026-06-15"


@pytest.mark.parametrize("language,expected", [
    ("fr", "fr"),
    ("FR", "fr"),
    ("de", "de"),
    ("fr-CH", None),      # a locale, not the two-letter code the domain declares
    ("français", None),
    ("", None),
])
def test_only_a_two_letter_language_code_is_typed(language, expected):
    assert opportunity_from_v1_job(a_row(language=language)).posting_language == expected


def test_a_local_timestamp_is_anchored_by_the_caller_not_by_a_guess():
    """V1 stores local time with no offset, so the zone comes from outside the data.

    The caller owns that decision — the mapper never reads a clock — and the
    original string stays in `raw` so a later pass can revisit the assumption.
    """
    opportunity = opportunity_from_v1_job(
        a_row(discovered_date="2026-06-18T08:30:00"),
        default_timezone=timezone(timedelta(hours=2)))
    assert opportunity.discovered_at == datetime(2026, 6, 18, 6, 30, tzinfo=UTC)
    assert opportunity.source.raw["v1_discovered_date"] == "2026-06-18T08:30:00"


def test_an_offset_v1_already_carries_is_respected():
    opportunity = opportunity_from_v1_job(a_row(discovered_date="2026-06-18T08:30:00+02:00"),
                                          default_timezone=UTC)
    assert opportunity.discovered_at == datetime(2026, 6, 18, 6, 30, tzinfo=UTC)


def test_a_date_only_discovery_stamp_is_midnight_in_the_callers_zone():
    """What `insert_job` writes by default: `datetime.now().date().isoformat()`."""
    opportunity = opportunity_from_v1_job(a_row(discovered_date="2026-06-18"),
                                          default_timezone=UTC)
    assert opportunity.discovered_at == datetime(2026, 6, 18, 0, 0, tzinfo=UTC)


def test_discovery_time_is_the_only_honest_fetch_time():
    """V1 records one instant per row; a different `fetched_at` would be invented."""
    opportunity = opportunity_from_v1_job(a_row())
    assert opportunity.source.fetched_at == opportunity.discovered_at
    assert opportunity.source.external_id is None, "V1's row id is local to its database"
    assert opportunity.source.raw["v1_id"] == "7"


@pytest.mark.parametrize("column", ["source", "company", "title", "discovered_date"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_row_missing_its_identity_or_provenance_refuses_to_map(column, value):
    with pytest.raises(V1MappingError) as failure:
        opportunity_from_v1_job(a_row(**{column: value}))
    assert column in str(failure.value)


@pytest.mark.parametrize("job_id", [None, "", "not-an-id", 1.5, "1.5", True])
def test_a_row_without_a_usable_integer_id_refuses_to_map(job_id):
    """The id is what makes the import idempotent, so it cannot be improvised.

    `1.5` is in the list because `int(1.5)` is `1`: a truncated id would derive
    the `OpportunityId` of a different V1 row and merge two postings.
    """
    with pytest.raises(V1MappingError):
        opportunity_from_v1_job(a_row(id=job_id))


def test_an_integral_id_from_a_json_export_is_accepted():
    """`sqlite3` returns an `int`, but a V1 export may quote it."""
    assert opportunity_from_v1_job(a_row(id=" 7 ")).id == opportunity_id_for_v1_job(7)


def test_an_unparseable_discovery_stamp_is_the_one_date_that_raises():
    """Unlike `posted_date`: `discovered_at` is required, so it cannot degrade."""
    with pytest.raises(V1MappingError) as failure:
        opportunity_from_v1_job(a_row(discovered_date="18/06/2026"))
    assert "discovered_date" in str(failure.value)
    assert "18/06/2026" in str(failure.value)


@pytest.mark.parametrize("v1_score,expected", [(0, 0.0), (47, 0.47), (73, 0.73),
                                               (100, 1.0)])
def test_a_v1_score_becomes_a_unit_interval_score(v1_score, expected):
    """V1's 0-100 integer is exactly what the V2 domain refuses to accept."""
    assert v1_score_to_unit_interval(v1_score) == pytest.approx(expected)


@pytest.mark.parametrize("v1_score", [-1, 101, 1000])
def test_a_score_outside_v1s_own_constraint_is_corruption_not_a_clamp(v1_score):
    """V1's `scores` table CHECKs 0-100, so a stray value means a broken row."""
    with pytest.raises(V1MappingError):
        v1_score_to_unit_interval(v1_score)
