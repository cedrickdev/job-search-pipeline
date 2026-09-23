"""The provider-neutral LLM platform (Phase 11).

The boundary that turns "the LLM" into a replaceable infrastructure dependency: a
business service composes a typed `LLMRequest` and hands it to the `LLMRouter`,
which selects an `LLMProvider` from the registry and runs it. Provider specifics —
a Claude CLI subprocess, an OpenAI-compatible HTTP call, a local server — live only
in `providers`, so no business code branches on which provider it holds
(docs/LLM_PROVIDER_ARCHITECTURE.md §3, CLAUDE.md).

Import the contract and the failure vocabulary from here; reach into the submodules
only for a concrete provider or the router composition.
"""
from backend.app.llm.capabilities import BASELINE_CAPABILITY, Capability
from backend.app.llm.contracts import (
    DEFAULT_OUTPUT_CAP_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    FinishReason,
    LLMMessage,
    LLMProvider,
    LLMProviderMetadata,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    MessageRole,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderTransport,
    SessionContext,
    StreamEventType,
    StructuredOutputSpec,
    TaskPurpose,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    validate_stream_order,
)
from backend.app.llm.failures import (
    LLMError,
    LLMFailureCode,
    classify_provider_failure,
    redact_secrets,
)
from backend.app.llm.registry import (
    LLMProviderRegistry,
    LLMRegistryError,
    LLMRegistryErrorCode,
)

__all__ = [
    "BASELINE_CAPABILITY",
    "Capability",
    "DEFAULT_OUTPUT_CAP_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "FinishReason",
    "LLMError",
    "LLMFailureCode",
    "LLMMessage",
    "LLMProvider",
    "LLMProviderMetadata",
    "LLMProviderRegistry",
    "LLMRegistryError",
    "LLMRegistryErrorCode",
    "LLMRequest",
    "LLMResponse",
    "LLMStreamEvent",
    "MessageRole",
    "ProviderHealth",
    "ProviderHealthStatus",
    "ProviderTransport",
    "SessionContext",
    "StreamEventType",
    "StructuredOutputSpec",
    "TaskPurpose",
    "TokenUsage",
    "ToolCall",
    "ToolDefinition",
    "classify_provider_failure",
    "redact_secrets",
    "validate_stream_order",
]
