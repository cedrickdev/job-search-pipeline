"""The Phase 9 eligibility policy: its version, and the legal-safety rule.

Two things live here so neither is scattered: the version stamped onto every
`EligibilityResult` the engine produces, and the single function that decides how
far a rule of a given authority is allowed to go. Keeping the second as an
explicit choice the engine makes — rather than a mistake the domain validator
catches — is what stops the engine from ever *trying* to build a verdict the
domain would refuse.
"""
from backend.app.domain.eligibility import EligibilityStatus, RuleAuthority

# The eligibility engine and policy that produced a verdict, stamped onto
# `EligibilityResult.policy_version` for audit. A change to how the engine reaches
# its verdicts is a change to this string, so a re-evaluation is never silently
# compared with one decided under different rules.
ELIGIBILITY_POLICY_VERSION = "eligibility-policy/1.0"


def permitted_block_status(authority: RuleAuthority) -> EligibilityStatus:
    """The strongest verdict a rule of this authority may reach on a failed gate.

    This is the legal-safety rule of docs/COUNTRY_PACKS.md §Eligibility, expressed
    as a decision taken *before* a check is built: only a `VERIFIED` rule — one an
    operator has checked against the actual legal source — may refuse an
    application (`INELIGIBLE`). Everything weaker (an operator's pack value, a
    posting's own claim, no stated provenance) raises `REVIEW_REQUIRED` so a human
    confirms it instead. The Swiss student-permit hours cap is the canonical case:
    it is `OPERATOR_CONFIG`, so a job that exceeds it can only ever reach review,
    never refusal, on the pack's word alone.

    `EligibilityCheck` enforces the same rule structurally; routing every blocking
    decision through here is why the engine never constructs a check that would
    trip it.
    """
    if authority is RuleAuthority.VERIFIED:
        return EligibilityStatus.INELIGIBLE
    return EligibilityStatus.REVIEW_REQUIRED
