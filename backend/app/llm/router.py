"""Choosing a provider for a request, deterministically, and running it.

The router is the one place a request meets a provider, and the reason no business
code ever names one (docs/LLM_PROVIDER_ARCHITECTURE.md §28). A service builds an
`LLMRequest` and a `RoutingPolicy` and calls `route`; the router filters the registry
by capability and privacy, orders the survivors by a fixed rule, and runs the first —
falling to the next only when the policy opted into fallback and the failure is one a
different provider could plausibly survive.

**The ordering is deterministic (§28).** Most specific first:

1. an explicit `provider_key` override on the policy — an operator pinning one;
2. the policy's `preferred_keys`, in the order given — a task's stated preference;
3. everything else the filters left, by the registry's total order (priority, key).

A request routed twice against an unchanged registry picks the same provider, because
every tie has a key tie-break — which is what makes a fallback chain reproducible
rather than a coin flip.

**Privacy is enforced before capability spends anything (§12).** A `LOCAL_ONLY`
policy filters to providers that run on this machine *by validated address*, so a
prompt carrying candidate data cannot be sent to a hosted API even by
misconfiguration. `SPECIFIC_CONNECTION_ONLY` narrows to named keys and never falls
back off them. `EXTERNAL_ALLOWED` is the only class that may reach a remote provider,
and even then fallback across the local/remote boundary is opt-in, never implicit.

**Fallback is explicit and recorded (§29).** A provider whose failure is retryable
is retried within itself a bounded number of times; a provider that still fails, or
fails unretryably, hands off to the next candidate *only if* `allow_fallback` is set —
and the resulting run records `fallback_from` and a `fallback_reason` so an operator
can see the platform did not silently switch models.
"""
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from backend.app.llm.capabilities import Capability
from backend.app.llm.contracts import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMStreamGen,
    StreamEventType,
)
from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.registry import LLMProviderRegistry


class PrivacyClass(StrEnum):
    """How far a request's data may travel (§12).

    A property of the *request*, decided by the caller from what the prompt carries,
    not a property of a provider. The router turns it into a filter over the registry
    before any provider is tried.
    """

    # The prompt may go only to a provider that runs on this machine. The default for
    # anything carrying candidate evidence: the safe choice, chosen explicitly.
    LOCAL_ONLY = "LOCAL_ONLY"
    # The prompt may go to a remote provider. The caller has judged its content safe
    # to send off the machine.
    EXTERNAL_ALLOWED = "EXTERNAL_ALLOWED"
    # The prompt may go only to the named connections, wherever they are, and never
    # falls back off them. For an operator pinning a task to one provider.
    SPECIFIC_CONNECTION_ONLY = "SPECIFIC_CONNECTION_ONLY"


# How many times a single provider is retried on a retryable failure before the
# router gives up on it (§65). Small and bounded: a retryable failure that persists
# through three tries is not going to clear on a fourth, and an unbounded retry turns
# a rate limit into a spend spiral.
DEFAULT_MAX_ATTEMPTS_PER_PROVIDER = 2


@dataclass(frozen=True)
class RoutingPolicy:
    """How to choose a provider for one request, and whether to fall back.

    Assembled by a service (often from a user's stored default and a task preference)
    and handed to the router with the request. `privacy` is required — there is no
    default, because guessing "external is fine" is exactly the mistake that leaks a
    prompt, so a caller must state it.
    """

    privacy: PrivacyClass
    # An operator's hard pin: if set, only this provider is considered, and a failure
    # is a failure — no fallback off an explicit choice.
    provider_key: str | None = None
    # A task's ordered preference, tried before the registry's own order. Not a pin:
    # a preferred provider that is filtered out (wrong capability, wrong privacy) is
    # simply skipped, and the next candidate serves.
    preferred_keys: tuple[str, ...] = ()
    # For SPECIFIC_CONNECTION_ONLY: the only keys allowed. Ignored for the other
    # classes, where the privacy filter does the narrowing.
    allowed_keys: tuple[str, ...] = ()
    # Whether a failing provider may hand off to the next candidate. Off by default:
    # switching models is a decision, and a caller opts in rather than discovering
    # after the fact that a different model answered.
    allow_fallback: bool = False
    max_attempts_per_provider: int = DEFAULT_MAX_ATTEMPTS_PER_PROVIDER


@dataclass
class RoutingAttempt:
    """One provider's turn in serving a request — what it was and how it ended.

    Recorded whether it succeeded or failed, so `RoutingOutcome.attempts` is the audit
    trail a fallback leaves: which providers were tried, in order, and why each earlier
    one gave way. `failure` is `None` on the attempt that succeeded.
    """

    provider_key: str
    succeeded: bool
    failure: LLMError | None = None


@dataclass
class RoutingOutcome:
    """The result of routing: the answer, and the trail of how it was reached.

    `response` is the successful `LLMResponse`; `provider_key` names who produced it.
    `attempts` lists every provider tried in order — a single entry when the first
    served, more when the policy fell back. `fallback_from` and `fallback_reason` are
    set when the serving provider was not the first choice, so the telemetry run and a
    status page can show the platform switched and why (§29, §56).
    """

    response: LLMResponse
    provider_key: str
    attempts: list[RoutingAttempt] = field(default_factory=list)
    fallback_from: str | None = None
    fallback_reason: LLMFailureCode | None = None


class NoProviderAvailable(LLMError):
    """No registered provider satisfied the request's capabilities and privacy.

    An `LLMError` subclass so a caller catches one exception type, with a code that
    tells the two apart: `CAPABILITY_NOT_SUPPORTED` when the filters left nothing that
    could serve the shape of the request, `PROVIDER_MISCONFIGURED` when providers
    exist but none is usable under the privacy class (no local provider for a
    `LOCAL_ONLY` task, say).
    """

    def __init__(self, code: LLMFailureCode, detail: str) -> None:
        super().__init__(code, detail=detail)


class LLMRouter:
    """Selects and runs a provider for each request, per a `RoutingPolicy`."""

    def __init__(self, registry: LLMProviderRegistry) -> None:
        self._registry = registry

    def candidates(self, request: LLMRequest,
                   policy: RoutingPolicy) -> tuple[LLMProvider, ...]:
        """The providers eligible for this request, in the order they will be tried.

        Public so a caller — or a test — can see the routing decision without running
        a generation. The order is the deterministic rule from the module docstring:
        an explicit pin alone, else preferred keys first and the registry order after,
        with capability and privacy already applied.
        """
        required = request.required_capabilities()
        local_only = policy.privacy is PrivacyClass.LOCAL_ONLY
        allow_list = self._allow_list(policy)
        eligible = self._registry.candidates(
            required_capabilities=required,
            local_only=local_only,
            provider_keys=allow_list)
        return self._order(eligible, policy)

    def _allow_list(self, policy: RoutingPolicy) -> tuple[str, ...]:
        if policy.provider_key is not None:
            return (policy.provider_key,)
        if policy.privacy is PrivacyClass.SPECIFIC_CONNECTION_ONLY:
            return policy.allowed_keys
        return ()

    def _order(self, providers: Sequence[LLMProvider],
               policy: RoutingPolicy) -> tuple[LLMProvider, ...]:
        """Preferred keys first (in the order given), then the registry's own order.

        `providers` arrives already in registry order (priority, then key), so the
        preferred ones are lifted to the front and the rest keep that stable order —
        which is what makes the whole sequence reproducible.
        """
        if not policy.preferred_keys:
            return tuple(providers)
        by_key = {p.metadata.provider_key: p for p in providers}
        front = [by_key[key] for key in policy.preferred_keys if key in by_key]
        front_keys = {p.metadata.provider_key for p in front}
        rest = [p for p in providers if p.metadata.provider_key not in front_keys]
        return tuple(front + rest)

    async def route(self, request: LLMRequest,
                    policy: RoutingPolicy) -> RoutingOutcome:
        """Choose a provider and run it, falling back if the policy allows.

        Raises `NoProviderAvailable` when nothing is eligible, and the last provider's
        `LLMError` when every eligible provider failed. On success returns a
        `RoutingOutcome` whose `attempts` records the whole trail and whose
        `fallback_from`/`fallback_reason` are set when the winner was not the first
        choice.
        """
        candidates = self.candidates(request, policy)
        self._refuse_if_empty(request, policy, candidates)

        attempts: list[RoutingAttempt] = []
        first_key = candidates[0].metadata.provider_key
        first_failure: LLMError | None = None
        for index, provider in enumerate(candidates):
            key = provider.metadata.provider_key
            try:
                response = await self._run_with_retries(provider, request, policy)
            except LLMError as error:
                attempts.append(RoutingAttempt(key, succeeded=False, failure=error))
                if first_failure is None:
                    first_failure = error
                if not policy.allow_fallback or index == len(candidates) - 1:
                    raise
                continue
            attempts.append(RoutingAttempt(key, succeeded=True))
            outcome = RoutingOutcome(response=response, provider_key=key,
                                     attempts=attempts)
            if key != first_key and first_failure is not None:
                outcome.fallback_from = first_key
                outcome.fallback_reason = first_failure.code
            return outcome
        # Unreachable: the loop returns on success and re-raises on the last failure.
        raise first_failure or NoProviderAvailable(
            LLMFailureCode.PROVIDER_INTERNAL_ERROR, "routing produced no outcome")

    async def stream(self, request: LLMRequest,
                     policy: RoutingPolicy) -> LLMStreamGen:
        """Stream from the chosen provider, with fallback *only before the first byte*.

        A stream that has already emitted a TEXT_DELTA cannot fall back — the consumer
        has seen tokens, and replaying from a different provider would duplicate them
        (the same rule V1's stale-session recovery follows). So fallback here is
        limited to a provider that fails before producing any content: its ERROR is
        swallowed and the next candidate is tried; once content flows, an ERROR is
        forwarded and the stream ends.
        """
        candidates = self.candidates(request, policy)
        self._refuse_if_empty(request, policy, candidates)
        last_error: LLMStreamEvent | None = None
        for index, provider in enumerate(candidates):
            produced = False
            async for event in provider.stream(request):
                if event.type is StreamEventType.ERROR and not produced:
                    last_error = event
                    break
                if event.type is StreamEventType.TEXT_DELTA:
                    produced = True
                yield event
                if event.type is StreamEventType.COMPLETED:
                    return
            else:
                # The provider's stream ended without a terminal ERROR-before-content
                # we caught, and without COMPLETED returning — nothing more to try.
                return
            if not policy.allow_fallback or index == len(candidates) - 1:
                if last_error is not None:
                    yield last_error
                return
        if last_error is not None:
            yield last_error

    async def _run_with_retries(self, provider: LLMProvider, request: LLMRequest,
                                policy: RoutingPolicy) -> LLMResponse:
        """Run one provider, retrying a retryable failure up to the policy's bound."""
        attempts = max(1, policy.max_attempts_per_provider)
        last: LLMError | None = None
        for _ in range(attempts):
            try:
                return await provider.generate(request)
            except LLMError as error:
                last = error
                if not error.retryable:
                    raise
        assert last is not None
        raise last

    def _refuse_if_empty(self, request: LLMRequest, policy: RoutingPolicy,
                         candidates: Sequence[LLMProvider]) -> None:
        """Raise a typed refusal when nothing is eligible, saying which reason.

        Tells "the shape cannot be served anywhere" from "providers exist but none is
        allowed here", because the fixes differ: the first needs a more capable
        provider, the second a privacy or configuration change.
        """
        if candidates:
            return
        required = request.required_capabilities()
        # Would anything serve if privacy were not the constraint? If a
        # capability-only filter also finds nothing, the shape is the problem.
        capable = self._registry.candidates(required_capabilities=required,
                                             usable_only=False)
        if not capable:
            missing = ", ".join(sorted(
                c.value for c in _first_missing(self._registry, required)))
            raise NoProviderAvailable(
                LLMFailureCode.CAPABILITY_NOT_SUPPORTED,
                f"no provider supports the required capabilities ({missing})")
        raise NoProviderAvailable(
            LLMFailureCode.PROVIDER_MISCONFIGURED,
            f"no provider is available under the {policy.privacy} privacy policy")


def _first_missing(registry: LLMProviderRegistry,
                   required: frozenset[Capability]) -> frozenset[Capability]:
    """The capabilities no registered provider has, for a precise refusal message."""
    have: set[Capability] = set()
    for provider in registry:
        have |= provider.metadata.capabilities
    return frozenset(required) - have
