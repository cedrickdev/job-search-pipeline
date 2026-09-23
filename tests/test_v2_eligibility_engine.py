# tests/test_v2_eligibility_engine.py
"""`evaluate_eligibility` — the deterministic gate engine, and its legal safety.

Where `test_v2_eligibility.py` pins what an `EligibilityResult` may *hold*, this
pins what the *engine* concludes from a real profile, posting and Country Pack.
The spine is the phase order's four non-negotiables:

- WORK_AUTHORIZATION is always emitted, so a result always carries a verdict;
- missing evidence is INCOMPLETE, an unknown status is REVIEW_REQUIRED — neither
  is ever a refusal;
- a candidate's own declaration may refuse, an operator-maintained pack rule may
  not (the §59 legal-safety invariant, given its own named regression below);
- nothing here is a score, and eligibility never reads a match dimension.
"""
from datetime import UTC, datetime

from backend.app.domain.candidate import WorkAuthorizationStatus
from backend.app.domain.common import (
    LanguageLevel,
    LanguageProficiency,
    LanguageRequirement,
    Location,
    WorkloadRange,
)
from backend.app.domain.eligibility import (
    DeterminationSource,
    EligibilityRequirement,
    EligibilityStatus,
    RuleAuthority,
)
from backend.app.domain.identifiers import eligibility_result_id
from backend.app.eligibility import ELIGIBILITY_POLICY_VERSION, evaluate_eligibility
from country_packs.ch import pack as ch_pack
from tests.v2_builders import (
    a_candidate_profile,
    a_work_authorization,
    an_opportunity,
)

NOW = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)

# The real Swiss pack, not a fixture: the legal-safety regression only means
# something if it runs against the same B_STUDENT rule the product ships.
CH_PACK = ch_pack.load()


def _checks_by_requirement(result):
    """The checks grouped by gate; LANGUAGE_MINIMUM may repeat, so this maps to lists."""
    grouped: dict[EligibilityRequirement, list] = {}
    for check in result.checks:
        grouped.setdefault(check.requirement, []).append(check)
    return grouped


def _only(result, requirement):
    checks = _checks_by_requirement(result)[requirement]
    assert len(checks) == 1, f"expected exactly one {requirement}, got {len(checks)}"
    return checks[0]


# --- the §59 named regression -------------------------------------------------

def test_operator_config_permit_cap_cannot_by_itself_produce_ineligible():
    """§59: the Swiss student-permit 15h/week cap can never *refuse* on its own.

    A B_STUDENT holder against a posting whose hours plainly exceed 15h/week is the
    canonical legal-safety case. The cap is real and it is `OPERATOR_CONFIG`, not a
    number this repository asserts as law — so the PERMIT_HOURS_CAP gate must reach
    REVIEW_REQUIRED, never INELIGIBLE, and with no other failing gate the whole
    result must not be INELIGIBLE either. This is the invariant that stops a wrong
    YAML value from becoming an automatic "you may not apply".
    """
    student = a_candidate_profile(
        work_authorizations=(a_work_authorization(
            country="CH",
            status=WorkAuthorizationStatus.STUDENT_PERMIT_WITH_WORK_RIGHTS),),
        # Meets the posting's required fr/C1, so language cannot muddy the verdict.
        languages=(LanguageProficiency(language="fr", level=LanguageLevel.C2),))
    # 80–100% of a 42h Swiss week is ~33.6–42h — well over the 15h cap, at both ends.
    full_time = an_opportunity(
        location=Location(country="CH", region="Vaud", city="Lausanne"),
        workload=WorkloadRange(min_percent=80, max_percent=100),
        language_requirements=(
            LanguageRequirement(language="fr", minimum_level=LanguageLevel.C1),))

    result = evaluate_eligibility(student, full_time, pack=CH_PACK, now=NOW)

    cap = _only(result, EligibilityRequirement.PERMIT_HOURS_CAP)
    assert cap.status is EligibilityStatus.REVIEW_REQUIRED
    assert cap.status is not EligibilityStatus.INELIGIBLE
    assert cap.determined_by is DeterminationSource.COUNTRY_PACK_RULE
    assert cap.authority is RuleAuthority.OPERATOR_CONFIG
    assert cap.reasons  # a non-ELIGIBLE gate must say why
    # The aggregate obeys it too: worst-of over the gates is REVIEW_REQUIRED here,
    # because the only non-ELIGIBLE gate is the (unverified) cap.
    assert result.status is EligibilityStatus.REVIEW_REQUIRED
    assert result.status is not EligibilityStatus.INELIGIBLE
    assert result.is_blocking is False


def test_the_ch_pack_student_permit_is_operator_config_not_verified():
    """Guards the regression's premise: if the pack shipped VERIFIED, §59 wouldn't hold.

    The invariant is only meaningful because the shipped rule is *not* verified. If
    someone raises B_STUDENT to VERIFIED in the YAML, this fails first and points at
    the deliberate legal decision rather than letting the regression above quietly
    change meaning.
    """
    student_permit = CH_PACK.eligibility.permit_for("B_STUDENT")
    assert student_permit is not None
    assert student_permit.weekly_hours_cap == 15.0
    assert student_permit.authority is RuleAuthority.OPERATOR_CONFIG
    assert student_permit.authority is not RuleAuthority.VERIFIED


# --- work authorisation: the baseline gate ------------------------------------

def test_work_authorization_is_always_emitted():
    """The one unconditional gate: a result always carries at least this verdict."""
    result = evaluate_eligibility(a_candidate_profile(), an_opportunity(),
                                  pack=None, now=NOW)
    assert EligibilityRequirement.WORK_AUTHORIZATION in _checks_by_requirement(result)


def test_a_citizen_is_eligible_to_work_in_their_country():
    result = evaluate_eligibility(a_candidate_profile(), an_opportunity(),
                                  pack=CH_PACK, now=NOW)
    work = _only(result, EligibilityRequirement.WORK_AUTHORIZATION)
    assert work.status is EligibilityStatus.ELIGIBLE
    assert work.determined_by is DeterminationSource.CANDIDATE_DECLARATION


def test_no_recorded_authorization_is_incomplete_not_ineligible():
    """Silence is not a closed gate: an unrecorded right to work is INCOMPLETE."""
    profile = a_candidate_profile(work_authorizations=())
    result = evaluate_eligibility(profile, an_opportunity(), pack=CH_PACK, now=NOW)
    work = _only(result, EligibilityRequirement.WORK_AUTHORIZATION)
    assert work.status is EligibilityStatus.INCOMPLETE
    assert work.status is not EligibilityStatus.INELIGIBLE
    assert work.reasons


def test_a_candidate_declaring_no_right_to_work_is_ineligible():
    """A person's own statement that they may not work *is* strong enough to refuse.

    The contrast with the pack cap is the whole point: a `CANDIDATE_DECLARATION`
    may block, an `OPERATOR_CONFIG` pack rule may not.
    """
    profile = a_candidate_profile(work_authorizations=(a_work_authorization(
        country="CH", status=WorkAuthorizationStatus.NOT_AUTHORIZED),))
    result = evaluate_eligibility(profile, an_opportunity(), pack=CH_PACK, now=NOW)
    work = _only(result, EligibilityRequirement.WORK_AUTHORIZATION)
    assert work.status is EligibilityStatus.INELIGIBLE
    assert work.determined_by is DeterminationSource.CANDIDATE_DECLARATION
    assert result.is_blocking is True


def test_needing_sponsorship_routes_to_review_not_refusal():
    profile = a_candidate_profile(work_authorizations=(a_work_authorization(
        country="CH", status=WorkAuthorizationStatus.REQUIRES_SPONSORSHIP),))
    result = evaluate_eligibility(profile, an_opportunity(), pack=CH_PACK, now=NOW)
    work = _only(result, EligibilityRequirement.WORK_AUTHORIZATION)
    assert work.status is EligibilityStatus.REVIEW_REQUIRED
    assert work.reasons


def test_a_posting_with_no_country_cannot_assess_authorization():
    """No country to check against is INCOMPLETE, never a guess in either direction."""
    result = evaluate_eligibility(a_candidate_profile(),
                                  an_opportunity(location=None), pack=CH_PACK, now=NOW)
    work = _only(result, EligibilityRequirement.WORK_AUTHORIZATION)
    assert work.status is EligibilityStatus.INCOMPLETE


# --- the permit hours cap gate ------------------------------------------------

def test_a_capped_permit_within_the_posting_hours_is_eligible():
    """The cap is a gate that can *pass*: a posting under 15h/week clears it."""
    student = a_candidate_profile(work_authorizations=(a_work_authorization(
        country="CH",
        status=WorkAuthorizationStatus.STUDENT_PERMIT_WITH_WORK_RIGHTS),))
    small = an_opportunity(
        location=Location(country="CH", city="Lausanne"),
        workload=WorkloadRange(min_weekly_hours=8.0, max_weekly_hours=12.0),
        language_requirements=())
    result = evaluate_eligibility(student, small, pack=CH_PACK, now=NOW)
    cap = _only(result, EligibilityRequirement.PERMIT_HOURS_CAP)
    assert cap.status is EligibilityStatus.ELIGIBLE


def test_the_permit_cap_gate_is_omitted_when_nothing_caps_the_candidate():
    """A citizen has no capped permit, so the gate is not emitted at all — not passed.

    Omission is the honest answer when there is no cap to check; reporting ELIGIBLE
    would claim a check the engine did not perform.
    """
    result = evaluate_eligibility(a_candidate_profile(), an_opportunity(),
                                  pack=CH_PACK, now=NOW)
    assert EligibilityRequirement.PERMIT_HOURS_CAP not in _checks_by_requirement(result)


def test_the_permit_cap_gate_is_omitted_without_a_pack():
    """No pack means no cap source, so the gate cannot be evaluated and is omitted."""
    student = a_candidate_profile(work_authorizations=(a_work_authorization(
        country="CH",
        status=WorkAuthorizationStatus.STUDENT_PERMIT_WITH_WORK_RIGHTS),))
    result = evaluate_eligibility(student, an_opportunity(), pack=None, now=NOW)
    assert EligibilityRequirement.PERMIT_HOURS_CAP not in _checks_by_requirement(result)


# --- language minimums --------------------------------------------------------

def test_a_declared_language_below_the_minimum_is_ineligible():
    """A candidate's own below-floor declaration closes the gate — it is their fact."""
    profile = a_candidate_profile(
        languages=(LanguageProficiency(language="de", level=LanguageLevel.A2),))
    opportunity = an_opportunity(
        location=Location(country="CH", city="Zurich"),
        language_requirements=(
            LanguageRequirement(language="de", minimum_level=LanguageLevel.C1),))
    result = evaluate_eligibility(profile, opportunity, pack=CH_PACK, now=NOW)
    language = _only(result, EligibilityRequirement.LANGUAGE_MINIMUM)
    assert language.status is EligibilityStatus.INELIGIBLE
    assert language.determined_by is DeterminationSource.CANDIDATE_DECLARATION


def test_an_undeclared_required_language_is_incomplete_not_ineligible():
    """Not listing a language is unknown, not lacking it: INCOMPLETE, never a refusal."""
    profile = a_candidate_profile(
        languages=(LanguageProficiency(language="fr", level=LanguageLevel.C2),))
    opportunity = an_opportunity(
        location=Location(country="CH", city="Zurich"),
        language_requirements=(
            LanguageRequirement(language="de", minimum_level=LanguageLevel.B2),))
    result = evaluate_eligibility(profile, opportunity, pack=CH_PACK, now=NOW)
    language = _only(result, EligibilityRequirement.LANGUAGE_MINIMUM)
    assert language.status is EligibilityStatus.INCOMPLETE
    assert language.status is not EligibilityStatus.INELIGIBLE


def test_a_preferred_language_is_not_an_eligibility_gate():
    """`required=False` is matching's concern; eligibility emits no gate for it."""
    profile = a_candidate_profile(languages=())
    opportunity = an_opportunity(
        location=Location(country="CH", city="Lausanne"),
        language_requirements=(
            LanguageRequirement(language="en", minimum_level=LanguageLevel.B2,
                                required=False),))
    result = evaluate_eligibility(profile, opportunity, pack=CH_PACK, now=NOW)
    assert EligibilityRequirement.LANGUAGE_MINIMUM not in _checks_by_requirement(result)


# --- provenance and idempotence -----------------------------------------------

def test_the_result_names_the_pair_and_stamps_its_policy():
    profile = a_candidate_profile()
    opportunity = an_opportunity()
    result = evaluate_eligibility(profile, opportunity, pack=CH_PACK, now=NOW)
    assert result.user_id == profile.user_id
    assert result.candidate_profile_id == profile.id
    assert result.opportunity_id == opportunity.id
    assert result.determined_at == NOW
    assert result.policy_version == ELIGIBILITY_POLICY_VERSION


def test_re_evaluating_the_same_pair_is_idempotent():
    """A deterministic engine writes the same id for the same pair, whatever the clock."""
    profile = a_candidate_profile()
    opportunity = an_opportunity()
    first = evaluate_eligibility(profile, opportunity, pack=CH_PACK, now=NOW)
    second = evaluate_eligibility(profile, opportunity, pack=CH_PACK,
                                  now=datetime(2026, 6, 1, tzinfo=UTC))
    assert first.id == second.id == eligibility_result_id(profile.id, opportunity.id)
    assert first.status == second.status


def test_reasons_carry_only_typed_codes_no_leaked_internals():
    """Reason codes are UI-safe: uppercase identifiers, never secrets or stack text.

    A closed gate has to explain itself, but the explanation the phase order permits
    is a typed code and a plain sentence — not an environment variable, a credential
    or an exception's repr leaking through the detail.
    """
    profile = a_candidate_profile(work_authorizations=(a_work_authorization(
        country="CH", status=WorkAuthorizationStatus.NOT_AUTHORIZED),))
    result = evaluate_eligibility(profile, an_opportunity(), pack=CH_PACK, now=NOW)
    for check in result.checks:
        for reason in check.reasons:
            assert reason.code == reason.code.upper()
            assert " " not in reason.code
            haystack = f"{reason.code} {reason.detail}".lower()
            for forbidden in ("traceback", "secret", "api_key", "password",
                              "0x", "env["):
                assert forbidden not in haystack
