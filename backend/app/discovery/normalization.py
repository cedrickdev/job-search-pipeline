"""V1's eleven scraped columns → one typed `Opportunity`, through a Country Pack.

The V1 sources all end at `pipeline/sources/_common.py:normalize(**fields)`, which
returns the eleven `JOB_COLUMNS` as free text and interprets none of them. V1 then
stores that text and reasons about it nowhere. This module is where the
interpretation happens, and every country-specific word it needs comes from the
pack it is handed — never from a table in this file. That is the §2 rule made
executable: `alternance` is `WORK_STUDY` because `country_packs/ch` says so, and a
France pack could say something different without this module changing.

Three traps found during the V1 inspection, each fixed here rather than worked
around later:

**The salary column is not always a salary.** `migros`, `coop` and `jobscout24`
put the activity rate ("80%") in V1's `salary`. Reading that as a wage produces an
80-franc job; reading it as a workload is what `STRUCTURED_WORKLOAD` and the pack's
`activity_rate_labels` exist for.

**A description must never be classified.** "Apprentissage automatique" is French
for machine learning, so classifying a description would file data-science roles
under `APPRENTICESHIP`. Classification reads the title and the contract field only.

**Nothing is invented.** A scraped salary string stays a string: no adapter claims
`STRUCTURED_SALARY`, so no `SalaryRange` is built from prose here, and the original
text survives in `source.raw` for a later pass. CLAUDE.md forbids fabricating
candidate facts; fabricating employer facts is no better.
"""
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from backend.app.compat.v1_jobs import (
    V1_CONTRACT_TYPE_BY_TOKEN,
    V1_OPPORTUNITY_TYPE_BY_TOKEN,
    V1_WORKPLACE_MODE_BY_TOKEN,
    v1_dedup_fingerprint,
    v1_parse_date,
    v1_token,
)
from backend.app.discovery.capabilities import SourceCapability
from backend.app.discovery.contracts import SourceMetadata
from backend.app.domain.common import Location, WorkloadRange
from backend.app.domain.identifiers import discovered_opportunity_id
from backend.app.domain.opportunity import (
    ContractType,
    Opportunity,
    OpportunitySourceRecord,
    OpportunityType,
    WorkplaceMode,
)
from country_packs.contracts import CountryPack

# The columns `pipeline/sources/_common.py` produces. Repeated rather than
# imported: `backend` must not depend on `pipeline`
# (tests/test_v2_domain_purity.py), and a source that hands back a column not in
# this list is asking for a `raw` key nobody named.
V1_POSTING_COLUMNS: Final[tuple[str, ...]] = (
    "source", "company", "title", "url", "location", "remote_policy",
    "contract_type", "salary", "description", "language", "posted_date",
)

# Copied verbatim into a typed field, so `raw` would only duplicate them.
_VERBATIM_COLUMNS: Final[frozenset[str]] = frozenset({
    "source", "company", "title", "description",
})

# "80%", "60-80%", "60 – 80 %": the three shapes the Swiss boards print.
_PERCENT_RANGE: Final = re.compile(
    r"(\d{1,3})\s*(?:%\s*)?(?:[-–—/]|\bto\b|\bbis\b|\bà\b|\ba\b)\s*(\d{1,3})\s*%"
)
_PERCENT: Final = re.compile(r"(\d{1,3})\s*%")


class PostingRejected(ValueError):
    """One posting could not become an `Opportunity`; the sweep continues.

    Deliberately not a failure of the *source*: a board that returns 40 usable
    postings and one row with no company is healthy, and §12's isolation rule
    works at the source level. The adapter counts these into
    `DiscoveryMetrics.postings_skipped` and, at most, raises one
    `POSTING_SKIPPED` warning.
    """

    def __init__(self, reason: str, *, column: str) -> None:
        self.column = column
        super().__init__(f"{reason} (column {column!r})")


def _text(posting: Mapping[str, Any], column: str) -> str | None:
    """A column as trimmed text, or `None` when absent, null or blank."""
    value = posting.get(column)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required(posting: Mapping[str, Any], column: str) -> str:
    text = _text(posting, column)
    if text is None:
        raise PostingRejected("required value is missing", column=column)
    return text


def classify_opportunity_type(
    pack: CountryPack, *, title: str, contract: str | None
) -> OpportunityType | None:
    """The category, from the title first, the contract field next, English last.

    Two reasons for that order. The title says what the role *is* while the
    contract field says what shape it takes, so "Stage marketing" with
    `contract_type="temps partiel"` is an `INTERNSHIP` that happens to be
    part-time — one `classify` call over both strings would answer `PART_TIME`,
    because the pack resolves ties by term length and "temps partiel" is the longer
    match. And the pack has to outrank the vendor-token table: Welcome to the
    Jungle publishes `contract_type="fulltime"` for internships, which the English
    table alone would file as `FULL_TIME`.

    The description is not an argument to this function, and that is the point —
    see the module docstring.
    """
    for text in (title, contract):
        from_pack = pack.opportunity_types.classify(text)
        if from_pack is not None:
            return from_pack
    return V1_OPPORTUNITY_TYPE_BY_TOKEN.get(v1_token(contract)) if contract else None


def classify_contract_type(
    pack: CountryPack, *, title: str, contract: str | None
) -> ContractType | None:
    """The legal basis, from the contract field first and the title second.

    The mirror image of `classify_opportunity_type`'s order, and for the mirror
    reason: a contract field is *about* the legal basis, while a title mentioning
    "CDI" is doing so as an aside.
    """
    for text in (contract, title):
        from_pack = pack.terminology.contract_type_for(text)
        if from_pack is not None:
            return from_pack
    return V1_CONTRACT_TYPE_BY_TOKEN.get(v1_token(contract)) if contract else None


def classify_workplace_mode(
    pack: CountryPack, *, remote_policy: str | None, title: str, location: str | None
) -> WorkplaceMode | None:
    """Where the work happens, from the three fields that can honestly say.

    `location` is included because `pipeline/sources/indeed_ch.py` writes the word
    "remote" into the location and nothing else, and excluded from
    `classify_opportunity_type` because a place name is not a category. The
    description is excluded everywhere: "possibilité de télétravail après six mois"
    is not a remote job.
    """
    for text in (remote_policy, title, location):
        from_pack = pack.terminology.workplace_mode_for(text)
        if from_pack is not None:
            return from_pack
    if remote_policy:
        return V1_WORKPLACE_MODE_BY_TOKEN.get(v1_token(remote_policy))
    return None


def _percent_bounds(text: str) -> tuple[int, int] | None:
    """The percentage a string states, as an ordered pair, or `None`."""
    ranged = _PERCENT_RANGE.search(text)
    if ranged is not None:
        low, high = int(ranged.group(1)), int(ranged.group(2))
        return (min(low, high), max(low, high)) if low and high else None
    single = _PERCENT.search(text)
    if single is None:
        return None
    exact = int(single.group(1))
    # A posting saying "80%" means exactly 80%, not "at least 80%".
    return (exact, exact) if 1 <= exact <= 100 else None


def parse_workload(
    pack: CountryPack,
    *,
    metadata: SourceMetadata,
    title: str,
    salary: str | None,
    contract: str | None,
) -> WorkloadRange | None:
    """The activity rate, read only from fields entitled to state one.

    Which fields those are is the whole difficulty. A percentage in the *title* is
    the activity rate — Swiss titles say "Vendeur/euse 60-80%" and a title is a
    curated phrase, not prose. A percentage in the *salary* column is one only when
    the source is known to put it there (`STRUCTURED_WORKLOAD`, which `migros`,
    `coop` and `jobscout24` earn) or when the text names it with one of the pack's
    `activity_rate_labels`. Everything else — above all the description, where "20%
    de rabais collaborateur" is a staff discount — is left alone.

    `min_weekly_hours` is deliberately not derived from
    `PackMetadata.full_time_weekly_hours`: 42 h is an operator's convention, and
    turning "80%" into "33.6 h" here would publish that convention as a fact the
    employer never stated. A consumer that wants hours has the pack and can convert.
    """
    structured = metadata.supports(SourceCapability.STRUCTURED_WORKLOAD)
    candidates: tuple[tuple[str | None, bool], ...] = (
        (title, True),
        (salary, structured or pack.terminology.mentions_activity_rate(salary)),
        (contract, pack.terminology.mentions_activity_rate(contract)),
    )
    for text, trusted in candidates:
        if not text or not trusted:
            continue
        bounds = _percent_bounds(text)
        if bounds is not None:
            return WorkloadRange(min_percent=bounds[0], max_percent=bounds[1])
    return None


def _raw_snapshot(posting: Mapping[str, Any]) -> dict[str, str]:
    """Every value not copied into a typed field, kept as the source said it (§15).

    Keys are the source's own column names, unprefixed. `backend/app/compat/
    v1_jobs.py` prefixes its keys `v1_` for the opposite reason: there, the values
    came out of V1's database, and the prefix is what distinguishes a migrated
    snapshot from a live one.
    """
    snapshot: dict[str, str] = {}
    for column in V1_POSTING_COLUMNS:
        if column in _VERBATIM_COLUMNS:
            continue
        text = _text(posting, column)
        if text is not None:
            snapshot[column] = text
    return snapshot


def opportunity_from_posting(
    posting: Mapping[str, Any],
    *,
    metadata: SourceMetadata,
    pack: CountryPack,
    fetched_at: datetime,
    external_id: str | None = None,
) -> Opportunity:
    """One V1 posting dict → one `Opportunity`.

    `posting` is whatever `pipeline/sources/_common.py:normalize()` returned, so
    this function reads plain data and the V1 modules need no change (§7).

    `external_id` is optional because V1 throws the source's own id away —
    `greenhouse`, `lever` and `ashby` all publish one and `normalize()` has no
    column for it. An adapter that recovers it passes it here and gets a stabler
    identity than a URL; the URL is the fallback, and `dedup_fingerprint` is what
    matches the same posting across two boards.

    Raises `PostingRejected` when the row cannot answer who is hiring, for what,
    or where it was found.
    """
    company = _required(posting, "company")
    title = _required(posting, "title")
    url = _text(posting, "url")
    if url is not None and not url.startswith(("http://", "https://")):
        # `HttpUrlStr` exists so that nothing downstream is handed a `mailto:` or a
        # scraped fragment and opens it.
        url = None

    external_key = external_id or url
    if external_key is None:
        raise PostingRejected("posting has neither an external id nor a URL",
                              column="url")

    location_text = _text(posting, "location")
    contract = _text(posting, "contract_type")
    salary = _text(posting, "salary")
    language = _text(posting, "language")
    posted = _text(posting, "posted_date")

    return Opportunity(
        id=discovered_opportunity_id(metadata.source_key, external_key),
        source=OpportunitySourceRecord(
            source_key=metadata.source_key,
            external_id=external_id,
            source_url=url,
            fetched_at=fetched_at,
            raw=_raw_snapshot(posting),
        ),
        company_name=company,
        title=title,
        description=_text(posting, "description"),
        opportunity_type=classify_opportunity_type(pack, title=title,
                                                   contract=contract),
        contract_type=classify_contract_type(pack, title=title, contract=contract),
        workplace_mode=classify_workplace_mode(pack, remote_policy=_text(
            posting, "remote_policy"), title=title, location=location_text),
        workload=parse_workload(pack, metadata=metadata, title=title, salary=salary,
                                contract=contract),
        # No `salary`: the column holds scraped prose ("CHF 25.-/h", "80-100%",
        # Ashby's `compensationTierSummary`), no adapter claims STRUCTURED_SALARY,
        # and the string survives in `raw` for a parser that can do better.
        salary=None,
        location=_location(location_text, metadata),
        posting_language=pack.terminology.language_for(language),
        posted_at=v1_parse_date(posted) if posted else None,
        discovered_at=fetched_at,
        # Only when the source says its URL is where one applies; for a search
        # result it is a posting page, and Phase 11 owns the difference.
        application_url=(url if url is not None
                         and metadata.supports(SourceCapability.DIRECT_APPLY_URL)
                         else None),
        dedup_fingerprint=v1_dedup_fingerprint(company, title),
    )


def _location(raw: str | None, metadata: SourceMetadata) -> Location | None:
    """Free text plus, when it is not a guess, the country.

    The country comes from the *source*, not from the pack being swept: a CH sweep
    on LinkedIn can legitimately return a Lyon posting, so only a source that
    serves exactly one country (`jobup`, `indeed_ch`, `migros`, `coop`,
    `jobscout24`, `manpower`) can have its country filled in here. City, region and
    coordinates stay empty — Phase 7 owns geocoding.
    """
    country = metadata.sole_country
    if raw is None and country is None:
        return None
    return Location(raw=raw, country=country)
