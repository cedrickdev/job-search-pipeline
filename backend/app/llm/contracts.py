"""The typed contract every LLM provider speaks, and nothing provider-specific.

This is the boundary docs/LLM_PROVIDER_ARCHITECTURE.md §3 and CLAUDE.md draw: above
it, a service composes an `LLMRequest` out of typed messages and a purpose and hands
it to a provider; below it, an adapter turns that request into whatever its
transport needs — a CLI argv, an HTTP body — and returns an `LLMResponse` or a
stream of `LLMStreamEvent`s. Neither side names the other's world. A service never
sees an argv or a `base_url`; an adapter never sees a `CandidateProfile`.

Everything here is a frozen Pydantic value with `extra="forbid"`, the same contract
the domain uses (`backend.app.domain.base`) and for the same reason: an
LLM-produced payload that invents a field must fail rather than lose it silently.
These models are *not* domain models — they live in the infrastructure layer, so
this module is free to be imported by adapters that also import `httpx` or
`subprocess`, which the domain purity test forbids under `backend/app/domain`.

`LLMProvider` at the bottom is the Protocol the router holds. It is shaped after
`OpportunitySource` and `CompanyDiscoveryProvider` so the mental model transfers: a
provider **describes itself** (`metadata`), **generates** (one-shot and streaming),
and **can be probed** (`healthcheck`). A failure is raised as `LLMError`
(`backend.app.llm.failures`), never returned as a sentinel, because a caller that
forgot to check a sentinel would treat a failure as an empty answer.
"""
from collections.abc import AsyncIterator, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.llm.capabilities import BASELINE_CAPABILITY, Capability

# How long, in seconds, a single provider call may run before it is abandoned. A
# ceiling the adapter enforces (a subprocess deadline, an httpx timeout); the router
# may lower it per request but never silently raise it past what a provider allows.
DEFAULT_TIMEOUT_SECONDS: float = 120.0

# The most output bytes an adapter will accumulate before it stops with
# `OUTPUT_LIMIT_EXCEEDED`. Mirrors V1's `server/chat.py` 256 KiB cap: a runaway
# generation must not exhaust memory, and a truncated answer is a failure, never a
# silently shortened success.
DEFAULT_OUTPUT_CAP_BYTES: int = 256 * 1024


class LLMValue(BaseModel):
    """Frozen, closed base for every value in the LLM contract.

    Not `DomainModel`: this layer is infrastructure, and importing the domain base
    would drag the LLM contract into the domain's dependency graph, which the purity
    test polices. The config is the same on purpose — `frozen` makes a request a
    value that can be logged and retried without a defensive copy, and
    `extra="forbid"` makes a hallucinated key on a parsed response an error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class MessageRole(StrEnum):
    """Who a message in the conversation is from (§4).

    Four roles, matching the shape every provider — chat API, CLI, local server —
    already speaks, so an adapter maps them without inventing a fifth. `TOOL` carries
    the result of a tool the application ran, fed back in for the model to continue;
    it is never a tool the provider ran itself, because the provider proposes and the
    application executes (CLAUDE.md).
    """

    SYSTEM = "SYSTEM"
    USER = "USER"
    ASSISTANT = "ASSISTANT"
    TOOL = "TOOL"


class LLMMessage(LLMValue):
    """One turn in the conversation handed to a provider.

    Deliberately text-plus-metadata rather than a provider's rich content-block
    union: the platform's tasks are text tasks, and a provider that wants blocks
    builds them from this in its adapter. `name` distinguishes a tool result's
    source when `role` is `TOOL`; `tool_call_id` correlates a `TOOL` message with the
    `ToolCall` it answers, so a multi-tool turn is unambiguous.
    """

    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def _tool_fields_belong_to_tool_messages(self) -> Self:
        if self.tool_call_id is not None and self.role is not MessageRole.TOOL:
            raise ValueError("tool_call_id belongs only to a TOOL message")
        return self

    @classmethod
    def system(cls, content: str) -> Self:
        return cls(role=MessageRole.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Self:
        return cls(role=MessageRole.USER, content=content)

    @classmethod
    def assistant(cls, content: str) -> Self:
        return cls(role=MessageRole.ASSISTANT, content=content)


class ToolDefinition(LLMValue):
    """A tool the model may propose calling, described in provider-neutral JSON.

    `parameters` is a JSON Schema object. The application decides what the tool
    *does* — this only tells the model the tool exists and what shape its arguments
    take. A provider without `Capability.TOOLS` never receives these: the router
    refuses the task before it reaches an adapter that would drop them.
    """

    name: Annotated[str, Field(min_length=1)]
    description: str
    parameters: Mapping[str, Any] = Field(default_factory=dict)


class ToolCall(LLMValue):
    """A tool invocation the model proposed — a request to act, not an action.

    Returned inside an `LLMResponse` or emitted as a `TOOL_PROPOSAL` stream event.
    The application validates `arguments` against the tool's schema and decides
    whether to run it; nothing here executes anything (CLAUDE.md: LLM output proposes
    typed actions, the application validates and executes them).
    """

    id: str
    name: Annotated[str, Field(min_length=1)]
    arguments: Mapping[str, Any] = Field(default_factory=dict)


class StructuredOutputSpec(LLMValue):
    """A request for JSON matching a schema, validated by the layer, not the model.

    `schema` is the JSON Schema the response must satisfy. `name` labels it for
    providers that want one. The provider is asked to produce conforming JSON, but
    the layer re-validates the result independently and raises
    `STRUCTURED_OUTPUT_INVALID` on a mismatch (§42) — a provider's claim to have
    honoured a schema is never taken on trust.
    """

    name: Annotated[str, Field(min_length=1)]
    schema_: Mapping[str, Any] = Field(alias="schema")
    # Whether a single bounded repair attempt is allowed on a first invalid answer
    # (§42). One, never a loop: an unbounded repair turns a broken provider into a
    # spend spiral.
    allow_repair: bool = True

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class TaskPurpose(StrEnum):
    """Why the platform is calling an LLM (§40).

    A closed set, because a purpose selects a prompt and a routing preference and
    feeds a telemetry dimension — all three break if it is a free string. A new use
    case adds a member here. Phase 11 wires only the two Phase 10 already needs
    (résumé and cover-letter tailoring) plus the chat and interview-prep purposes V1
    carries; the rest are declared as the vocabulary later phases will fill.
    """

    RESUME_TAILORING = "RESUME_TAILORING"
    COVER_LETTER = "COVER_LETTER"
    CAREER_CHAT = "CAREER_CHAT"
    INTERVIEW_PREP = "INTERVIEW_PREP"
    # A call with no product-level purpose — a healthcheck probe, a test. Named so a
    # purpose is never absent and telemetry never has a null dimension.
    GENERIC = "GENERIC"


class SessionContext(LLMValue):
    """Enough to continue a prior exchange, when a provider can (§38).

    `external_session_id` is the provider's own handle for a conversation — the
    Claude CLI's `--resume` id, for one — and is nullable because a stateless
    provider has none and a first turn has not been given one yet. The router pairs
    this with a stored `ProviderSession` (`backend.app.llm.sessions`); the request
    only needs to say "resume this if you can".
    """

    external_session_id: str | None = None


class LLMRequest(LLMValue):
    """Everything a provider needs for one call, and nothing about which provider.

    Assembled by a service (often from a `PromptTemplate`) and handed to the router,
    which chooses a provider and passes it down unchanged. `model` is optional
    because a connection carries a default; a request that names one overrides it.
    `max_output_tokens`, `temperature` and `reasoning_effort` are hints an adapter
    maps to its transport or ignores if its provider has no such control — with
    `reasoning_effort` gated behind `Capability.REASONING_CONTROL` so a task that
    depends on it is routed only to a provider that has it.
    """

    messages: Annotated[tuple[LLMMessage, ...], Field(min_length=1)]
    system: str | None = None
    model: str | None = None
    purpose: TaskPurpose = TaskPurpose.GENERIC
    max_output_tokens: Annotated[int, Field(gt=0)] | None = None
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None
    reasoning_effort: str | None = None
    structured_output: StructuredOutputSpec | None = None
    tools: tuple[ToolDefinition, ...] = ()
    session: SessionContext | None = None
    timeout_seconds: Annotated[float, Field(gt=0)] = DEFAULT_TIMEOUT_SECONDS
    # A stable prompt provenance stamp (`name/version`), recorded on the telemetry
    # run so an answer can be traced to the prompt that produced it (§39, §56).
    prompt_name: str | None = None
    prompt_version: str | None = None

    @model_validator(mode="after")
    def _tool_messages_have_ids(self) -> Self:
        for message in self.messages:
            if message.role is MessageRole.TOOL and message.tool_call_id is None:
                raise ValueError("a TOOL message must carry the tool_call_id it answers")
        return self

    def required_capabilities(self) -> frozenset[Capability]:
        """The capabilities a provider must have to serve this request.

        Derived from the request's shape, so the router filters on fact rather than
        on a caller's say-so: asking for structured output *is* requiring
        `STRUCTURED_OUTPUT`, and the two cannot drift. `TEXT_GENERATION` is always
        required — every request produces text.
        """
        needed = {BASELINE_CAPABILITY}
        if self.structured_output is not None:
            needed.add(Capability.STRUCTURED_OUTPUT)
        if self.tools:
            needed.add(Capability.TOOLS)
        if self.reasoning_effort is not None:
            needed.add(Capability.REASONING_CONTROL)
        if self.system is not None:
            needed.add(Capability.SYSTEM_INSTRUCTIONS)
        if self.session is not None and self.session.external_session_id is not None:
            needed.add(Capability.SESSION_RESUME)
        return frozenset(needed)


class TokenUsage(LLMValue):
    """What a call cost in tokens and money, with the unknown kept as null (§58).

    Every field is nullable and defaults to `None`, and that is the contract, not an
    oversight: a CLI that never prints usage yields `None`, which a telemetry query
    treats as "unknown" rather than summing a fabricated 0 into a total. Cost is
    rarer still — most local and CLI providers cannot know it — so it too is null
    unless a provider genuinely reports it.
    """

    prompt_tokens: Annotated[int, Field(ge=0)] | None = None
    completion_tokens: Annotated[int, Field(ge=0)] | None = None
    total_tokens: Annotated[int, Field(ge=0)] | None = None
    cost_usd: Annotated[float, Field(ge=0.0)] | None = None


class FinishReason(StrEnum):
    """Why a completion ended (§60).

    Told apart from a failure: a `STOP` is a whole answer, a `LENGTH` was cut at the
    token budget, a `TOOL_CALLS` stopped to propose tools. A provider that does not
    report one yields `STOP` by default only when the answer was in fact complete;
    an adapter that cannot tell uses `UNKNOWN` rather than guessing `STOP`.
    """

    STOP = "STOP"
    LENGTH = "LENGTH"
    TOOL_CALLS = "TOOL_CALLS"
    CONTENT_FILTERED = "CONTENT_FILTERED"
    UNKNOWN = "UNKNOWN"


class LLMResponse(LLMValue):
    """The whole answer to a one-shot call.

    `text` is the generated content — empty string, never null, when the model only
    proposed tools. `tool_calls` are proposals for the application to validate and
    run. `structured_output` is present only when the request asked for it and the
    layer validated it against the schema; a request that asked and got nothing valid
    never produces a response — it raises `STRUCTURED_OUTPUT_INVALID`. `model` and
    `external_session_id` report what actually served the call, which may differ from
    what was requested (a connection's default model, a freshly issued session id).
    """

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    structured_output: Mapping[str, Any] | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    finish_reason: FinishReason = FinishReason.STOP
    model: str | None = None
    external_session_id: str | None = None


class StreamEventType(StrEnum):
    """The kinds of event a streaming call emits (§6).

    An ordered life: exactly one `STARTED` first and exactly one terminal event last
    (`COMPLETED` on success, `ERROR` on failure), with any number of `TEXT_DELTA`,
    `TOOL_PROPOSAL` and `USAGE` events between. A consumer that sees `ERROR` gets no
    `COMPLETED`, so a broken stream is never mistaken for a finished one.
    """

    STARTED = "STARTED"
    TEXT_DELTA = "TEXT_DELTA"
    TOOL_PROPOSAL = "TOOL_PROPOSAL"
    USAGE = "USAGE"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


class LLMStreamEvent(LLMValue):
    """One event in a streaming generation.

    A single tagged type rather than a union of six models, because a consumer reads
    `type` and then the one field it implies, and a union would force a match on
    every branch to reach the delta it wants. The validator ties each payload to its
    type so an impossible event — a `TEXT_DELTA` with no text, an `ERROR` with no
    code — cannot be constructed. `external_session_id` rides on `STARTED` (or
    `COMPLETED`) so a caller can persist the session a resume will need.
    """

    type: StreamEventType
    text: str | None = None
    tool_call: ToolCall | None = None
    usage: TokenUsage | None = None
    response: LLMResponse | None = None
    error_code: str | None = None
    error_detail: str | None = None
    external_session_id: str | None = None

    @model_validator(mode="after")
    def _payload_matches_type(self) -> Self:
        if self.type is StreamEventType.TEXT_DELTA and not self.text:
            raise ValueError("a TEXT_DELTA event must carry non-empty text")
        if self.type is StreamEventType.TOOL_PROPOSAL and self.tool_call is None:
            raise ValueError("a TOOL_PROPOSAL event must carry a tool_call")
        if self.type is StreamEventType.USAGE and self.usage is None:
            raise ValueError("a USAGE event must carry usage")
        if self.type is StreamEventType.ERROR and self.error_code is None:
            raise ValueError("an ERROR event must carry an error_code")
        return self

    @classmethod
    def started(cls, *, external_session_id: str | None = None) -> Self:
        return cls(type=StreamEventType.STARTED,
                   external_session_id=external_session_id)

    @classmethod
    def text_delta(cls, text: str) -> Self:
        return cls(type=StreamEventType.TEXT_DELTA, text=text)

    @classmethod
    def completed(cls, response: LLMResponse) -> Self:
        return cls(type=StreamEventType.COMPLETED, response=response,
                   external_session_id=response.external_session_id)

    @classmethod
    def errored(cls, code: str, detail: str) -> Self:
        return cls(type=StreamEventType.ERROR, error_code=code, error_detail=detail)


class ProviderTransport(StrEnum):
    """How an adapter reaches its provider (§9).

    Descriptive, and — like `CompanyProviderType` — never dispatched on: the moment
    the router branched on this, the "no hard-coded provider" rule would be back in
    another shape. It classifies for a status page and decides nothing. `CLI` runs a
    local subprocess; `OPENAI_COMPATIBLE_API` speaks HTTP to a remote endpoint;
    `LOCAL_OPENAI_COMPATIBLE` speaks HTTP to a loopback server on this machine.
    """

    CLI = "CLI"
    OPENAI_COMPATIBLE_API = "OPENAI_COMPATIBLE_API"
    LOCAL_OPENAI_COMPATIBLE = "LOCAL_OPENAI_COMPATIBLE"


class ProviderHealthStatus(StrEnum):
    """The runtime answer to "can this provider serve a request now?" (§37).

    Separate from `Capability` on purpose (see `backend.app.llm.capabilities`): a
    provider can be fully capable and momentarily `UNAVAILABLE`, or capable and
    `AUTH_REQUIRED` until a key is set. `UNKNOWN` is the honest state before anything
    has probed it — distinct from `UNAVAILABLE`, which is a probe that failed.
    """

    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    MISCONFIGURED = "MISCONFIGURED"

    @property
    def is_usable(self) -> bool:
        """Whether the router should consider a provider in this state at all.

        `HEALTHY` and `DEGRADED` are usable — degraded is slow or partial, not down.
        `UNKNOWN` is usable too: a provider nobody has probed yet is given the
        benefit of the doubt, because refusing to try is how a healthy provider that
        was simply never healthchecked becomes invisible.
        """
        return self in (ProviderHealthStatus.HEALTHY, ProviderHealthStatus.DEGRADED,
                        ProviderHealthStatus.UNKNOWN)


class ProviderHealth(LLMValue):
    """What a provider last said about its own reachability.

    `detail` is a secret-free sentence for an operator, produced through
    `backend.app.llm.failures.redact_secrets`, never a raw provider message.
    `latency_ms` is worth carrying on a failure too: a 30-second timeout and an
    instant refusal are different problems with the same status.
    """

    status: ProviderHealthStatus
    detail: str | None = None
    latency_ms: Annotated[int, Field(ge=0)] | None = None

    @property
    def is_usable(self) -> bool:
        return self.status.is_usable

    @classmethod
    def unknown(cls) -> Self:
        return cls(status=ProviderHealthStatus.UNKNOWN)


# The return type of `stream`: an async iterator of events. Named so the Protocol
# and every adapter refer to one alias rather than restating the generic, and so a
# reader sees "a stream of events" rather than the machinery.
LLMStreamGen = AsyncIterator[LLMStreamEvent]


class LLMProviderMetadata(LLMValue):
    """Who a provider is and what it can do — what the router filters and orders on.

    `provider_key` is the stable identity a `LLMRun` and a `ProviderSession` are
    stamped with, so a rename would orphan the telemetry recorded under the old one.
    `capabilities` is the static set the router intersects with a request's
    requirements. `local` is derived from the transport but stated here so a privacy
    filter reads one field rather than re-deciding from the transport enum. `priority`
    orders providers when nothing more specific chooses — lower runs first, ties
    broken by key so ordering is total and a run is reproducible.
    """

    provider_key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_\-]*$")]
    display_name: Annotated[str, Field(min_length=1)]
    transport: ProviderTransport
    capabilities: frozenset[Capability] = frozenset({BASELINE_CAPABILITY})
    default_model: str | None = None
    priority: Annotated[int, Field(ge=0)] = 100

    @model_validator(mode="after")
    def _generation_is_claimed(self) -> Self:
        if BASELINE_CAPABILITY not in self.capabilities:
            raise ValueError(
                f"a provider must claim {BASELINE_CAPABILITY}; it is what a provider is")
        return self

    @property
    def local(self) -> bool:
        """Whether the provider executes on this machine (§89).

        Read from the declared capability, which the adapter sets from its *validated*
        address rather than its label — a loopback OpenAI-compatible server is local,
        a remote one is not, regardless of what a connection is named.
        """
        return Capability.LOCAL_EXECUTION in self.capabilities

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def supports_all(self, capabilities: frozenset[Capability]) -> bool:
        return capabilities <= self.capabilities

    def missing(self, capabilities: frozenset[Capability]) -> frozenset[Capability]:
        """The required capabilities this provider lacks — for a clear refusal."""
        return frozenset(capabilities) - self.capabilities


@runtime_checkable
class LLMProvider(Protocol):
    """A replaceable LLM, behind one shape (docs/LLM_PROVIDER_ARCHITECTURE.md §3).

    Four members, and nothing that reveals a transport. A provider:

    - **describes itself** through `metadata`, which the registry and router filter
      and order on;
    - **generates** one-shot via `generate`, returning an `LLMResponse`;
    - **streams** via `stream`, yielding `LLMStreamEvent`s;
    - **can be probed** through `healthcheck`, so a settings page need not run a real
      generation to show a provider's state.

    `generate` and `stream` raise `LLMError` on failure — never a sentinel — so a
    caller cannot mistake a failure for an empty answer. `healthcheck` returns a
    `ProviderHealth` and raises nothing: a provider being down is data, not an
    exception the settings page has to catch.

    Like `OpportunitySource`, this Protocol has a non-method member (`metadata`), so
    `isinstance` works and `issubclass` raises `TypeError`.
    """

    @property
    def metadata(self) -> LLMProviderMetadata: ...

    async def generate(self, request: LLMRequest) -> LLMResponse: ...

    def stream(self, request: LLMRequest) -> "LLMStreamGen": ...

    async def healthcheck(self) -> ProviderHealth: ...


def validate_stream_order(events: Sequence[LLMStreamEvent]) -> None:
    """Assert a stream obeyed its life: one STARTED, one terminal, order kept.

    A test and a defensive adapter helper, not something the hot path runs on every
    event. It makes the ordering rule in `StreamEventType` checkable rather than
    merely documented: a stream that emitted two `STARTED`s, or content after its
    terminal event, is a protocol error a consumer should never have to survive.
    """
    if not events:
        raise ValueError("a stream must emit at least STARTED and a terminal event")
    if events[0].type is not StreamEventType.STARTED:
        raise ValueError("a stream must open with STARTED")
    terminal = {StreamEventType.COMPLETED, StreamEventType.ERROR}
    if events[-1].type not in terminal:
        raise ValueError("a stream must end with COMPLETED or ERROR")
    started = sum(1 for e in events if e.type is StreamEventType.STARTED)
    if started != 1:
        raise ValueError("a stream must carry exactly one STARTED event")
    ended = sum(1 for e in events if e.type in terminal)
    if ended != 1:
        raise ValueError("a stream must carry exactly one terminal event")
