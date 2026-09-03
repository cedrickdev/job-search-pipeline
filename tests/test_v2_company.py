# tests/test_v2_company.py
"""`Company` and `CompanyLocation` — employers as entities, not strings.

The interesting assertions are the two the spontaneous-application feature
depends on: a company card cannot show a competitor's branch, and "we never
looked" is a different answer from "there is no channel".
"""
import pytest
from pydantic import ValidationError

from backend.app.domain.common import GeoPoint, Location
from backend.app.domain.company import Company, CompanyLocation
from backend.app.domain.identifiers import new_company_id, new_company_location_id


def a_location(company_id, city="Yverdon-les-Bains", is_headquarters=False):
    return CompanyLocation(
        id=new_company_location_id(),
        company_id=company_id,
        location=Location(country="CH", city=city),
        is_headquarters=is_headquarters,
    )


def test_a_company_needs_only_a_name():
    company = Company(id=new_company_id(), name="Migros Vaud")
    assert company.website is None
    assert company.careers_url is None
    assert company.locations == ()


def test_a_blank_employer_name_is_refused():
    with pytest.raises(ValidationError):
        Company(id=new_company_id(), name="   ")


def test_never_looked_is_not_the_same_answer_as_no_channel():
    """A strategy that read `None` as `False` would skip half the market."""
    unknown = Company(id=new_company_id(), name="Unknown Inc")
    assert unknown.accepts_spontaneous_applications is None
    refused = Company(id=new_company_id(), name="Closed Inc",
                      accepts_spontaneous_applications=False)
    assert refused.accepts_spontaneous_applications is False


def test_company_urls_must_be_fetchable():
    for field in ("website", "careers_url"):
        assert Company(id=new_company_id(), name="Migros",
                       **{field: "https://example.test"})
        with pytest.raises(ValidationError):
            Company(id=new_company_id(), name="Migros",
                    **{field: "www.example.test"})


def test_a_chain_is_one_employer_and_many_sites():
    """Forty branches are one card and forty markers on the map."""
    company_id = new_company_id()
    company = Company(
        id=company_id,
        name="Migros Vaud",
        locations=tuple(a_location(company_id, city=city)
                        for city in ("Yverdon-les-Bains", "Lausanne", "Nyon")),
    )
    assert len(company.locations) == 3
    assert {loc.company_id for loc in company.locations} == {company_id}


def test_a_site_belonging_to_another_company_is_refused():
    company_id = new_company_id()
    stray = a_location(new_company_id())
    with pytest.raises(ValidationError) as failure:
        Company(id=company_id, name="Migros Vaud",
                locations=(a_location(company_id), stray))
    assert "belong to another company" in str(failure.value)


def test_a_company_has_at_most_one_headquarters():
    company_id = new_company_id()
    with pytest.raises(ValidationError) as failure:
        Company(id=company_id, name="Migros Vaud", locations=(
            a_location(company_id, city="Lausanne", is_headquarters=True),
            a_location(company_id, city="Nyon", is_headquarters=True),
        ))
    assert "at most one headquarters" in str(failure.value)


def test_one_headquarters_among_branches_is_fine():
    company_id = new_company_id()
    company = Company(id=company_id, name="Migros Vaud", locations=(
        a_location(company_id, city="Lausanne", is_headquarters=True),
        a_location(company_id, city="Nyon"),
    ))
    headquarters = [loc for loc in company.locations if loc.is_headquarters]
    assert len(headquarters) == 1
    assert headquarters[0].location.city == "Lausanne"


def test_a_site_must_say_where_it_is():
    with pytest.raises(ValidationError):
        CompanyLocation(id=new_company_location_id(), company_id=new_company_id(),
                        location=Location())


def test_a_site_can_already_carry_the_point_phase_7_will_use():
    """`location.point` is where the PostGIS geometry attaches in Phase 2."""
    company_id = new_company_id()
    site = CompanyLocation(
        id=new_company_location_id(),
        company_id=company_id,
        location=Location(country="CH", city="Yverdon-les-Bains",
                          point=GeoPoint(latitude=46.7785, longitude=6.6411)),
    )
    assert site.location.point.latitude == pytest.approx(46.7785)
