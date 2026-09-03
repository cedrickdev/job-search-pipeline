# tests/test_v2_search.py
"""`SearchProfile` and the `SearchArea` union.

V1 keeps this in `config/searches.yaml`: free-text locations and a keyword
blacklist that excludes "stage", "alternance" and "apprenti" — the very
opportunity types V2 makes first-class. So two properties matter here: an area
cannot be malformed (that is what the discriminated union buys), and a
restriction is expressed as an allow-list rather than as excluded words.
"""
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.app.domain.common import GeoPoint, WorkloadRange
from backend.app.domain.identifiers import new_search_profile_id, new_user_id
from backend.app.domain.opportunity import ContractType, OpportunityType, WorkplaceMode
from backend.app.domain.search import (
    CountrySearchArea,
    RadiusSearchArea,
    RemoteOnlySearchArea,
    SearchAreaKind,
    SearchProfile,
)

CREATED_AT = datetime(2026, 2, 1, 8, 0, tzinfo=UTC)
UPDATED_AT = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
YVERDON = GeoPoint(latitude=46.7785, longitude=6.6411)


def a_profile(**overrides):
    fields = {
        "id": new_search_profile_id(),
        "user_id": new_user_id(),
        "name": "Student jobs, Vaud",
        "areas": (CountrySearchArea(country="CH"),),
        "created_at": CREATED_AT,
        "updated_at": UPDATED_AT,
    }
    fields.update(overrides)
    return SearchProfile(**fields)


def test_the_three_area_shapes_construct_and_tag_themselves():
    assert CountrySearchArea(country="CH").kind is SearchAreaKind.COUNTRY
    assert RadiusSearchArea(center=YVERDON, radius_km=25.0).kind is SearchAreaKind.RADIUS
    assert RemoteOnlySearchArea().kind is SearchAreaKind.REMOTE_ONLY


def test_an_area_is_parsed_back_into_its_own_class_by_its_tag():
    """The union has to survive a round trip through plain data (Phase 2, an API)."""
    profile = a_profile(areas=(
        {"kind": "COUNTRY", "country": "CH"},
        {"kind": "RADIUS", "center": {"latitude": 46.7785, "longitude": 6.6411},
         "radius_km": 25.0},
        {"kind": "REMOTE_ONLY", "country": "CH"},
    ))
    assert isinstance(profile.areas[0], CountrySearchArea)
    assert isinstance(profile.areas[1], RadiusSearchArea)
    assert isinstance(profile.areas[2], RemoteOnlySearchArea)
    assert profile.areas[1].radius_km == 25.0


def test_an_area_cannot_be_malformed():
    with pytest.raises(ValidationError):   # a country has no radius
        a_profile(areas=({"kind": "COUNTRY", "country": "CH", "radius_km": 25.0},))
    with pytest.raises(ValidationError):   # a radius cannot omit its size
        a_profile(areas=({"kind": "RADIUS",
                          "center": {"latitude": 46.8, "longitude": 6.6}},))
    with pytest.raises(ValidationError):   # nor its centre
        a_profile(areas=({"kind": "RADIUS", "radius_km": 25.0},))
    with pytest.raises(ValidationError):   # nor say which shape it is
        a_profile(areas=({"country": "CH"},))


@pytest.mark.parametrize("radius_km", [0.0, -5.0, 500.1, 20000.0])
def test_a_radius_must_be_a_usable_distance(radius_km):
    with pytest.raises(ValidationError):
        RadiusSearchArea(center=YVERDON, radius_km=radius_km)


def test_a_radius_area_deliberately_has_no_country():
    """A 30 km circle around Geneva covers two countries.

    Right-to-work is an eligibility question, not a discovery filter, so adding
    a country here would silently drop the French side of a commute.
    """
    assert "country" not in RadiusSearchArea.model_fields


def test_a_search_with_no_area_would_sweep_the_planet():
    with pytest.raises(ValidationError):
        a_profile(areas=())


def test_an_empty_collection_means_no_restriction():
    """Stated once in the class docstring, asserted once here."""
    profile = a_profile()
    assert profile.opportunity_types == ()
    assert profile.allows_opportunity_type(OpportunityType.STUDENT_JOB)
    assert profile.allows_opportunity_type(OpportunityType.FREELANCE)
    assert profile.allows_source("lever")
    assert profile.allows_source("a-board-nobody-has-written-yet")


def test_an_allow_list_replaces_v1s_keyword_blacklist():
    """V1 excludes the word "stage"; V2 asks for `INTERNSHIP` explicitly."""
    profile = a_profile(opportunity_types=(OpportunityType.STUDENT_JOB,
                                           OpportunityType.INTERNSHIP,
                                           OpportunityType.APPRENTICESHIP))
    assert profile.allows_opportunity_type(OpportunityType.INTERNSHIP)
    assert not profile.allows_opportunity_type(OpportunityType.FULL_TIME)


def test_an_unclassified_posting_still_passes_discovery():
    """Dropping unknowns here looks exactly like a broken source."""
    profile = a_profile(opportunity_types=(OpportunityType.STUDENT_JOB,))
    assert profile.allows_opportunity_type(None)


def test_a_source_allow_list_narrows_the_sweep():
    profile = a_profile(source_keys=("lever", "ashby"))
    assert profile.allows_source("lever")
    assert not profile.allows_source("wtj")


@pytest.mark.parametrize("areas,modes,expected", [
    ((RemoteOnlySearchArea(),), (), True),
    ((CountrySearchArea(country="CH"),), (WorkplaceMode.REMOTE,), True),
    ((CountrySearchArea(country="CH"),), (), True),        # no restriction at all
    ((CountrySearchArea(country="CH"),), (WorkplaceMode.ON_SITE,), False),
    ((CountrySearchArea(country="CH"), RemoteOnlySearchArea()),
     (WorkplaceMode.ON_SITE,), True),                      # one remote area is enough
])
def test_includes_remote_answers_whether_remote_boards_are_worth_querying(
        areas, modes, expected):
    assert a_profile(areas=areas, workplace_modes=modes).includes_remote is expected


def test_a_remote_only_search_cannot_exclude_remote():
    with pytest.raises(ValidationError) as failure:
        a_profile(areas=(RemoteOnlySearchArea(),),
                  workplace_modes=(WorkplaceMode.ON_SITE,))
    assert "cannot exclude REMOTE" in str(failure.value)


def test_a_remote_only_search_may_still_accept_a_hybrid_arrangement():
    profile = a_profile(areas=(RemoteOnlySearchArea(country="CH"),),
                        workplace_modes=(WorkplaceMode.REMOTE, WorkplaceMode.HYBRID))
    assert profile.includes_remote


def test_timestamps_must_not_run_backwards():
    with pytest.raises(ValidationError) as failure:
        a_profile(created_at=UPDATED_AT, updated_at=CREATED_AT)
    assert "updated_at must not precede created_at" in str(failure.value)
    assert a_profile(created_at=CREATED_AT, updated_at=CREATED_AT)


def test_a_saved_search_carries_the_whole_v1_config_and_more():
    """Everything `config/searches.yaml` holds, typed, plus what it cannot say."""
    profile = a_profile(
        areas=(RadiusSearchArea(center=YVERDON, radius_km=25.0,
                                label="25 km around Yverdon"),),
        queries=("vendeur", "employé de commerce"),
        title_keywords=("vendeur", "caissier"),
        excluded_keywords=("directeur",),
        opportunity_types=(OpportunityType.STUDENT_JOB,),
        contract_types=(ContractType.FIXED_TERM,),
        workplace_modes=(WorkplaceMode.ON_SITE,),
        posting_languages=("fr", "de"),
        workload=WorkloadRange(min_weekly_hours=8.0, max_weekly_hours=20.0),
        source_keys=("migros",),
        is_active=False,
    )
    assert profile.areas[0].label == "25 km around Yverdon"
    assert profile.posting_languages == ("fr", "de")
    assert profile.workload.max_weekly_hours == 20.0
    assert profile.is_active is False
