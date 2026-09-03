# tests/test_v2_opportunity.py
"""`Opportunity` — the V2 central abstraction.

Two things are being pinned here beyond ordinary field validation: the enum
membership (the phase order requires nine opportunity types and forbids country
vocabulary in the universal model), and the honesty of the unset value — `None`
means "not classified", never "some default".
"""
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from backend.app.domain.common import (
    LanguageLevel,
    LanguageRequirement,
    Location,
    WorkloadRange,
)
from backend.app.domain.identifiers import new_company_id, new_opportunity_id
from backend.app.domain.opportunity import (
    ContractType,
    Opportunity,
    OpportunitySourceRecord,
    OpportunityType,
    WorkplaceMode,
)

FETCHED_AT = datetime(2026, 3, 1, 6, 0, tzinfo=UTC)


def a_source(**overrides):
    fields = {"source_key": "lever", "fetched_at": FETCHED_AT}
    fields.update(overrides)
    return OpportunitySourceRecord(**fields)


def an_opportunity(**overrides):
    """The five non-negotiable fields, and nothing else."""
    fields = {
        "id": new_opportunity_id(),
        "source": a_source(),
        "company_name": "Migros Vaud",
        "title": "Vendeur polyvalent",
        "discovered_at": FETCHED_AT,
    }
    fields.update(overrides)
    return Opportunity(**fields)


def test_a_search_hit_is_enough_to_build_an_opportunity():
    """Discovery is incremental: identity, provenance, employer, title, instant."""
    opportunity = an_opportunity()
    assert opportunity.company_name == "Migros Vaud"
    assert opportunity.description is None
    assert opportunity.opportunity_type is None
    assert opportunity.contract_type is None
    assert opportunity.workplace_mode is None
    assert opportunity.workload is None
    assert opportunity.salary is None
    assert opportunity.location is None
    assert opportunity.posting_language is None
    assert opportunity.language_requirements == ()
    assert opportunity.posted_at is None
    assert opportunity.application_url is None
    assert opportunity.company_id is None
    assert opportunity.dedup_fingerprint is None


@pytest.mark.parametrize("missing",
                         ["id", "source", "company_name", "title", "discovered_at"])
def test_an_opportunity_that_cannot_say_who_or_what_is_refused(missing):
    fields = {
        "id": new_opportunity_id(),
        "source": a_source(),
        "company_name": "Migros Vaud",
        "title": "Vendeur polyvalent",
        "discovered_at": FETCHED_AT,
    }
    del fields[missing]
    with pytest.raises(ValidationError):
        Opportunity(**fields)


@pytest.mark.parametrize("field", ["company_name", "title"])
def test_a_blank_employer_or_title_is_refused(field):
    with pytest.raises(ValidationError):
        an_opportunity(**{field: "   "})


def test_an_invented_field_is_refused():
    """`extra="forbid"`: an LLM that hallucinates a key must fail loudly."""
    with pytest.raises(ValidationError) as failure:
        an_opportunity(seniority_guess="junior")
    assert "seniority_guess" in str(failure.value)


def test_the_nine_required_opportunity_types_are_present_and_alone():
    """The phase order lists exactly these nine.

    Asserted as an equality, not a superset: adding a tenth member is a domain
    decision that must be made deliberately, and the most likely accidental
    addition is a country-specific one.
    """
    assert {member.value for member in OpportunityType} == {
        "FULL_TIME", "PART_TIME", "STUDENT_JOB", "INTERNSHIP", "APPRENTICESHIP",
        "WORK_STUDY", "GRADUATE", "TEMPORARY", "FREELANCE",
    }


def test_the_universal_model_carries_no_country_vocabulary():
    """"alternance" is `WORK_STUDY` here; the local word lives in a Country Pack."""
    values = {member.value
              for enum in (OpportunityType, WorkplaceMode, ContractType)
              for member in enum}
    for local_word in ("ALTERNANCE", "STAGE", "APPRENTI", "CDI", "CDD",
                       "TEMPORAERARBEIT", "MINIJOB", "DUALES_STUDIUM",
                       "AUSBILDUNG", "INTERIM"):
        assert local_word not in values


def test_workplace_and_contract_vocabularies_are_pinned():
    assert {member.value for member in WorkplaceMode} == {
        "ON_SITE", "HYBRID", "REMOTE"}
    assert {member.value for member in ContractType} == {
        "PERMANENT", "FIXED_TERM", "TEMPORARY_AGENCY", "SERVICE_CONTRACT"}


@pytest.mark.parametrize("mode,expected", [
    (WorkplaceMode.REMOTE, True),
    (WorkplaceMode.HYBRID, False),
    (WorkplaceMode.ON_SITE, False),
    (None, False),
])
def test_is_remote_is_true_only_for_remote(mode, expected):
    assert an_opportunity(workplace_mode=mode).is_remote is expected


def test_type_and_mode_and_contract_vary_independently():
    """An internship can be fixed-term and hybrid; none implies another."""
    opportunity = an_opportunity(opportunity_type=OpportunityType.INTERNSHIP,
                                 contract_type=ContractType.FIXED_TERM,
                                 workplace_mode=WorkplaceMode.HYBRID)
    assert opportunity.opportunity_type is OpportunityType.INTERNSHIP
    assert opportunity.contract_type is ContractType.FIXED_TERM
    assert opportunity.workplace_mode is WorkplaceMode.HYBRID


def test_a_language_cannot_be_required_twice():
    with pytest.raises(ValidationError) as failure:
        an_opportunity(language_requirements=(
            LanguageRequirement(language="fr", minimum_level=LanguageLevel.B2),
            LanguageRequirement(language="fr", minimum_level=LanguageLevel.C1),
        ))
    assert "must not repeat a language" in str(failure.value)


def test_several_languages_may_be_required_at_once():
    opportunity = an_opportunity(language_requirements=(
        LanguageRequirement(language="fr", minimum_level=LanguageLevel.C1),
        LanguageRequirement(language="de", minimum_level=LanguageLevel.B1,
                            required=False),
    ))
    assert len(opportunity.language_requirements) == 2


def test_the_language_of_the_text_is_not_a_demand_on_the_candidate():
    """A French posting does not fabricate a "French required" requirement."""
    opportunity = an_opportunity(posting_language="fr")
    assert opportunity.posting_language == "fr"
    assert opportunity.language_requirements == ()


def test_the_employer_string_survives_canonicalization():
    """At discovery the employer is a string; Phase 6 adds the id beside it."""
    company_id = new_company_id()
    opportunity = an_opportunity(company_name="Migros Vaud", company_id=company_id)
    assert opportunity.company_name == "Migros Vaud"
    assert opportunity.company_id == company_id


def test_the_descriptive_half_is_carried_verbatim_when_present():
    opportunity = an_opportunity(
        description="Service clientèle en magasin.",
        location=Location(raw="Yverdon-les-Bains, Suisse"),
        workload=WorkloadRange(min_percent=40, max_percent=60),
        posted_at=date(2026, 2, 14),
        application_url="https://jobs.example.test/apply/1",
        dedup_fingerprint="9f2b" * 16,
    )
    assert opportunity.location.raw == "Yverdon-les-Bains, Suisse"
    assert opportunity.workload.max_percent == 60
    assert opportunity.posted_at == date(2026, 2, 14)
    assert opportunity.dedup_fingerprint == "9f2b" * 16


def test_a_source_record_starts_with_an_empty_raw_snapshot():
    record = a_source()
    assert record.raw == {}
    assert record.external_id is None
    assert record.source_url is None


def test_a_source_key_is_a_plain_string_so_a_board_is_a_plugin():
    """No enum: adding a board must not require editing the domain."""
    for source_key in ("lever", "ashby", "wtj", "migros", "a-new-board-2026"):
        assert a_source(source_key=source_key).source_key == source_key
    with pytest.raises(ValidationError):
        a_source(source_key="  ")


def test_a_source_url_must_be_fetchable():
    assert a_source(source_url="https://jobs.example.test/1").source_url
    for rejected in ("mailto:jobs@example.test", "javascript:void(0)", "/jobs/1"):
        with pytest.raises(ValidationError):
            a_source(source_url=rejected)


def test_a_fetch_instant_cannot_be_ambiguous():
    with pytest.raises(ValidationError):
        a_source(fetched_at=datetime(2026, 3, 1, 6, 0))


def test_raw_keeps_what_the_board_literally_said():
    """Normalization is lossy and revisable, so the un-normalized text survives."""
    record = a_source(raw={"employmentType": "FullTime", "workplaceType": "hybrid"})
    assert record.raw["employmentType"] == "FullTime"


def test_a_record_carrying_raw_is_equal_by_value_but_unhashable():
    """The one documented consequence of a mapping field on a frozen model."""
    left = an_opportunity(source=a_source(raw={"employmentType": "FullTime"}))
    right = left.model_copy()
    assert left == right
    with pytest.raises(TypeError):
        hash(left)
