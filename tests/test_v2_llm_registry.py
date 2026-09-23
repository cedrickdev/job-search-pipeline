# tests/test_v2_llm_registry.py
"""What the provider registry guarantees: a total order, a unique key, honest health.

The registry names no provider — `bootstrap` fills it — so these tests build it from
`FakeProvider`s and pin the three properties the router and the settings page depend
on: a duplicate `provider_key` is a loud composition error (two providers under one
name would orphan telemetry), the candidate order is total and reproducible, and
health is last-write-wins in memory with `UNKNOWN` for a provider nobody probed.
"""
import pytest

from backend.app.llm.capabilities import BASELINE_CAPABILITY, Capability
from backend.app.llm.contracts import ProviderHealth, ProviderHealthStatus
from backend.app.llm.registry import (
    LLMProviderRegistry,
    LLMRegistryError,
    LLMRegistryErrorCode,
)
from tests.v2_llm import FakeProvider


def test_registering_two_providers_under_one_key_is_refused():
    registry = LLMProviderRegistry()
    registry.register(FakeProvider(provider_key="dup"))
    with pytest.raises(LLMRegistryError) as caught:
        registry.register(FakeProvider(provider_key="dup"))
    assert caught.value.code is LLMRegistryErrorCode.DUPLICATE_PROVIDER


def test_getting_an_unregistered_key_is_a_typed_error():
    registry = LLMProviderRegistry()
    with pytest.raises(LLMRegistryError) as caught:
        registry.get("missing")
    assert caught.value.code is LLMRegistryErrorCode.UNKNOWN_PROVIDER


def test_iteration_is_ordered_by_priority_then_key():
    registry = LLMProviderRegistry()
    registry.register_all([
        FakeProvider(provider_key="zeta", priority=10),
        FakeProvider(provider_key="alpha", priority=90),
        FakeProvider(provider_key="mu", priority=10),
    ])
    assert [p.metadata.provider_key for p in registry] == ["mu", "zeta", "alpha"]


def test_candidates_filter_on_capability():
    registry = LLMProviderRegistry()
    registry.register_all([
        FakeProvider(provider_key="plain",
                     capabilities=frozenset({BASELINE_CAPABILITY})),
        FakeProvider(provider_key="tools",
                     capabilities=frozenset({BASELINE_CAPABILITY, Capability.TOOLS})),
    ])
    keys = [p.metadata.provider_key for p in registry.candidates(
        required_capabilities=frozenset({Capability.TOOLS}))]
    assert keys == ["tools"]


def test_candidates_filter_on_local_execution():
    registry = LLMProviderRegistry()
    registry.register_all([
        FakeProvider(provider_key="remote", local=False),
        FakeProvider(provider_key="local", local=True),
    ])
    keys = [p.metadata.provider_key
            for p in registry.candidates(local_only=True)]
    assert keys == ["local"]


def test_an_unprobed_provider_is_unknown_but_usable():
    registry = LLMProviderRegistry()
    registry.register(FakeProvider(provider_key="a"))
    assert registry.health_for("a").status is ProviderHealthStatus.UNKNOWN
    # UNKNOWN counts as usable, so an unprobed provider is still a candidate.
    assert [p.metadata.provider_key for p in registry.candidates()] == ["a"]


def test_an_unavailable_provider_is_filtered_from_usable_candidates():
    registry = LLMProviderRegistry()
    registry.register(FakeProvider(provider_key="a"))
    registry.record_health("a", ProviderHealth(
        status=ProviderHealthStatus.UNAVAILABLE))
    assert registry.candidates() == ()
    # ...but it is still there when the health filter is off.
    assert [p.metadata.provider_key
            for p in registry.candidates(usable_only=False)] == ["a"]


def test_recording_health_for_an_unregistered_provider_is_refused():
    registry = LLMProviderRegistry()
    with pytest.raises(LLMRegistryError) as caught:
        registry.record_health("ghost", ProviderHealth.unknown())
    assert caught.value.code is LLMRegistryErrorCode.UNKNOWN_PROVIDER


def test_health_reports_every_provider_including_the_unprobed():
    registry = LLMProviderRegistry()
    registry.register_all([FakeProvider(provider_key="a"),
                           FakeProvider(provider_key="b")])
    registry.record_health("a", ProviderHealth(status=ProviderHealthStatus.HEALTHY))
    health = registry.health()
    assert health["a"].status is ProviderHealthStatus.HEALTHY
    assert health["b"].status is ProviderHealthStatus.UNKNOWN
