# tests/test_v2_llm_router.py
"""How the router chooses a provider, and how it falls back — the platform's core.

The router is the one place a request meets a provider, so these tests pin the two
guarantees business code above it relies on: the choice is deterministic (a request
routed twice against an unchanged registry picks the same provider), and privacy is
enforced before anything is spent (a LOCAL_ONLY prompt never reaches a remote
provider). Fallback is exercised as the explicit, recorded thing it is — off by
default, opt-in, and never silent.

Providers are `FakeProvider`s: the behaviour under test is the router's selection and
fallback, not any adapter's transport.
"""
import pytest

from backend.app.llm.capabilities import BASELINE_CAPABILITY, Capability
from backend.app.llm.contracts import (
    LLMMessage,
    LLMRequest,
    StructuredOutputSpec,
)
from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.registry import LLMProviderRegistry
from backend.app.llm.router import (
    LLMRouter,
    NoProviderAvailable,
    PrivacyClass,
    RoutingPolicy,
)
from tests.v2_llm import FakeProvider

pytestmark = pytest.mark.asyncio

_EXTERNAL = RoutingPolicy(privacy=PrivacyClass.EXTERNAL_ALLOWED)


def _request(**overrides) -> LLMRequest:
    fields = {"messages": (LLMMessage.user("hi"),)}
    fields.update(overrides)
    return LLMRequest(**fields)


def _router(*providers: FakeProvider) -> LLMRouter:
    registry = LLMProviderRegistry()
    registry.register_all(providers)
    return LLMRouter(registry)


# --- ordering --------------------------------------------------------------

async def test_candidates_are_ordered_by_priority_then_key():
    low = FakeProvider(provider_key="zeta", priority=10)
    high = FakeProvider(provider_key="alpha", priority=90)
    mid = FakeProvider(provider_key="mu", priority=10)
    router = _router(high, low, mid)
    keys = [p.metadata.provider_key
            for p in router.candidates(_request(), _EXTERNAL)]
    # priority 10 before 90; within 10, key order mu < zeta.
    assert keys == ["mu", "zeta", "alpha"]


async def test_preferred_keys_are_lifted_to_the_front():
    a = FakeProvider(provider_key="a", priority=10)
    b = FakeProvider(provider_key="b", priority=20)
    c = FakeProvider(provider_key="c", priority=30)
    router = _router(a, b, c)
    policy = RoutingPolicy(privacy=PrivacyClass.EXTERNAL_ALLOWED,
                           preferred_keys=("c", "b"))
    keys = [p.metadata.provider_key for p in router.candidates(_request(), policy)]
    assert keys == ["c", "b", "a"]


async def test_an_explicit_provider_key_pins_to_one():
    a = FakeProvider(provider_key="a", priority=10)
    b = FakeProvider(provider_key="b", priority=20)
    router = _router(a, b)
    policy = RoutingPolicy(privacy=PrivacyClass.EXTERNAL_ALLOWED, provider_key="b")
    keys = [p.metadata.provider_key for p in router.candidates(_request(), policy)]
    assert keys == ["b"]


# --- capability filtering --------------------------------------------------

async def test_a_provider_lacking_a_required_capability_is_filtered_out():
    plain = FakeProvider(provider_key="plain",
                         capabilities=frozenset({BASELINE_CAPABILITY}))
    structured = FakeProvider(
        provider_key="structured",
        capabilities=frozenset({BASELINE_CAPABILITY, Capability.STRUCTURED_OUTPUT}))
    router = _router(plain, structured)
    request = _request(structured_output=StructuredOutputSpec(
        name="x", schema={"type": "object"}))
    keys = [p.metadata.provider_key for p in router.candidates(request, _EXTERNAL)]
    assert keys == ["structured"]


async def test_a_request_no_provider_can_serve_is_refused_as_capability():
    plain = FakeProvider(provider_key="plain",
                         capabilities=frozenset({BASELINE_CAPABILITY}))
    router = _router(plain)
    request = _request(structured_output=StructuredOutputSpec(
        name="x", schema={"type": "object"}))
    with pytest.raises(NoProviderAvailable) as caught:
        await router.route(request, _EXTERNAL)
    assert caught.value.code is LLMFailureCode.CAPABILITY_NOT_SUPPORTED


# --- privacy ---------------------------------------------------------------

async def test_local_only_never_reaches_a_remote_provider():
    remote = FakeProvider(provider_key="remote", local=False)
    router = _router(remote)
    policy = RoutingPolicy(privacy=PrivacyClass.LOCAL_ONLY)
    with pytest.raises(NoProviderAvailable) as caught:
        await router.route(_request(), policy)
    # Providers exist, but none is usable under the privacy class.
    assert caught.value.code is LLMFailureCode.PROVIDER_MISCONFIGURED


async def test_local_only_selects_the_local_provider():
    remote = FakeProvider(provider_key="remote", local=False, priority=10)
    local = FakeProvider(provider_key="local", local=True, priority=20)
    router = _router(remote, local)
    policy = RoutingPolicy(privacy=PrivacyClass.LOCAL_ONLY)
    keys = [p.metadata.provider_key for p in router.candidates(_request(), policy)]
    assert keys == ["local"]


async def test_specific_connection_only_narrows_to_the_allowed_keys():
    a = FakeProvider(provider_key="a", priority=10)
    b = FakeProvider(provider_key="b", priority=20)
    c = FakeProvider(provider_key="c", priority=30)
    router = _router(a, b, c)
    policy = RoutingPolicy(privacy=PrivacyClass.SPECIFIC_CONNECTION_ONLY,
                           allowed_keys=("a", "c"))
    keys = {p.metadata.provider_key for p in router.candidates(_request(), policy)}
    assert keys == {"a", "c"}


# --- running and fallback --------------------------------------------------

async def test_route_runs_the_first_candidate_and_returns_its_answer():
    provider = FakeProvider(provider_key="a", text="the answer")
    outcome = await _router(provider).route(_request(), _EXTERNAL)
    assert outcome.response.text == "the answer"
    assert outcome.provider_key == "a"
    assert provider.calls == 1
    assert [a.provider_key for a in outcome.attempts] == ["a"]


async def test_a_failure_does_not_fall_back_unless_the_policy_allows():
    failing = FakeProvider(provider_key="a", priority=10,
                           error=LLMError(LLMFailureCode.PROVIDER_INTERNAL_ERROR))
    backup = FakeProvider(provider_key="b", priority=20, text="backup")
    router = _router(failing, backup)
    with pytest.raises(LLMError):
        await router.route(_request(), _EXTERNAL)  # allow_fallback is False
    assert backup.calls == 0


async def test_fallback_hands_off_to_the_next_candidate():
    failing = FakeProvider(provider_key="a", priority=10,
                           error=LLMError(LLMFailureCode.PROVIDER_UNAVAILABLE))
    backup = FakeProvider(provider_key="b", priority=20, text="backup")
    router = _router(failing, backup)
    policy = RoutingPolicy(privacy=PrivacyClass.EXTERNAL_ALLOWED, allow_fallback=True)
    outcome = await router.route(_request(), policy)
    assert outcome.response.text == "backup"
    assert outcome.provider_key == "b"
    assert outcome.fallback_from == "a"
    assert outcome.fallback_reason is LLMFailureCode.PROVIDER_UNAVAILABLE
    assert [a.provider_key for a in outcome.attempts] == ["a", "b"]


async def test_a_retryable_failure_is_retried_within_the_same_provider():
    # Fails once (retryable), then succeeds — a bounded retry recovers it.
    flaky = FakeProvider(provider_key="a",
                         error=LLMError(LLMFailureCode.PROVIDER_RATE_LIMITED),
                         fail_times=1, text="recovered")
    policy = RoutingPolicy(privacy=PrivacyClass.EXTERNAL_ALLOWED,
                           max_attempts_per_provider=2)
    outcome = await _router(flaky).route(_request(), policy)
    assert outcome.response.text == "recovered"
    assert flaky.calls == 2


async def test_an_unretryable_failure_is_not_retried():
    failing = FakeProvider(provider_key="a",
                           error=LLMError(LLMFailureCode.PROVIDER_AUTH_REQUIRED),
                           fail_times=1, text="never")
    policy = RoutingPolicy(privacy=PrivacyClass.EXTERNAL_ALLOWED,
                           max_attempts_per_provider=3)
    with pytest.raises(LLMError) as caught:
        await _router(failing).route(_request(), policy)
    assert caught.value.code is LLMFailureCode.PROVIDER_AUTH_REQUIRED
    assert failing.calls == 1  # auth failures are not retryable


async def test_routing_the_same_request_twice_picks_the_same_provider():
    a = FakeProvider(provider_key="a", priority=10)
    b = FakeProvider(provider_key="b", priority=10)  # same priority, key tie-break
    router = _router(a, b)
    first = await router.route(_request(), _EXTERNAL)
    second = await router.route(_request(), _EXTERNAL)
    assert first.provider_key == second.provider_key == "a"


async def test_an_empty_registry_refuses_with_capability_not_supported():
    router = LLMRouter(LLMProviderRegistry())
    with pytest.raises(NoProviderAvailable) as caught:
        await router.route(_request(), _EXTERNAL)
    assert caught.value.code is LLMFailureCode.CAPABILITY_NOT_SUPPORTED
