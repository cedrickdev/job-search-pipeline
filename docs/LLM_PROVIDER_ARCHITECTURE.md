# LLM Provider Architecture

## 1. Goal

The LLM must be a replaceable reasoning dependency.

Job Search Pipeline must support several connection modes without allowing any one provider to leak into business logic.

Required categories:

1. Claude Code CLI/session integration;
2. Codex CLI/session integration;
3. generic external API via `base_url + api_key + model`;
4. native/provider adapters where useful;
5. local OpenAI-compatible servers such as Ollama/LM Studio.

Because CLI interfaces evolve, CLI adapters must perform capability/version detection and isolate command-line details inside the adapter.

## 2. Important distinction

Claude Code and Codex are coding-agent/CLI environments, while external LLM gateways are API providers.

They must not be represented as the same transport internally.

Use:

```text
LLMProvider
  ├── CLIProvider
  │    ├── ClaudeCodeProvider
  │    └── CodexProvider
  └── APIProvider
       ├── OpenAICompatibleProvider
       ├── AnthropicProvider
       ├── GeminiProvider
       └── LocalOpenAICompatibleProvider
```

The public application interface remains common.

## 3. Provider-neutral contract

Suggested concepts:

```python
class LLMClient(Protocol):
    async def generate(
        self,
        request: LLMRequest,
    ) -> LLMResponse:
        ...

    async def stream(
        self,
        request: LLMRequest,
    ) -> AsyncIterator[LLMEvent]:
        ...

    async def healthcheck(self) -> ProviderHealth:
        ...

    def capabilities(self) -> LLMCapabilities:
        ...
```

`LLMRequest` should contain:

- messages;
- system instructions;
- model;
- temperature/reasoning policy where supported;
- structured output schema;
- tools allowed;
- metadata;
- timeout;
- max output tokens.

## 4. Connection profile

Persist a user/provider connection as:

```text
LLMConnection
- id
- user_id
- provider_type
- display_name
- base_url nullable
- model
- encrypted_api_key nullable
- cli_profile nullable
- enabled
- priority
- created_at
- updated_at
```

Do not persist provider-specific fields such as `claude_session_id` in generic conversation tables.

Use a generalized provider session:

```text
ProviderSession
- conversation_id
- provider_connection_id
- external_session_id
- metadata
```

## 5. Generic gateway support

The application must allow configuration like:

```text
Provider type: OpenAI-compatible
Base URL: https://gateway.example.com/v1
API key: ********
Model: external-model-name
```

Do not assume the hostname belongs to OpenAI.

Required configurable fields:

- `base_url`;
- `api_key`;
- `model`;
- optional custom headers;
- optional organization/project identifier;
- timeout;
- TLS verification policy must default to secure and must not be silently disabled.

The first API contract to support should be OpenAI-compatible chat/responses semantics through a contained adapter.

## 6. Provider router

A router chooses the provider based on:

- user preference;
- task capability;
- provider availability;
- model requirements;
- cost limits;
- latency policy;
- privacy policy;
- fallback configuration.

Example:

```text
Resume tailoring → structured-output capable model
Chat → preferred interactive model
Interview simulation → streaming model
Cheap extraction → small/low-cost model
```

The router must not silently switch to a provider that violates the user's privacy or cost policy.

## 7. Fallbacks

Fallbacks should be explicit and auditable.

Example:

1. preferred provider unavailable;
2. router checks allowed fallback list;
3. execution uses fallback;
4. run records `fallback_from` and `fallback_reason`.

## 8. Structured outputs

High-impact tasks require schema validation.

Examples:

- application decision;
- eligibility extraction;
- CV rewrite;
- interview score;
- action proposals.

Invalid model output:

1. does not mutate state;
2. may be retried with constrained repair;
3. ultimately fails with a typed error.

## 9. Tool permissions

LLM reasoning and tool execution must be separated.

The model may propose:

```json
{
  "type": "regenerate_resume",
  "application_id": "...",
  "args": {}
}
```

Application services validate:

- authorization;
- current state;
- user policy;
- limits;
- input schema.

Only then is a tool/action executed.

## 10. Prompt architecture

Prompts should be versioned by task.

Suggested structure:

```text
llm/prompts/
  opportunity_analysis/
  matching/
  resume_tailoring/
  cover_letter/
  interview/
  chat/
```

Each prompt version records:

- name;
- version;
- schema version;
- model-independent instructions;
- tests/evaluation fixtures.

## 11. Context management

Do not dump the entire candidate and opportunity database into every request.

Build task-specific context:

- only relevant candidate evidence;
- normalized opportunity requirements;
- company facts;
- user policy;
- prior conversation summary where required.

Keep access to raw source data for traceability.

## 12. Cost and token telemetry

For each LLM call record when available:

- provider;
- model;
- prompt/input tokens;
- output tokens;
- cached tokens;
- latency;
- estimated cost;
- request purpose;
- success/failure.

## 13. Privacy

The UI must disclose which provider receives candidate data.

A user should be able to choose:

- local-only;
- one external provider;
- selected provider fallback set.

Sensitive credential values must never be returned to the frontend after storage.

## 14. Migration from V1

V1 currently contains Claude-specific concepts plus Ollama/LM Studio support.

Migration order:

1. define generic request/response/capability schemas;
2. wrap the current Claude CLI implementation behind `ClaudeCodeProvider`;
3. wrap the existing OpenAI-compatible local implementation;
4. rename generic session persistence;
5. add `CodexProvider`;
6. add user-configurable OpenAI-compatible gateway;
7. remove provider-specific imports from chat/application modules.

Do not remove the working V1 paths until adapter parity tests pass.
