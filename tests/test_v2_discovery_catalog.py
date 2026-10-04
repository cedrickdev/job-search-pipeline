"""Swiss catalog source boundary and pure saved-search decisions."""

import pytest

from backend.app.domain.identifiers import new_opportunity_id
from backend.app.domain.opportunity import (
    ContractType,
    Opportunity,
    OpportunitySourceRecord,
    OpportunityType,
    WorkplaceMode,
)
from backend.app.domain.search import CountrySearchArea
from backend.app.discovery.requests import discovery_requests_for
from backend.app.discovery.registry import SourceRegistry
from country_packs.ch import pack as ch_pack
from tests.v2_discovery import NOW, FakeSource, a_metadata, a_search_profile


def an_opportunity(**overrides) -> Opportunity:
    fields = {
        "id": new_opportunity_id(),
        "source": OpportunitySourceRecord(source_key="jobup", fetched_at=NOW),
        "company_name": "Neocraft SA",
        "title": "Développeur Python",
        "discovered_at": NOW,
    }
    fields.update(overrides)
    return Opportunity(**fields)


def test_ch_request_excludes_linkedin_even_when_adapter_is_registered():
    pack = ch_pack.load()
    registry = SourceRegistry()
    registry.register(FakeSource(a_metadata("jobup")))
    registry.register(FakeSource(a_metadata("linkedin")))
    request = discovery_requests_for(a_search_profile(), country="CH")[0]

    selected = registry.sources_for(
        country=request.country, source_keys=request.source_keys, pack=pack
    )

    assert tuple(source.metadata.source_key for source in selected) == ("jobup",)
    assert pack.binding_for("linkedin") is not None
    assert not pack.binding_for("linkedin").enabled


def test_inactive_or_non_ch_search_cannot_match_the_ch_catalog():
    from backend.app.discovery.catalog import matches_search

    opportunity = an_opportunity()
    inactive = a_search_profile(is_active=False)
    french = a_search_profile(areas=(CountrySearchArea(country="FR"),))

    assert not matches_search(inactive, opportunity)
    assert discovery_requests_for(french, country="CH") == ()


@pytest.mark.parametrize(("profile_fields", "opportunity_fields", "expected"), [
    ({"opportunity_types": (OpportunityType.INTERNSHIP,)},
     {"opportunity_type": OpportunityType.FULL_TIME}, False),
    ({"contract_types": (ContractType.FIXED_TERM,)},
     {"contract_type": ContractType.PERMANENT}, False),
    ({"workplace_modes": (WorkplaceMode.REMOTE,)},
     {"workplace_mode": WorkplaceMode.ON_SITE}, False),
    ({"posting_languages": ("fr",)}, {"posting_language": "de"}, False),
    ({"excluded_keywords": ("PYTHON",)}, {}, False),
    ({"excluded_keywords": ("kubernetes",)},
     {"description": "Experience with KUBERNETES required"}, False),
    ({"title_keywords": ("designer",)}, {}, False),
    ({"title_keywords": ("PYTHON",)}, {}, True),
    ({"queries": ("java",)}, {}, True),
    ({"opportunity_types": (OpportunityType.INTERNSHIP,)},
     {"opportunity_type": OpportunityType.INTERNSHIP}, True),
])
def test_saved_search_filters_collected_opportunities(
    profile_fields, opportunity_fields, expected
):
    from backend.app.discovery.catalog import matches_search

    assert matches_search(
        a_search_profile(**profile_fields), an_opportunity(**opportunity_fields)
    ) is expected


def test_same_title_roles_with_different_verified_targets_remain_distinct():
    from backend.app.discovery.catalog import canonical_target

    first = an_opportunity(
        source=OpportunitySourceRecord(
            source_key="greenhouse", fetched_at=NOW,
            source_url="https://boards.greenhouse.io/acme/jobs/1001",
        ),
        dedup_fingerprint="same-company-and-title",
    )
    second = an_opportunity(
        source=OpportunitySourceRecord(
            source_key="greenhouse", fetched_at=NOW,
            source_url="https://boards.greenhouse.io/acme/jobs/1002",
        ),
        dedup_fingerprint="same-company-and-title",
    )

    assert canonical_target(first) == "https://boards.greenhouse.io/acme/jobs/1001"
    assert canonical_target(second) == "https://boards.greenhouse.io/acme/jobs/1002"


def test_cross_board_copy_uses_only_a_verified_ats_job_detail_target():
    from backend.app.discovery.catalog import canonical_target

    board_copy = an_opportunity(
        source=OpportunitySourceRecord(
            source_key="jobup", fetched_at=NOW,
            source_url="https://www.jobup.ch/en/jobs/detail/abc",
        ),
        application_url="https://jobs.lever.co/acme/role-123?utm_source=jobup#apply",
    )
    ats_copy = an_opportunity(
        source=OpportunitySourceRecord(
            source_key="lever", fetched_at=NOW,
            source_url="https://jobs.lever.co/acme/role-123",
        ),
    )

    assert canonical_target(board_copy) == canonical_target(ats_copy)
    assert canonical_target(ats_copy) == "https://jobs.lever.co/acme/role-123"


def test_board_url_and_company_title_are_not_canonical_targets():
    from backend.app.discovery.catalog import canonical_target

    board_only = an_opportunity(
        source=OpportunitySourceRecord(
            source_key="jobup", fetched_at=NOW,
            source_url="https://www.jobup.ch/en/jobs/detail/abc",
        ),
        dedup_fingerprint="same-company-and-title",
    )
    arbitrary_apply = an_opportunity(
        source=OpportunitySourceRecord(source_key="jobup", fetched_at=NOW),
        application_url="https://example.ch/apply/role-123",
        dedup_fingerprint="same-company-and-title",
    )

    assert canonical_target(board_only) is None
    assert canonical_target(arbitrary_apply) is None
