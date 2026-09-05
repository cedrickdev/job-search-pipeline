"""The typed shape of a Country Pack: data, and nothing that executes.

Every model here is a frozen `DomainModel`, so a loaded pack is a value that can
be shared between the registry, the orchestrator and a normalizer without anyone
copying it defensively. The only methods are lookups — `classify`,
`workplace_mode_for`, `binding_for` — and they are pure. No HTTP client, no LLM
call, no browser: those belong to adapters and providers (§1).

What a pack must be able to answer is fixed by §1 of the phase order: ISO code,
display name, locales, currency, languages, terminology, enabled sources,
supported opportunity categories, eligibility metadata and source configuration
*references*. Each is a field below, and `credential_env_vars`-shaped fields are
constrained to variable NAMES so a pack file physically cannot hold a secret.

On imports: this module reads the universal vocabulary it maps onto — the domain
enums and `SourceCapability`/`SourceKey` from the discovery *contracts* — and
never the discovery machinery (`registry`, `orchestrator`, `normalization`,
`adapters`, `bootstrap`). A pack is read by discovery; it never drives it.
"""
import re
import unicodedata
from collections.abc import Mapping
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.discovery.capabilities import SourceCapability
from backend.app.discovery.contracts import EnvVarName, SourceKey
from backend.app.domain.base import (
    CountryCode,
    CurrencyCode,
    DomainModel,
    HttpUrlStr,
    LanguageCode,
    NonEmptyStr,
)
from backend.app.domain.candidate import WorkAuthorizationStatus
from backend.app.domain.eligibility import EligibilityRequirement
from backend.app.domain.opportunity import (
    ContractType,
    OpportunityType,
    WorkplaceMode,
)

# A BCP-47-shaped locale, kept narrow: language, optional region. Enough for
# "fr-CH" and "de-CH", and it refuses the free text that would otherwise end up
# in a `lang` attribute.
Locale = Annotated[str, Field(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")]


def normalize_term(value: str) -> str:
    """The comparable form of a local term.

    Lower case, accents folded, typographic punctuation flattened, whitespace
    collapsed. Each step exists because of a real mismatch:

    - **accents** — a board prints "Apprentissage" and an operator types
      "apprentissage"; a German-Swiss board prints "Beschäftigungsgrad" where the
      YAML says "beschaeftigungsgrad".
    - **apostrophes and dashes** — "taux d'activité" and "taux d’activité" differ
      by one invisible code point (U+0027 vs U+2019), and a listing page will use
      whichever its CMS emitted. Folding both to ASCII is what stops a mapping from
      being silently unmatchable.

    Not a general slug function: the remaining punctuation is preserved, so "cdd"
    and "c.d.d." stay different terms. A mapping key is a word, not an identifier.
    """
    decomposed = unicodedata.normalize("NFKD", value.translate(_PUNCTUATION_FOLD))
    folded = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(folded.lower().split())


# Characters that must never reach a comparison, folded onto what a YAML author
# types: curly quotes, the standalone acute accent, the dash family and the
# non-breaking space. Applied *before* NFKD, so every entry does real work — after
# decomposition some of them would already have collapsed into something else and
# the table would quietly become half dead code. Built once: `str.translate` with
# a prepared table is the cheap path, and this runs per posting per field.
_PUNCTUATION_FOLD = str.maketrans({
    "‘": "'", "’": "'", "ʼ": "'", "´": "'",
    "“": '"', "”": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    " ": " ",
})



def _term_keys(mapping: Mapping[str, object], label: str) -> None:
    """Reject a mapping whose keys are not already normalized.

    Loud at load time rather than silently unmatchable at run time: a key with a
    stray capital or a combining accent would simply never fire, and the symptom
    would be "that board classifies nothing", three layers away from the cause.
    """
    offenders = sorted(k for k in mapping if k != normalize_term(k))
    if offenders:
        raise ValueError(
            f"{label} keys must be normalized (lower case, accents folded, "
            f"single spaces); rewrite {offenders} as "
            f"{[normalize_term(k) for k in offenders]}")


def _contains_term(text: str, term: str) -> bool:
    """Whether `term` appears in already-normalized `text` as a whole word.

    Boundaries are not decoration. `"stage"` occurs inside `"stagiaire"`, and a
    substring match would classify a permanent posting for a *former* intern as an
    internship. `(?<!\\w)` / `(?!\\w)` rather than `\\b` so a term that begins or
    ends with punctuation ("c.d.d.") still matches.
    """
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None


def _ordered_terms(terms: Mapping[str, object]) -> list[str]:
    """Longest first, then alphabetical — a total order, so matching is stable.

    §9 asks for a *deterministic* mapping. Longest-first is the rule that makes it
    correct as well as stable: "emploi etudiant" must win over "emploi", or a
    student job would be classified `FULL_TIME` depending on dict order.
    """
    return sorted(terms, key=lambda term: (-len(term), term))


class PackMetadata(DomainModel):
    """Who the country is, in the terms the platform needs (§1).

    `full_time_weekly_hours` is here because `WorkloadRange` refuses to guess it:
    a Swiss "80%" is roughly 33.6 hours and a French one would not be, so the
    conversion cannot live in the universal domain. This is the field its
    docstring points at.

    `default_locale` must be one of `locales`, and every locale's language must be
    one of `languages` — otherwise a pack can claim to serve "it-CH" while
    declaring only French and German, and a UI would render a locale the content
    does not exist in.
    """

    country: CountryCode
    display_name: NonEmptyStr
    default_locale: Locale
    locales: Annotated[tuple[Locale, ...], Field(min_length=1)]
    currency: CurrencyCode
    languages: Annotated[tuple[LanguageCode, ...], Field(min_length=1)]
    timezone: NonEmptyStr
    full_time_weekly_hours: Annotated[float, Field(gt=0.0, le=80.0)]
    documentation_url: HttpUrlStr | None = None
    notes: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _locales_and_languages_agree(self) -> Self:
        if len(set(self.locales)) != len(self.locales):
            raise ValueError("locales must not repeat")
        if len(set(self.languages)) != len(self.languages):
            raise ValueError("languages must not repeat")
        if self.default_locale not in self.locales:
            raise ValueError(
                f"default_locale {self.default_locale!r} must be one of "
                f"{list(self.locales)}")
        orphans = sorted({loc.split("-")[0] for loc in self.locales}
                         - set(self.languages))
        if orphans:
            raise ValueError(
                f"locales declare languages the pack does not support: {orphans}")
        return self

    def weekly_hours_for_percent(self, percent: float) -> float:
        """A percentage activity rate in this country's weekly hours.

        The one conversion the universal domain is not allowed to perform.
        """
        return self.full_time_weekly_hours * percent / 100.0


class SourceBinding(DomainModel):
    """One country's decision to use one source (§1, §8).

    Two switches decide whether a source runs, and both must say yes: the
    source's own `SourceMetadata.enabled` ("this implementation is fit to run")
    and this `enabled` ("this country uses it"). Keeping them apart is what lets
    an operator disable a broken adapter everywhere without editing five packs, and
    lets a pack drop a board that is useless locally without touching the adapter.

    `priority` overrides the source's default ordering *for this country only*.
    `jobup.ch` deserves to run before an aggregator in Switzerland; it would not in
    Germany, where it does not operate.

    `expects_capabilities` is a contract check, not a claim: the pack states what
    it is relying on, and `bootstrap` verifies the registered adapter really claims
    it. Without this, deleting `KEYWORD_SEARCH` from an adapter during a refactor
    would silently turn a keyword sweep into a listing dump. It is also where §16's
    "invalid capability name" is caught, because a typo here fails at load.

    `config_env_vars` names the environment variables that configure this source
    for this country — names only. §1 forbids credentials in a pack file, and the
    `EnvVarName` pattern makes the prohibition structural rather than a review
    convention: `JOOBLE_API_KEY` matches, an actual key does not.
    """

    source_key: SourceKey
    enabled: bool = True
    priority: Annotated[int, Field(ge=0)] | None = None
    expects_capabilities: frozenset[SourceCapability] = frozenset()
    config_env_vars: tuple[EnvVarName, ...] = ()
    notes: NonEmptyStr | None = None


class OpportunityTypeMap(DomainModel):
    """Local vocabulary → the universal `OpportunityType` (§2, §9).

    This is where "apprentissage", "stage", "emploi étudiant" and "temporaire"
    stop being Swiss words. §2 is explicit that `ALTERNANCE` must never join the
    universal enum because one country says it; the mapping below is the
    alternative, and it is per-country by construction.

    `supported` is the country's answer to §1's "supported opportunity
    categories": the types this pack expects to see. It is descriptive — nothing
    filters on it — and a term may map to a type outside it without error, because
    a board printing something unexpected is a fact, not a misconfiguration.

    `classify` is deterministic: longest term first, then alphabetical, first
    match wins. A caller gets the same answer for the same text on every machine
    and every run, which is what makes the mapping testable at all.
    """

    supported: tuple[OpportunityType, ...] = ()
    terms: Mapping[str, OpportunityType] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _terms_are_normalized(self) -> Self:
        _term_keys(self.terms, "opportunity_types.terms")
        if len(set(self.supported)) != len(self.supported):
            raise ValueError("supported must not repeat an opportunity type")
        return self

    def classify(self, *texts: str | None) -> OpportunityType | None:
        """The first universal type any of `texts` names, or `None`.

        `None` is a real answer — "no local term appeared" — and is what keeps an
        unclassified posting honest instead of defaulting it to `FULL_TIME`, which
        is how a student-jobs product ends up recommending career changes.
        """
        haystack = normalize_term(" ".join(t for t in texts if t))
        if not haystack:
            return None
        for term in _ordered_terms(self.terms):
            if _contains_term(haystack, term):
                return self.terms[term]
        return None


class TerminologyMap(DomainModel):
    """The rest of the local vocabulary a normalizer needs (§9).

    Three mappings and one label list, chosen from what the V1 sources actually
    print rather than from what a country might theoretically say:

    - `workplace_modes` — "télétravail", "home office", "hybride". V1 stores a
      free-text `remote_policy` column and nothing interprets it.
    - `contract_types` — "CDI", "CDD", "temporaire", "mandat". V1 stores the
      board's own words in `contract_type`.
    - `languages` — "français" → `fr`, for the posting language.
    - `activity_rate_labels` — the words that introduce a percentage.
      `pipeline/sources/{migros,coop,jobscout24}.py` put "80%" in the *salary*
      column, so a normalizer that trusted the column name would report an 80-franc
      salary. The labels are what let it recognise a workload instead.

    Lookups follow the same deterministic longest-first rule as
    `OpportunityTypeMap.classify`.
    """

    workplace_modes: Mapping[str, WorkplaceMode] = Field(default_factory=dict)
    contract_types: Mapping[str, ContractType] = Field(default_factory=dict)
    languages: Mapping[str, LanguageCode] = Field(default_factory=dict)
    activity_rate_labels: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def _terms_are_normalized(self) -> Self:
        _term_keys(self.workplace_modes, "terminology.workplace_modes")
        _term_keys(self.contract_types, "terminology.contract_types")
        _term_keys(self.languages, "terminology.languages")
        unnormalized = sorted(label for label in self.activity_rate_labels
                              if label != normalize_term(label))
        if unnormalized:
            raise ValueError(
                "terminology.activity_rate_labels must be normalized; rewrite "
                f"{unnormalized}")
        return self

    def workplace_mode_for(self, *texts: str | None) -> WorkplaceMode | None:
        return self._lookup(self.workplace_modes, texts)

    def contract_type_for(self, *texts: str | None) -> ContractType | None:
        return self._lookup(self.contract_types, texts)

    def language_for(self, *texts: str | None) -> str | None:
        return self._lookup(self.languages, texts)

    def mentions_activity_rate(self, *texts: str | None) -> bool:
        """Whether any text introduces a percentage as an activity rate."""
        haystack = normalize_term(" ".join(t for t in texts if t))
        return any(_contains_term(haystack, label)
                   for label in self.activity_rate_labels)

    @staticmethod
    def _lookup[T](mapping: Mapping[str, T],
                   texts: tuple[str | None, ...]) -> T | None:
        haystack = normalize_term(" ".join(t for t in texts if t))
        if not haystack:
            return None
        for term in _ordered_terms(mapping):
            if _contains_term(haystack, term):
                return mapping[term]
        return None


class PermitRule(DomainModel):
    """One residence/work permit, and what the platform may conclude from it.

    A *hook*, not an engine. §9 forbids implementing Phase 9's eligibility logic
    here, so this model carries no verdict: it maps a local permit code onto the
    universal `WorkAuthorizationStatus` the domain already has, and optionally
    carries the weekly-hours cap `WorkAuthorization.permit_hours_cap` says a
    Country Pack must supply.

    `reference_url` is required in spirit and enforced in the CH pack's review
    rather than by the type: these are legal parameters maintained by an operator,
    not facts this repository asserts. docs/COUNTRY_PACKS.md §Eligibility says so
    in the same words, because a wrong number here would become a wrong
    eligibility verdict in Phase 9 with nothing in between to catch it.
    """

    code: NonEmptyStr
    display_name: NonEmptyStr
    status: WorkAuthorizationStatus
    weekly_hours_cap: Annotated[float, Field(gt=0.0, le=168.0)] | None = None
    reference_url: HttpUrlStr | None = None
    notes: NonEmptyStr | None = None


class EligibilityMetadata(DomainModel):
    """The country-specific parameters Phase 9 will evaluate against (§9).

    `requirements_in_scope` is the pack telling a future engine which of the nine
    `EligibilityRequirement` members this country actually has data for. Declaring
    `PERMIT_HOURS_CAP` without a permit carrying a cap would promise a check
    nothing can perform, so the validator refuses it.
    """

    minimum_working_age: Annotated[int, Field(ge=0, le=30)] | None = None
    permits: tuple[PermitRule, ...] = ()
    requirements_in_scope: frozenset[EligibilityRequirement] = frozenset()
    reference_urls: tuple[HttpUrlStr, ...] = ()
    notes: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _hooks_are_backed_by_data(self) -> Self:
        codes = [permit.code for permit in self.permits]
        if len(set(codes)) != len(codes):
            raise ValueError(f"permit codes must not repeat: {sorted(codes)}")
        if EligibilityRequirement.PERMIT_HOURS_CAP in self.requirements_in_scope \
                and not any(p.weekly_hours_cap is not None for p in self.permits):
            raise ValueError(
                "PERMIT_HOURS_CAP is declared in scope but no permit carries a "
                "weekly_hours_cap, so nothing could evaluate it")
        if EligibilityRequirement.MINIMUM_AGE in self.requirements_in_scope \
                and self.minimum_working_age is None:
            raise ValueError(
                "MINIMUM_AGE is declared in scope but minimum_working_age is unset")
        return self

    def permit_for(self, code: str) -> PermitRule | None:
        normalized = code.strip().upper()
        return next((p for p in self.permits if p.code.upper() == normalized), None)


class CountryPack(DomainModel):
    """One country, fully described (§1).

    Assembled by `loader.load_pack` from five YAML files, which is why the five
    fields map one-to-one onto them: `metadata.yaml`, `sources.yaml`,
    `opportunity_types.yaml`, `terminology.yaml`, `eligibility.yaml`. Splitting
    them is not decoration — they change for different reasons and by different
    people: a source binding is an operations decision, a permit cap is a legal
    one, and terminology grows every time a board renames a field.

    Nothing here is user-scoped. A pack describes a country, not a candidate; what
    a particular person may do with a permit is `CandidateProfile` plus Phase 9.
    """

    metadata: PackMetadata
    sources: tuple[SourceBinding, ...] = ()
    opportunity_types: OpportunityTypeMap = OpportunityTypeMap()
    terminology: TerminologyMap = TerminologyMap()
    eligibility: EligibilityMetadata = EligibilityMetadata()

    @model_validator(mode="after")
    def _bindings_are_unique(self) -> Self:
        keys = [binding.source_key for binding in self.sources]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(
                "a country must bind each source at most once; duplicated: "
                f"{duplicates}")
        return self

    @property
    def country(self) -> str:
        return self.metadata.country

    @property
    def enabled_bindings(self) -> tuple[SourceBinding, ...]:
        """The bindings this country wants, in a stable order.

        Sorted by the pack's own priority override and then by key, so a sweep
        that logs its source order produces a reviewable diff rather than whatever
        order the YAML happened to be in. A binding with no override sorts as if it
        carried the registry's default, and the registry re-sorts anyway using the
        source's real priority — this order is what makes *reading a pack*
        deterministic.
        """
        return tuple(sorted(
            (b for b in self.sources if b.enabled),
            key=lambda b: (b.priority if b.priority is not None else 100,
                           b.source_key)))

    @property
    def enabled_source_keys(self) -> tuple[str, ...]:
        return tuple(b.source_key for b in self.enabled_bindings)

    def binding_for(self, source_key: str) -> SourceBinding | None:
        """The binding for a key, enabled or not. `None` means the pack is silent.

        Silence is not consent: `registry` treats an unbound source as one this
        country does not use, so adding an adapter does not quietly switch it on
        everywhere.
        """
        return next((b for b in self.sources if b.source_key == source_key), None)

    def allows_source(self, source_key: str) -> bool:
        binding = self.binding_for(source_key)
        return binding is not None and binding.enabled






