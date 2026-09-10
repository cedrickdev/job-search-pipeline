# tests/test_v2_company_identity.py
"""Company identity: the derivations, the signals, and the merges that must not happen.

§2 of the Phase 6 order is a prohibition before it is a feature — the system must
see that `Logitech`, `Logitech Europe S.A.`, `LOGITECH` and `Logitech SA` *may* be
one employer, and it must never merge two companies on name similarity alone. Both
halves are asserted here, and the negative half is the one worth keeping if the
others were ever cut: every test whose name contains "not" is a duplicate the
directory is allowed to show rather than a wrong merge it is not allowed to make.

There is no fuzzy matching to test, which is the point: `backend.app.companies.identity`
compares deterministically derived keys with `==`, so every assertion below is
reproducible and a failure names the exact key that drifted rather than a threshold
that moved.

The Swiss pack is loaded for real. A synthetic suffix list can prove
`comparison_key` strips a suffix; only `country_packs/ch/terminology.yaml` can prove
that *Switzerland's* forms are the ones it strips, which is what §19 asks for.
"""
import pytest

from backend.app.companies.identity import (
    SHARED_ATS_HOSTS,
    STRONG_SIGNALS,
    CompanyIdentity,
    IdentitySignal,
    IdentityVerdict,
    comparison_key,
    compare,
    identity_of,
    identity_of_claim,
    is_employer_domain,
    legal_suffixes_for,
    prefers_domain,
)
from backend.app.domain.company import (
    AtsPlatform,
    DetectedATS,
    DetectionStatus,
    Evidence,
    normalize_company_name,
    normalize_domain,
)
from country_packs.ch import pack as ch_pack
from tests.v2_builders import a_company


@pytest.fixture(scope="module")
def ch():
    """The real Swiss pack, loaded once. Frozen, so sharing it is safe."""
    return ch_pack.load()


@pytest.fixture(scope="module")
def suffixes(ch):
    return legal_suffixes_for(ch)


def an_identity(name="Fixture SA", **overrides) -> CompanyIdentity:
    """A `CompanyIdentity` with the two derived name forms already consistent.

    Built directly rather than through `identity_of`, because most tests here are
    about `compare` and want to state one signal at a time: a fixture that went
    through a `Company` would also have to carry a website, and half these tests are
    precisely about what happens when it does not.
    """
    fields = {
        "name": name,
        "normalized_name": normalize_company_name(name),
        "legal_form_key": comparison_key(name, legal_suffixes=("sa", "sarl", "ag")),
    }
    fields.update(overrides)
    return CompanyIdentity(**fields)


# --- §3: normalization removes punctuation and accents, and never a word ------

@pytest.mark.parametrize(("raw", "expected"), [
    ("Logitech Europe S.A.", "logitech europe sa"),
    ("L'Oreal Suisse", "loreal suisse"),
    ("Sàrl Dupont", "sarl dupont"),
    ("MIGROS   VAUD", "migros vaud"),
    ("Acme & Co", "acme co"),
    ("Acme/Co", "acme co"),
    ("Acme-Co", "acme co"),
    ("  Nestlé  ", "nestle"),
    ("Straßen AG", "strassen ag"),
])
def test_a_name_normalizes_the_same_way_however_a_source_spells_it(raw, expected):
    """The two punctuation classes, accents, case and whitespace, in one table.

    The dot/apostrophe rule is the one that carries weight: `S.A.` has to collapse to
    `sa` because the Swiss pack spells its suffix list that way, and turning the dots
    into spaces would produce `s a`, which no suffix matches — the legal-form
    comparison would then silently never fire.
    """
    assert normalize_company_name(raw) == expected


@pytest.mark.parametrize("raw", ["Acme Switzerland", "Acme Suisse", "Acme Group",
                                 "Acme Holding"])
def test_normalization_never_drops_a_meaningful_word(raw):
    """§3, by name: `Acme Switzerland` must not become `acme`.

    Every word survives normalization. Dropping a country or a `Group` is a
    country-aware judgement, it belongs to `comparison_key`, and even there the CH
    pack deliberately does not list these — see `docs/COMPANY_DISCOVERY.md`.
    """
    assert normalize_company_name(raw) == raw.strip().casefold()


def test_a_name_that_is_only_punctuation_normalizes_to_nothing():
    """`""` rather than an exception: the caller decides, and every model refuses it.

    A company row whose comparison form were empty would match every other broken
    row, which is why `Company._a_name_has_a_comparison_form` rejects it at
    construction instead of storing it.
    """
    assert normalize_company_name("—") == ""
    assert normalize_company_name("...") == ""


@pytest.mark.parametrize(("raw", "expected"), [
    ("https://WWW.Logitech.com/fr/", "logitech.com"),
    ("http://logitech.com", "logitech.com"),
    ("logitech.com.", "logitech.com"),
    ("logitech.com", "logitech.com"),
    ("https://logitech.com:8443/jobs?x=1", "logitech.com"),
    ("https://user:pw@logitech.com/", "logitech.com"),
    ("https://JOBS.Logitech.COM/", "jobs.logitech.com"),
])
def test_a_website_reduces_to_one_comparison_host(raw, expected):
    """Domain equality is the strongest non-manual signal, so it has one spelling.

    A bare host is accepted as well as a URL because a seed configuration writes
    `logitech.com`, and `urlsplit` would read that as a path — which would make the
    strongest signal in the phase silently absent for every configured seed.
    """
    assert normalize_domain(raw) == expected


def test_a_value_with_no_host_in_it_normalizes_to_nothing():
    assert normalize_domain("") == ""
    assert normalize_domain("/careers") == ""


def test_a_company_falls_back_to_its_careers_host_when_it_has_no_website():
    """A company discovered from a board has no corporate site, and its host is all
    the domain evidence there is. `identity_of` is what refuses to *trust* it."""
    board_only = a_company(website=None,
                           careers_url="https://boards.greenhouse.io/acme")
    assert board_only.normalized_domain == "boards.greenhouse.io"
    assert a_company().normalized_domain == "example.test"


# --- §3: the legal-form key is computed, country-aware, and never stored -------

@pytest.mark.parametrize(("name", "expected"), [
    ("Logitech Europe S.A.", "logitech europe"),
    ("Logitech SA", "logitech"),
    ("Logitech Sàrl", "logitech"),
    ("Dupont GmbH", "dupont"),
    ("Dupont AG", "dupont"),
    ("Migros Genossenschaft", "migros"),
    ("Acme Ltd", "acme"),
])
def test_a_trailing_swiss_legal_form_is_stripped_for_comparison(name, expected,
                                                               suffixes):
    """The four language regions plus the English forms Swiss boards print verbatim.

    Asserted against the real pack, so this fails if somebody removes a suffix from
    `country_packs/ch/terminology.yaml` — the file is the configuration §19 puts in
    the pack, and the stripping is the workflow §19 keeps in the service.
    """
    assert comparison_key(name, legal_suffixes=suffixes) == expected


def test_the_longest_matching_legal_form_wins(suffixes):
    """`societe cooperative` must not be shadowed by `cooperative`.

    Both are in the Swiss list, and Coop's registered name ends with the long one. A
    shortest-first loop would leave `coop societe`, which is a comparison key no
    other record would ever produce.
    """
    assert comparison_key("Coop Societe Cooperative",
                          legal_suffixes=suffixes) == "coop"


def test_a_legal_form_is_only_stripped_from_the_end(suffixes):
    """`SA Journalière` is a name that starts with two letters, not a legal form."""
    assert comparison_key("SA Journalière", legal_suffixes=suffixes) \
        == "sa journaliere"


def test_only_one_legal_form_is_stripped_even_when_two_trail(suffixes):
    """Deliberately once, not in a loop.

    `Acme SA Sàrl` is not a real registered name, and a loop would collapse `Acme
    AG`, `Acme` and `Acme AG AG` into the same key while making the rule much harder
    to reason about than "at most one suffix, from the end".
    """
    assert comparison_key("Acme SA Sàrl", legal_suffixes=suffixes) == "acme sa"


def test_a_company_actually_called_sa_keeps_its_name(suffixes):
    """The empty-key case. `SA` on its own is an employer, not a bare legal form,
    and returning `""` would produce a key that matches every unnamed record."""
    assert comparison_key("SA", legal_suffixes=suffixes) == "sa"
    assert comparison_key("AG", legal_suffixes=suffixes) == "ag"


def test_a_meaningful_word_is_never_a_legal_form(suffixes):
    """§3's named prohibition, at the layer that could actually violate it.

    `switzerland`, `suisse`, `group` and `holding` are absent from the Swiss list on
    purpose: `Acme Switzerland` and `Acme` are frequently two different legal
    entities, and a key that conflated them would hand `resolve` a false match with
    a strong-looking name agreement.
    """
    for name in ("Acme Switzerland", "Acme Suisse", "Acme Group", "Acme Holding"):
        assert comparison_key(name, legal_suffixes=suffixes) \
            == normalize_company_name(name)


def test_a_company_in_a_country_with_no_pack_is_compared_on_its_whole_name():
    """`()` rather than a built-in default list.

    Conservative in the only direction that is safe: with no suffix list the key is
    the full normalized name, which can only ever produce *fewer* matches. A guessed
    default list would produce wrong ones in a country nobody has modelled.
    """
    assert legal_suffixes_for(None) == ()
    assert comparison_key("Logitech Europe S.A.") == "logitech europe sa"


def test_a_key_that_normalizes_to_nothing_stays_nothing():
    assert comparison_key("—", legal_suffixes=("sa",)) == ""


def test_the_country_domain_preference_is_a_tie_break_a_pack_supplies(ch):
    """`.ch` and `.swiss`, and never a filter.

    A Swiss employer on a `.com` is ordinary, so this reports plausibility rather
    than deciding anything — `compare` never even looks at it. Asserted here so the
    pack field is exercised by something other than its own validator.
    """
    assert prefers_domain(ch, "migros.ch")
    assert prefers_domain(ch, "post.swiss")
    assert not prefers_domain(ch, "logitech.com")
    assert not prefers_domain(None, "migros.ch")
    assert not prefers_domain(ch, "")


# --- §2: a shared ATS host is not a shared identity ---------------------------

@pytest.mark.parametrize("host", sorted(SHARED_ATS_HOSTS))
def test_no_shared_ats_host_is_ever_read_as_an_employer_domain(host):
    """The set that stops the entire Greenhouse customer base becoming one company.

    Domain equality decides `SAME_COMPANY` on its own, so a platform host inside
    that rule would merge every unrelated employer that rents the same board. The
    subdomain form matters too: `acme.workable.com` is Workable's address, not
    Acme's, which is why the check is a suffix test.
    """
    assert not is_employer_domain(host)
    assert not is_employer_domain(f"acme.{host}")


def test_an_employers_own_host_is_an_employer_domain():
    assert is_employer_domain("logitech.com")
    assert is_employer_domain("jobs.migros.ch")
    assert not is_employer_domain("")


def test_a_company_known_only_by_its_board_contributes_no_domain_evidence(ch):
    """The projection is honest, the identity is careful.

    `Company.normalized_domain` returns `boards.greenhouse.io` because that is the
    host on file; `identity_of` drops it, because it is not this employer's. The
    split is deliberate — the accessor describes the row, the identity decides what
    counts as evidence.
    """
    company = a_company(website=None,
                        careers_url="https://boards.greenhouse.io/acme")
    identity = identity_of(company, pack=ch)
    assert company.normalized_domain == "boards.greenhouse.io"
    assert identity.employer_domain is None


def test_a_claim_prefers_the_website_and_falls_back_to_the_careers_host():
    """Both are read, in that order, and a shared board host is skipped either way."""
    with_site = identity_of_claim("Acme", website="https://acme.test",
                                  careers_url="https://jobs.lever.co/acme")
    board_only = identity_of_claim("Acme", careers_url="https://jobs.lever.co/acme")
    careers_only = identity_of_claim("Acme", careers_url="https://acme.test/jobs")
    assert with_site.employer_domain == "acme.test"
    assert board_only.employer_domain is None
    assert careers_only.employer_domain == "acme.test"


# --- identity_of: a stored company becomes comparable -------------------------

def test_a_stored_company_carries_its_derived_forms_and_its_detected_ats(ch):
    """What `resolve` is handed, built once per record rather than per comparison."""
    company = a_company(
        name="Logitech Europe S.A.", website="https://www.logitech.com/fr-ch/",
        careers_url=None, country="CH",
        detected_ats=DetectedATS(
            platform=AtsPlatform.GREENHOUSE, organization_id="logitech",
            status=DetectionStatus.CONFIRMED, detected_by="configured_ats",
            evidence=(Evidence(code="ATS_BOARD_URL", detail="board addressed"),)))
    identity = identity_of(company, pack=ch, aliases=("LOGITECH", "   "),
                           confirmed_aliases=("Logitech SA",),
                           external_ids=("greenhouse:logitech",))
    assert identity.normalized_name == "logitech europe sa"
    assert identity.legal_form_key == "logitech europe"
    assert identity.employer_domain == "logitech.com"
    assert identity.country == "CH"
    assert identity.ats_platform is AtsPlatform.GREENHOUSE
    assert identity.ats_organization_id == "logitech"
    # An alias that normalizes to nothing is not an alias: it would otherwise become
    # an empty name form that matches every other record with a broken label.
    assert identity.aliases == frozenset({"logitech"})
    assert identity.confirmed_aliases == frozenset({"logitech sa"})
    assert identity.external_ids == frozenset({"greenhouse:logitech"})


def test_every_name_form_is_the_union_of_the_two_keys_and_the_aliases():
    identity = an_identity("Logitech SA", aliases=frozenset({"logitech europe"}))
    assert identity.every_name_form == frozenset({"logitech sa", "logitech",
                                                  "logitech europe"})


def test_a_company_with_no_ats_and_no_aliases_states_that_rather_than_guessing():
    identity = identity_of(a_company(detected_ats=None))
    assert identity.ats_platform is None
    assert identity.ats_organization_id is None
    assert identity.aliases == frozenset()
    assert identity.external_ids == frozenset()


# --- §2: the verdicts ---------------------------------------------------------

def test_one_shared_employer_domain_decides_the_identity_against_two_names():
    """Rule 1, and the example the phase order gives.

    `Logitech` and `Logitech Europe S.A.` share nothing but a host, and that is
    enough: a server answers for `logitech.com`, so somebody outside this codebase
    asserted the connection. This is the assertion that makes the whole design
    worthwhile — without it the directory would hold one row per spelling.
    """
    assessment = compare(
        identity_of_claim("Logitech", website="https://logitech.com"),
        identity_of_claim("Logitech Europe S.A.",
                          website="https://www.logitech.com/fr/"))
    assert assessment.verdict is IdentityVerdict.SAME_COMPANY
    assert assessment.is_same_company
    assert IdentitySignal.EMPLOYER_DOMAIN in assessment.strong_signals
    assert "one employer" in assessment.detail


def test_a_shared_ats_organization_decides_it_too():
    """The board token is the platform's own identifier for one customer."""
    assessment = compare(
        an_identity("Acme", ats_platform=AtsPlatform.LEVER,
                    ats_organization_id="acme"),
        an_identity("Acme International", ats_platform=AtsPlatform.LEVER,
                    ats_organization_id="acme"))
    assert assessment.verdict is IdentityVerdict.SAME_COMPANY
    assert assessment.strong_signals == frozenset({IdentitySignal.ATS_ORGANIZATION})


def test_the_same_organization_token_on_two_platforms_is_not_a_match():
    """`acme` on Greenhouse and `acme` on Lever are two unrelated tenancies.

    An organization id is an identity *within* its platform. Comparing the tokens
    without the platform is the cheap mistake that would merge two companies that
    happen to have picked the same slug.
    """
    assessment = compare(
        an_identity("Acme", ats_platform=AtsPlatform.GREENHOUSE,
                    ats_organization_id="acme"),
        an_identity("Beta", ats_platform=AtsPlatform.LEVER,
                    ats_organization_id="acme"))
    assert assessment.verdict is IdentityVerdict.INSUFFICIENT_EVIDENCE


def test_a_platform_with_no_organization_token_matches_nothing():
    """Two companies both known to be "on Greenhouse" are not thereby related."""
    assessment = compare(
        an_identity("Acme", ats_platform=AtsPlatform.GREENHOUSE),
        an_identity("Beta", ats_platform=AtsPlatform.GREENHOUSE))
    assert assessment.verdict is IdentityVerdict.INSUFFICIENT_EVIDENCE


def test_a_shared_external_identifier_decides_it():
    """A provider's own id for an employer: it said these are the same sighting."""
    assessment = compare(
        an_identity("Acme", external_ids=frozenset({"configured_ats:greenhouse:acme"})),
        an_identity("Acme Holding",
                    external_ids=frozenset({"configured_ats:greenhouse:acme"})))
    assert assessment.verdict is IdentityVerdict.SAME_COMPANY
    assert assessment.strong_signals == frozenset({IdentitySignal.EXTERNAL_ID})


def test_an_operator_confirmed_alias_is_the_strongest_signal_there_is():
    """§2 lists a manually confirmed alias among the evidence signals.

    A human saying "these two are the same employer" outranks every derivation here,
    and it is the only route to `SAME_COMPANY` for two records that share no domain,
    no board and no external id.
    """
    assessment = compare(
        an_identity("Logitech", confirmed_aliases=frozenset({"logitech europe sa"})),
        an_identity("Logitech Europe S.A."))
    assert assessment.verdict is IdentityVerdict.SAME_COMPANY
    assert assessment.strong_signals == frozenset({IdentitySignal.CONFIRMED_ALIAS})


def test_two_different_employer_domains_are_distinct_even_with_identical_names():
    """Rule 2, and what keeps two same-named unrelated firms apart.

    Both published a site and published different ones. Whatever the names say, the
    strongest evidence available disagrees — and `DISTINCT` here is what stops a
    later pass from treating the name agreement as a reason to merge.
    """
    assessment = compare(
        identity_of_claim("Migros", website="https://migros.ch"),
        identity_of_claim("Migros", website="https://migros-online.test"))
    assert assessment.verdict is IdentityVerdict.DISTINCT
    assert IdentitySignal.NORMALIZED_NAME in assessment.signals
    assert assessment.strong_signals == frozenset()
    assert "different employer domains" in assessment.detail


def test_a_stronger_signal_outranks_two_differing_domains():
    """Order matters: rule 1 is applied before rule 2.

    A confirmed alias or a shared board token means somebody asserted the link, and
    two hosts is exactly what a company with a legacy domain looks like.
    """
    assessment = compare(
        identity_of_claim("Acme", website="https://acme.test",
                          ats_platform=AtsPlatform.ASHBY,
                          ats_organization_id="acme"),
        identity_of_claim("Acme", website="https://acme-group.test",
                          ats_platform=AtsPlatform.ASHBY,
                          ats_organization_id="acme"))
    assert assessment.verdict is IdentityVerdict.SAME_COMPANY


def test_an_identical_name_alone_is_a_possible_match_and_never_a_merge():
    """Rule 3, and the prohibition the whole module exists to hold (§2, §24).

    `LOGITECH` and `Logitech` normalize to the same string and nothing else is
    known. That is an invitation to look, not a licence to merge: `resolution` turns
    this into `AMBIGUOUS` and leaves the opportunity unlinked.
    """
    assessment = compare(an_identity("LOGITECH"), an_identity("Logitech"))
    assert assessment.verdict is IdentityVerdict.POSSIBLE_MATCH
    assert assessment.signals == frozenset({IdentitySignal.NORMALIZED_NAME,
                                            IdentitySignal.LEGAL_FORM_KEY})
    assert assessment.strong_signals == frozenset()
    assert "a name is not an identity" in assessment.detail


def test_two_names_differing_only_by_a_legal_form_are_a_possible_match():
    """`Logitech SA` and `Logitech Sàrl` share a comparison key and nothing more.

    They may well be the same group and may well be two companies; a legal form is
    the one difference that most often means "different entity, same parent". The
    trade is recorded in `docs/COMPANY_DISCOVERY.md` as a known risk: near-duplicates
    stay visible until a domain or an operator resolves them.
    """
    assessment = compare(an_identity("Logitech SA"), an_identity("Logitech Sàrl"))
    assert assessment.verdict is IdentityVerdict.POSSIBLE_MATCH
    assert assessment.signals == frozenset({IdentitySignal.LEGAL_FORM_KEY})


def test_a_known_alias_from_a_board_is_supporting_evidence_only():
    """The other half of §4: a source's own spelling is not an operator's word."""
    assessment = compare(
        an_identity("Logitech", aliases=frozenset({"logitech europe sa"})),
        an_identity("Logitech Europe S.A."))
    assert assessment.verdict is IdentityVerdict.POSSIBLE_MATCH
    assert IdentitySignal.KNOWN_ALIAS in assessment.signals
    assert assessment.strong_signals == frozenset()


@pytest.mark.parametrize(("left", "right"), [
    ("Logitec", "Logitech"),
    ("Migros Vaud", "Migros Genève"),
    ("Acme Switzerland", "Acme"),
    ("Nestlé Nespresso", "Nestlé Waters"),
])
def test_names_that_merely_look_alike_share_no_signal_at_all(left, right):
    """There is no edit distance, no token overlap and no threshold to tune.

    Every one of these pairs would score highly under any fuzzy metric, and each is
    plausibly two different employers. `INSUFFICIENT_EVIDENCE` is the honest answer,
    and it is deliberately not `DISTINCT`: a later pass that learns a domain can
    still resolve them.
    """
    assessment = compare(an_identity(left), an_identity(right))
    assert assessment.verdict is IdentityVerdict.INSUFFICIENT_EVIDENCE
    assert assessment.signals == frozenset()


def test_nothing_comparable_is_not_the_same_answer_as_different_companies():
    """Rule 4, stated as the distinction it protects.

    Concluding "different employers" from "we know nothing" is how a duplicate gets
    created on every sweep: the resolver would insert a new company each time,
    because the stored one was declared unrelated.
    """
    assessment = compare(an_identity("Acme"), an_identity("Beta"))
    assert assessment.verdict is IdentityVerdict.INSUFFICIENT_EVIDENCE
    assert assessment.verdict is not IdentityVerdict.DISTINCT
    assert "no comparable evidence" in assessment.detail


def test_a_comparison_reads_the_same_in_both_directions():
    """Symmetry, over the four verdicts.

    `resolve` compares a claim against each stored candidate in whatever order the
    shortlist arrived in, so an asymmetric rule would make the outcome depend on the
    query plan. The alias signals are the ones that could plausibly go wrong, since
    each side's aliases are checked against the other's every name form.
    """
    pairs = (
        (identity_of_claim("Acme", website="https://acme.test"),
         identity_of_claim("Acme Holding", website="https://acme.test")),
        (identity_of_claim("Migros", website="https://migros.ch"),
         identity_of_claim("Migros", website="https://migros-online.test")),
        (an_identity("Acme", aliases=frozenset({"beta"})), an_identity("Beta")),
        (an_identity("Acme"), an_identity("Beta")),
    )
    for left, right in pairs:
        assert compare(left, right).verdict == compare(right, left).verdict
        assert compare(left, right).signals == compare(right, left).signals


def test_only_four_signals_can_conclude_an_identity_on_their_own():
    """The strong/supporting split, pinned as a set.

    Moving `NORMALIZED_NAME` into this frozenset would be a one-word change that
    silently turns every same-named pair into a merge, which is why the membership
    is asserted rather than left to the comment beside it.
    """
    assert STRONG_SIGNALS == frozenset({
        IdentitySignal.EXTERNAL_ID, IdentitySignal.ATS_ORGANIZATION,
        IdentitySignal.EMPLOYER_DOMAIN, IdentitySignal.CONFIRMED_ALIAS})
    assert IdentitySignal.NORMALIZED_NAME not in STRONG_SIGNALS
    assert IdentitySignal.LEGAL_FORM_KEY not in STRONG_SIGNALS
    assert IdentitySignal.KNOWN_ALIAS not in STRONG_SIGNALS
    assert IdentitySignal.COUNTRY_DOMAIN_PREFERENCE not in STRONG_SIGNALS


def test_the_country_domain_preference_never_appears_in_a_verdict(ch):
    """It is not a fact two records share, so it cannot be evidence that they match.

    `resolution` applies it as a tie-break between candidates that already tied. If
    it leaked into `compare` it would become a reason to merge a Swiss company with
    another Swiss company.
    """
    assessment = compare(
        identity_of_claim("Acme", website="https://acme.ch", country="CH"),
        identity_of_claim("Beta", website="https://beta.ch", country="CH"))
    assert IdentitySignal.COUNTRY_DOMAIN_PREFERENCE not in assessment.signals
    assert assessment.verdict is IdentityVerdict.DISTINCT
