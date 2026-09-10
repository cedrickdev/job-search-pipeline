# tests/test_v2_company.py
"""`Company` and the vocabulary Phase 6 gave it — employers as entities, not strings.

The interesting assertions are the two the spontaneous-application feature
depends on: a company card cannot show a competitor's branch, and "we never
looked" is a different answer from "there is no channel".

Phase 6 is what makes the second one hold under pressure. A verdict about an
employer must carry its basis, a detection must show its work, a provenance row
must not carry a credential, and a second sighting of the same thing must land on
the id the first one used. Those invariants are exercised end to end in
`tests/test_v2_company_persistence.py`, but that suite skips wherever no
PostgreSQL is configured — so they are asserted here too, against the models
alone, where nothing can skip.

Normalization itself belongs to `tests/test_v2_company_identity.py`; the only part
of it here is a company refusing a name that has no comparison form at all.
"""
import pytest
from pydantic import ValidationError

from backend.app.domain.common import GeoPoint, Location
from backend.app.domain.company import (
    AtsPlatform,
    CareerSiteKind,
    Company,
    CompanyIdentityStatus,
    CompanyLocation,
    CompanySeedKind,
    DetectedATS,
    DetectionStatus,
    Evidence,
    SpontaneousApplicationChannel,
    SpontaneousApplicationSupport,
    normalize_company_name,
)
from backend.app.domain.identifiers import (
    career_site_id,
    company_alias_id,
    company_discovery_record_id,
    new_company_id,
    new_company_location_id,
)
from tests.v2_builders import LATER, NOW
from tests.v2_companies import a_career_site, a_discovery_record, an_alias


def a_location(company_id, city="Yverdon-les-Bains", is_headquarters=False):
    return CompanyLocation(
        id=new_company_location_id(),
        company_id=company_id,
        location=Location(country="CH", city=city),
        is_headquarters=is_headquarters,
    )


def an_evidence(code="BOARD_LINKED_FROM_CAREERS_PAGE",
                detail="the careers page links to the board"):
    return Evidence(code=code, detail=detail)


def a_channel(support=SpontaneousApplicationSupport.SUPPORTED, **overrides):
    """A verdict carrying the basis a decided one is required to have (§12)."""
    decided = support is not SpontaneousApplicationSupport.UNKNOWN
    fields = {
        "support": support,
        "observed_by": "test_provider" if decided else None,
        "evidence": (an_evidence(),) if decided else (),
    }
    fields.update(overrides)
    return SpontaneousApplicationChannel(**fields)


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


# --- §1: a company starts as a claim, and says so -----------------------------


def test_a_company_nobody_has_corroborated_is_only_seeded():
    """`SEEDED` is the default because a name is all a seed ever is (§7).

    `VERIFIED` is the member no Phase 6 code path sets on its own — a human or a
    redirect we followed ourselves earns it — and the default being the weakest of
    the three is what keeps that promise cheap to keep.
    """
    company = Company(id=new_company_id(), name="Migros Vaud")
    assert company.identity_status is CompanyIdentityStatus.SEEDED
    assert company.detected_ats is None
    assert company.spontaneous_application_channel is None


def test_a_name_with_no_comparison_form_is_refused_though_it_is_not_blank():
    """`"—"` passes `NonEmptyStr` and normalizes to `""`, which matches everything.

    Refusing it here means the error names the company. Storing it would produce an
    empty `normalized_name`, and every other broken row would then look like the
    same employer.
    """
    with pytest.raises(ValidationError) as failure:
        Company(id=new_company_id(), name="—")
    assert "letter or digit" in str(failure.value)


# --- §12: a verdict about an employer carries its basis ------------------------


@pytest.mark.parametrize(("support", "flag"), [
    (SpontaneousApplicationSupport.SUPPORTED, True),
    (SpontaneousApplicationSupport.NOT_SUPPORTED, False),
    (SpontaneousApplicationSupport.UNKNOWN, None),
])
def test_the_flag_and_the_evidence_backed_verdict_say_the_same_thing(support, flag):
    """Two representations of one answer, so a reader may use either.

    The boolean is what Phase 1 defined and what the API has always published; the
    channel is the evidence-backed form §12 asks for. Keeping both is a deliberate
    compatibility choice, and this is the pairing that makes it safe.
    """
    company = Company(id=new_company_id(), name="Acme SA",
                      accepts_spontaneous_applications=flag,
                      spontaneous_application_channel=a_channel(support))
    assert company.accepts_spontaneous_applications is flag
    assert company.spontaneous_application_channel.support is support


@pytest.mark.parametrize(("flag", "support"), [
    (False, SpontaneousApplicationSupport.SUPPORTED),
    (True, SpontaneousApplicationSupport.NOT_SUPPORTED),
    (True, SpontaneousApplicationSupport.UNKNOWN),
    (None, SpontaneousApplicationSupport.SUPPORTED),
])
def test_a_verdict_that_contradicts_the_flag_beside_it_is_refused(flag, support):
    """The third case is the one this rule exists for.

    `accepts_spontaneous_applications=True` beside a channel saying nobody looked is
    exactly the inference §12 forbids — "no active jobs, so presumably they take
    spontaneous applications" — written as a row rather than as a branch.
    """
    with pytest.raises(ValidationError) as failure:
        Company(id=new_company_id(), name="Acme SA",
                accepts_spontaneous_applications=flag,
                spontaneous_application_channel=a_channel(support))
    assert "contradicts" in str(failure.value)


def test_unknown_is_the_one_verdict_that_needs_no_evidence():
    """Demanding a basis for "nobody looked" would mean inventing one."""
    channel = SpontaneousApplicationChannel()
    assert channel.support is SpontaneousApplicationSupport.UNKNOWN
    assert channel.is_decided is False
    assert channel.evidence == ()
    assert channel.observed_by is None


@pytest.mark.parametrize("support", [SpontaneousApplicationSupport.SUPPORTED,
                                     SpontaneousApplicationSupport.NOT_SUPPORTED])
def test_a_decided_verdict_states_what_it_rests_on_and_who_observed_it(support):
    """Both halves, because either one missing makes the verdict unauditable.

    An operator looking at "this employer does not accept spontaneous applications"
    has to be able to ask *who says so*, and open what they saw.
    """
    with pytest.raises(ValidationError) as unevidenced:
        SpontaneousApplicationChannel(support=support, observed_by="test_provider")
    assert "use UNKNOWN when nobody has looked" in str(unevidenced.value)

    with pytest.raises(ValidationError) as unattributed:
        SpontaneousApplicationChannel(support=support, evidence=(an_evidence(),))
    assert "which provider observed it" in str(unattributed.value)


def test_a_refusal_that_points_at_an_application_form_contradicts_itself():
    """A URL for the form *is* the channel, so the verdict cannot be NOT_SUPPORTED."""
    with pytest.raises(ValidationError) as failure:
        a_channel(SpontaneousApplicationSupport.NOT_SUPPORTED,
                  url="https://example.test/spontaneous")
    assert "contradicts itself" in str(failure.value)


# --- §10: detection is not verification ----------------------------------------


def test_a_detection_shows_its_work():
    with pytest.raises(ValidationError) as failure:
        DetectedATS(platform=AtsPlatform.GREENHOUSE, detected_by="test_provider")
    assert "at least one piece of evidence" in str(failure.value)


def test_a_detector_that_concluded_nothing_stores_nothing():
    """`UNKNOWN` is a detector's return value, never a row.

    A stored `DetectedATS` asserts a platform. One asserting a platform *and*
    ignorance about it would be a claim nothing could act on, so the absence is
    modelled as `Company.detected_ats is None` instead.
    """
    with pytest.raises(ValidationError) as failure:
        DetectedATS(platform=AtsPlatform.GREENHOUSE, detected_by="test_provider",
                    status=DetectionStatus.UNKNOWN, evidence=(an_evidence(),))
    assert "Company.detected_ats as None" in str(failure.value)


def test_a_confirmed_detection_names_the_organization_it_confirmed():
    """`LIKELY` may be vague; `CONFIRMED` has to be fetchable.

    Without the organization identifier nothing can read the board, which makes the
    claim untestable — and an untestable `CONFIRMED` is precisely the "the HTML
    contains the word Greenhouse" reading §10 rules out.
    """
    likely = DetectedATS(platform=AtsPlatform.LEVER, detected_by="test_provider",
                         evidence=(an_evidence(),))
    assert likely.status is DetectionStatus.LIKELY
    assert likely.organization_id is None

    with pytest.raises(ValidationError) as failure:
        DetectedATS(platform=AtsPlatform.LEVER, detected_by="test_provider",
                    status=DetectionStatus.CONFIRMED, evidence=(an_evidence(),))
    assert "organization identifier" in str(failure.value)

    confirmed = DetectedATS(platform=AtsPlatform.LEVER, detected_by="test_provider",
                            status=DetectionStatus.CONFIRMED, organization_id="acme",
                            evidence=(an_evidence(),))
    assert confirmed.organization_id == "acme"


def test_evidence_may_name_no_url_because_a_configuration_file_has_none():
    """The configured seed is why `source_url` is optional (§5).

    Requiring a URL would leave the one provider that reads `config/companies.yaml`
    unable to state why it believes anything — and the phase would then either drop
    the evidence rule or invent a URL to satisfy it.
    """
    from_a_file = Evidence(code="CONFIGURED_SEED",
                           detail="listed in config/companies.yaml")
    assert from_a_file.source_url is None
    assert from_a_file.observed_at is None

    with pytest.raises(ValidationError):
        Evidence(code="configured seed", detail="a reason code groups a dashboard")


def test_the_vocabularies_are_exactly_what_this_deployment_can_act_on():
    """Two closed sets, and both closures are substantive.

    A fourth `AtsPlatform` would name a board no source plugin can fetch (§9), and a
    sixth `CompanySeedKind` would be a way of learning about an employer that Phase
    6 has no provider for — a crawler seed, which §7 rules out.
    """
    assert {platform.value for platform in AtsPlatform} == {
        "GREENHOUSE", "LEVER", "ASHBY"}
    assert {kind.value for kind in CompanySeedKind} == {
        "OPPORTUNITY", "CONFIGURED", "ATS_ORGANIZATION", "WEBSITE", "MANUAL"}

    with pytest.raises(ValidationError):
        DetectedATS(platform="WORKDAY", detected_by="test_provider",
                    evidence=(an_evidence(),))


# --- §4, §5, §11: provenance rows, and the ids that make them idempotent -------


@pytest.mark.parametrize("key", ["Greenhouse", "greenhouse-ats", "1jobup",
                                 "configured ats", "", "a" * 41])
def test_a_provenance_key_is_one_stable_lower_case_identity(key):
    """The same shape as a source key, so "who told us" reads the same everywhere.

    Every other test in the phase is the positive case: `test_provider` and
    `configured_ats` are accepted throughout. What has to be refused is the spelling
    that would make one provider's rows look like two providers' rows.
    """
    with pytest.raises(ValidationError):
        an_alias(source_key=key)


def test_an_alias_that_normalizes_to_nothing_is_refused():
    """An alias is a claim about a name, so it has to contain a name."""
    with pytest.raises(ValidationError) as failure:
        an_alias(alias="—")
    assert "letter or digit" in str(failure.value)


def test_an_alias_last_seen_before_it_was_first_seen_is_refused():
    with pytest.raises(ValidationError) as failure:
        an_alias(first_seen_at=LATER, last_seen_at=NOW)
    assert "precedes" in str(failure.value)


def test_the_same_label_spelled_two_ways_is_one_alias_row():
    """§23's idempotence, at the level the uniqueness of the row rests on.

    The id is derived from the *normalized* label, so `LOGITECH  s.a.` and
    `Logitech SA` are one claim recorded once. Keying on the raw string would store
    both and then have to explain which of them is canonical — the drift §4 forbids.
    """
    company_id = new_company_id()
    alias = an_alias(company_id=company_id, alias="LOGITECH  s.a.")

    assert alias.normalized_alias == "logitech sa"
    assert company_alias_id(company_id, normalize_company_name("Logitech SA")) == \
        company_alias_id(company_id, alias.normalized_alias)
    assert company_alias_id(company_id, "logitech sa") != \
        company_alias_id(new_company_id(), "logitech sa")


def test_a_board_whose_platform_nobody_can_name_is_refused():
    """An `ATS_BOARD` exists to be fetched, and the platform is how (§11)."""
    with pytest.raises(ValidationError) as failure:
        a_career_site(kind=CareerSiteKind.ATS_BOARD)
    assert "name its platform" in str(failure.value)

    board = a_career_site(url="https://boards.greenhouse.test/acme",
                          kind=CareerSiteKind.ATS_BOARD,
                          platform=AtsPlatform.GREENHOUSE)
    assert board.verification_status is DetectionStatus.LIKELY


def test_a_careers_page_needs_no_platform_and_claims_no_check():
    """`last_checked_at` stays `None` for everything Phase 6 writes.

    Nothing in this phase fetches a URL — §9 rules out the crawling that would —
    so a timestamp here would record a check that never happened. The column is for
    the phase that does fetch.
    """
    site = a_career_site()
    assert site.kind is CareerSiteKind.CAREERS_PAGE
    assert site.platform is None
    assert site.last_checked_at is None


def test_a_site_checked_before_it_was_discovered_is_refused():
    with pytest.raises(ValidationError) as failure:
        a_career_site(discovered_at=LATER, last_checked_at=NOW)
    assert "precedes" in str(failure.value)


def test_two_endpoints_of_one_employer_are_two_rows_and_one_url_is_one_row():
    """Which is why §11 has records rather than a single `careers_url` field."""
    company_id = new_company_id()
    assert career_site_id(company_id, "https://acme.test/jobs") != \
        career_site_id(company_id, "https://boards.greenhouse.test/acme")
    assert career_site_id(company_id, "https://acme.test/jobs") == \
        career_site_id(company_id, "https://acme.test/jobs")


@pytest.mark.parametrize("key", ["Authorization", "x-api-key", "set-cookie",
                                 "session_id", "board_token", "client_secret"])
def test_a_sighting_carrying_a_credential_shaped_key_is_refused(key):
    """§5 and §26, enforced on payloads nobody reviewed.

    Raw provider metadata is stored and later shown to an operator, so a header bag
    that arrived with a bearer token in it must not become a row. The model refuses
    rather than redacts on purpose: dropping the key silently would let the next
    adapter hand us the same bag unnoticed.
    """
    with pytest.raises(ValidationError) as failure:
        a_discovery_record(raw={key: "irrelevant", "headcount": "300"})
    assert key in str(failure.value)


def test_a_sighting_keeps_the_harmless_metadata_a_provider_returned():
    """The anti-vacuity half: the rule refuses key names, not payloads."""
    record = a_discovery_record(raw={"board": "acme", "headcount": "300"})
    assert record.raw == {"board": "acme", "headcount": "300"}


def test_a_sighting_the_resolver_refused_to_attach_is_still_a_sighting():
    """§14: an ambiguous seed stays recorded and unlinked.

    Discarding it would mean rediscovering the same employer and refusing it again
    on every sweep, with nothing to show an operator who wants to settle it.
    """
    unlinked = a_discovery_record()
    assert unlinked.company_id is None
    assert unlinked.confidence is DetectionStatus.LIKELY


def test_re_pointing_a_sighting_does_not_make_it_a_second_sighting():
    """The record is the provider's, keyed by what the provider called them (§5).

    A sighting first attached to a provisional company and later moved to the
    confirmed one is the same observation, so its id cannot depend on `company_id`.
    """
    linked = a_discovery_record(company_id=new_company_id())
    unlinked = a_discovery_record()

    assert linked.id == unlinked.id == company_discovery_record_id("test_provider",
                                                                   "fixture sa")
    assert company_discovery_record_id("configured_ats", "acme") != \
        company_discovery_record_id("stored_opportunities", "acme")
