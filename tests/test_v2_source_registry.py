# tests/test_v2_source_registry.py
"""Which sources exist, which a country may use, in what order — and composition.

`sources_for` is four subtractive filters and one total ordering, and each is
tested on its own so a failure names the rule that broke. The `pack` filter and
the `required_capabilities` filter are the two worth reading: a pack that does not
bind a source silences it for that country only, and an *advisory* claim is not an
answer to "I need this filter applied" — treating it as one is how a search
silently returns the wrong city.

The second half covers `bootstrap`. §16 asks for composition mistakes to be loud
at startup, and there are three: a pack enabling a source nobody registered, a
pack enabling a source that does not serve the pack's own country, and a pack
expecting a capability the adapter does not claim. The middle one has its own
error code because its symptom is deceptive — the binding looks applied and the
source is filtered out by country, so the sweep is quietly narrower than the pack.
"""
import pytest

from backend.app.discovery.bootstrap import (
    build_country_packs,
    build_discovery,
    build_source_registry,
    verify_pack_expectations,
)
from backend.app.discovery.capabilities import SourceCapability
from backend.app.discovery.contracts import OpportunitySource, SourceHealthStatus
from backend.app.discovery.registry import (
    SourceRegistry,
    SourceRegistryError,
    SourceRegistryErrorCode,
)
from country_packs.contracts import SourceBinding
from country_packs.registry import CountryPackRegistry
from tests.v2_discovery import FakeSource, a_health, a_metadata, a_pack

Cap = SourceCapability


def a_registry(*sources: OpportunitySource) -> SourceRegistry:
    registry = SourceRegistry()
    registry.register_all(sources)
    return registry


def a_source(source_key: str = "fake_board", **overrides) -> FakeSource:
    return FakeSource(a_metadata(source_key, **overrides))


# --- identity ----------------------------------------------------------------

def test_a_source_is_found_under_the_key_its_metadata_declares():
    board = a_source("jobup")
    registry = a_registry(board)
    assert registry.get("jobup") is board
    assert registry.metadata_for("jobup").source_key == "jobup"
    assert "jobup" in registry
    assert len(registry) == 1


def test_two_adapters_cannot_answer_to_one_key():
    """One name, one implementation: otherwise one of them silently never runs."""
    registry = a_registry(a_source("jobup"))
    with pytest.raises(SourceRegistryError) as raised:
        registry.register(a_source("jobup"))
    assert raised.value.code is SourceRegistryErrorCode.DUPLICATE_SOURCE
    assert raised.value.source_key == "jobup"
    assert len(registry) == 1


def test_asking_for_an_unregistered_key_names_the_key():
    registry = a_registry(a_source("jobup"))
    with pytest.raises(SourceRegistryError) as raised:
        registry.get("jooble")
    assert raised.value.code is SourceRegistryErrorCode.UNKNOWN_SOURCE
    assert "jooble" in str(raised.value)
    assert "jooble" not in registry


def test_a_non_string_key_is_simply_absent_rather_than_an_error():
    """`__contains__` answers a question; it does not validate its argument."""
    registry = a_registry(a_source("jobup"))
    assert 42 not in registry
    assert None not in registry


def test_the_registry_lists_its_keys_in_a_stable_order():
    registry = a_registry(a_source("wtj"), a_source("coop"), a_source("jobup"))
    assert registry.source_keys == ("coop", "jobup", "wtj")


# --- selection ---------------------------------------------------------------

def test_a_source_that_does_not_serve_the_country_is_not_selected():
    """`indeed` (FR) against a CH sweep, in miniature."""
    swiss = a_source("indeed_ch", countries=("CH",))
    french = a_source("indeed", countries=("FR",))
    registry = a_registry(swiss, french)
    assert registry.sources_for(country="CH") == (swiss,)
    assert registry.sources_for(country="FR") == (french,)


def test_a_source_declaring_no_country_is_selected_everywhere():
    anywhere = a_source("greenhouse")
    assert a_registry(anywhere).sources_for(country="CM") == (anywhere,)


def test_a_source_the_pack_does_not_bind_is_not_selected():
    """Silence is not consent: registering an adapter switches it on nowhere."""
    bound = a_source("jobup")
    unbound = a_source("jooble")
    registry = a_registry(bound, unbound)
    pack = a_pack("CH", SourceBinding(source_key="jobup"))
    assert registry.sources_for(country="CH", pack=pack) == (bound,)


def test_a_source_the_pack_disables_is_not_selected():
    """The hook that silences one board for one country without touching others."""
    registry = a_registry(a_source("jobup"), a_source("jooble"))
    pack = a_pack("CH", SourceBinding(source_key="jobup"),
                  SourceBinding(source_key="jooble", enabled=False))
    assert tuple(s.metadata.source_key
                 for s in registry.sources_for(country="CH", pack=pack)) == ("jobup",)


def test_a_source_that_disabled_itself_is_not_selected_even_when_bound():
    """Both switches must say yes: the implementation's and the country's."""
    registry = a_registry(a_source("jobup", enabled=False))
    pack = a_pack("CH", SourceBinding(source_key="jobup"))
    assert registry.sources_for(country="CH", pack=pack) == ()


def test_a_disabled_source_can_still_be_listed_on_purpose():
    """For a status page: "registered but switched off" is not the same as absent."""
    off = a_source("jobup", enabled=False)
    registry = a_registry(off)
    assert registry.sources_for(include_disabled=True) == (off,)


def test_the_callers_allow_list_narrows_the_selection():
    registry = a_registry(a_source("jobup"), a_source("jooble"))
    selected = registry.sources_for(source_keys=("jooble",))
    assert tuple(s.metadata.source_key for s in selected) == ("jooble",)


def test_an_empty_allow_list_restricts_nothing():
    """The repo-wide convention, asserted where breaking it would widen a search."""
    registry = a_registry(a_source("jobup"), a_source("jooble"))
    assert len(registry.sources_for(source_keys=())) == 2


def test_a_required_capability_must_be_claimed():
    plain = a_source("migros", capabilities=frozenset())
    searching = a_source("jobup")
    registry = a_registry(plain, searching)
    selected = registry.sources_for(
        required_capabilities=frozenset({Cap.KEYWORD_SEARCH}))
    assert selected == (searching,)


def test_a_required_capability_must_be_claimed_reliably():
    """The rule that keeps a "filtered by Lausanne" search honest.

    jobup accepts a location and only reranks by it. A caller that *requires*
    `LOCATION_SEARCH` is asking for a filter, and an advisory claim does not
    answer that question — the source is still selectable for a search that does
    not require it, which is what the second assertion pins.
    """
    ranker = a_source("jobup",
                      capabilities=frozenset({Cap.LOCATION_SEARCH}),
                      advisory_capabilities=frozenset({Cap.LOCATION_SEARCH}))
    filterer = a_source("coop", capabilities=frozenset({Cap.LOCATION_SEARCH}))
    registry = a_registry(ranker, filterer)
    assert registry.sources_for(
        required_capabilities=frozenset({Cap.LOCATION_SEARCH})) == (filterer,)
    assert len(registry.sources_for()) == 2


def test_several_required_capabilities_all_have_to_hold():
    one = a_source("jobup", capabilities=frozenset({Cap.KEYWORD_SEARCH}))
    both = a_source("coop", capabilities=frozenset({Cap.KEYWORD_SEARCH,
                                                    Cap.HEALTHCHECK}))
    registry = a_registry(one, both)
    assert registry.sources_for(required_capabilities=frozenset(
        {Cap.KEYWORD_SEARCH, Cap.HEALTHCHECK})) == (both,)


def test_nothing_matching_is_an_empty_tuple_and_not_an_error():
    """§18: an empty source set is a reportable outcome, not an exception.

    The orchestrator turns it into `NO_SOURCE_SELECTED`; raising here would make
    "this country is not configured yet" indistinguishable from a bug.
    """
    assert a_registry(a_source("jobup", countries=("CH",))).sources_for(
        country="FR") == ()


# --- ordering ----------------------------------------------------------------

def test_sources_are_ordered_by_priority_and_never_by_registration():
    late = a_source("aaa_first_alphabetically", priority=90)
    early = a_source("zzz_last_alphabetically", priority=10)
    registry = a_registry(late, early)
    assert registry.sources_for() == (early, late)


def test_a_tie_breaks_on_the_key_so_a_sweep_is_reproducible():
    """Why it matters: a `limit` that truncates the tail must truncate the same one.

    Two sources swapping places between runs makes results look flaky for no
    reason, and the flakiness would only show up in production volumes.
    """
    registry = a_registry(a_source("wtj", priority=50), a_source("coop", priority=50),
                          a_source("jobup", priority=50))
    assert tuple(s.metadata.source_key for s in registry.sources_for()) == (
        "coop", "jobup", "wtj")


def test_the_pack_reorders_a_country_sweep_without_touching_the_adapter():
    """Switzerland puts the local boards first; Germany would not. §8."""
    registry = a_registry(a_source("jooble", priority=10),
                          a_source("jobup", priority=90))
    pack = a_pack("CH", SourceBinding(source_key="jobup", priority=1),
                  SourceBinding(source_key="jooble", priority=80))
    ordered = registry.sources_for(country="CH", pack=pack)
    assert tuple(s.metadata.source_key for s in ordered) == ("jobup", "jooble")
    # The adapters' own numbers are untouched: the pack decided, not the code.
    assert registry.metadata_for("jobup").priority == 90


def test_a_binding_without_a_priority_falls_back_to_the_adapters_own():
    registry = a_registry(a_source("jooble", priority=10),
                          a_source("jobup", priority=90))
    pack = a_pack("CH", SourceBinding(source_key="jobup"),
                  SourceBinding(source_key="jooble"))
    ordered = registry.sources_for(country="CH", pack=pack)
    assert tuple(s.metadata.source_key for s in ordered) == ("jooble", "jobup")


def test_iterating_the_registry_yields_the_same_order_as_a_sweep():
    registry = a_registry(a_source("wtj", priority=50), a_source("jobup", priority=10))
    assert tuple(s.metadata.source_key for s in registry) == ("jobup", "wtj")


# --- health, remembered ------------------------------------------------------

def test_the_registry_remembers_the_last_thing_a_source_said():
    """So "what is the state of discovery?" needs no second sweep (§12)."""
    registry = a_registry(a_source("jobup"))
    assert registry.health_for("jobup") is None
    registry.record_health(a_health("jobup"))
    assert registry.health_for("jobup").status is SourceHealthStatus.HEALTHY
    assert registry.unusable_source_keys() == ()


def test_the_last_word_wins_and_a_recovery_clears_the_outage():
    registry = a_registry(a_source("jobup"))
    registry.record_health(a_health("jobup", SourceHealthStatus.UNAVAILABLE))
    assert registry.unusable_source_keys() == ("jobup",)
    registry.record_health(a_health("jobup"))
    assert registry.unusable_source_keys() == ()


def test_health_records_are_reported_in_key_order():
    registry = a_registry(a_source("wtj"), a_source("coop"))
    registry.record_health(a_health("wtj"))
    registry.record_health(a_health("coop", SourceHealthStatus.MISCONFIGURED))
    assert tuple(record.source_key for record in registry.health()) == ("coop", "wtj")
    assert registry.unusable_source_keys() == ("coop",)


def test_a_degraded_source_is_still_usable():
    """It answered part of the request, and that part is real data."""
    registry = a_registry(a_source("jobup"))
    registry.record_health(a_health("jobup", SourceHealthStatus.DEGRADED))
    assert registry.unusable_source_keys() == ()


# --- §16: composition, checked at startup ------------------------------------
#
# `boards={}` throughout: the ATS adapters read `config/companies.yaml` by default
# and a test that depended on an operator's file would pass or fail for reasons
# that have nothing to do with the registry.

def _packs(*bindings: SourceBinding, country: str = "CH") -> CountryPackRegistry:
    return CountryPackRegistry((a_pack(country, *bindings),))


def test_the_swiss_composition_registers_thirteen_and_sweeps_twelve():
    """Acceptance criterion 1, end to end and offline.

    Pack load → adapter build → expectation check → priority-ordered selection.
    Nothing here makes a request: building an adapter binds a callable.
    """
    discovery = build_discovery(boards={})
    assert len(discovery.registry) == 13
    assert discovery.packs.countries == ("CH",)
    pack = discovery.packs.get("CH")
    selected = discovery.registry.sources_for(country="CH", pack=pack)
    assert tuple(s.metadata.source_key for s in selected) == (
        "jobup", "indeed_ch", "jobscout24", "migros", "coop", "manpower", "wtj",
        "linkedin", "jooble", "greenhouse", "lever", "ashby")


def test_the_french_indeed_is_registered_and_never_swept_for_switzerland():
    """Acceptance criterion 8's cheapest demonstration (§8)."""
    discovery = build_discovery(boards={})
    assert "indeed" in discovery.registry
    selected = discovery.registry.sources_for(country="CH",
                                              pack=discovery.packs.get("CH"))
    assert "indeed" not in {s.metadata.source_key for s in selected}


def test_every_wrapped_source_satisfies_the_source_contract():
    """§3, §7: thirteen V1 modules behind one Protocol, indistinguishable."""
    for source in build_discovery(boards={}).registry:
        assert isinstance(source, OpportunitySource)


def test_the_real_swiss_pack_expectations_hold_against_the_real_adapters():
    """The three build steps in the open, so a regression names the step.

    `build_discovery` runs the same check internally; asserting it separately is
    what turns "the CH pack expects nothing that was never implemented" into a
    statement a reader can find.
    """
    packs = build_country_packs()
    registry = build_source_registry(packs=packs, boards={})
    verify_pack_expectations(registry, packs)  # must not raise


def test_a_pack_enabling_a_source_nobody_registered_stops_the_process():
    """A key in `sources.yaml` with no adapter behind it is a typo, at best."""
    registry = a_registry(a_source("jobup"))
    with pytest.raises(SourceRegistryError) as raised:
        verify_pack_expectations(registry, _packs(SourceBinding(source_key="jooble")))
    assert raised.value.code is SourceRegistryErrorCode.UNKNOWN_SOURCE
    assert raised.value.source_key == "jooble"


def test_a_pack_enabling_a_foreign_source_has_its_own_error_code():
    """The deceptive one: the binding looks applied and the sweep is narrower.

    `sources_for` would filter `indeed` out by country and report nothing unusual,
    so the pack would claim twelve sources and run eleven. Hence a distinct code
    rather than folding it into the unknown-source case.
    """
    registry = a_registry(a_source("indeed", countries=("FR",)))
    with pytest.raises(SourceRegistryError) as raised:
        verify_pack_expectations(registry, _packs(SourceBinding(source_key="indeed")))
    assert raised.value.code is SourceRegistryErrorCode.COUNTRY_NOT_SERVED
    assert "FR" in str(raised.value)


def test_a_pack_expecting_a_capability_the_adapter_lacks_stops_the_process():
    """The case §16 is written for: a claim corrected downwards.

    Someone reads the V1 code, finds the healthcheck was never implemented, and
    removes the claim. A country relying on it has to hear about that at startup —
    the alternative is a status page that reports nothing and looks fine.
    """
    registry = a_registry(a_source("jobup",
                                   capabilities=frozenset({Cap.KEYWORD_SEARCH})))
    with pytest.raises(SourceRegistryError) as raised:
        verify_pack_expectations(registry, _packs(
            SourceBinding(source_key="jobup",
                          expects_capabilities=frozenset({Cap.HEALTHCHECK}))))
    assert raised.value.code is SourceRegistryErrorCode.CAPABILITY_NOT_CLAIMED
    assert "HEALTHCHECK" in str(raised.value)


def test_an_advisory_claim_still_answers_a_packs_expectation():
    """`supports`, not `supports_reliably` — and the CH pack depends on it.

    jobup takes a location and only reranks by it. The pack expecting
    `LOCATION_SEARCH` is asking the adapter to *send* the location, which it does;
    a pack that needed the result set actually narrowed would ask the registry for
    a reliable claim instead, which is the second assertion here.
    """
    ranker = a_source("jobup",
                      capabilities=frozenset({Cap.LOCATION_SEARCH}),
                      advisory_capabilities=frozenset({Cap.LOCATION_SEARCH}))
    registry = a_registry(ranker)
    binding = SourceBinding(source_key="jobup",
                            expects_capabilities=frozenset({Cap.LOCATION_SEARCH}))
    verify_pack_expectations(registry, _packs(binding))  # must not raise
    assert registry.sources_for(
        required_capabilities=frozenset({Cap.LOCATION_SEARCH})) == ()


def test_a_parked_binding_may_name_a_source_that_does_not_exist_yet():
    """Only enabled bindings are checked, so a pack can be written ahead of code.

    An operator noting "we want jooble next, here is its env var" must not have to
    wait for the adapter, and the entry documents the intent where the decision
    belongs. Nothing selects it: `enabled: false` already said so.
    """
    registry = a_registry(a_source("jobup"))
    packs = _packs(SourceBinding(source_key="jobup"),
                   SourceBinding(source_key="not_written_yet", enabled=False,
                                 expects_capabilities=frozenset({Cap.RADIUS_SEARCH})))
    verify_pack_expectations(registry, packs)  # must not raise
    assert registry.sources_for(country="CH", pack=packs.get("CH")) \
        == (registry.get("jobup"),)


def test_two_compositions_share_no_health_state():
    """§16's other half: no module-level singleton, asserted rather than trusted.

    Health accumulates in the registry as sweeps run. A cached composition would
    leak one test's outage into the next and one deployment's into a reload.
    """
    first = build_discovery(boards={})
    second = build_discovery(boards={})
    first.registry.record_health(a_health("jobup", SourceHealthStatus.UNAVAILABLE))
    assert first.registry.unusable_source_keys() == ("jobup",)
    assert second.registry.health_for("jobup") is None
    assert first.registry is not second.registry
    assert first.registry.get("jobup") is not second.registry.get("jobup")
