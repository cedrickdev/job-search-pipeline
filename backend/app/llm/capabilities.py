"""What an LLM provider can be asked to do, declared before it is asked.

The provider-neutral platform's first rule (docs/LLM_PROVIDER_ARCHITECTURE.md §7,
CLAUDE.md) is that business code never branches on *which* provider it holds. A
résumé service does not ask "is this Claude?"; it asks "can this provider return
structured output?" and lets the router refuse a provider that cannot. `Capability`
is the vocabulary that question is written in, so a mismatch is a typed, reportable
outcome — `CAPABILITY_NOT_SUPPORTED` — rather than a stack trace three layers down
when a CLI adapter is handed a `response_schema` it has no way to honour.

Deliberately separate from `ProviderHealth` (`backend.app.llm.failures`): a
capability is a *static* fact about an adapter's shape — a CLI subprocess cannot
stream token deltas the way an HTTP SSE endpoint can — while health is the
*runtime* answer to "is it reachable right now?". A provider can be perfectly
capable and momentarily unavailable, and collapsing the two would make a
misconfigured API key look like a missing feature. The router filters on
capability first (does this provider even fit the task?) and consults health
second (can it serve it now?).
"""
from enum import StrEnum


class Capability(StrEnum):
    """One thing a provider either can or cannot do (§7-8).

    A member is declared when it is something a task legitimately *requires* and a
    provider legitimately *lacks* — the intersection is what the router filters on.
    Every provider declares its own set in `LLMProviderMetadata.capabilities`; an
    empty intersection with a task's requirements is `CAPABILITY_NOT_SUPPORTED`.
    """

    # The floor: the provider can turn a prompt into text. Every provider claims
    # it — a provider that cannot generate is not a provider — but it is named so a
    # task can state it rather than assume it.
    TEXT_GENERATION = "TEXT_GENERATION"
    # It can yield incremental token deltas as they are produced, not only a final
    # answer. The chat surface needs it; a one-shot structured extraction does not.
    STREAMING = "STREAMING"
    # It can be asked to return JSON matching a schema, and does so as a first-class
    # request feature rather than by prompt cajoling. A CLI adapter that only prints
    # free text does not claim it, so the router never hands it a `response_schema`.
    STRUCTURED_OUTPUT = "STRUCTURED_OUTPUT"
    # It can be given typed tool definitions and propose tool calls. Distinct from
    # STRUCTURED_OUTPUT: a tool proposal is a *request to act*, validated and
    # executed by the application, never by the provider (CLAUDE.md).
    TOOLS = "TOOLS"
    # It can resume a prior conversation from a provider-issued session id, so a
    # multi-turn exchange need not resend the whole history. The Claude CLI has it;
    # a stateless HTTP completion does not.
    SESSION_RESUME = "SESSION_RESUME"
    # It honours a reasoning-effort control (a thinking budget, an effort tier).
    # Named so a task that wants deeper reasoning can ask, and be told when nobody
    # can answer rather than having the parameter silently dropped.
    REASONING_CONTROL = "REASONING_CONTROL"
    # It accepts a distinct system instruction separate from the user turn. Most do;
    # a bare completion endpoint that only takes one prompt string does not, and a
    # task relying on a system role must know before it sends.
    SYSTEM_INSTRUCTIONS = "SYSTEM_INSTRUCTIONS"
    # It reports token counts (prompt/completion) for a request. Absence is honest,
    # not zero: a CLI that never prints usage yields `null` counts, and telemetry
    # records the unknown rather than inventing a 0 that a dashboard would sum.
    TOKEN_USAGE = "TOKEN_USAGE"  # noqa: S105 — a capability name, not a credential
    # It reports a monetary cost for a request. Rare — most local and CLI providers
    # cannot — so cost is `null` unless a provider genuinely knows it.
    COST_USAGE = "COST_USAGE"
    # It runs on the local machine (a loopback OpenAI-compatible server, a CLI on
    # this host) rather than sending the prompt to a third party. This is what a
    # `LOCAL_ONLY` privacy class filters on: the candidate's data never leaves the
    # box (docs/LLM_PROVIDER_ARCHITECTURE.md §12).
    LOCAL_EXECUTION = "LOCAL_EXECUTION"


# The capability every provider must claim: a provider that cannot generate text is
# not a provider. Named so `LLMProviderMetadata` can assert it rather than trusting
# each adapter to remember.
BASELINE_CAPABILITY = Capability.TEXT_GENERATION
