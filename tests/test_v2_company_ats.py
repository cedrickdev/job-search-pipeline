# tests/test_v2_company_ats.py
"""ATS detection: URL structure and configuration, and nothing else.

§9 bounds the ambition to the three platforms V1 already reads, and §10 bounds the
claim: detection is not verification. Both bounds are asserted here as much as the
positive behaviour is — the tests named around "not" and "never" cover the evidence
this module deliberately refuses, which is the half a future change is most likely
to erode by adding a page fetch and reporting the word "Greenhouse" as a detection.

Every test is a pure function call. Nothing here opens a socket, and there is no code
path in `backend.app.companies.ats` that could.
"""
import pytest

from backend.app.companies.ats import (
    SOURCE_KEY_PLATFORMS,
    board_url,
    detect_from_configuration,
    detect_from_url,
    detect_from_urls,
    platform_for_source_key,
    platforms_from_source_keys,
)
from backend.app.domain.company import AtsPlatform, CareerSiteKind, DetectionStatus
from tests.v2_builders import NOW

DETECTOR = "configured_ats"


# --- §9: the three platforms, on every host they actually serve boards from ----

@pytest.mark.parametrize(("url", "platform", "token"), [
    ("https://boards.greenhouse.io/acme", AtsPlatform.GREENHOUSE, "acme"),
    ("https://boards.greenhouse.io/acme/jobs/4001", AtsPlatform.GREENHOUSE, "acme"),
    ("https://job-boards.greenhouse.io/acme", AtsPlatform.GREENHOUSE, "acme"),
    ("https://boards-api.greenhouse.io/v1/boards/acme/jobs",
     AtsPlatform.GREENHOUSE, "acme"),
    ("https://api.greenhouse.io/v1/boards/acme", AtsPlatform.GREENHOUSE, "acme"),
    ("https://jobs.lever.co/acme", AtsPlatform.LEVER, "acme"),
    ("https://jobs.lever.co/acme/1a2b3c", AtsPlatform.LEVER, "acme"),
    ("https://api.lever.co/v0/postings/acme?mode=json", AtsPlatform.LEVER, "acme"),
    ("https://jobs.ashbyhq.com/acme", AtsPlatform.ASHBY, "acme"),
    ("https://api.ashbyhq.com/posting-api/job-board/acme", AtsPlatform.ASHBY, "acme"),
])
def test_a_board_url_names_its_platform_and_its_organization(url, platform, token):
    """One row per host in `_BOARD_HOSTS`, including the API forms.

    The API hosts are in the table because a company's configured URL is whichever
    one its previous integration exported, and a detector that only knew the public
    hosts would report `UNKNOWN` for an employer whose board it can already read.
    """
    detection = detect_from_url(url, detected_by=DETECTOR)
    assert detection is not None
    assert detection.platform is platform
    assert detection.organization_id == token
    assert detection.status is DetectionStatus.CONFIRMED


def test_a_board_url_is_confirmed_because_the_platform_addressed_it():
    """§10's strong case, with its evidence: the host is the platform's own.

    Nobody had to interpret anything — `boards.greenhouse.io/acme` is the address
    Greenhouse serves Acme's board at, and the token in it is the one an API call
    needs. That is why this is `CONFIRMED` while a configuration line is not.
    """
    detection = detect_from_url("https://boards.greenhouse.io/acme",
                                detected_by="opportunity_source", observed_at=NOW)
    assert detection is not None
    assert detection.detected.status is DetectionStatus.CONFIRMED
    assert detection.detected.detected_by == "opportunity_source"
    assert [evidence.code for evidence in detection.detected.evidence] \
        == ["ATS_BOARD_URL"]
    only = detection.detected.evidence[0]
    assert only.source_url == "https://boards.greenhouse.io/acme"
    assert only.observed_at == NOW
    assert "addresses this board directly" in only.detail


def test_a_detection_carries_the_public_board_url_not_the_api_one():
    """A `CareerSite` row is something a person may click.

    `boards-api.greenhouse.io/v1/boards/acme/jobs` answers JSON to a browser, so the
    detection normalizes to the canonical public board for the platform. The URL that
    produced the detection stays in the evidence.
    """
    detection = detect_from_url("https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                                detected_by=DETECTOR)
    assert detection is not None
    assert detection.board_url == "https://boards.greenhouse.io/acme"
    assert detection.detected.evidence[0].source_url \
        == "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    assert detection.site_kind is CareerSiteKind.ATS_BOARD


@pytest.mark.parametrize(("platform", "expected"), [
    (AtsPlatform.GREENHOUSE, "https://boards.greenhouse.io/acme"),
    (AtsPlatform.LEVER, "https://jobs.lever.co/acme"),
    (AtsPlatform.ASHBY, "https://jobs.ashbyhq.com/acme"),
])
def test_every_platform_can_state_a_board_url_for_an_organization(platform, expected):
    """One template per platform, and all three are covered.

    A missing template would raise `KeyError` deep inside a discovery pass rather
    than fail here, and the mapping is what makes a configured organization usable
    as a career site at all.
    """
    assert board_url(platform, "acme") == expected


def test_greenhouses_embedded_board_form_still_names_the_organization():
    """The token is in the query string on Greenhouse's older embed URL.

    Which is why `_path_of` keeps the query: this form appears on employers' own
    careers pages far more often than the bare board URL does, and treating it as
    unrecognized would lose the strongest evidence available for exactly the
    companies whose careers page has already been found.
    """
    for url in ("https://boards.greenhouse.io/embed/job_board?for=acme",
                "https://boards.greenhouse.io/embed/job_board/?for=acme"):
        detection = detect_from_url(url, detected_by=DETECTOR)
        assert detection is not None
        assert detection.organization_id == "acme"
        assert detection.status is DetectionStatus.CONFIRMED


def test_a_url_may_omit_its_scheme_and_still_be_read():
    """Configured values are routinely written as bare hosts.

    `Evidence.source_url` requires a fetchable URL, so the scheme is added back — an
    assertion that is safe because all three platforms are HTTPS-only.
    """
    detection = detect_from_url("boards.greenhouse.io/acme", detected_by=DETECTOR)
    assert detection is not None
    assert detection.organization_id == "acme"
    assert detection.detected.evidence[0].source_url \
        == "https://boards.greenhouse.io/acme"


def test_the_host_is_matched_case_insensitively_and_the_token_is_not():
    """Two different rules, and both are deliberate.

    A hostname is case-insensitive by definition, so `BOARDS.Greenhouse.IO` is the
    same host. A board token is a path segment the platform treats as opaque, so
    casefolding it could produce a URL that 404s — and the identity layer compares
    organization ids with `==`, where an invented lowercase form would stop matching
    the one V1's configuration holds.
    """
    detection = detect_from_url("https://BOARDS.Greenhouse.IO/AcmeEurope",
                                detected_by=DETECTOR)
    assert detection is not None
    assert detection.platform is AtsPlatform.GREENHOUSE
    assert detection.organization_id == "AcmeEurope"


@pytest.mark.parametrize("token", ["acme-europe", "acme.io", "acme_ch", "Acme2"])
def test_an_organization_token_may_contain_the_punctuation_platforms_allow(token):
    detection = detect_from_url(f"https://jobs.lever.co/{token}",
                                detected_by=DETECTOR)
    assert detection is not None
    assert detection.organization_id == token


# --- §10: what deliberately produces no detection at all ----------------------

@pytest.mark.parametrize("url", [
    "https://acme.test/careers",
    "https://acme.test/jobs?ats=greenhouse",
    "https://acme.test/careers/powered-by-lever",
    "https://acme.test/#ashby",
])
def test_the_name_of_a_platform_in_a_url_is_not_a_detection(url):
    """§10's named insufficient case, at the only layer that could accept it.

    "The page mentions Greenhouse" is exactly the evidence the phase order calls
    weak. Rather than represent it as `LIKELY`, this module has no code path that
    reads a body or a word — the host is the platform's or there is no detection.
    """
    assert detect_from_url(url, detected_by=DETECTOR) is None


def test_a_lookalike_host_is_not_the_platform():
    """The suffix rule is anchored, so a hostname that merely contains a board
    host is not one. `boards.greenhouse.io.evil.test` is somebody else's domain."""
    assert detect_from_url("https://boards.greenhouse.io.evil.test/acme",
                           detected_by=DETECTOR) is None
    assert detect_from_url("https://notboards.greenhouse.io/acme",
                           detected_by=DETECTOR) is None


@pytest.mark.parametrize("url", [
    "https://boards.greenhouse.io/",
    "https://boards.greenhouse.io",
    "https://jobs.lever.co/",
    "https://boards.greenhouse.io/embed/job_board?for=",
    "https://api.greenhouse.io/v1/applications",
    "https://api.lever.co/v0/opportunities",
])
def test_a_platform_host_with_no_organization_in_it_detects_nothing(url):
    """`None`, not a platform with a missing organization id.

    A detection nothing can act on is worse than no detection, because it looks like
    progress: the company would show "on Greenhouse" on its page and no source could
    ever fetch a posting for it. `DetectedATS` refuses that construction too, so this
    is the same rule stated at both layers.
    """
    assert detect_from_url(url, detected_by=DETECTOR) is None


def test_a_value_with_no_host_at_all_detects_nothing():
    assert detect_from_url("", detected_by=DETECTOR) is None
    assert detect_from_url("/careers", detected_by=DETECTOR) is None


# --- §10: a configured organization is a claim, not an observation -------------

def test_a_configured_organization_is_likely_and_says_why_it_is_only_likely():
    """§8 makes V1's `config/companies.yaml` reusable evidence; §10 caps its weight.

    That file's loader skips malformed entries and validates nothing about the ones
    it keeps, so a token in it means somebody typed it — not that a board answered.
    The URL is still built, because it is exactly what V1 fetches, and a later phase
    that fetches it may promote the status.
    """
    detection = detect_from_configuration(
        AtsPlatform.LEVER, "acme", detected_by=DETECTOR, observed_at=NOW)
    assert detection.status is DetectionStatus.LIKELY
    assert detection.status is not DetectionStatus.CONFIRMED
    assert detection.platform is AtsPlatform.LEVER
    assert detection.organization_id == "acme"
    assert detection.board_url == "https://jobs.lever.co/acme"
    assert [evidence.code for evidence in detection.detected.evidence] \
        == ["ATS_CONFIGURED_ORGANIZATION"]
    only = detection.detected.evidence[0]
    assert only.source_url == "https://jobs.lever.co/acme"
    assert only.observed_at == NOW
    assert "unverified" in only.detail


def test_the_two_detection_routes_never_produce_the_same_evidence_code():
    """The distinction survives storage: the codes differ, so a stored detection
    still says whether a platform addressed the board or an operator typed it."""
    from_url = detect_from_url("https://jobs.lever.co/acme", detected_by=DETECTOR)
    from_config = detect_from_configuration(AtsPlatform.LEVER, "acme",
                                            detected_by=DETECTOR)
    assert from_url is not None
    assert from_url.board_url == from_config.board_url
    assert from_url.detected.evidence[0].code \
        != from_config.detected.evidence[0].code
    assert from_url.status is not from_config.status


def test_a_detection_reports_the_platform_of_the_verdict_it_wraps():
    """`AtsDetection` is a view over `DetectedATS`, not a second copy of it.

    Two independently stored platform values would be one more thing to keep in
    step; the properties exist so a service can read the platform without unpacking,
    and this pins that they cannot disagree.
    """
    detection = detect_from_configuration(AtsPlatform.ASHBY, "acme",
                                          detected_by=DETECTOR)
    assert detection.platform is detection.detected.platform
    assert detection.organization_id == detection.detected.organization_id
    assert detection.status is detection.detected.status


# --- §23: ambiguous evidence resolves the same way every time -----------------

def test_the_board_wins_over_the_careers_page_whatever_order_they_arrive_in():
    """A company's own careers URL names no platform; its board does.

    This is the ordinary ambiguous case — several URLs on file, at most some of them
    boards — and the answer must not depend on which one a provider happened to list
    first.
    """
    urls = ["https://acme.test/careers", "https://jobs.lever.co/acme"]
    for candidate in (urls, list(reversed(urls))):
        detection = detect_from_urls(candidate, detected_by=DETECTOR)
        assert detection is not None
        assert detection.platform is AtsPlatform.LEVER


def test_two_boards_on_file_resolve_to_the_first_one_listed():
    """Input order is the tie-break, and it has to be *a* rule rather than none.

    Every URL-derived detection is `CONFIRMED` today, so the confidence ranking in
    `detect_from_urls` never fires and input order is what actually decides. Both
    halves are asserted because an unstable choice here would rewrite a company's ATS
    columns on alternate sweeps, which §23 forbids.
    """
    urls = ["https://jobs.lever.co/acme", "https://boards.greenhouse.io/acme"]
    first = detect_from_urls(urls, detected_by=DETECTOR)
    assert first is not None
    assert first.platform is AtsPlatform.LEVER
    again = detect_from_urls(urls, detected_by=DETECTOR)
    assert again is not None
    assert again.platform is first.platform
    assert again.organization_id == first.organization_id
    swapped = detect_from_urls(list(reversed(urls)), detected_by=DETECTOR)
    assert swapped is not None
    assert swapped.platform is AtsPlatform.GREENHOUSE


def test_a_company_whose_urls_name_no_platform_gets_no_detection():
    assert detect_from_urls(["https://acme.test/", "https://acme.test/jobs"],
                            detected_by=DETECTOR) is None
    assert detect_from_urls([], detected_by=DETECTOR) is None


# --- §8: V1's configuration keys are the mapping, stated once -----------------

def test_the_three_v1_ats_source_keys_map_to_the_three_platforms():
    """§9's scope, pinned. A fourth entry here is a fourth platform to support.

    The keys are `config/companies.yaml`'s top-level keys *and* the registered
    `source_key` of the three ATS source plugins, which is why one mapping serves
    both rather than two constants drifting apart.
    """
    assert SOURCE_KEY_PLATFORMS == {"greenhouse": AtsPlatform.GREENHOUSE,
                                    "lever": AtsPlatform.LEVER,
                                    "ashby": AtsPlatform.ASHBY}
    assert set(SOURCE_KEY_PLATFORMS.values()) == set(AtsPlatform)


@pytest.mark.parametrize(("source_key", "platform"), [
    ("greenhouse", AtsPlatform.GREENHOUSE),
    ("lever", AtsPlatform.LEVER),
    ("ashby", AtsPlatform.ASHBY),
])
def test_an_ats_source_key_resolves_to_its_platform(source_key, platform):
    assert platform_for_source_key(source_key) is platform


@pytest.mark.parametrize("source_key", ["jobup", "adzuna", "", "greenhouse_eu"])
def test_a_source_that_is_not_an_ats_resolves_to_no_platform(source_key):
    """`None` rather than a guess. A job board is not an employer's ATS, and
    inventing a platform for one would attach a board URL to every company it
    published."""
    assert platform_for_source_key(source_key) is None


def test_configured_source_keys_become_platforms_once_each_in_a_stable_order():
    """Deduplicated and ordered by first appearance, ignoring anything unmapped.

    The configured-ATS provider turns the YAML's keys into platforms with this, and a
    duplicate would make it discover the same organizations twice — harmless only
    because the writes are idempotent, and not worth relying on.
    """
    assert platforms_from_source_keys(
        ["lever", "greenhouse", "lever", "jobup", "ashby"]) == (
        AtsPlatform.LEVER, AtsPlatform.GREENHOUSE, AtsPlatform.ASHBY)
    assert platforms_from_source_keys([]) == ()
    assert platforms_from_source_keys(["jobup"]) == ()
