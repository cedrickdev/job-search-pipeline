# tests/test_v2_source_capabilities.py
"""What a source may claim, and what the thirteen wrapped sources actually claim.

Two halves. The first is the model: `supports` / `supports_reliably` / `serves`
and the three coherence rules `SourceMetadata` enforces. The second reads the
real catalog, because §4 says "assign only capabilities the implementation really
supports" and that is a claim about `adapters/v1_catalog.py`, not about a type —
a table of expected holders is the only way an over-claim shows up as a failure
instead of as a search that silently returns the wrong city.

The empty expectations matter as much as the populated ones. `RADIUS_SEARCH` with
no holder is what makes §11's degradation policy reachable and tested; the day a
source grows a real radius parameter, this file is where that decision is
recorded.
"""
import pytest
from pydantic import ValidationError

from backend.app.discovery.adapters.v1_catalog import (
    ATS_SOURCES,
    QUERY_SOURCES,
    source_metadata,
)
from backend.app.discovery.capabilities import SourceCapability
from backend.app.discovery.contracts import SourceMetadata, SourceType
from tests.v2_discovery import a_metadata

Cap = SourceCapability

CATALOG = source_metadata()
BY_KEY = {metadata.source_key: metadata for metadata in CATALOG}


# --- the model ---------------------------------------------------------------

def test_a_source_without_a_declared_country_serves_all_of_them():
    """The repo-wide convention: an empty collection restricts nothing.

    `greenhouse` is why — a board is wherever the company is, and requiring it to
    enumerate countries would make every new pack edit an adapter.
    """
    anywhere = a_metadata("greenhouse_like")
    assert anywhere.countries == ()
    assert anywhere.serves("CH")
    assert anywhere.serves("CM")
    assert anywhere.sole_country is None


def test_a_source_declaring_one_country_answers_for_it_alone():
    swiss = a_metadata("jobup_like", countries=("CH",))
    assert swiss.serves("CH")
    assert not swiss.serves("FR")
    assert swiss.sole_country == "CH"


def test_a_source_serving_two_countries_can_name_neither_as_its_own():
    """`sole_country` feeds `Location.country`, so a guess here invents a fact."""
    both = a_metadata("border", countries=("CH", "FR"))
    assert both.serves("FR")
    assert both.sole_country is None


def test_an_advisory_claim_is_a_claim_but_never_a_reliable_one():
    """The distinction the whole capability model exists for.

    jobup accepts a location and folds it into its free-text term. Withholding
    `LOCATION_SEARCH` would stop the adapter sending the location at all and
    change V1's results; claiming it plainly would let a caller believe the result
    set was narrowed. Both, plus advisory, is the only honest shape.
    """
    jobup_like = a_metadata(
        "ranker",
        capabilities=frozenset({Cap.KEYWORD_SEARCH, Cap.LOCATION_SEARCH}),
        advisory_capabilities=frozenset({Cap.LOCATION_SEARCH}))
    assert jobup_like.supports(Cap.LOCATION_SEARCH)
    assert not jobup_like.supports_reliably(Cap.LOCATION_SEARCH)
    assert jobup_like.supports_reliably(Cap.KEYWORD_SEARCH)


def test_a_capability_that_is_not_claimed_at_all_is_supported_neither_way():
    plain = a_metadata("plain")
    assert not plain.supports(Cap.RADIUS_SEARCH)
    assert not plain.supports_reliably(Cap.RADIUS_SEARCH)


def test_an_advisory_capability_must_first_be_claimed():
    """Advisory-only-and-unclaimed would mean "ranks by something it cannot take"."""
    with pytest.raises(ValidationError, match="advisory"):
        a_metadata("incoherent",
                   advisory_capabilities=frozenset({Cap.LOCATION_SEARCH}))


def test_a_source_needing_credentials_must_name_the_variables():
    """Otherwise `MISCONFIGURED` cannot say what to set, which is its whole value."""
    with pytest.raises(ValidationError, match="environment variables"):
        a_metadata("keyed", requires_credentials=True)


def test_metadata_cannot_carry_a_credential_value():
    """§5, enforced by `EnvVarName` rather than by review.

    A real API key does not match `^[A-Z][A-Z0-9_]*$`, so "put the secret in
    metadata" is not a mistake someone can make and have reviewed later.
    """
    with pytest.raises(ValidationError):
        a_metadata("keyed", requires_credentials=True,
                   credential_env_vars=("b7f3c1e9-secret-value",))
    named = a_metadata("keyed", requires_credentials=True,
                       credential_env_vars=("JOOBLE_API_KEY",))
    assert named.credential_env_vars == ("JOOBLE_API_KEY",)


@pytest.mark.parametrize("key", ["JobUp", "jobup.ch", "jobup-ch", "2jobs", "", "a b"])
def test_a_source_key_is_a_stable_lower_snake_identity(key):
    """§5: identity is not derived from a class name and not free text."""
    with pytest.raises(ValidationError):
        a_metadata(key)


def test_a_source_cannot_list_a_country_twice():
    with pytest.raises(ValidationError, match="repeat"):
        a_metadata("doubled", countries=("CH", "CH"))


def test_source_type_is_descriptive_and_never_dispatched_on():
    """Pinned as a fact about the enum: adding a member changes no behaviour.

    The orchestrator branching on this value is what the abstraction forbids, and
    `test_v2_discovery_boundaries.py` is where that is asserted structurally.
    """
    assert set(SourceType) == {
        SourceType.ATS_BOARD, SourceType.JOB_BOARD, SourceType.AGGREGATOR,
        SourceType.SEARCH_INDEX, SourceType.COMPANY_CAREER_SITE,
        SourceType.STAFFING_AGENCY}


# --- §4 against the real catalog ---------------------------------------------
#
# One table, and it is the point of this file. Every entry was read off a line of
# V1 code (`adapters/v1_catalog.py` cites which); an over-claim here is a search
# that says it filtered when it did not, and the empty rows are what make §11's
# radius policy a tested outcome rather than a paragraph.

EXPECTED_HOLDERS: dict[SourceCapability, tuple[str, ...]] = {
    Cap.KEYWORD_SEARCH: ("coop", "indeed", "indeed_ch", "jobup", "jooble",
                         "linkedin", "wtj"),
    Cap.LOCATION_SEARCH: ("coop", "indeed", "indeed_ch", "jobup", "jooble",
                          "linkedin", "wtj"),
    # No V1 source takes a distance parameter (§11), reads a next-page token,
    # filters by remote or by opportunity type, publishes a machine-readable wage
    # or a structured location. Six deliberate blanks.
    Cap.RADIUS_SEARCH: (),
    Cap.PAGINATION: (),
    Cap.REMOTE_FILTER: (),
    Cap.OPPORTUNITY_TYPE_FILTER: (),
    Cap.STRUCTURED_SALARY: (),
    Cap.STRUCTURED_LOCATION: (),
    # `f_TPR` and `fromage` are real request parameters; the other ten ignore the
    # window, which is what `LOOKBACK_IGNORED` reports.
    Cap.INCREMENTAL_DISCOVERY: ("indeed", "indeed_ch", "linkedin"),
    # The three boards that print an activity rate into V1's `salary` column.
    Cap.STRUCTURED_WORKLOAD: ("coop", "jobscout24", "migros"),
    Cap.COMPANY_FILTER: ("ashby", "greenhouse", "lever"),
    Cap.ATS_METADATA: ("ashby", "greenhouse", "lever"),
    Cap.DIRECT_APPLY_URL: ("ashby", "coop", "greenhouse"),
    Cap.HEALTHCHECK: ("ashby", "coop", "greenhouse", "jobscout24", "jooble",
                      "lever", "manpower", "migros", "wtj"),
}


@pytest.mark.parametrize("capability", list(SourceCapability))
def test_every_capability_is_claimed_by_exactly_the_sources_that_have_it(capability):
    holders = tuple(sorted(m.source_key for m in CATALOG if m.supports(capability)))
    assert holders == EXPECTED_HOLDERS[capability]


def test_the_capability_table_covers_the_whole_vocabulary():
    """A new member has to be given an expected holder set, empty or not."""
    assert set(EXPECTED_HOLDERS) == set(SourceCapability)


def test_thirteen_sources_are_wrapped_under_thirteen_distinct_keys():
    """§7: twelve Swiss-relevant boards plus `indeed` (FR), each named once."""
    keys = [metadata.source_key for metadata in CATALOG]
    assert len(keys) == 13
    assert len(set(keys)) == 13
    assert len(QUERY_SOURCES) == 10
    assert len(ATS_SOURCES) == 3


@pytest.mark.parametrize(("source_key", "advisory"), [
    ("jobup", {Cap.LOCATION_SEARCH}),
    ("wtj", {Cap.LOCATION_SEARCH}),
    ("coop", {Cap.KEYWORD_SEARCH}),
])
def test_a_parameter_that_only_reranks_is_marked_advisory(source_key, advisory):
    """jobup and wtj concatenate the location; coop's own text relevance is loose."""
    assert BY_KEY[source_key].advisory_capabilities == frozenset(advisory)


def test_only_three_sources_rerank_rather_than_filter():
    """Named exhaustively, so a fourth is a decision and not a slip."""
    ranking_only = {m.source_key for m in CATALOG if m.advisory_capabilities}
    assert ranking_only == {"jobup", "wtj", "coop"}


@pytest.mark.parametrize("source_key", ["jobscout24", "migros", "manpower"])
def test_a_hardcoded_listing_claims_neither_keyword_nor_location(source_key):
    """The V1 finding this model exists for: three sources read neither argument.

    V1 passes both anyway, once per query × location, and fetches the same page
    nine times. Here the claim is absent, so the plan is one call and the caller
    is told with `KEYWORD_IGNORED` / `LOCATION_IGNORED`.
    """
    metadata = BY_KEY[source_key]
    assert not metadata.supports(Cap.KEYWORD_SEARCH)
    assert not metadata.supports(Cap.LOCATION_SEARCH)


def test_coop_is_the_only_source_whose_location_is_a_real_filter():
    """Applied client-side on the canton attribute — the one reliable one."""
    reliable = {m.source_key for m in CATALOG
                if m.supports_reliably(Cap.LOCATION_SEARCH)}
    assert "coop" in reliable
    assert "jobup" not in reliable and "wtj" not in reliable


@pytest.mark.parametrize("source_key", ["jobup", "linkedin", "indeed", "indeed_ch"])
def test_an_endpoint_behind_anti_bot_protection_is_not_probed(source_key):
    """Withholding HEALTHCHECK is a deliberate claim, not an omission.

    A probe of these four would spend a request likely to be refused for reasons
    unrelated to whether the source works, and the next sweep answers the same
    question for free. `V1SourceAdapter.healthcheck` documents what a caller sees.
    """
    assert not BY_KEY[source_key].supports(Cap.HEALTHCHECK)


def test_jooble_is_the_only_source_that_can_be_misconfigured():
    """One documented key-based API in the set, and it names its variable (§1)."""
    keyed = {m.source_key for m in CATALOG if m.requires_credentials}
    assert keyed == {"jooble"}
    assert BY_KEY["jooble"].credential_env_vars == ("JOOBLE_API_KEY",)


def test_only_indeed_serves_a_country_switzerland_does_not_sweep():
    """Acceptance criterion 8, for free: a foreign source is data, not an edit.

    V1 excluded fr.indeed.com with a nine-line comment inside its orchestrator.
    Here it is registered, declares FR, and no CH sweep can select it.
    """
    assert BY_KEY["indeed"].countries == ("FR",)
    assert not BY_KEY["indeed"].serves("CH")
    assert BY_KEY["indeed_ch"].countries == ("CH",)


def test_no_source_sets_its_own_priority():
    """Sweep order is the country's judgement, so every adapter keeps the default.

    `country_packs/ch/sources.yaml` is where jobup runs first; an adapter that
    ranked itself would make that decision unreviewable from the pack.
    """
    assert {metadata.priority for metadata in CATALOG} == {100}


def test_every_wrapped_source_is_fit_to_run():
    """`enabled` here is the implementation's own switch, and none is broken today.

    A country choosing not to use one is `SourceBinding.enabled`, which is what
    `test_v2_source_registry.py` exercises.
    """
    assert all(metadata.enabled for metadata in CATALOG)


@pytest.mark.parametrize("metadata", CATALOG, ids=lambda m: m.source_key)
def test_every_catalog_entry_documents_itself(metadata: SourceMetadata):
    """`notes` is where the capability claim cites the V1 line behind it.

    Not decoration: the next person to read `advisory_capabilities` needs to know
    *why*, and the answer lives beside the claim or nowhere.
    """
    assert metadata.notes is not None
    assert metadata.display_name
    assert metadata.advisory_capabilities <= metadata.capabilities
