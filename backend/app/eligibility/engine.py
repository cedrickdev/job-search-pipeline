"""The deterministic eligibility engine.

Where the match engine asks "how well does this fit?", this one asks "may this
application happen at all?" — a set of gates, each answered ELIGIBLE, INCOMPLETE,
REVIEW_REQUIRED or INELIGIBLE, and never a score. It reads a `CandidateProfile`,
an `Opportunity` and a `CountryPack`, and produces an `EligibilityResult` whose
aggregate verdict the domain derives from the checks.

The rules it must obey are the reason this is a separate engine from matching:

- *Missing evidence is INCOMPLETE, never a refusal.* An unrecorded work
  authorization, an undeclared language, an unknown status — all route to human
  review or to "we don't know yet", never to INELIGIBLE. Silence does not close a
  gate.
- *Operator-maintained pack data cannot refuse on its own.* Every blocking
  decision is routed through `permitted_block_status`, so the Swiss student-permit
  cap (an `OPERATOR_CONFIG` value) raises REVIEW_REQUIRED rather than INELIGIBLE.
  Only a candidate's own declaration or a `VERIFIED` rule may block.
- *A permit hours cap is a gate, not a low SCHEDULE_FIT.* The same 15h-vs-20h
  conflict that lowers a schedule score also closes a permit gate; the two axes
  reach opposite-looking answers about the same numbers, and both are correct.

Today the engine can evaluate WORK_AUTHORIZATION (always — it is the baseline
gate), PERMIT_HOURS_CAP (when a capped permit and a comparable workload both
exist) and LANGUAGE_MINIMUM (per required language). MINIMUM_AGE is inévaluable —
the candidate model carries no birthdate — so it is omitted rather than guessed,
and location reachability stays a preference in the match engine.
"""
from datetime import datetime

from backend.app.domain.candidate import (
    CandidateProfile,
    WorkAuthorization,
    WorkAuthorizationStatus,
)
from backend.app.domain.common import Reason, ReasonImpact, WorkloadRange
from backend.app.domain.eligibility import (
    DeterminationSource,
    EligibilityCheck,
    EligibilityRequirement,
    EligibilityResult,
    EligibilityStatus,
    RuleAuthority,
)
from backend.app.domain.identifiers import eligibility_result_id
from backend.app.domain.opportunity import Opportunity
from backend.app.eligibility.policy import (
    ELIGIBILITY_POLICY_VERSION,
    permitted_block_status,
)
from country_packs.contracts import CountryPack

# The statuses that answer WORK_AUTHORIZATION affirmatively. REQUIRES_SPONSORSHIP
# is pointedly not here: it is a "maybe", routed to review, not a yes.
_AUTHORISED: frozenset[WorkAuthorizationStatus] = frozenset({
    WorkAuthorizationStatus.CITIZEN,
    WorkAuthorizationStatus.PERMANENT_RESIDENT,
    WorkAuthorizationStatus.WORK_PERMIT_HELD,
    WorkAuthorizationStatus.STUDENT_PERMIT_WITH_WORK_RIGHTS,
})


def evaluate_eligibility(profile: CandidateProfile, opportunity: Opportunity, *,
                         pack: CountryPack | None,
                         now: datetime) -> EligibilityResult:
    """Evaluate every gate for one candidate/opportunity pair.

    Always returns a result: WORK_AUTHORIZATION is emitted unconditionally, so the
    "at least one check" the domain requires is always met, even when the honest
    answer to every gate is "we don't know".
    """
    checks: list[EligibilityCheck] = [_work_authorization_check(profile, opportunity)]
    permit_cap = _permit_hours_cap_check(profile, opportunity, pack)
    if permit_cap is not None:
        checks.append(permit_cap)
    checks.extend(_language_minimum_checks(profile, opportunity))
    return EligibilityResult(
        id=eligibility_result_id(profile.id, opportunity.id),
        user_id=profile.user_id,
        candidate_profile_id=profile.id,
        opportunity_id=opportunity.id,
        checks=tuple(checks),
        policy_version=ELIGIBILITY_POLICY_VERSION,
        determined_at=now,
    )


def _reason(code: str, detail: str,
            impact: ReasonImpact = ReasonImpact.NEGATIVE) -> Reason:
    return Reason(code=code, detail=detail, impact=impact)


def _work_authorization_check(profile: CandidateProfile,
                              opportunity: Opportunity) -> EligibilityCheck:
    """The baseline gate: may the candidate work where the posting is?

    Sourced from the candidate's own recorded authorization, which is a
    `CANDIDATE_DECLARATION` — strong enough to refuse (a person stating they are
    not authorized is not an unverified legal rule), but silence about it is
    INCOMPLETE, never a refusal. A posting with no country cannot be assessed at
    all, and says so.
    """
    requirement = EligibilityRequirement.WORK_AUTHORIZATION
    country = opportunity.location.country if opportunity.location else None
    if country is None:
        return EligibilityCheck(
            requirement=requirement, status=EligibilityStatus.INCOMPLETE,
            determined_by=DeterminationSource.DETERMINISTIC_RULE,
            detail="the posting states no country, so work authorisation cannot be "
                   "assessed",
            reasons=(_reason("WORK_AUTHORIZATION_COUNTRY_UNKNOWN",
                             "the posting names no country to check a right to work "
                             "against", ReasonImpact.NEUTRAL),))
    authorization = profile.authorization_for(country)
    if authorization is None:
        return EligibilityCheck(
            requirement=requirement, status=EligibilityStatus.INCOMPLETE,
            determined_by=DeterminationSource.DETERMINISTIC_RULE,
            detail=f"the candidate has recorded no right-to-work status for {country}",
            reasons=(_reason("WORK_AUTHORIZATION_NOT_RECORDED",
                             f"no work authorisation is on file for {country}",
                             ReasonImpact.NEUTRAL),))
    status = authorization.status
    if status in _AUTHORISED:
        return EligibilityCheck(
            requirement=requirement, status=EligibilityStatus.ELIGIBLE,
            determined_by=DeterminationSource.CANDIDATE_DECLARATION,
            detail=f"the candidate holds {status} in {country}")
    if status is WorkAuthorizationStatus.NOT_AUTHORIZED:
        return EligibilityCheck(
            requirement=requirement, status=EligibilityStatus.INELIGIBLE,
            determined_by=DeterminationSource.CANDIDATE_DECLARATION,
            detail=f"the candidate has declared they are not authorised to work in "
                   f"{country}",
            reasons=(_reason("WORK_AUTHORIZATION_DENIED",
                             f"the candidate declares no right to work in {country}"),))
    if status is WorkAuthorizationStatus.REQUIRES_SPONSORSHIP:
        return EligibilityCheck(
            requirement=requirement, status=EligibilityStatus.REVIEW_REQUIRED,
            determined_by=DeterminationSource.CANDIDATE_DECLARATION,
            detail=f"working in {country} would require sponsorship; a human must "
                   f"confirm the employer offers it",
            reasons=(_reason("WORK_AUTHORIZATION_NEEDS_SPONSORSHIP",
                             f"the candidate would need sponsorship to work in "
                             f"{country}"),))
    return EligibilityCheck(
        requirement=requirement, status=EligibilityStatus.INCOMPLETE,
        determined_by=DeterminationSource.DETERMINISTIC_RULE,
        detail=f"the candidate's right-to-work status in {country} is unknown",
        reasons=(_reason("WORK_AUTHORIZATION_UNKNOWN",
                         f"the candidate's right to work in {country} is not yet "
                         f"known", ReasonImpact.NEUTRAL),))


def _permit_hours_cap_check(profile: CandidateProfile, opportunity: Opportunity,
                            pack: CountryPack | None) -> EligibilityCheck | None:
    """The permit hours gate — emitted only when there is genuinely a cap to check.

    The cap comes from the Country Pack's permit rule for the candidate's status
    (an `OPERATOR_CONFIG` value that, per `permitted_block_status`, can only ever
    raise review) or, failing that, from a cap the candidate declared on their own
    authorization. Either way the verdict is routed through the policy, so this
    check is *structurally incapable* of refusing on unverified pack data — the
    §59 legal-safety invariant. Absent a cap or a comparable workload, there is
    nothing to check and the gate is omitted rather than reported as passing.
    """
    if pack is None:
        return None
    country = opportunity.location.country if opportunity.location else None
    if country is None or country != pack.country:
        return None
    authorization = profile.authorization_for(country)
    if authorization is None:
        return None
    cap, authority, determined_by = _effective_cap(authorization, pack)
    if cap is None or opportunity.workload is None:
        return None
    job_low, job_high = _workload_hours(opportunity.workload, pack)
    if job_low is None and job_high is None:
        return None

    requirement = EligibilityRequirement.PERMIT_HOURS_CAP
    # At least one bound is known (guarded above); the band spans what there is.
    bounds = [hours for hours in (job_low, job_high) if hours is not None]
    posting_low, posting_high = min(bounds), max(bounds)

    if posting_high <= cap:
        return EligibilityCheck(
            requirement=requirement, status=EligibilityStatus.ELIGIBLE,
            determined_by=determined_by, authority=authority,
            detail=f"the permit caps paid work at {cap:g}h/week and the posting "
                   f"stays within it")
    if posting_low <= cap < posting_high:
        # The lower end fits, the upper does not: a reduced schedule might work, so
        # a human decides rather than the engine refusing outright.
        return EligibilityCheck(
            requirement=requirement, status=EligibilityStatus.REVIEW_REQUIRED,
            determined_by=determined_by, authority=authority,
            detail=f"the permit caps paid work at {cap:g}h/week; the posting's upper "
                   f"hours exceed it",
            reasons=(_reason("PERMIT_HOURS_CAP_PARTIALLY_EXCEEDED",
                             f"part of the posting's hours exceed the {cap:g}h/week "
                             f"permit cap"),))
    return EligibilityCheck(
        requirement=requirement, status=permitted_block_status(authority),
        determined_by=determined_by, authority=authority,
        detail=f"the permit caps paid work at {cap:g}h/week; the posting asks for at "
               f"least {posting_low:g}h",
        reasons=(_reason("PERMIT_HOURS_CAP_EXCEEDED",
                         f"the posting's hours exceed the {cap:g}h/week permit cap"),))


def _effective_cap(authorization: WorkAuthorization, pack: CountryPack
                   ) -> tuple[float | None, RuleAuthority, DeterminationSource]:
    """The weekly-hours cap that applies, with its authority and who supplied it.

    The pack's rule for the candidate's permit class comes first — it is the
    maintained source — and carries its own authority (`OPERATOR_CONFIG` by
    default). A cap the candidate recorded on their own authorization is the
    fallback, and is treated as `UNKNOWN` authority so it, too, can only raise
    review. `None` means no cap applies, and the gate is not emitted.
    """
    for permit in pack.eligibility.permits:
        if permit.weekly_hours_cap is not None and permit.status is authorization.status:
            return (permit.weekly_hours_cap, permit.authority,
                    DeterminationSource.COUNTRY_PACK_RULE)
    if authorization.permit_hours_cap is not None:
        return (authorization.permit_hours_cap, RuleAuthority.UNKNOWN,
                DeterminationSource.CANDIDATE_DECLARATION)
    return None, RuleAuthority.UNKNOWN, DeterminationSource.DETERMINISTIC_RULE


def _language_minimum_checks(profile: CandidateProfile, opportunity: Opportunity
                             ) -> list[EligibilityCheck]:
    """One gate per *required* language; nice-to-haves are matching's business.

    The three-way split follows the phase order's insistence that unknown never
    blocks. A language the candidate declared below the floor is a candidate's own
    statement about themselves and closes the gate (INELIGIBLE); a language they
    never declared — or an empty language list — is unknown and routes to
    INCOMPLETE, because not listing a language is not the same as lacking it.
    """
    requirement = EligibilityRequirement.LANGUAGE_MINIMUM
    declared = {proficiency.language: proficiency.level
                for proficiency in profile.languages}
    checks: list[EligibilityCheck] = []
    for language_requirement in opportunity.language_requirements:
        if not language_requirement.required:
            continue
        language = language_requirement.language.upper()
        minimum = language_requirement.minimum_level
        level = declared.get(language_requirement.language)
        if level is None:
            unknown = ("the candidate has recorded no languages"
                       if not profile.languages
                       else f"the candidate has not declared {language}")
            checks.append(EligibilityCheck(
                requirement=requirement, status=EligibilityStatus.INCOMPLETE,
                determined_by=DeterminationSource.DETERMINISTIC_RULE,
                detail=f"the posting requires {language} {minimum}; {unknown}",
                reasons=(_reason("LANGUAGE_PROFICIENCY_UNKNOWN"
                                 if not profile.languages else "LANGUAGE_NOT_DECLARED",
                                 f"the candidate's {language} level is not known",
                                 ReasonImpact.NEUTRAL),)))
        elif level.meets(minimum):
            checks.append(EligibilityCheck(
                requirement=requirement, status=EligibilityStatus.ELIGIBLE,
                determined_by=DeterminationSource.CANDIDATE_DECLARATION,
                detail=f"the candidate declares {language} {level}, meeting the "
                       f"required {minimum}"))
        else:
            checks.append(EligibilityCheck(
                requirement=requirement, status=EligibilityStatus.INELIGIBLE,
                determined_by=DeterminationSource.CANDIDATE_DECLARATION,
                detail=f"the candidate declares {language} {level}, below the "
                       f"required {minimum}",
                reasons=(_reason("LANGUAGE_BELOW_MINIMUM",
                                 f"the candidate's {language} {level} is below the "
                                 f"required {minimum}"),)))
    return checks


def _workload_hours(workload: WorkloadRange,
                    pack: CountryPack) -> tuple[float | None, float | None]:
    """A workload as a (low, high) weekly-hours band, converting percent via the pack.

    The same conversion the match engine uses and the only one sanctioned
    (`PackMetadata.weekly_hours_for_percent`). Kept private to each engine so
    neither reaches into the other's internals; the shared authority is the pack
    method, not a shared helper.
    """
    if workload.min_weekly_hours is not None or workload.max_weekly_hours is not None:
        return workload.min_weekly_hours, workload.max_weekly_hours
    if workload.min_percent is None and workload.max_percent is None:
        return None, None
    low = (pack.metadata.weekly_hours_for_percent(workload.min_percent)
           if workload.min_percent is not None else None)
    high = (pack.metadata.weekly_hours_for_percent(workload.max_percent)
            if workload.max_percent is not None else None)
    return low, high
