# tests/test_v2_company_resolution.py
"""Resolving a claimed employer to a stored company, and refusing to when unsure.

The three outcomes are not three shades of the same answer. `MATCHED` links,
`UNRESOLVED` creates, and `AMBIGUOUS` does neither and asks for a human — §14's
"the opportunity may remain unlinked" is a deliberate outcome rather than a failure,
and most of the tests below exist to keep it reachable. A resolver that never says
`AMBIGUOUS` is a resolver that corrupts company identity quietly.

`resolve` reads and compares. It writes nothing, holds no state, samples no clock and
calls no LLM (§25), so the idempotency §13 and §23 require is a property of its shape
and not of a cache — which is what the repeated-call tests here pin.
"""
from uuid import UUID

import pytest

from backend.app.companies.identity import (
    CompanyIdentity,
    IdentitySignal,
    comparison_key,
    identity_of_claim,
)
from backend.app.companies.resolution import (
    CORROBORATING_SIGNALS,
    CompanyResolution,
    ResolutionOutcome,
    has_corroboration,
    name_lookup_keys,
    resolve,
    stored_company_identity_pairs,
    strong_lookup_keys,
)
from backend.app.domain.company import AtsPlatform, normalize_company_name
from backend.app.domain.identifiers import CompanyId
from country_packs.ch import pack as ch_pack
from tests.v2_builders import COMPANY, OTHER_COMPANY, a_company

THIRD_COMPANY = CompanyId(UUID("00000000-0000-4000-8000-000000000033"))
SUFFIXES = ("sa", "sarl", "ag", "gmbh")


@pytest.fixture(scope="module")
def ch():
    return ch_pack.load()


def an_identity(name, **overrides) -> CompanyIdentity:
    """A stored company's identity, stated one signal at a time.

    Built directly rather than from a `Company` row for the same reason as in
    `test_v2_company_identity.py`: most tests here want a candidate that agrees on
    exactly one thing, and a company built through the domain would also carry a
    website.
    """
    fields = {"name": name, "normalized_name": normalize_company_name(name),
              "legal_form_key": comparison_key(name, legal_suffixes=SUFFIXES)}
    fields.update(overrides)
    return CompanyIdentity(**fields)


def a_claim(name="Logitech", **overrides) -> CompanyIdentity:
    """What a posting or a seed asserts, as `identity_of_claim` builds it."""
    return identity_of_claim(name, **overrides)


# --- §13: MATCHED, and only on evidence outside the name ----------------------

def test_a_posting_resolves_to_the_company_that_shares_its_domain():
    """The §27 walkthrough: `Opportunity(company_name="Logitech")` finds Logitech.

    The link is made on the host, not the label — which is what allows the posting's
    `Logitech` and the stored `Logitech Europe S.A.` to be one employer without any
    name rule having to be trusted.
    """
    resolution = resolve(
        a_claim("Logitech", website="https://logitech.com"),
        [(COMPANY, an_identity("Logitech Europe S.A.",
                               employer_domain="logitech.com"))])
    assert resolution.outcome is ResolutionOutcome.MATCHED
    assert resolution.is_matched
    assert resolution.company_id == COMPANY
    assert resolution.candidate_ids == (COMPANY,)
    assert resolution.detail == ("'Logitech' is 'Logitech Europe S.A.': agreed on "
                                 "EMPLOYER_DOMAIN")


def test_a_posting_resolves_on_the_board_it_was_scraped_from():
    """The other everyday route: an ATS source knows the organization token.

    A posting collected from `jobs.lever.co/acme` carries Lever's own identifier for
    that employer, which is stronger than anything its title says.
    """
    resolution = resolve(
        a_claim("ACME", ats_platform=AtsPlatform.LEVER, ats_organization_id="acme"),
        [(COMPANY, an_identity("Acme Group", ats_platform=AtsPlatform.LEVER,
                               ats_organization_id="acme"))])
    assert resolution.outcome is ResolutionOutcome.MATCHED
    assert resolution.company_id == COMPANY
    assert "ATS_ORGANIZATION" in resolution.detail


def test_resolving_the_same_claim_again_finds_the_company_it_created():
    """§13's idempotency, in the form the ingestion loop actually needs.

    A seed that was `UNRESOLVED` becomes a company; the next sweep presents the same
    claim and must now match it rather than insert a second row. This is the whole
    of §23's no-duplicates guarantee at the resolution layer.
    """
    claim = a_claim("Logitech", website="https://logitech.com")
    first = resolve(claim, [])
    assert first.outcome is ResolutionOutcome.UNRESOLVED
    stored = an_identity("Logitech", employer_domain="logitech.com")
    second = resolve(claim, [(COMPANY, stored)])
    third = resolve(claim, [(COMPANY, stored)])
    assert second.outcome is ResolutionOutcome.MATCHED
    assert second.company_id == COMPANY
    assert second == third


def test_a_match_is_the_same_whichever_order_the_shortlist_arrives_in():
    """The shortlist comes from a query, so its order is the database's business.

    A resolution that depended on it would flip between sweeps and — since a flip
    means a different `company_id` — would rewrite links that were already correct.
    """
    stored = [(COMPANY, an_identity("Logitech", employer_domain="logitech.com")),
              (OTHER_COMPANY, an_identity("Logitech"))]
    claim = a_claim("Logitech", website="https://logitech.com")
    forward = resolve(claim, stored)
    backward = resolve(claim, list(reversed(stored)))
    assert forward.outcome is backward.outcome is ResolutionOutcome.MATCHED
    assert forward.company_id == backward.company_id == COMPANY
    assert set(forward.candidate_ids) == set(backward.candidate_ids)


def test_a_name_only_lookalike_does_not_stop_a_strong_match():
    """One strong candidate wins even beside a name-only one.

    Requiring a *sole* candidate rather than a sole strong candidate would make every
    company with a same-named neighbour permanently unlinkable, which is the failure
    mode that makes an over-cautious resolver as useless as a reckless one.
    """
    resolution = resolve(
        a_claim("Logitech", website="https://logitech.com"),
        [(COMPANY, an_identity("Logitech", employer_domain="logitech.com")),
         (OTHER_COMPANY, an_identity("Logitech"))])
    assert resolution.outcome is ResolutionOutcome.MATCHED
    assert resolution.company_id == COMPANY
    assert set(resolution.candidate_ids) == {COMPANY, OTHER_COMPANY}


# --- §14: AMBIGUOUS, the outcome that must stay reachable ---------------------

def test_two_stored_companies_matching_strongly_are_left_alone():
    """Two rows claiming one domain is a pre-existing duplicate, not a decision.

    Picking one would bury the problem under a link that looks authoritative. §24
    keeps merging explicit, so the honest move is to report both and write nothing.
    """
    resolution = resolve(
        a_claim("Logitech", website="https://logitech.com"),
        [(COMPANY, an_identity("Logitech", employer_domain="logitech.com")),
         (OTHER_COMPANY, an_identity("Logitech SA", employer_domain="logitech.com"))])
    assert resolution.outcome is ResolutionOutcome.AMBIGUOUS
    assert resolution.company_id is None
    assert set(resolution.candidate_ids) == {COMPANY, OTHER_COMPANY}
    assert "already duplicates" in resolution.detail


def test_a_single_name_only_candidate_is_ambiguous_and_never_a_match():
    """§2's prohibition, at the layer that would have done the merging.

    There is exactly one stored `Logitech` and the claim says `Logitech`. It is still
    not a match: "there is only one of them" is not corroboration, and the candidate
    is reported so an operator can confirm the alias by hand.
    """
    resolution = resolve(a_claim("Logitech"), [(COMPANY, an_identity("Logitech"))])
    assert resolution.outcome is ResolutionOutcome.AMBIGUOUS
    assert resolution.company_id is None
    assert resolution.candidate_ids == (COMPANY,)
    assert resolution.candidates[0].name == "Logitech"
    assert IdentitySignal.NORMALIZED_NAME in resolution.candidates[0].assessment.signals
    assert "a name is not an identity" in resolution.detail


def test_several_name_only_candidates_are_all_reported():
    """The review case: three `Migros` rows and no way to tell them apart yet."""
    resolution = resolve(
        a_claim("Migros"),
        [(COMPANY, an_identity("Migros")), (OTHER_COMPANY, an_identity("MIGROS")),
         (THIRD_COMPANY, an_identity("Migros SA"))])
    assert resolution.outcome is ResolutionOutcome.AMBIGUOUS
    assert set(resolution.candidate_ids) == {COMPANY, OTHER_COMPANY, THIRD_COMPANY}
    assert "3 stored companies" in resolution.detail


def test_an_unresolved_or_ambiguous_answer_never_carries_a_company_id():
    """The invariant `_validated` enforces at every construction site in the module.

    A caller that checked `resolution.company_id` before `resolution.outcome` would
    otherwise link a company the resolver explicitly declined to choose — and that is
    a plausible mistake, which is why the id is absent rather than merely ignored.
    """
    shortlists: tuple[list[tuple[CompanyId, CompanyIdentity]], ...] = (
        [],
        [(COMPANY, an_identity("Logitech"))],
        [(COMPANY, an_identity("Logitech", employer_domain="logitech.com")),
         (OTHER_COMPANY, an_identity("Logitech", employer_domain="logitech.com"))],
        [(COMPANY, an_identity("Beta"))],
    )
    for stored in shortlists:
        resolution = resolve(a_claim("Logitech", website="https://logitech.com"),
                             stored)
        assert (resolution.company_id is None) is not resolution.is_matched


# --- §13: UNRESOLVED, which is not the same as ambiguous ----------------------

def test_a_claim_nothing_matches_is_unresolved_so_the_caller_may_create_it():
    resolution = resolve(a_claim("Logitech", website="https://logitech.com"),
                         [(COMPANY, an_identity("Nestle",
                                                employer_domain="nestle.com"))])
    assert resolution.outcome is ResolutionOutcome.UNRESOLVED
    assert resolution.company_id is None
    assert resolution.candidates == ()
    assert "no stored company shares any comparable evidence" in resolution.detail


def test_a_company_the_evidence_rules_out_is_not_even_a_candidate():
    """`DISTINCT` and `INSUFFICIENT_EVIDENCE` are dropped rather than listed.

    Reporting them would turn every `AMBIGUOUS` review into a walk through the whole
    shortlist, and `DISTINCT` in particular is the one verdict that positively says
    "not this one".
    """
    resolution = resolve(
        a_claim("Migros", website="https://migros.ch"),
        [(COMPANY, an_identity("Migros", employer_domain="migros-online.test")),
         (OTHER_COMPANY, an_identity("Coop", employer_domain="coop.ch"))])
    assert resolution.outcome is ResolutionOutcome.UNRESOLVED
    assert resolution.candidates == ()


def test_unresolved_and_ambiguous_stay_distinguishable():
    """They call for opposite actions, which is the whole reason for two names.

    Collapsing them would make "create the company" the answer to "a human should
    look at this", and every doubtful sweep would add a duplicate row.
    """
    nothing = resolve(a_claim("Logitech"), [])
    doubtful = resolve(a_claim("Logitech"), [(COMPANY, an_identity("Logitech"))])
    assert nothing.outcome is ResolutionOutcome.UNRESOLVED
    assert doubtful.outcome is ResolutionOutcome.AMBIGUOUS
    assert nothing.outcome is not doubtful.outcome


# --- §13: the posting's own company name is not this module's business ---------

def test_a_resolution_has_no_field_through_which_a_name_could_be_rewritten():
    """§13, structurally: nothing here can overwrite `Opportunity.company_name`.

    `resolve` takes identities and returns an id, a candidate list and a sentence.
    The caller sets `company_id` beside the untouched posting string, so the original
    label survives as provenance — asserted at the service layer for the write path
    and here for the shape that makes it impossible in the first place.
    """
    assert set(CompanyResolution.model_fields) == {"outcome", "company_id",
                                                   "candidates", "detail"}
    resolution = resolve(a_claim("Logitech", website="https://logitech.com"),
                         [(COMPANY, an_identity("Logitech",
                                                employer_domain="logitech.com"))])
    assert not hasattr(resolution, "company_name")


# --- §19: the country-domain preference is recorded, never decisive ------------

def test_a_local_domain_is_reported_on_a_candidate_and_decides_nothing(ch):
    """Two Swiss-looking candidates, one on `.ch`, and still `AMBIGUOUS`.

    The flag exists so an operator reviewing the ambiguity can see which candidate
    looked more local. Promoting it to a decider would let a `.ch` duplicate outrank
    a legitimate `.com` employer on nothing but a suffix.
    """
    resolution = resolve(
        a_claim("Acme"),
        [(COMPANY, an_identity("Acme", employer_domain="acme.ch")),
         (OTHER_COMPANY, an_identity("Acme", employer_domain="acme.com"))],
        pack=ch)
    assert resolution.outcome is ResolutionOutcome.AMBIGUOUS
    flags = {candidate.company_id: candidate.prefers_country_domain
             for candidate in resolution.candidates}
    assert flags == {COMPANY: True, OTHER_COMPANY: False}


def test_without_a_pack_no_candidate_prefers_any_domain():
    """A country nobody has modelled produces no preference rather than a guess."""
    resolution = resolve(a_claim("Acme"),
                         [(COMPANY, an_identity("Acme",
                                                employer_domain="acme.ch"))])
    assert resolution.candidates[0].prefers_country_domain is False


def test_a_candidate_carries_its_evidence_rather_than_a_score(ch):
    """§2 asks for typed evidence. A number would invite a threshold, and a
    threshold is the fuzzy merge the phase order forbids."""
    resolution = resolve(a_claim("Logitech", website="https://logitech.com"),
                         [(COMPANY, an_identity("Logitech SA",
                                                employer_domain="logitech.com"))],
                         pack=ch)
    candidate = resolution.candidates[0]
    assert set(type(candidate).model_fields) == {"company_id", "name", "assessment",
                                                 "prefers_country_domain"}
    assert candidate.assessment.strong_signals == frozenset(
        {IdentitySignal.EMPLOYER_DOMAIN})


# --- the shortlist the caller is expected to fetch ----------------------------

def test_the_strong_lookup_keys_are_the_ones_worth_an_indexed_query():
    """Domain, then platform-qualified organization, then external ids.

    The platform is part of the ATS key because `acme` on Greenhouse and `acme` on
    Lever are two tenancies — the same reason `compare` refuses to match them.
    """
    identity = a_claim("Acme", website="https://acme.test",
                       ats_platform=AtsPlatform.GREENHOUSE,
                       ats_organization_id="acme",
                       external_ids=("manual_seed:acme", "configured_ats:acme"))
    assert strong_lookup_keys(identity) == ("acme.test", "GREENHOUSE:acme",
                                            "configured_ats:acme", "manual_seed:acme")


def test_a_claim_with_nothing_but_a_name_has_no_strong_key_to_look_up():
    """Which is exactly why such a claim can only ever reach `AMBIGUOUS`."""
    assert strong_lookup_keys(a_claim("Acme")) == ()


def test_a_platform_without_an_organization_is_not_a_lookup_key():
    assert strong_lookup_keys(a_claim("Acme",
                                      ats_platform=AtsPlatform.GREENHOUSE)) == ()


def test_the_name_keys_include_the_legal_form_so_a_short_claim_finds_a_long_row(ch):
    """`Logitech` has to find the row stored as `logitech sa`.

    Only the legal-form key does that, and it has to be searched on both sides —
    which is why the repository indexes it and this function returns both forms.
    """
    identity = a_claim("Logitech SA", pack=ch)
    assert name_lookup_keys(identity) == ("logitech", "logitech sa")


def test_the_name_keys_carry_the_aliases_and_never_an_empty_form():
    identity = an_identity("Acme", aliases=frozenset({"acme group", ""}))
    assert name_lookup_keys(identity) == ("acme", "acme group")


# --- how much a claim is worth on its own -------------------------------------

@pytest.mark.parametrize(("claim", "corroborated"), [
    (identity_of_claim("Acme"), False),
    (identity_of_claim("Acme", country="CH"), False),
    (identity_of_claim("Acme", website="https://acme.test"), True),
    (identity_of_claim("Acme", careers_url="https://jobs.lever.co/acme"), False),
    (identity_of_claim("Acme", ats_platform=AtsPlatform.LEVER), False),
    (identity_of_claim("Acme", ats_platform=AtsPlatform.LEVER,
                       ats_organization_id="acme"), True),
    (identity_of_claim("Acme", external_ids=("manual_seed:acme",)), True),
])
def test_a_claim_is_corroborated_only_by_something_that_is_not_its_name(claim,
                                                                       corroborated):
    """What separates a `SEEDED` company from a `PROVISIONAL` one (§1).

    A bare name is a label somebody typed; a domain, a board token or a provider's
    own identifier is a second party agreeing. The careers-only row is the
    interesting one: a shared board host is nobody's domain, so it corroborates
    nothing.
    """
    assert has_corroboration(claim) is corroborated


def test_only_signals_outside_the_name_can_corroborate_a_new_company():
    """Pinned as a set for the same reason `STRONG_SIGNALS` is.

    Adding `NORMALIZED_NAME` here would promote every name-only seed to
    `PROVISIONAL`, which is a claim about verification that nothing performed.
    """
    assert CORROBORATING_SIGNALS == frozenset({IdentitySignal.EMPLOYER_DOMAIN,
                                               IdentitySignal.ATS_ORGANIZATION,
                                               IdentitySignal.EXTERNAL_ID})
    assert IdentitySignal.NORMALIZED_NAME not in CORROBORATING_SIGNALS
    assert IdentitySignal.CONFIRMED_ALIAS not in CORROBORATING_SIGNALS


# --- pairing companies with their identities ---------------------------------

def test_companies_and_identities_are_zipped_into_the_shortlist_shape():
    companies = [a_company(id=COMPANY, name="Acme", locations=()),
                 a_company(id=OTHER_COMPANY, name="Beta", locations=())]
    identities = [an_identity("Acme"), an_identity("Beta")]
    assert stored_company_identity_pairs(companies, identities) == (
        (COMPANY, identities[0]), (OTHER_COMPANY, identities[1]))


def test_an_uneven_pairing_is_refused_rather_than_silently_truncated():
    """`zip` would drop the extra and compare one company against another's evidence.

    The consequence of that off-by-one is a wrong merge on strong-looking evidence,
    which nothing downstream would report — cheap to check, expensive to miss.
    """
    with pytest.raises(ValueError, match="a pairing this uneven"):
        stored_company_identity_pairs([a_company(id=COMPANY)],
                                      [an_identity("Acme"), an_identity("Beta")])
