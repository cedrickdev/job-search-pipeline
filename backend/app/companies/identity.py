"""Deciding whether two records describe the same employer — on evidence, not vibes.

§2 of the phase order is a prohibition before it is a feature: the system must be
able to see that `Logitech`, `Logitech Europe S.A.`, `LOGITECH` and `Logitech SA`
may be one company, and it must **not** merge companies on name similarity alone.
This module is where both halves live.

**There is no fuzzy matching here at all.** No edit distance, no token overlap
ratio, no threshold. Every comparison is exact equality of a deterministically
derived key, which is what makes the outcome reproducible, explainable and
testable — and what makes "never silently merge on low confidence" a property of
the code rather than a tuning exercise. Two names that merely *look* alike produce
`POSSIBLE_MATCH` and stop there; a human or a stronger signal resolves it.

**The legal-form key is computed, never stored.** `comparison_key` strips a
trailing legal form — `sa`, `sàrl`, `ag`, `gmbh`, `genossenschaft` — using the list
the Country Pack supplies (§19: the pack holds configuration, this module holds the
workflow). The result exists for the duration of a comparison. Nothing writes it to
a column, because a stored key would freeze one country's rules into a row that
another country's pack would read differently. What *is* stored is
`Company.normalized_name`, which removes no words (§3).

**Strong evidence and supporting evidence are different things.** A company's own
domain, an ATS organization identifier and a provider's external id are strong: a
server or an operator asserted them. A name — however normalized — is supporting.
One strong signal decides; supporting signals alone never do. That asymmetry is
§3's "domain identity should be stronger evidence than loose name similarity",
implemented rather than intended.
"""
from collections.abc import Iterable, Sequence
from enum import StrEnum

from backend.app.domain.base import CountryCode, DomainModel, NonEmptyStr
from backend.app.domain.company import (
    AtsPlatform,
    Company,
    normalize_company_name,
    normalize_domain,
)
from country_packs.contracts import CountryPack

# Hosts that belong to an applicant tracking system rather than to an employer.
# The distinction is load-bearing: two unrelated companies both hosted on
# `boards.greenhouse.io` share a domain and are not the same company, so treating
# domain equality as strong evidence without this set would merge the entire
# Greenhouse customer base into one row.
#
# `myworkdayjobs.com` and `smartrecruiters.com` are listed although Phase 6 detects
# neither: the harm of an unlisted shared host is a wrong merge, and the cost of an
# unused entry is one line.
SHARED_ATS_HOSTS: frozenset[str] = frozenset({
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "api.greenhouse.io",
    "jobs.lever.co",
    "api.lever.co",
    "jobs.ashbyhq.com",
    "api.ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "workable.com",
    "personio.de",
    "recruitee.com",
    "bamboohr.com",
    "teamtailor.com",
})


def is_employer_domain(host: str) -> bool:
    """Whether a host identifies an employer rather than a platform it rents.

    A subdomain of a shared host counts as shared — `acme.workable.com` is
    Workable's, not Acme's — which is why this is a suffix test and not a
    membership test.
    """
    if not host:
        return False
    return not any(host == shared or host.endswith(f".{shared}")
                   for shared in SHARED_ATS_HOSTS)


def comparison_key(name: str, *, legal_suffixes: Sequence[str] = ()) -> str:
    """`name` with its trailing legal form removed, for comparison only (§3).

    Strips at most one suffix, from the end, matched whole-word against the
    already-normalized name, longest first so `societe cooperative` cannot be
    shadowed by `cooperative`. Only the *end* of the name is considered: `SA
    Journalière` is not a legal form and stripping a leading `sa` would corrupt it.

    Applied repeatedly? No — deliberately once. `Acme SA Sàrl` is not a real legal
    name, and a loop would let `Acme AG` and `Acme` and `Acme AG AG` all collapse
    together while making the rule much harder to reason about.

    Never removes a meaningful word. `Acme Switzerland` keeps `switzerland`,
    because the CH pack does not list it as a legal form — §3 forbids exactly that
    transformation, and the pack is where the prohibition is expressed as data.

    Returns the normalized name unchanged when nothing matches, and refuses to
    return an empty key: `SA` on its own is a company called SA, not a bare legal
    form, so the suffix is kept.
    """
    normalized = normalize_company_name(name)
    if not normalized:
        return ""
    for suffix in sorted(legal_suffixes, key=lambda term: (-len(term), term)):
        if normalized == suffix:
            continue
        if normalized.endswith(f" {suffix}"):
            return normalized[: -len(suffix)].strip()
    return normalized


def legal_suffixes_for(pack: CountryPack | None) -> tuple[str, ...]:
    """The pack's legal forms, or nothing at all when no pack applies.

    `()` rather than a built-in default list: a company whose country we do not
    have a pack for gets its full name compared, which is conservative — it can
    only ever produce fewer matches, never a wrong one.
    """
    return () if pack is None else tuple(pack.terminology.company_legal_suffixes)


def prefers_domain(pack: CountryPack | None, host: str) -> bool:
    """Whether a host sits on one of the country's own public suffixes.

    A tie-break and nothing more (`PackMetadata.company_domain_suffixes` says so in
    its own comment): a Swiss employer on a `.com` is ordinary, so this never
    filters and never decides a verdict. It is reported as a signal so a reviewer
    looking at a `POSSIBLE_MATCH` can see which candidate looked more local.
    """
    if pack is None or not host:
        return False
    return any(host.endswith(suffix)
               for suffix in pack.metadata.company_domain_suffixes)


class IdentitySignal(StrEnum):
    """One comparable fact two company records can agree on.

    Split into strong and supporting by `STRONG_SIGNALS` below. The enum is flat
    because a report groups by it, and a dashboard should not have to know the
    strength rule to render "matched on".
    """

    # --- strong: something outside this codebase asserted it ------------------
    EXTERNAL_ID = "EXTERNAL_ID"
    ATS_ORGANIZATION = "ATS_ORGANIZATION"
    EMPLOYER_DOMAIN = "EMPLOYER_DOMAIN"
    CONFIRMED_ALIAS = "CONFIRMED_ALIAS"

    # --- supporting: derived from a label somebody typed ----------------------
    NORMALIZED_NAME = "NORMALIZED_NAME"
    LEGAL_FORM_KEY = "LEGAL_FORM_KEY"
    KNOWN_ALIAS = "KNOWN_ALIAS"

    # --- tie-break: never evidence of identity, only of plausibility ----------
    COUNTRY_DOMAIN_PREFERENCE = "COUNTRY_DOMAIN_PREFERENCE"


# What is allowed to conclude `SAME_COMPANY` on its own. `CONFIRMED_ALIAS` is in
# here because §2 lists a manually confirmed alias among the evidence signals: an
# operator saying "these are the same" is the strongest signal the system has.
STRONG_SIGNALS: frozenset[IdentitySignal] = frozenset({
    IdentitySignal.EXTERNAL_ID,
    IdentitySignal.ATS_ORGANIZATION,
    IdentitySignal.EMPLOYER_DOMAIN,
    IdentitySignal.CONFIRMED_ALIAS,
})


class IdentityVerdict(StrEnum):
    """What a comparison concluded (§2).

    Four members, and the two in the middle are the ones that keep company identity
    intact. `POSSIBLE_MATCH` is what a name agreement produces — it is an invitation
    to look, not a licence to merge (§24). `INSUFFICIENT_EVIDENCE` is what two
    records with nothing comparable produce, and it is emphatically not `DISTINCT`:
    concluding "different companies" from "we know nothing" is how a duplicate gets
    created every sweep.
    """

    SAME_COMPANY = "SAME_COMPANY"
    POSSIBLE_MATCH = "POSSIBLE_MATCH"
    DISTINCT = "DISTINCT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class CompanyIdentity(DomainModel):
    """The comparable facts about one employer, pack-aware and pre-derived.

    Built by `identity_of` from a stored `Company`, or by a provider from what it
    just discovered, so the comparison below never has to care which side is which.
    Deriving the keys once also means the pack's suffix list is applied exactly
    once per record instead of once per comparison.

    `aliases` are normalized labels; `confirmed_aliases` is the subset an operator
    vouched for. Keeping them apart is what makes `CONFIRMED_ALIAS` a strong signal
    while a board's own spelling stays supporting.
    """

    name: NonEmptyStr
    normalized_name: str
    legal_form_key: str
    country: CountryCode | None = None
    employer_domain: str | None = None
    ats_platform: AtsPlatform | None = None
    ats_organization_id: str | None = None
    external_ids: frozenset[str] = frozenset()
    aliases: frozenset[str] = frozenset()
    confirmed_aliases: frozenset[str] = frozenset()

    @property
    def every_name_form(self) -> frozenset[str]:
        """Every normalized label this record answers to, aliases included."""
        return frozenset({self.normalized_name, self.legal_form_key}) | self.aliases


class IdentityAssessment(DomainModel):
    """A verdict, the signals behind it, and a sentence explaining it.

    The explanation is not decoration: §14 lets an ambiguous resolution leave an
    opportunity unlinked, and the operator who later looks at that opportunity
    needs to know whether nothing was found or two candidates tied.
    """

    verdict: IdentityVerdict
    signals: frozenset[IdentitySignal] = frozenset()
    detail: NonEmptyStr

    @property
    def is_same_company(self) -> bool:
        return self.verdict is IdentityVerdict.SAME_COMPANY

    @property
    def strong_signals(self) -> frozenset[IdentitySignal]:
        return self.signals & STRONG_SIGNALS


def identity_of(company: Company, *, pack: CountryPack | None = None,
                aliases: Iterable[str] = (),
                confirmed_aliases: Iterable[str] = (),
                external_ids: Iterable[str] = ()) -> CompanyIdentity:
    """The comparable form of a stored company.

    `aliases` come from the `company_aliases` table rather than from the company
    object, because aliases are not aggregate children (see
    `backend.app.domain.company`) and a caller that has not loaded them should pass
    nothing rather than have this function pretend there are none of interest.

    The domain is taken only when it is an employer's own: a company known solely
    by its Greenhouse board contributes no domain evidence, which is correct — its
    host belongs to Greenhouse.
    """
    host = company.normalized_domain
    detected = company.detected_ats
    return CompanyIdentity(
        name=company.name,
        normalized_name=company.normalized_name,
        legal_form_key=comparison_key(company.name,
                                      legal_suffixes=legal_suffixes_for(pack)),
        country=company.country,
        employer_domain=host if host and is_employer_domain(host) else None,
        ats_platform=detected.platform if detected else None,
        ats_organization_id=detected.organization_id if detected else None,
        external_ids=frozenset(external_ids),
        aliases=frozenset(normalize_company_name(alias) for alias in aliases
                          if normalize_company_name(alias)),
        confirmed_aliases=frozenset(normalize_company_name(alias)
                                    for alias in confirmed_aliases
                                    if normalize_company_name(alias)),
    )


def identity_of_claim(name: str, *, pack: CountryPack | None = None,
                      website: str | None = None,
                      careers_url: str | None = None,
                      country: str | None = None,
                      ats_platform: AtsPlatform | None = None,
                      ats_organization_id: str | None = None,
                      external_ids: Iterable[str] = ()) -> CompanyIdentity:
    """The comparable form of something a provider just claimed.

    The counterpart to `identity_of` for the side that has no `Company` yet: a
    seed, a candidate handed back by a provider, or a posting's `company_name`.
    Takes raw strings and applies exactly the same derivations, so a discovered
    claim and a stored row are compared on identical terms.
    """
    for candidate in (website, careers_url):
        host = normalize_domain(candidate) if candidate else ""
        if host and is_employer_domain(host):
            employer_domain: str | None = host
            break
    else:
        employer_domain = None
    return CompanyIdentity(
        name=name,
        normalized_name=normalize_company_name(name),
        legal_form_key=comparison_key(name,
                                      legal_suffixes=legal_suffixes_for(pack)),
        country=CountryCode(country) if country else None,
        employer_domain=employer_domain,
        ats_platform=ats_platform,
        ats_organization_id=ats_organization_id,
        external_ids=frozenset(external_ids),
    )


def _matching_signals(left: CompanyIdentity,
                      right: CompanyIdentity) -> set[IdentitySignal]:
    """Every fact the two records agree on. Order-independent by construction."""
    signals: set[IdentitySignal] = set()
    if left.external_ids & right.external_ids:
        signals.add(IdentitySignal.EXTERNAL_ID)
    if left.ats_platform is not None and left.ats_platform == right.ats_platform \
            and left.ats_organization_id is not None \
            and left.ats_organization_id == right.ats_organization_id:
        signals.add(IdentitySignal.ATS_ORGANIZATION)
    if left.employer_domain is not None \
            and left.employer_domain == right.employer_domain:
        signals.add(IdentitySignal.EMPLOYER_DOMAIN)
    if left.confirmed_aliases & right.every_name_form \
            or right.confirmed_aliases & left.every_name_form:
        signals.add(IdentitySignal.CONFIRMED_ALIAS)
    if left.normalized_name and left.normalized_name == right.normalized_name:
        signals.add(IdentitySignal.NORMALIZED_NAME)
    if left.legal_form_key and left.legal_form_key == right.legal_form_key:
        signals.add(IdentitySignal.LEGAL_FORM_KEY)
    if left.aliases & right.every_name_form or right.aliases & left.every_name_form:
        signals.add(IdentitySignal.KNOWN_ALIAS)
    return signals


def _domains_contradict(left: CompanyIdentity, right: CompanyIdentity) -> bool:
    """Two employers each with their own, different domain.

    Only meaningful when *both* sides have one. A record with no domain does not
    contradict anything — it is silent, and silence is the case
    `INSUFFICIENT_EVIDENCE` exists for.
    """
    return (left.employer_domain is not None and right.employer_domain is not None
            and left.employer_domain != right.employer_domain)


def compare(left: CompanyIdentity, right: CompanyIdentity) -> IdentityAssessment:
    """Whether these two records are one employer (§2).

    The rules, in the order they are applied:

    1. **A strong signal decides.** A shared employer domain, a shared ATS
       organization, a shared external id or a confirmed alias means
       `SAME_COMPANY` — even against differing names, which is the whole point:
       `Logitech` and `Logitech Europe S.A.` on `logitech.com` are one company.
    2. **Differing employer domains with no strong signal are `DISTINCT`.** Two
       companies that each published their own site and published different ones
       are not the same employer, whatever their names say. This is what keeps
       `Migros Genossenschaft Zürich` apart from a same-named unrelated firm.
    3. **Name agreement alone is `POSSIBLE_MATCH`.** Never a merge (§2, §24). The
       caller decides what to do with it; `resolution` turns it into `AMBIGUOUS`
       and leaves the opportunity unlinked (§14).
    4. **Nothing comparable is `INSUFFICIENT_EVIDENCE`**, which is not `DISTINCT`.

    `COUNTRY_DOMAIN_PREFERENCE` never appears here: it is not a fact two records
    share, and `resolution` applies it as a tie-break between candidates that
    already tied.
    """
    signals = _matching_signals(left, right)
    strong = signals & STRONG_SIGNALS
    if strong:
        return IdentityAssessment(
            verdict=IdentityVerdict.SAME_COMPANY,
            signals=frozenset(signals),
            detail="one employer: agreed on "
                   f"{', '.join(sorted(signal.value for signal in strong))}")
    if _domains_contradict(left, right):
        return IdentityAssessment(
            verdict=IdentityVerdict.DISTINCT,
            signals=frozenset(signals),
            detail=f"different employer domains ({left.employer_domain} and "
                   f"{right.employer_domain}) and no stronger signal agreeing")
    if signals:
        return IdentityAssessment(
            verdict=IdentityVerdict.POSSIBLE_MATCH,
            signals=frozenset(signals),
            detail="names agree ("
                   f"{', '.join(sorted(signal.value for signal in signals))}) but "
                   "nothing outside the name corroborates it; a name is not an "
                   "identity")
    return IdentityAssessment(
        verdict=IdentityVerdict.INSUFFICIENT_EVIDENCE,
        detail="no comparable evidence: neither a domain, an ATS organization, an "
               "external identifier nor a name form is shared")
