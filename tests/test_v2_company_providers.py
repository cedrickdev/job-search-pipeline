# tests/test_v2_company_providers.py
"""The provider contracts, the registry that chooses among them, and the pass.

Three layers, and one property runs through all of them: **no provider is named
anywhere the phase order says one may not be**. `registry.py` and `orchestrator.py`
are exercised here exclusively through `FakeCompanyProvider`, so if either ever grew
an `if provider_key == "configured_ats"` these tests would still pass and
`test_v2_company_boundaries.py` would fail — the same division of labour Phase 5
settled on between its orchestration and boundary suites.

The other property, §26: one provider failing costs its neighbours nothing. Several
tests below are about what does *not* happen — a raising provider does not end the
pass, an empty registry does not look like "no employers exist", and an exception
message carrying a credential does not reach a health record.

Nothing here opens a socket, and one test proves it rather than asserting it: it
patches `socket.socket.connect` to fail and runs a full bootstrap pass through it.
"""
import asyncio
import socket

import pytest
from pydantic import ValidationError

from backend.app.companies.bootstrap import (
    build_company_discovery,
    build_company_provider_registry,
)
from backend.app.companies.contracts import (
    MAX_COMPANIES_PER_PROVIDER,
    CompanyDiscoveryCapability,
    CompanyDiscoveryProvider,
    CompanyDiscoveryRequest,
    CompanyDiscoveryResult,
    CompanyDiscoveryWarning,
    CompanyDiscoveryWarningCode,
    CompanyProviderMetadata,
    CompanyProviderType,
    CompanySeed,
    DiscoveredCareerSite,
    DiscoveredCompany,
    ProviderFailureCode,
    ProviderHealthStatus,
)
from backend.app.companies.orchestrator import (
    CompanyDiscoveryOrchestrator,
    CompanyDiscoveryReport,
)
from backend.app.companies.providers.base import LocalCompanyProvider, SeedBatch
from backend.app.companies.providers.configured_ats import ConfiguredAtsCompanyProvider
from backend.app.companies.providers.manual_seed import (
    ManualCompanySeed,
    ManualSeedCompanyProvider,
)
from backend.app.companies.providers.stored_opportunities import (
    POSTINGS_PER_PASS,
    StoredOpportunityCompanyProvider,
)
from backend.app.companies.registry import (
    CompanyProviderRegistry,
    ProviderRegistryError,
    ProviderRegistryErrorCode,
)
from backend.app.domain.common import Location
from backend.app.domain.company import (
    AtsPlatform,
    CareerSiteKind,
    CompanySeedKind,
    DetectionStatus,
    Evidence,
    SpontaneousApplicationSupport,
)
from tests.v2_builders import NOW, a_source_record, an_opportunity
from tests.v2_companies import (
    Board,
    FakeCompanyProvider,
    RecordingLister,
    a_discovered_company,
    a_provider_health,
    a_provider_metadata,
    a_provider_result,
    a_seed,
    frozen_clock,
)

Cap = CompanyDiscoveryCapability
Warn = CompanyDiscoveryWarningCode
CLOCK = frozen_clock()
AN_EVIDENCE = Evidence(code="EVIDENCE_UNDER_TEST", detail="something was observed",
                       observed_at=NOW)


def a_registry(*providers) -> CompanyProviderRegistry:
    registry = CompanyProviderRegistry()
    registry.register_all(providers)
    return registry


def an_orchestrator(registry: CompanyProviderRegistry, *,
                    max_concurrency: int = 4) -> CompanyDiscoveryOrchestrator:
    return CompanyDiscoveryOrchestrator(registry=registry, clock=CLOCK,
                                        max_concurrency=max_concurrency)


class Gate:
    """Counts how many providers are in flight at once, releasing at a threshold.

    Lifted from `test_v2_discovery_orchestration.py` for the same reason it exists
    there: a concurrency bound is worth observing rather than trusting, and counting
    arrivals is deterministic where a `sleep` is not.
    """

    def __init__(self, expected: int) -> None:
        self.in_flight = 0
        self.peak = 0
        self._expected = expected
        self._released = asyncio.Event()

    async def __call__(self, provider: CompanyDiscoveryProvider) -> None:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        if self.in_flight >= self._expected:
            self._released.set()
        await self._released.wait()
        self.in_flight -= 1


class ScriptedProvider(LocalCompanyProvider):
    """A `LocalCompanyProvider` whose source is a `SeedBatch` handed to it.

    The base class is where "enforce the limit, warn, never raise" lives, and it has
    to be tested through *something*. This subclass is the smallest thing that
    exercises it without also exercising a YAML file, an operator's list or the
    postings table — the three real providers have their own sections below.
    """

    def __init__(self, batch: SeedBatch | None = None, *,
                 error: BaseException | None = None, **metadata_overrides) -> None:
        super().__init__(metadata=a_provider_metadata(**metadata_overrides),
                         clock=CLOCK)
        self._batch = batch if batch is not None else SeedBatch(())
        self._error = error

    def _seeds(self, request: CompanyDiscoveryRequest) -> SeedBatch:
        if self._error is not None:
            raise self._error
        return self._batch


# --- §6: the protocol, and who satisfies it -----------------------------------

def test_the_three_real_providers_and_the_fake_all_satisfy_the_protocol():
    """§6's contract, checked structurally — nothing subclasses the Protocol.

    `LocalCompanyProvider` is a plain base class, which is what lets a provider be
    written without importing the Protocol at all. The check that matters is that the
    registry's parameter type is honoured by the objects `bootstrap` puts in it.
    """
    providers = (
        FakeCompanyProvider(),
        ManualSeedCompanyProvider(),
        ConfiguredAtsCompanyProvider(boards={}),
        StoredOpportunityCompanyProvider(opportunities=RecordingLister()),
    )
    for provider in providers:
        assert isinstance(provider, CompanyDiscoveryProvider)


def test_the_protocol_carries_a_non_method_member_so_issubclass_is_refused():
    """The same shape as `OpportunitySource`, and the same consequence.

    `metadata` is a property rather than a method, so a runtime `issubclass` cannot
    be answered. Pinned because a future edit turning it into `def metadata()` would
    silently change what a caller may check.
    """
    with pytest.raises(TypeError):
        issubclass(FakeCompanyProvider, CompanyDiscoveryProvider)  # type: ignore[misc]


# --- §18: what a provider may claim about itself ------------------------------

def test_a_provider_that_declares_no_country_serves_every_country():
    """The convention the whole repo uses: an empty collection restricts nothing.

    Every Phase 6 provider is country-agnostic — `config/companies.yaml` says nothing
    about where an employer is — so this is the default path rather than an edge case.
    """
    metadata = a_provider_metadata()
    assert metadata.countries == ()
    assert metadata.serves("CH")
    assert metadata.serves("FR")


def test_a_provider_that_names_its_countries_serves_only_those():
    metadata = a_provider_metadata(countries=("CH",))
    assert metadata.serves("CH")
    assert not metadata.serves("FR")


def test_a_provider_cannot_name_the_same_country_twice():
    """A repeat means two different intentions were merged by hand.

    Cheap to catch at composition time; invisible afterwards, because `serves` would
    answer the same either way and the duplicate would only show up in a report.
    """
    with pytest.raises(ValidationError, match="must not repeat a country"):
        a_provider_metadata(countries=("CH", "CH"))


def test_a_provider_that_needs_credentials_must_name_the_variables():
    """§26's redaction depends on this list: no names, nothing to blank.

    No Phase 6 provider needs a credential, and the rule is enforced anyway — the day
    one does, the failure is a refused construction rather than an unredacted health
    detail.
    """
    with pytest.raises(ValidationError, match="must name the environment variables"):
        a_provider_metadata(requires_credentials=True)


def test_a_capability_a_provider_did_not_claim_is_not_supported():
    metadata = a_provider_metadata(capabilities=frozenset({Cap.COUNTRY_FILTER}))
    assert metadata.supports(Cap.COUNTRY_FILTER)
    assert not metadata.supports(Cap.ATS_DETECTION)


# --- §7 and §10: a seed and a claim carry their own coherence -----------------

def test_an_ats_seed_without_its_platform_and_token_is_refused():
    """Without both, nothing can fetch the board the seed exists to describe."""
    with pytest.raises(ValidationError, match="must name the platform"):
        a_seed(kind=CompanySeedKind.ATS_ORGANIZATION)
    with pytest.raises(ValidationError, match="must name the platform"):
        a_seed(kind=CompanySeedKind.ATS_ORGANIZATION,
               ats_platform=AtsPlatform.GREENHOUSE)


def test_an_organization_identifier_without_a_platform_is_refused_on_any_seed():
    """`acme` on Greenhouse and `acme` on Lever are two tenancies (§2).

    An identifier stored without its platform would compare equal across platforms,
    which is exactly the wrong merge `identity.compare` is written to avoid.
    """
    with pytest.raises(ValidationError, match="only unique per platform"):
        a_seed(ats_organization_id="acme")


def test_an_ats_board_career_site_must_name_its_platform():
    """The same rule `CareerSite` enforces, one step earlier (§11)."""
    with pytest.raises(ValidationError, match="must name its platform"):
        DiscoveredCareerSite(url="https://boards.greenhouse.io/acme",
                             kind=CareerSiteKind.ATS_BOARD)


def test_a_reported_ats_platform_must_carry_evidence():
    """§10: detection is not verification, so a detection says what it saw."""
    with pytest.raises(ValidationError, match="must carry evidence"):
        a_discovered_company(ats_platform=AtsPlatform.LEVER)


def test_an_ats_status_without_a_platform_says_nothing_and_is_refused():
    with pytest.raises(ValidationError, match="says nothing"):
        a_discovered_company(ats_status=DetectionStatus.CONFIRMED)


def test_a_platform_reported_with_its_evidence_and_its_status_is_accepted():
    """The positive path, so the three refusals above are not the whole shape."""
    company = a_discovered_company(ats_platform=AtsPlatform.LEVER,
                                   ats_organization_id="acme",
                                   ats_status=DetectionStatus.CONFIRMED,
                                   ats_evidence=(AN_EVIDENCE,))
    assert company.ats_status is DetectionStatus.CONFIRMED
    assert company.ats_evidence == (AN_EVIDENCE,)


def test_a_decided_spontaneous_application_verdict_must_carry_evidence():
    """§12, at the contract boundary: `SUPPORTED` is a claim about an employer.

    The alternative is a stored `SUPPORTED` nobody can review, which is the same
    thing as a guess — and §12's forbidden inference (no active jobs, therefore
    spontaneous applications allowed) would arrive through exactly this field.
    """
    for verdict in (SpontaneousApplicationSupport.SUPPORTED,
                    SpontaneousApplicationSupport.NOT_SUPPORTED):
        with pytest.raises(ValidationError, match="must carry evidence"):
            a_discovered_company(spontaneous_application=verdict)


def test_an_unknown_spontaneous_application_state_needs_no_evidence():
    """Which is why `UNKNOWN` is the default: nobody has looked, and saying so is
    free. Every Phase 6 provider but the manual one stops here."""
    company = a_discovered_company()
    assert company.spontaneous_application is SpontaneousApplicationSupport.UNKNOWN
    assert company.spontaneous_application_evidence == ()


def test_a_discovered_company_reports_the_name_of_the_seed_it_wraps():
    """One name, read through a property: two copies would be one more thing to
    keep in step, and the seed's name is the label a provider actually saw."""
    company = a_discovered_company("Logitech Europe S.A.")
    assert company.name == "Logitech Europe S.A." == company.seed.name


# --- §20 and §18: what a request may ask for ----------------------------------

@pytest.mark.parametrize("limit", [0, -1, MAX_COMPANIES_PER_PROVIDER + 1])
def test_a_request_limit_outside_the_bound_is_refused(limit):
    """`MAX_COMPANIES_PER_PROVIDER` is a ceiling on what a caller may ask for.

    Not a target: every Phase 6 provider reads a bounded local source. The bound
    exists so a future provider cannot hand an unbounded list to a service that would
    then write all of it (§1).
    """
    with pytest.raises(ValidationError):
        CompanyDiscoveryRequest(limit=limit)


def test_a_request_may_not_repeat_a_provider_in_its_allow_list():
    with pytest.raises(ValidationError, match="must not repeat a provider"):
        CompanyDiscoveryRequest(provider_keys=("aaa", "aaa"))


def test_an_empty_allow_list_allows_every_provider():
    """Same convention as `countries`, and the ordinary case: a pass with no
    allow-list runs everything registered."""
    assert CompanyDiscoveryRequest().allows_provider("configured_ats")
    assert CompanyDiscoveryRequest(provider_keys=("aaa",)).allows_provider("aaa")
    assert not CompanyDiscoveryRequest(provider_keys=("aaa",)).allows_provider("bbb")


# --- §26: a result speaks for its own provider --------------------------------

def test_a_result_whose_health_names_another_provider_is_refused():
    """The mistake this catches is a copied line in a provider, and its consequence
    is a status page attributing an outage to the wrong provider."""
    with pytest.raises(ValidationError, match="but this is"):
        CompanyDiscoveryResult(provider_key="aaa", health=a_provider_health("bbb"))


def test_a_result_may_not_carry_a_warning_attributed_to_another_provider():
    with pytest.raises(ValidationError, match="attributed to another provider"):
        CompanyDiscoveryResult(
            provider_key="aaa", health=a_provider_health("aaa"),
            warnings=(CompanyDiscoveryWarning(code=Warn.SEED_SKIPPED,
                                              detail="a line was unreadable",
                                              provider_key="bbb"),))


def test_a_result_carrying_an_unattributed_warning_is_accepted():
    """`provider_key=None` is a pass-level warning, which the orchestrator emits."""
    result = CompanyDiscoveryResult(
        provider_key="aaa", health=a_provider_health("aaa"),
        warnings=(CompanyDiscoveryWarning(code=Warn.NOTHING_CONFIGURED,
                                          detail="nothing is configured"),))
    assert result.warnings[0].provider_key is None


@pytest.mark.parametrize(("status", "usable"), [
    (ProviderHealthStatus.HEALTHY, True),
    (ProviderHealthStatus.DEGRADED, True),
    (ProviderHealthStatus.MISCONFIGURED, False),
    (ProviderHealthStatus.UNAVAILABLE, False),
])
def test_a_result_is_usable_exactly_when_its_health_is(status, usable):
    """A degraded provider still returned companies; a misconfigured one did not."""
    result = a_provider_result("aaa", health=a_provider_health("aaa", status=status))
    assert result.is_usable is usable


# --- §18: the registry ---------------------------------------------------------

def test_a_registered_provider_is_reachable_by_its_key():
    provider = FakeCompanyProvider(a_provider_metadata("aaa"))
    registry = a_registry(provider)
    assert registry.get("aaa") is provider
    assert registry.metadata_for("aaa").provider_key == "aaa"
    assert "aaa" in registry
    assert len(registry) == 1


def test_registering_two_providers_under_one_key_is_refused_loudly():
    """§5 stores `provider_key` on every discovery record.

    Two providers answering to one name would make that column point at whichever
    was registered second, so this is a composition-time bug to fix rather than a
    condition to degrade around.
    """
    registry = a_registry(FakeCompanyProvider(a_provider_metadata("aaa")))
    with pytest.raises(ProviderRegistryError, match="already registered") as caught:
        registry.register(FakeCompanyProvider(a_provider_metadata("aaa")))
    assert caught.value.code is ProviderRegistryErrorCode.DUPLICATE_PROVIDER
    assert caught.value.provider_key == "aaa"


def test_asking_for_a_provider_nobody_registered_names_the_key():
    registry = a_registry()
    with pytest.raises(ProviderRegistryError, match="no provider is registered") as e:
        registry.get("nope")
    assert e.value.code is ProviderRegistryErrorCode.UNKNOWN_PROVIDER
    assert e.value.provider_key == "nope"
    assert "nope" not in registry


def test_a_non_string_key_is_simply_not_contained():
    """`__contains__` answers rather than raising, because `in` is a question."""
    assert 42 not in a_registry(FakeCompanyProvider())


def test_providers_run_in_priority_order_and_break_ties_on_their_key():
    """§23's reproducibility, at the level that decides which tail a limit cuts.

    Registration order deliberately contradicts both, so a sort that fell back to
    insertion order would fail here.
    """
    registry = a_registry(
        FakeCompanyProvider(a_provider_metadata("zzz", priority=10)),
        FakeCompanyProvider(a_provider_metadata("bbb", priority=99)),
        FakeCompanyProvider(a_provider_metadata("aaa", priority=10)))
    assert [p.metadata.provider_key for p in registry] == ["aaa", "zzz", "bbb"]
    assert [p.metadata.provider_key for p in registry.providers_for()] \
        == ["aaa", "zzz", "bbb"]
    assert registry.provider_keys == ("aaa", "bbb", "zzz")


def test_a_disabled_provider_is_left_out_unless_it_is_asked_for_by_name():
    """`enabled` is the provider's own switch: "this implementation is fit to run".

    `include_disabled` exists for a status page, which has to be able to say a
    provider exists and is switched off — the alternative is an operator wondering
    why a provider they configured never appears anywhere.
    """
    registry = a_registry(
        FakeCompanyProvider(a_provider_metadata("aaa")),
        FakeCompanyProvider(a_provider_metadata("bbb", enabled=False)))
    assert [p.metadata.provider_key for p in registry.providers_for()] == ["aaa"]
    assert [p.metadata.provider_key
            for p in registry.providers_for(include_disabled=True)] == ["aaa", "bbb"]


def test_a_country_selects_the_providers_that_serve_it():
    registry = a_registry(
        FakeCompanyProvider(a_provider_metadata("aaa", countries=("CH",))),
        FakeCompanyProvider(a_provider_metadata("bbb", countries=("FR",))),
        FakeCompanyProvider(a_provider_metadata("ccc")))
    assert [p.metadata.provider_key for p in registry.providers_for(country="CH")] \
        == ["aaa", "ccc"]


def test_every_required_capability_must_be_claimed_not_merely_one_of_them():
    """`required_capabilities` is a conjunction, which is the useful reading: a
    caller that needs a website *and* a country filter cannot use a provider that
    offers only one of them."""
    registry = a_registry(
        FakeCompanyProvider(a_provider_metadata(
            "aaa", capabilities=frozenset({Cap.WEBSITE_DISCOVERY,
                                           Cap.COUNTRY_FILTER}))),
        FakeCompanyProvider(a_provider_metadata(
            "bbb", capabilities=frozenset({Cap.WEBSITE_DISCOVERY}))))
    selected = registry.providers_for(required_capabilities=frozenset(
        {Cap.WEBSITE_DISCOVERY, Cap.COUNTRY_FILTER}))
    assert [p.metadata.provider_key for p in selected] == ["aaa"]


def test_the_callers_allow_list_narrows_the_selection_further():
    registry = a_registry(FakeCompanyProvider(a_provider_metadata("aaa")),
                          FakeCompanyProvider(a_provider_metadata("bbb")))
    assert [p.metadata.provider_key
            for p in registry.providers_for(provider_keys=("bbb",))] == ["bbb"]


def test_a_selection_that_matches_nothing_is_empty_rather_than_an_error():
    """§18: an empty provider set is a reportable outcome, not a failure.

    The orchestrator turns it into `NO_PROVIDER_SELECTED`, which is what keeps
    "nothing is registered for FR" distinguishable from "no employer exists in FR".
    """
    registry = a_registry(FakeCompanyProvider(a_provider_metadata("aaa",
                                                                 countries=("CH",))))
    assert registry.providers_for(country="FR") == ()
    assert a_registry().providers_for() == ()


def test_the_registry_remembers_the_last_thing_each_provider_said():
    """A status page has to answer "how is company discovery?" without running a
    pass, and last-write-wins is the honest semantics for an in-process record."""
    registry = a_registry(FakeCompanyProvider(a_provider_metadata("aaa")))
    registry.record_health(a_provider_health("aaa"))
    registry.record_health(a_provider_health(
        "aaa", status=ProviderHealthStatus.UNAVAILABLE))
    assert registry.health_for("aaa").status is ProviderHealthStatus.UNAVAILABLE
    assert registry.health_for("bbb") is None


def test_health_records_are_reported_in_key_order_with_the_unusable_named():
    registry = a_registry()
    registry.record_health(a_provider_health("zzz"))
    registry.record_health(a_provider_health(
        "aaa", status=ProviderHealthStatus.MISCONFIGURED,
        reason=ProviderFailureCode.SOURCE_MISCONFIGURED,
        detail="required configuration is missing"))
    assert [h.source_key for h in registry.health()] == ["aaa", "zzz"]
    assert registry.unusable_provider_keys() == ("aaa",)


# --- the shared provider base: limit, warnings, and never raising --------------

@pytest.mark.asyncio
async def test_a_provider_holds_itself_to_the_limit_and_says_that_it_did():
    """A narrower answer than the question has to say why (§18, provider level).

    Silent truncation is the failure that reads as success: a caller sees ten
    companies, asked for ten, and never learns there were forty.
    """
    provider = ScriptedProvider(SeedBatch([a_discovered_company("Acme"),
                                           a_discovered_company("Beta")]))
    result = await provider.discover(CompanyDiscoveryRequest(limit=1))
    assert [company.name for company in result.companies] == ["Acme"]
    assert [warning.code for warning in result.warnings] == [Warn.LIMIT_TRUNCATED]
    assert "2 companies are available" in result.warnings[0].detail
    assert result.warnings[0].provider_key == "fake_provider"
    assert result.health.status is ProviderHealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_every_skipped_entry_is_reported_and_leaves_the_provider_degraded():
    """A provider that read four lines out of five answered partially (§26).

    One warning per skip rather than a count, because each names a different line an
    operator has to go and fix, and `DEGRADED` rather than `HEALTHY` because the
    answer is incomplete — while staying usable, since the four lines are real.
    """
    provider = ScriptedProvider(SeedBatch([a_discovered_company("Acme")],
                                          skipped=("the first line is unreadable",
                                                   "the second line has no name")))
    result = await provider.discover(CompanyDiscoveryRequest())
    assert [warning.code for warning in result.warnings] == [Warn.SEED_SKIPPED] * 2
    assert [warning.detail for warning in result.warnings] == [
        "the first line is unreadable", "the second line has no name"]
    assert result.health.status is ProviderHealthStatus.DEGRADED
    assert result.health.reason is ProviderFailureCode.SOURCE_PARTIAL_FAILURE
    assert result.is_usable
    assert "2 entries" in result.health.detail


@pytest.mark.asyncio
async def test_an_empty_source_is_healthy_and_says_nothing_is_configured():
    """§28's precondition: `config/companies.yaml` ships empty.

    A fresh deployment must not look broken, so the empty source is `HEALTHY` with a
    warning that names the state. Conflating the two would make the first thing a new
    operator sees an outage they cannot fix.
    """
    provider = ScriptedProvider(SeedBatch((), empty_detail="nothing is configured yet"))
    result = await provider.discover(CompanyDiscoveryRequest())
    assert result.companies == ()
    assert [warning.code for warning in result.warnings] == [Warn.NOTHING_CONFIGURED]
    assert result.warnings[0].detail == "nothing is configured yet"
    assert result.health.status is ProviderHealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_a_source_that_yielded_nothing_because_everything_was_skipped_says_so():
    """The two conditions are different diagnoses and must not both be reported.

    "Nothing is configured" tells an operator to add an employer; "every line was
    unreadable" tells them to fix the file. Emitting both would leave them guessing
    which.
    """
    provider = ScriptedProvider(SeedBatch((), skipped=("the only line is unreadable",),
                                          empty_detail="nothing is configured yet"))
    result = await provider.discover(CompanyDiscoveryRequest())
    assert [warning.code for warning in result.warnings] == [Warn.SEED_SKIPPED]
    assert result.health.status is ProviderHealthStatus.DEGRADED


@pytest.mark.asyncio
async def test_a_source_with_no_empty_case_to_report_warns_about_nothing():
    result = await ScriptedProvider(SeedBatch(())).discover(CompanyDiscoveryRequest())
    assert result.warnings == ()
    assert result.health.status is ProviderHealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_a_provider_whose_source_raises_returns_a_failure_instead_of_raising():
    """The contract §26 depends on: `discover` is not allowed to raise.

    Contained twice — here and in the orchestrator — which costs nothing and makes a
    provider safe to call directly from a CLI or a test. The exception's own message
    stays out of the report, because it is the string that carries credentials.
    """
    provider = ScriptedProvider(error=RuntimeError("/etc/companies.yaml is a directory"))
    result = await provider.discover(CompanyDiscoveryRequest())
    assert result.companies == ()
    assert result.health.status is ProviderHealthStatus.UNAVAILABLE
    assert result.health.reason is ProviderFailureCode.SOURCE_ADAPTER_ERROR
    assert result.health.checked_at == NOW
    assert result.completed_at == NOW
    assert "companies.yaml" not in result.health.detail


@pytest.mark.asyncio
async def test_a_healthcheck_reads_the_source_without_discovering_anything():
    """`HEALTHY` means "the source is readable", not "it contains employers".

    Which is why an empty configuration passes: a status page that reported an
    outage for an unconfigured deployment would train an operator to ignore it.
    """
    healthy = await ScriptedProvider(
        SeedBatch((), empty_detail="nothing is configured yet")).healthcheck()
    assert healthy.status is ProviderHealthStatus.HEALTHY
    assert healthy.source_key == "fake_provider"
    assert healthy.checked_at == NOW

    broken = await ScriptedProvider(error=RuntimeError("boom")).healthcheck()
    assert broken.status is ProviderHealthStatus.UNAVAILABLE
    assert broken.reason is ProviderFailureCode.SOURCE_ADAPTER_ERROR


# --- §26: one provider failing costs its neighbours nothing --------------------

@pytest.mark.asyncio
async def test_a_provider_that_raises_does_not_end_the_pass():
    """The property the phase order states twice, and the reason `gather` is used
    over a semaphore rather than a `TaskGroup`: a `TaskGroup`'s first exception
    cancels its siblings, which is exactly what §26 forbids."""
    report = await an_orchestrator(a_registry(
        FakeCompanyProvider(a_provider_metadata("aaa"), error=RuntimeError("boom")),
        FakeCompanyProvider(a_provider_metadata("bbb"),
                            result=a_provider_result("bbb",
                                                     a_discovered_company("Acme"))),
    )).run(CompanyDiscoveryRequest())
    assert [result.provider_key for result in report.results] == ["aaa", "bbb"]
    assert {health.source_key: health.status for health in report.health} == {
        "aaa": ProviderHealthStatus.UNAVAILABLE,
        "bbb": ProviderHealthStatus.HEALTHY}
    assert [company.name for company in report.companies] == ["Acme"]
    assert report.unusable_provider_keys == ("aaa",)
    assert not report.is_complete


@pytest.mark.asyncio
async def test_a_credential_in_an_exception_never_reaches_a_health_record(monkeypatch):
    """§26, on the one string that could carry a secret out of the process.

    The exception names the variable *and* quotes its value *and* embeds an API key
    in a query string — V1's `FetchError(f"{url}: {last_error}")` shape. What comes
    out names the variable, which is the actionable half and cannot be a secret
    (`EnvVarName` constrains it to `^[A-Z][A-Z0-9_]*$`), and nothing else.
    """
    # Named `credential_value` rather than `secret` so ruff's S105 does not read a
    # fixture as a leaked one, the convention `tests/v2_rows.py` already follows.
    credential_value = "s3cret-value-0f1e2d3c4b5a"
    monkeypatch.setenv("COMPANY_PROVIDER_TOKEN", credential_value)
    provider = FakeCompanyProvider(
        a_provider_metadata("aaa", requires_credentials=True,
                            credential_env_vars=("COMPANY_PROVIDER_TOKEN",)),
        error=RuntimeError(
            f"GET https://boards.example.test/api?api_key={credential_value} "
            "failed: COMPANY_PROVIDER_TOKEN is expired"))
    report = await an_orchestrator(a_registry(provider)).run(CompanyDiscoveryRequest())
    detail = report.health[0].detail
    assert report.health[0].status is ProviderHealthStatus.MISCONFIGURED
    assert report.health[0].reason is ProviderFailureCode.SOURCE_MISCONFIGURED
    assert "COMPANY_PROVIDER_TOKEN" in detail
    assert credential_value not in detail
    assert "api_key" not in detail
    assert "https://" not in detail


@pytest.mark.asyncio
async def test_a_failure_detail_is_composed_rather_than_forwarded():
    """The stronger half of the same defence: nothing from the message is copied.

    A provider that failed on an unrecognized token gets the fixed sentence for its
    failure code — so a credential that no pattern would have matched still cannot
    reach a report, because there is no code path from `str(exc)` to `detail`.
    """
    provider = FakeCompanyProvider(
        a_provider_metadata("aaa"),
        error=RuntimeError("token 9f8e7d6c5b4a3f2e1d0c9b8a was refused"))
    report = await an_orchestrator(a_registry(provider)).run(CompanyDiscoveryRequest())
    assert report.health[0].detail == "the adapter failed before the source could answer"
    assert "9f8e7d6c5b4a3f2e1d0c9b8a" not in report.health[0].detail


@pytest.mark.asyncio
async def test_a_probe_that_raises_is_one_record_and_not_a_lost_round():
    """Same isolation as a pass, on the endpoint a status page calls.

    Every answer is recorded in the registry too, so the endpoint can be read again
    without probing the providers a second time.
    """
    good = FakeCompanyProvider(a_provider_metadata("zzz"))
    registry = a_registry(good, FakeCompanyProvider(a_provider_metadata("aaa"),
                                                   error=RuntimeError("boom")))
    records = await an_orchestrator(registry).healthcheck()
    assert [record.source_key for record in records] == ["aaa", "zzz"]
    assert records[0].status is ProviderHealthStatus.UNAVAILABLE
    assert records[1].status is ProviderHealthStatus.HEALTHY
    assert good.healthchecks == 1
    assert registry.health_for("aaa").status is ProviderHealthStatus.UNAVAILABLE
    assert registry.unusable_provider_keys() == ("aaa",)


@pytest.mark.asyncio
async def test_a_probe_only_reaches_the_providers_that_serve_the_country():
    swiss = FakeCompanyProvider(a_provider_metadata("aaa", countries=("CH",)))
    french = FakeCompanyProvider(a_provider_metadata("bbb", countries=("FR",)))
    records = await an_orchestrator(a_registry(swiss, french)).healthcheck(country="CH")
    assert [record.source_key for record in records] == ["aaa"]
    assert french.healthchecks == 0


# --- §18: an empty selection is a named outcome --------------------------------

@pytest.mark.asyncio
async def test_a_pass_with_no_eligible_provider_says_so_instead_of_returning_nothing():
    """"No provider is registered for FR" and "no employer exists in FR" are
    different facts, and docs/V2_SPECIFICATION.md §22 forbids hiding the first."""
    report = await an_orchestrator(a_registry()).run(CompanyDiscoveryRequest(
        country="CH"))
    assert report.results == ()
    assert report.companies == ()
    assert [warning.code for warning in report.warnings] == [Warn.NO_PROVIDER_SELECTED]
    assert report.warnings[0].provider_key is None
    assert "for CH" in report.warnings[0].detail
    assert not report.is_complete


@pytest.mark.asyncio
async def test_an_allow_list_that_matches_nothing_says_which_of_the_two_it_was():
    """An operator re-running one provider by name deserves to know their
    allow-list is why nothing ran, rather than reading it as an outage."""
    report = await an_orchestrator(a_registry(
        FakeCompanyProvider(a_provider_metadata("aaa")))).run(
        CompanyDiscoveryRequest(provider_keys=("bbb",)))
    assert [warning.code for warning in report.warnings] == [Warn.NO_PROVIDER_SELECTED]
    assert "within the caller's allow-list" in report.warnings[0].detail


@pytest.mark.asyncio
async def test_a_provider_that_cannot_filter_by_country_is_named_when_one_is_asked():
    """The one warning this codebase attributes to a provider at pass level.

    Phase 5's rule is that a sweep-level warning names no source, and it does not
    carry over here: this is a fact about a *named* provider's declared inability, and
    a reader has to know which provider returned more than they asked for.
    """
    registry = a_registry(
        FakeCompanyProvider(a_provider_metadata("aaa")),
        FakeCompanyProvider(a_provider_metadata(
            "bbb", capabilities=frozenset({Cap.COUNTRY_FILTER}))))
    report = await an_orchestrator(registry).run(CompanyDiscoveryRequest(country="CH"))
    assert [(warning.code, warning.provider_key) for warning in report.warnings] == [
        (Warn.COUNTRY_FILTER_NOT_SUPPORTED, "aaa")]
    assert "CH" in report.warnings[0].detail


@pytest.mark.asyncio
async def test_a_pass_with_no_country_warns_about_no_country_filter():
    """Nothing was restricted, so nothing failed to restrict."""
    report = await an_orchestrator(a_registry(
        FakeCompanyProvider(a_provider_metadata("aaa")))).run(CompanyDiscoveryRequest())
    assert report.warnings == ()
    assert report.is_complete


# --- §1: the orchestrator's own limit backstop ---------------------------------

@pytest.mark.asyncio
async def test_a_provider_that_ignores_the_limit_is_truncated_and_reported():
    """`MAX_COMPANIES_PER_PROVIDER`'s reason for existing.

    Every Phase 6 provider enforces its own limit; this is the backstop for the one
    that does not. Without it, an unbounded list reaches a service that writes all of
    it, and §1 asks that only justified information be persisted. The provider's own
    warnings survive the truncation.
    """
    result = a_provider_result(
        "aaa", *(a_discovered_company(name) for name in ("Acme", "Beta", "Gamma")),
        warnings=(CompanyDiscoveryWarning(code=Warn.PARTIAL_RESULTS,
                                          detail="its own warning survives",
                                          provider_key="aaa"),))
    report = await an_orchestrator(a_registry(
        FakeCompanyProvider(a_provider_metadata("aaa"), result=result))).run(
        CompanyDiscoveryRequest(limit=2))
    only = report.results[0]
    assert [company.name for company in only.companies] == ["Acme", "Beta"]
    assert [warning.code for warning in only.warnings] == [Warn.PARTIAL_RESULTS,
                                                           Warn.LIMIT_TRUNCATED]
    assert "the last 1 were dropped" in only.warnings[1].detail
    assert only.warnings[1].provider_key == "aaa"


@pytest.mark.asyncio
async def test_a_result_within_the_limit_is_passed_through_untouched():
    result = a_provider_result("aaa", a_discovered_company("Acme"))
    report = await an_orchestrator(a_registry(
        FakeCompanyProvider(a_provider_metadata("aaa"), result=result))).run(
        CompanyDiscoveryRequest(limit=2))
    assert report.results[0] == result


# --- §23: what a pass reports, and in what order ------------------------------

@pytest.mark.asyncio
async def test_a_pass_asks_every_provider_the_question_it_was_given():
    """The request reaches the providers unchanged, `limit` and country included.

    Worth asserting rather than assuming: a `limit` the orchestrator kept to itself
    would leave every provider returning its whole source and being truncated.
    """
    provider = FakeCompanyProvider(a_provider_metadata(
        "aaa", capabilities=frozenset({Cap.COUNTRY_FILTER})))
    request = CompanyDiscoveryRequest(country="CH", limit=7)
    await an_orchestrator(a_registry(provider)).run(request)
    assert provider.requests == [request]


@pytest.mark.asyncio
async def test_an_allow_list_keeps_the_other_providers_out_of_the_pass_entirely():
    aaa = FakeCompanyProvider(a_provider_metadata("aaa"))
    bbb = FakeCompanyProvider(a_provider_metadata("bbb"))
    report = await an_orchestrator(a_registry(aaa, bbb)).run(
        CompanyDiscoveryRequest(provider_keys=("bbb",)))
    assert bbb.requests and not aaa.requests
    assert report.provider_keys == ("bbb",)


@pytest.mark.asyncio
async def test_the_same_employer_from_two_providers_is_two_claims_not_one():
    """§5 keeps both provenances, so the pass must not deduplicate.

    A configured ATS organization and a stored opportunity routinely describe one
    company, and which `company_id` they resolve to is `resolution`'s decision at the
    service layer — collapsing them here would throw away the second discovery record.
    """
    logitech = a_discovered_company("Logitech")
    report = await an_orchestrator(a_registry(
        FakeCompanyProvider(a_provider_metadata("aaa"),
                            result=a_provider_result("aaa", logitech)),
        FakeCompanyProvider(a_provider_metadata("bbb"),
                            result=a_provider_result("bbb", logitech)))).run(
        CompanyDiscoveryRequest())
    assert [company.name for company in report.companies] == ["Logitech", "Logitech"]


@pytest.mark.asyncio
async def test_a_pass_records_what_each_provider_said_in_the_registry():
    """So a status endpoint can answer without running a second pass."""
    registry = a_registry(FakeCompanyProvider(a_provider_metadata("aaa"),
                                              error=RuntimeError("boom")))
    report = await an_orchestrator(registry).run(CompanyDiscoveryRequest())
    assert registry.health_for("aaa") == report.health[0]
    assert registry.unusable_provider_keys() == ("aaa",)


@pytest.mark.asyncio
async def test_a_pass_is_stamped_with_the_injected_clock():
    report = await an_orchestrator(a_registry(
        FakeCompanyProvider(a_provider_metadata("aaa")))).run(CompanyDiscoveryRequest())
    assert report.started_at == NOW
    assert report.duration_ms >= 0


def test_a_report_keeps_the_worst_condition_a_provider_reported():
    """Built directly, because one pass asks each provider once.

    The property still matters: a service that runs two passes in one process — a
    country at a time — must not have a later `HEALTHY` overwrite an earlier outage,
    or a partial answer would be reported as a complete one.
    """
    report = CompanyDiscoveryReport(
        started_at=NOW, duration_ms=1,
        results=(a_provider_result("aaa"),
                 a_provider_result("aaa", health=a_provider_health(
                     "aaa", status=ProviderHealthStatus.UNAVAILABLE)),
                 a_provider_result("bbb")))
    assert [(health.source_key, health.status) for health in report.health] == [
        ("aaa", ProviderHealthStatus.UNAVAILABLE), ("bbb", ProviderHealthStatus.HEALTHY)]
    assert report.provider_keys == ("aaa", "bbb")
    assert report.unusable_provider_keys == ("aaa",)
    assert not report.is_complete


def test_a_report_is_complete_only_when_nothing_warned_and_nobody_failed():
    """`is_complete` is what a caller reads before treating a pass as authoritative,
    so a truncated but healthy pass must not qualify."""
    assert CompanyDiscoveryReport(started_at=NOW, duration_ms=1,
                                  results=(a_provider_result("aaa"),)).is_complete
    truncated = CompanyDiscoveryReport(
        started_at=NOW, duration_ms=1,
        results=(a_provider_result("aaa", warnings=(CompanyDiscoveryWarning(
            code=Warn.LIMIT_TRUNCATED, detail="there were more",
            provider_key="aaa"),)),))
    assert not truncated.is_complete


# --- §26: bounded fan-out ------------------------------------------------------

@pytest.mark.asyncio
async def test_no_more_providers_are_in_flight_than_the_bound_allows():
    """The knob exists for a future provider that makes a request; today's three
    read a file or the local database. Counting arrivals keeps it deterministic."""
    gate = Gate(expected=2)
    registry = a_registry(*(FakeCompanyProvider(a_provider_metadata(key),
                                                on_discover=gate)
                            for key in ("aaa", "bbb", "ccc", "ddd")))
    report = await an_orchestrator(registry, max_concurrency=2).run(
        CompanyDiscoveryRequest())
    assert gate.peak == 2
    assert gate.in_flight == 0
    assert len(report.results) == 4


def test_a_pass_cannot_be_configured_to_run_no_provider_at_a_time():
    with pytest.raises(ValueError, match="max_concurrency must be at least 1"):
        CompanyDiscoveryOrchestrator(registry=a_registry(), max_concurrency=0)


# --- §8: the employers V1's configuration already names -----------------------

BOARDS = {"greenhouse": (Board("acme", "Acme SA"),),
          "lever": (Board("beta", "Beta AG"),)}


def a_configured_provider(boards=None) -> ConfiguredAtsCompanyProvider:
    return ConfiguredAtsCompanyProvider(
        boards=BOARDS if boards is None else boards, clock=CLOCK)


@pytest.mark.asyncio
async def test_a_configured_board_is_a_company_with_no_active_opportunity():
    """§28's acceptance criterion, at the provider that makes it true.

    An operator adds `- token: acme, company: Acme SA` under `greenhouse:` and Acme
    exists — this provider never looks at the postings table, so a company with zero
    vacancies is the *normal* result rather than a special case.
    """
    result = await a_configured_provider().discover(CompanyDiscoveryRequest())
    assert [company.name for company in result.companies] == ["Acme SA", "Beta AG"]
    acme = result.companies[0]
    assert acme.seed.kind is CompanySeedKind.ATS_ORGANIZATION
    assert acme.seed.external_id == "GREENHOUSE:acme"
    assert acme.seed.raw == {"source_key": "greenhouse", "token": "acme"}
    assert acme.seed.careers_url == "https://boards.greenhouse.io/acme"
    assert acme.ats_platform is AtsPlatform.GREENHOUSE
    assert acme.ats_organization_id == "acme"
    assert acme.ats_status is DetectionStatus.LIKELY
    assert [evidence.code for evidence in acme.ats_evidence] \
        == ["ATS_CONFIGURED_ORGANIZATION"]
    assert acme.career_sites == (DiscoveredCareerSite(
        url="https://boards.greenhouse.io/acme", kind=CareerSiteKind.ATS_BOARD,
        platform=AtsPlatform.GREENHOUSE,
        verification_status=DetectionStatus.LIKELY),)
    assert result.health.status is ProviderHealthStatus.HEALTHY
    assert result.warnings == ()


def test_a_configured_board_asserts_nothing_about_the_employers_own_site():
    """Because `config/companies.yaml` does not carry one.

    A board host is the platform's domain, not the employer's, and turning
    `boards.greenhouse.io` into Acme's website would be a fabricated fact that
    `identity.compare` would then treat as its strongest signal.
    """
    provider = a_configured_provider()
    seeds = provider._seeds(CompanyDiscoveryRequest())
    acme = seeds.companies[0]
    assert acme.website is None
    assert acme.country is None
    assert acme.locations == ()
    assert acme.spontaneous_application is SpontaneousApplicationSupport.UNKNOWN


@pytest.mark.asyncio
async def test_reading_the_same_configuration_twice_reports_the_same_companies():
    """§23 at its cheapest layer: the file did not change, so neither may the answer.

    Includes the evidence timestamps, which is why the clock is injected — a provider
    stamping `datetime.now()` would produce two unequal reports for one unchanged file.
    """
    provider = a_configured_provider()
    first = await provider.discover(CompanyDiscoveryRequest())
    second = await provider.discover(CompanyDiscoveryRequest())
    assert first == second


@pytest.mark.asyncio
async def test_a_platform_nobody_supports_is_reported_rather_than_ignored():
    """`workday:` in that file means an operator expects a board to be swept.

    Nothing sweeps it (§9 bounds Phase 6 to the three platforms V1 reads), and silence
    would leave them waiting for companies that will never appear.
    """
    result = await a_configured_provider(
        {"workday": (Board("acme", "Acme SA"),),
         "lever": (Board("beta", "Beta AG"),)}).discover(CompanyDiscoveryRequest())
    assert [company.name for company in result.companies] == ["Beta AG"]
    assert [warning.code for warning in result.warnings] == [Warn.SEED_SKIPPED]
    assert "'workday'" in result.warnings[0].detail
    assert "ashby, greenhouse, lever" in result.warnings[0].detail
    assert result.health.status is ProviderHealthStatus.DEGRADED


@pytest.mark.asyncio
async def test_a_configured_entry_whose_name_normalizes_to_nothing_is_skipped():
    """A name that normalizes to nothing could never be compared to a stored
    company, so it is reported rather than stored as an uncomparable row."""
    result = await a_configured_provider(
        {"lever": (Board("acme", "!!!"), Board("beta", "Beta AG"))}).discover(
        CompanyDiscoveryRequest())
    assert [company.name for company in result.companies] == ["Beta AG"]
    assert [warning.code for warning in result.warnings] == [Warn.SEED_SKIPPED]
    assert "'acme'" in result.warnings[0].detail
    assert "normalizes to nothing" in result.warnings[0].detail


@pytest.mark.asyncio
async def test_an_empty_configuration_file_is_a_healthy_provider_with_nothing_to_say():
    result = await a_configured_provider({}).discover(CompanyDiscoveryRequest())
    assert result.companies == ()
    assert [warning.code for warning in result.warnings] == [Warn.NOTHING_CONFIGURED]
    assert "config/companies.yaml" in result.warnings[0].detail
    assert result.health.status is ProviderHealthStatus.HEALTHY


def test_the_configured_ats_provider_does_not_claim_a_country_filter():
    """The honest metadata: that file says nothing about where an employer is.

    The orchestrator turns the missing capability into
    `COUNTRY_FILTER_NOT_SUPPORTED` whenever a request names a country, which is the
    report an operator can act on — as against a country silently ignored.
    """
    metadata = a_configured_provider().metadata
    assert metadata.provider_key == "configured_ats"
    assert metadata.provider_type is CompanyProviderType.CONFIGURATION
    assert metadata.priority == 10
    assert not metadata.supports(Cap.COUNTRY_FILTER)
    assert metadata.supports(Cap.ATS_DETECTION)
    assert metadata.credential_env_vars == ()
    assert metadata.countries == ()


# --- §7 and §12: what an operator can assert that nothing else can -------------

def a_manual_seed(name: str = "Acme SA", **overrides) -> ManualCompanySeed:
    fields = {"name": name}
    fields.update(overrides)
    return ManualCompanySeed(**fields)


def a_manual_provider(*seeds: ManualCompanySeed) -> ManualSeedCompanyProvider:
    return ManualSeedCompanyProvider(seeds=seeds, clock=CLOCK)


@pytest.mark.asyncio
async def test_an_operator_can_assert_a_website_and_a_spontaneous_channel():
    """The two things no other Phase 6 provider can say, and both because a person
    said them: a configured board has no employer domain, and deciding whether an
    application form exists means opening the page."""
    result = await a_manual_provider(a_manual_seed(
        website="https://acme.test", careers_url="https://acme.test/jobs",
        country="CH", locations=(Location(country="CH", city="Lausanne"),),
        aliases=("Acme Group",),
        spontaneous_application=SpontaneousApplicationSupport.SUPPORTED,
        spontaneous_application_url="https://acme.test/jobs/spontaneous",
        note="the careers page carries an open application form"),
    ).discover(CompanyDiscoveryRequest())
    company = result.companies[0]
    assert company.seed.kind is CompanySeedKind.MANUAL
    assert company.seed.external_id == "acme sa"
    assert company.seed.raw == {"asserted_by": "manual_seed"}
    assert company.website == "https://acme.test"
    assert company.country == "CH"
    assert company.locations == (Location(country="CH", city="Lausanne"),)
    assert company.aliases == ("Acme Group",)
    assert company.spontaneous_application is SpontaneousApplicationSupport.SUPPORTED
    evidence = company.spontaneous_application_evidence
    assert [item.code for item in evidence] == ["MANUAL_OPERATOR_ASSERTION"]
    assert evidence[0].detail == "the careers page carries an open application form"
    assert evidence[0].source_url == "https://acme.test/jobs/spontaneous"
    assert evidence[0].observed_at == NOW
    assert {site.url for site in company.career_sites} == {
        "https://acme.test/jobs", "https://acme.test/jobs/spontaneous"}
    assert company.confidence is DetectionStatus.LIKELY


@pytest.mark.parametrize("verdict", [SpontaneousApplicationSupport.SUPPORTED,
                                    SpontaneousApplicationSupport.NOT_SUPPORTED])
def test_a_decided_verdict_without_a_note_is_refused_at_composition_time(verdict):
    """§12: a verdict nobody can review is a guess with a provenance.

    Refused where the operator can still fix it rather than sanitized later, because
    the alternative is a stored `SUPPORTED` whose evidence reads "an operator asserted
    that Acme is an employer worth tracking" — a sentence that says nothing about an
    application form.
    """
    with pytest.raises(ValidationError, match="must carry a note"):
        a_manual_seed(spontaneous_application=verdict)


def test_a_seed_that_says_nothing_about_spontaneous_applications_needs_no_note():
    """`UNKNOWN` is the default and the honest state for a seed nobody examined."""
    seed = a_manual_seed()
    assert seed.spontaneous_application is SpontaneousApplicationSupport.UNKNOWN
    assert seed.note is None


@pytest.mark.asyncio
async def test_an_unexamined_seed_carries_no_spontaneous_application_evidence():
    """§12's forbidden inference has no code path: a company with no vacancies and
    no note stays `UNKNOWN` rather than becoming `SUPPORTED`."""
    result = await a_manual_provider(a_manual_seed()).discover(
        CompanyDiscoveryRequest())
    company = result.companies[0]
    assert company.spontaneous_application is SpontaneousApplicationSupport.UNKNOWN
    assert company.spontaneous_application_evidence == ()


@pytest.mark.asyncio
async def test_a_country_request_returns_only_the_seeds_that_named_that_country():
    """The honest filter this provider claims `COUNTRY_FILTER` for.

    A seed that names no country is not returned — inventing one for it would be a
    fabricated fact, and returning it anyway would make the capability a lie.
    """
    provider = a_manual_provider(a_manual_seed("Acme SA", country="CH"),
                                 a_manual_seed("Beta AG", country="FR"),
                                 a_manual_seed("Gamma SA"))
    swiss = await provider.discover(CompanyDiscoveryRequest(country="CH"))
    assert [company.name for company in swiss.companies] == ["Acme SA"]
    everything = await provider.discover(CompanyDiscoveryRequest())
    assert [company.name for company in everything.companies] == ["Acme SA", "Beta AG",
                                                                 "Gamma SA"]


@pytest.mark.asyncio
async def test_two_seeds_with_one_identity_keep_the_first_and_report_the_second():
    """A pasted-twice line would otherwise overwrite its own discovery record.

    Silently keeping one of two would look like the file had shrunk; §23's
    idempotence is about repeated *passes*, and this is a duplicate within one.
    """
    result = await a_manual_provider(a_manual_seed("Acme SA"),
                                     a_manual_seed("ACME SA")).discover(
        CompanyDiscoveryRequest())
    assert [company.name for company in result.companies] == ["Acme SA"]
    assert [warning.code for warning in result.warnings] == [Warn.SEED_SKIPPED]
    assert "'acme sa'" in result.warnings[0].detail
    assert result.health.status is ProviderHealthStatus.DEGRADED


def test_a_seeds_own_key_survives_a_correction_to_its_name():
    """Which is what `key` is for: fixing a typo must not create a second record.

    Without it the discovery record is derived from the normalized name, so
    `Acme S.A.` and `Acme SA` would be two sightings of one employer (§23).
    """
    assert a_manual_seed("Acme SA", key="acme-hq").external_id == "acme-hq"
    assert a_manual_seed("Acme S.A.", key="acme-hq").external_id == "acme-hq"
    assert a_manual_seed("Acme SA").external_id == "acme sa"


@pytest.mark.asyncio
async def test_a_manual_seed_whose_name_normalizes_to_nothing_is_skipped():
    result = await a_manual_provider(a_manual_seed("!!!")).discover(
        CompanyDiscoveryRequest())
    assert result.companies == ()
    assert [warning.code for warning in result.warnings] == [Warn.SEED_SKIPPED]
    assert "normalizes to nothing" in result.warnings[0].detail


@pytest.mark.asyncio
async def test_a_careers_url_that_is_a_board_becomes_one_row_and_not_two():
    """§11 asks for career sites as rows; it does not ask for contradictory rows.

    An operator who pastes the board URL into `careers_url` should get one endpoint
    labelled as the board it is, rather than two rows disagreeing about the kind — and
    the URL being the platform's own address is what makes this `CONFIRMED` (§10).
    """
    result = await a_manual_provider(a_manual_seed(
        careers_url="https://boards.greenhouse.io/acme")).discover(
        CompanyDiscoveryRequest())
    company = result.companies[0]
    assert len(company.career_sites) == 1
    site = company.career_sites[0]
    assert site.kind is CareerSiteKind.ATS_BOARD
    assert site.platform is AtsPlatform.GREENHOUSE
    assert site.verification_status is DetectionStatus.CONFIRMED
    assert company.ats_platform is AtsPlatform.GREENHOUSE
    assert company.ats_organization_id == "acme"
    assert company.ats_status is DetectionStatus.CONFIRMED
    assert [evidence.code for evidence in company.ats_evidence] == ["ATS_BOARD_URL"]


@pytest.mark.asyncio
async def test_a_deployment_with_no_manual_seed_is_healthy_and_says_so():
    result = await a_manual_provider().discover(CompanyDiscoveryRequest())
    assert result.companies == ()
    assert [warning.code for warning in result.warnings] == [Warn.NOTHING_CONFIGURED]
    assert result.health.status is ProviderHealthStatus.HEALTHY


def test_the_manual_provider_is_the_only_one_claiming_a_website_or_a_channel():
    """Both claims exist because a person made them, and priority 5 says so: an
    operator's assertion is the row other providers should resolve against."""
    metadata = a_manual_provider().metadata
    assert metadata.provider_key == "manual_seed"
    assert metadata.provider_type is CompanyProviderType.MANUAL
    assert metadata.priority == 5
    assert metadata.supports(Cap.WEBSITE_DISCOVERY)
    assert metadata.supports(Cap.SPONTANEOUS_APPLICATION_SIGNAL)
    assert metadata.supports(Cap.COUNTRY_FILTER)
    assert not a_configured_provider().metadata.supports(Cap.WEBSITE_DISCOVERY)


# --- §7 and §27: the employers the postings table already knows ----------------

def a_posting(company_name: str = "Logitech", **overrides):
    fields = {"company_name": company_name}
    fields.update(overrides)
    return an_opportunity(**fields)


def a_stored_provider(*postings, **overrides) -> StoredOpportunityCompanyProvider:
    fields = {"opportunities": RecordingLister(*postings), "clock": CLOCK}
    fields.update(overrides)
    return StoredOpportunityCompanyProvider(**fields)


@pytest.mark.asyncio
async def test_postings_naming_one_employer_differently_are_one_claim():
    """§27's walkthrough, and §4's rule about labels.

    `Logitech` and `LOGITECH` are grouped on the normalized name and presented under
    the first spelling a board actually published; the other becomes an alias rather
    than overwriting anything, because which label is canonical is resolution's
    decision and not this provider's.
    """
    result = await a_stored_provider(a_posting("Logitech"), a_posting("LOGITECH"),
                                     a_posting("Nestle")).discover(
        CompanyDiscoveryRequest())
    assert [company.name for company in result.companies] == ["Logitech", "Nestle"]
    logitech = result.companies[0]
    assert logitech.seed.kind is CompanySeedKind.OPPORTUNITY
    assert logitech.aliases == ("LOGITECH",)
    assert logitech.website is None
    assert logitech.confidence is DetectionStatus.LIKELY


@pytest.mark.asyncio
async def test_an_employer_is_keyed_on_its_name_and_not_on_a_posting():
    """Otherwise every new vacancy would add a discovery record for one company.

    The `raw` payload is where the several postings survive as a count and a set of
    source keys — enough for an operator to see why the employer is here, and no
    posting identifier that would make the seed's identity move (§23).
    """
    result = await a_stored_provider(
        a_posting("Logitech"),
        a_posting("Logitech", source=a_source_record(source_key="other_board",
                                                    external_id="posting-2"))).discover(
        CompanyDiscoveryRequest())
    seed = result.companies[0].seed
    assert seed.external_id == "logitech"
    assert seed.raw == {"posting_count": "2",
                        "source_keys": "other_board,test_board"}
    assert seed.source_url == "https://example.test/postings/1"


@pytest.mark.asyncio
async def test_an_application_url_on_a_platforms_own_host_is_a_confirmed_detection():
    """The strong-evidence case §10 describes, arriving from a posting.

    The company's own confidence stays `LIKELY` all the same: what is confirmed is
    that *some* employer publishes on that board, while the employer name still came
    out of a posting's free-text field.
    """
    result = await a_stored_provider(a_posting(
        "Acme", application_url="https://jobs.lever.co/acme/1a2b3c")).discover(
        CompanyDiscoveryRequest())
    company = result.companies[0]
    assert company.ats_platform is AtsPlatform.LEVER
    assert company.ats_organization_id == "acme"
    assert company.ats_status is DetectionStatus.CONFIRMED
    assert [evidence.code for evidence in company.ats_evidence] == ["ATS_BOARD_URL"]
    assert company.career_sites == (DiscoveredCareerSite(
        url="https://jobs.lever.co/acme", kind=CareerSiteKind.ATS_BOARD,
        platform=AtsPlatform.LEVER,
        verification_status=DetectionStatus.CONFIRMED),)
    assert company.confidence is DetectionStatus.LIKELY


@pytest.mark.asyncio
async def test_a_posting_whose_application_url_names_no_platform_detects_nothing():
    """And reports no career site, rather than the application URL as if it were
    one: a single posting's apply link is not an employer's careers endpoint."""
    result = await a_stored_provider(a_posting("Acme")).discover(
        CompanyDiscoveryRequest())
    company = result.companies[0]
    assert company.ats_platform is None
    assert company.ats_status is None
    assert company.career_sites == ()


@pytest.mark.asyncio
async def test_a_country_request_keeps_only_the_postings_that_say_that_country():
    """A posting carries `location.country`, which is why this provider genuinely
    claims `COUNTRY_FILTER` — and a posting with no location cannot answer."""
    result = await a_stored_provider(
        a_posting("Acme"),
        a_posting("Beta", location=Location(country="FR", city="Lyon")),
        a_posting("Gamma", location=None)).discover(
        CompanyDiscoveryRequest(country="CH"))
    assert [company.name for company in result.companies] == ["Acme"]
    assert result.warnings == ()


@pytest.mark.asyncio
async def test_the_locations_of_an_employer_are_deduplicated_and_never_geocoded():
    """§22: the posting's own text, passed through. Phase 7 owns geocoding.

    The country comes from the first location that names one rather than from a
    lookup, which is the only inference this provider makes and the only one the
    postings support.
    """
    result = await a_stored_provider(
        a_posting("Acme"), a_posting("Acme"),
        a_posting("Acme", location=Location(country="CH", city="Geneva"))).discover(
        CompanyDiscoveryRequest())
    company = result.companies[0]
    assert len(company.locations) == 2
    assert {location.city for location in company.locations} == {"Lausanne", "Geneva"}
    assert company.country == "CH"
    assert company.seed.country == "CH"


@pytest.mark.asyncio
async def test_a_posting_whose_employer_name_normalizes_to_nothing_is_reported():
    result = await a_stored_provider(a_posting("!!!"), a_posting("Acme")).discover(
        CompanyDiscoveryRequest())
    assert [company.name for company in result.companies] == ["Acme"]
    assert [warning.code for warning in result.warnings] == [Warn.SEED_SKIPPED]
    assert "'test_board'" in result.warnings[0].detail
    assert result.health.status is ProviderHealthStatus.DEGRADED


@pytest.mark.asyncio
async def test_the_number_of_postings_read_is_fixed_and_not_the_company_limit():
    """Hundreds of postings routinely name a few dozen employers.

    Reading only `limit` postings would return far fewer companies than the caller
    asked for, so the two bounds are deliberately different numbers.
    """
    lister = RecordingLister()
    provider = StoredOpportunityCompanyProvider(opportunities=lister,
                                                postings_per_pass=25, clock=CLOCK)
    await provider.discover(CompanyDiscoveryRequest(limit=5))
    assert lister.limits == [25]
    assert POSTINGS_PER_PASS == 500


def test_a_provider_that_would_read_no_posting_at_all_is_refused():
    with pytest.raises(ValueError, match="postings_per_pass must be at least 1"):
        StoredOpportunityCompanyProvider(opportunities=RecordingLister(),
                                         postings_per_pass=0)


@pytest.mark.asyncio
async def test_a_storage_failure_is_this_providers_health_and_not_an_exception():
    """The database being unreachable must not end a pass the other two providers
    could still answer (§26)."""
    result = await a_stored_provider(
        opportunities=RecordingLister(error=RuntimeError("the connection was reset"))
    ).discover(CompanyDiscoveryRequest())
    assert result.companies == ()
    assert result.health.status is ProviderHealthStatus.UNAVAILABLE
    assert result.health.reason is ProviderFailureCode.SOURCE_ADAPTER_ERROR


@pytest.mark.asyncio
async def test_a_database_with_no_postings_asks_for_a_discovery_sweep():
    result = await a_stored_provider().discover(CompanyDiscoveryRequest())
    assert result.companies == ()
    assert [warning.code for warning in result.warnings] == [Warn.NOTHING_CONFIGURED]
    assert "run a discovery sweep first" in result.warnings[0].detail
    assert result.health.status is ProviderHealthStatus.HEALTHY


def test_the_stored_opportunity_provider_claims_the_filters_a_posting_supports():
    metadata = a_stored_provider().metadata
    assert metadata.provider_key == "stored_opportunities"
    assert metadata.provider_type is CompanyProviderType.STORED_OPPORTUNITIES
    assert metadata.priority == 20
    assert metadata.supports(Cap.COUNTRY_FILTER)
    assert metadata.supports(Cap.LOCATION_DISCOVERY)
    assert not metadata.supports(Cap.WEBSITE_DISCOVERY)


# --- §18: composition is the only place a provider is named -------------------

def test_a_deployment_without_a_database_still_has_two_providers():
    """A missing lister is the absence of a database, not an empty source.

    So that provider is not registered — and the pass reports which providers ran, so
    the difference is visible rather than silent.
    """
    registry = build_company_provider_registry(boards={}, clock=CLOCK)
    assert registry.provider_keys == ("configured_ats", "manual_seed")


def test_an_empty_operator_list_still_registers_its_provider():
    """The asymmetry with the lister above, and the reason for it: "nothing
    configured" and "not deployed" must not look the same on a status page."""
    registry = build_company_provider_registry(
        boards={}, opportunities=RecordingLister(), clock=CLOCK)
    assert registry.provider_keys == ("configured_ats", "manual_seed",
                                      "stored_opportunities")
    assert [provider.metadata.provider_key for provider in registry] == [
        "manual_seed", "configured_ats", "stored_opportunities"]


@pytest.mark.asyncio
async def test_a_composed_pass_reports_every_provider_in_priority_order():
    """The end-to-end shape of a Phase 6 pass, with three real providers.

    Which is also the §28 demonstration in miniature: `Gamma SA` and `Acme SA` come
    from configuration and have no vacancy anywhere, and both are in the answer.
    """
    discovery = build_company_discovery(
        boards=BOARDS, opportunities=RecordingLister(a_posting("Logitech")),
        manual_seeds=(a_manual_seed("Gamma SA", website="https://gamma.test"),),
        clock=CLOCK)
    report = await discovery.orchestrator.run(CompanyDiscoveryRequest())
    assert [company.name for company in report.companies] == ["Gamma SA", "Acme SA",
                                                              "Beta AG", "Logitech"]
    assert report.provider_keys == ("configured_ats", "manual_seed",
                                    "stored_opportunities")
    assert report.is_complete
    assert discovery.registry.health_for("manual_seed").status \
        is ProviderHealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_a_full_company_discovery_pass_opens_no_socket(monkeypatch):
    """§30's "no live network calls in unit tests", proved rather than asserted.

    Every Phase 6 provider reads a local file, an in-memory list or the local
    database, and §9 forbids fetching a page to detect an ATS. Refusing every outbound
    connection is the check that would fail the day someone adds one — including
    inside `healthcheck`, which is the probe most likely to grow a request.
    """
    def refuse(*args, **kwargs):
        raise AssertionError("a company discovery provider opened a connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    discovery = build_company_discovery(
        boards=BOARDS, opportunities=RecordingLister(a_posting("Logitech")),
        manual_seeds=(a_manual_seed("Gamma SA"),), clock=CLOCK)
    report = await discovery.orchestrator.run(CompanyDiscoveryRequest())
    records = await discovery.orchestrator.healthcheck()
    assert len(report.companies) == 4
    assert [record.status for record in records] == [
        ProviderHealthStatus.HEALTHY] * 3


