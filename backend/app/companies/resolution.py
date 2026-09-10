"""Turning a claimed employer into a canonical `company_id` — or refusing to.

Two callers, one algorithm. §13 asks that an `Opportunity.company_name` resolve to
a canonical company; §7 asks that a seed not automatically become one. Both are the
same question — "which stored company, if any, is this?" — so both go through
`resolve`.

Three properties the phase order asks for, and where each is implemented:

**The posting's own string is never overwritten** (§13). Nothing in this module
returns a modified `Opportunity`; it returns a `CompanyResolution` carrying an id,
and the caller sets `company_id` beside the untouched `company_name`. There is no
code path here that could rewrite a name.

**Ambiguity refuses rather than guesses** (§14). Two candidates that both look
plausible produce `AMBIGUOUS` with both listed, and the opportunity stays unlinked.
That is a worse-looking outcome than a wrong link and a much better one: a wrong
link corrupts an identity that everything downstream trusts, and nothing later
reports it.

**Resolution is idempotent** (§13, §23). It reads and compares; it writes nothing,
holds no state and samples no clock. Running it twice over the same candidates
returns the same verdict, and running it after the company was created returns
`MATCHED` on the strong signal that created it.

No LLM, by requirement (§25) and by construction: the only comparison available is
`identity.compare`, which is exact equality of derived keys.
"""
from collections.abc import Sequence
from enum import StrEnum

from backend.app.companies.identity import (
    CompanyIdentity,
    IdentityAssessment,
    IdentitySignal,
    IdentityVerdict,
    compare,
    prefers_domain,
)
from backend.app.domain.base import DomainModel, NonEmptyStr
from backend.app.domain.company import Company
from backend.app.domain.identifiers import CompanyId
from country_packs.contracts import CountryPack


class ResolutionOutcome(StrEnum):
    """What resolving one claim against the stored companies concluded (§13, §14).

    `UNRESOLVED` and `AMBIGUOUS` are both "no id", and they are kept apart because
    they call for different actions: `UNRESOLVED` means create the company,
    `AMBIGUOUS` means a human should look. Collapsing them would make the first
    indistinguishable from the second and turn every doubtful case into a new
    duplicate row.
    """

    MATCHED = "MATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"


class ResolutionCandidate(DomainModel):
    """One stored company that might be the claim, with why it might be.

    Carries the assessment rather than a score: §2 asks for typed evidence, and a
    number would invite a threshold, which is exactly the fuzzy merge the phase
    order forbids.
    """

    company_id: CompanyId
    name: NonEmptyStr
    assessment: IdentityAssessment
    # Whether this candidate's domain sits on one of the country's own suffixes. A
    # tie-break between candidates that already tied, never evidence of identity
    # (`PackMetadata.company_domain_suffixes` says so itself).
    prefers_country_domain: bool = False


class CompanyResolution(DomainModel):
    """The answer, and everything needed to explain or review it.

    `company_id` is set only for `MATCHED`, which the validator enforces: an
    `AMBIGUOUS` resolution that happened to carry an id would be used as a match by
    the first caller that forgot to check the outcome.
    """

    outcome: ResolutionOutcome
    company_id: CompanyId | None = None
    candidates: tuple[ResolutionCandidate, ...] = ()
    detail: NonEmptyStr

    @property
    def is_matched(self) -> bool:
        return self.outcome is ResolutionOutcome.MATCHED

    @property
    def candidate_ids(self) -> tuple[CompanyId, ...]:
        return tuple(candidate.company_id for candidate in self.candidates)


def _validated(resolution: CompanyResolution) -> CompanyResolution:
    """The one invariant, applied at every construction site in this module.

    A module-level function rather than a validator on the model because
    `CompanyResolution` is also built by tests and by the service from stored state,
    and the rule being about *this* module's construction sites is clearer than a
    validator that would have to allow the reconstruction case.
    """
    if resolution.is_matched and resolution.company_id is None:
        raise ValueError("a MATCHED resolution must carry the company it matched")
    if not resolution.is_matched and resolution.company_id is not None:
        raise ValueError(f"a {resolution.outcome} resolution must not carry an id; "
                         "a caller that ignored the outcome would treat it as a match")
    return resolution


def resolve(claim: CompanyIdentity,
            stored: Sequence[tuple[CompanyId, CompanyIdentity]], *,
            pack: CountryPack | None = None) -> CompanyResolution:
    """Which stored company `claim` is, if the evidence says so (§2, §13, §14).

    `stored` is the *shortlist*, not the table: the caller has already narrowed it
    with an indexed lookup (by normalized name, by domain, by ATS organization, by
    alias), and this function decides among what came back. Keeping the query out of
    here is what lets the same rules run against a fake list in a unit test and
    against Postgres in production.

    The decision, in order:

    1. **Every strong match wins.** A single company agreeing on a domain, an ATS
       organization, an external id or a confirmed alias is `MATCHED`.
    2. **Two strong matches are `AMBIGUOUS`.** Two stored rows both claiming the
       same domain is a data problem that predates this claim, and picking one would
       bury it. §24 keeps merging explicit.
    3. **`POSSIBLE_MATCH` never resolves.** One name-only candidate is `AMBIGUOUS`,
       not `MATCHED` — §2 forbids merging on name similarity, and "there is only one
       of them" is not corroboration. The candidate is reported so the operator sees
       what was found.
    4. **Nothing comparable is `UNRESOLVED`**, and the caller may create a company.

    The country-domain preference is recorded on every candidate and used nowhere as
    a decider: it exists so an operator reviewing an `AMBIGUOUS` outcome can see
    which candidate looked more local. Deciding on it would make a `.ch` duplicate
    silently outrank a legitimate `.com` employer.
    """
    candidates = tuple(
        ResolutionCandidate(
            company_id=company_id,
            name=identity.name,
            assessment=assessment,
            prefers_country_domain=prefers_domain(pack, identity.employer_domain
                                                  or ""))
        for company_id, identity in stored
        if (assessment := compare(claim, identity)).verdict in (
            IdentityVerdict.SAME_COMPANY, IdentityVerdict.POSSIBLE_MATCH))

    strong = tuple(candidate for candidate in candidates
                   if candidate.assessment.is_same_company)
    if len(strong) == 1:
        matched = strong[0]
        signals = ", ".join(sorted(signal.value for signal
                                   in matched.assessment.strong_signals))
        return _validated(CompanyResolution(
            outcome=ResolutionOutcome.MATCHED,
            company_id=matched.company_id,
            candidates=candidates,
            detail=f"{claim.name!r} is {matched.name!r}: agreed on {signals}"))
    if len(strong) > 1:
        return _validated(CompanyResolution(
            outcome=ResolutionOutcome.AMBIGUOUS,
            candidates=candidates,
            detail=f"{len(strong)} stored companies each match {claim.name!r} on "
                   "strong evidence, which means two of them are already duplicates; "
                   "left unlinked rather than picking one"))
    if candidates:
        return _validated(CompanyResolution(
            outcome=ResolutionOutcome.AMBIGUOUS,
            candidates=candidates,
            detail=f"{len(candidates)} stored companies share a name form with "
                   f"{claim.name!r} and nothing outside the name corroborates it; a "
                   "name is not an identity (§2)"))
    return _validated(CompanyResolution(
        outcome=ResolutionOutcome.UNRESOLVED,
        detail=f"no stored company shares any comparable evidence with "
               f"{claim.name!r}"))


def strong_lookup_keys(identity: CompanyIdentity) -> tuple[str, ...]:
    """The values a repository should index-search on to build the shortlist.

    Returned as opaque strings in a stable order so the caller can pass them
    straight to a repository without re-deriving them — and so a test can assert
    that a resolution really was driven by a domain rather than by a name.

    Order is strongest first, which is only a documentation aid: `resolve` weighs
    the evidence itself, and a shortlist is a set.
    """
    keys: list[str] = []
    if identity.employer_domain:
        keys.append(identity.employer_domain)
    if identity.ats_platform is not None and identity.ats_organization_id:
        keys.append(f"{identity.ats_platform.value}:{identity.ats_organization_id}")
    keys.extend(sorted(identity.external_ids))
    return tuple(keys)


def name_lookup_keys(identity: CompanyIdentity) -> tuple[str, ...]:
    """The name forms a repository should search, deduplicated and ordered.

    Both the normalized name and the legal-form key, because the stored side may
    have either: `Logitech SA` is stored with `normalized_name="logitech sa"` and a
    claim of `Logitech` has to find it, which only the legal-form key does.
    """
    forms = {identity.normalized_name, identity.legal_form_key} | identity.aliases
    return tuple(sorted(form for form in forms if form))


# Signals that justify promoting a company's `identity_status` above `SEEDED`. A
# company created from a name alone stays `SEEDED`; one whose creation was
# corroborated by a domain or an ATS organization is `PROVISIONAL`. `VERIFIED` is
# never reached automatically (see `CompanyIdentityStatus`).
CORROBORATING_SIGNALS: frozenset[IdentitySignal] = frozenset({
    IdentitySignal.EMPLOYER_DOMAIN,
    IdentitySignal.ATS_ORGANIZATION,
    IdentitySignal.EXTERNAL_ID,
})


def has_corroboration(claim: CompanyIdentity) -> bool:
    """Whether a claim carries a second, non-name signal about the same employer.

    Read by the service when it creates a company from an unresolved claim: a seed
    that brought a domain or an ATS organization with it is `PROVISIONAL`, a bare
    name is `SEEDED`. Not part of `resolve`, because it says nothing about *which*
    company this is — only about how much the claim itself is worth.
    """
    return bool(claim.employer_domain
                or (claim.ats_platform is not None and claim.ats_organization_id)
                or claim.external_ids)


def stored_company_identity_pairs(
    companies: Sequence[Company],
    identities: Sequence[CompanyIdentity],
) -> tuple[tuple[CompanyId, CompanyIdentity], ...]:
    """Zip companies with their derived identities, refusing a mismatched pair.

    A convenience for callers that built both lists — the repository layer and the
    tests — and a guard against the off-by-one that would compare one company's name
    against another's domain. Cheap, and the failure it prevents is a wrong merge.
    """
    if len(companies) != len(identities):
        raise ValueError(
            f"{len(companies)} companies and {len(identities)} identities: a pairing "
            "this uneven would compare one company against another's evidence")
    return tuple((company.id, identity)
                 for company, identity in zip(companies, identities, strict=True))
