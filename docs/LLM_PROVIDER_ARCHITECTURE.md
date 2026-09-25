# LLM Provider Architecture

Built in Phase 11. This document is the reference for the provider-neutral LLM
platform: the boundary that turns "the LLM" into a replaceable infrastructure
dependency, so no business module ever branches on *which* provider it holds. The
settings surface a user configures connections through — the write side — is
documented separately in [LLM Connections](./LLM_CONNECTIONS.md); this document is the
runtime: the contract, the router, the adapters and the telemetry that sit under it.

The whole layer lives in `backend/app/llm/`. The one rule it exists to keep:

> Business code composes a typed `LLMRequest` and hands it to the router. It never
> names a provider, imports a provider SDK, or reads a provider-specific field. The
> single place allowed to know a provider type maps to a concrete adapter is
> `backend/app/llm/factory.py`.

```
                       business service
                             │  builds an LLMRequest + a RoutingPolicy
                             ▼
   LLMTelemetryRecorder ── wraps ──▶ LLMRouter ──filters/orders──▶ LLMProviderRegistry
        │ writes one LLMRun                 │ runs the first candidate
        ▼                                   ▼
     llm_runs                    ┌── LLMProvider (Protocol) ──┐
                                 │                            │
                          ClaudeCodeProvider          OpenAICompatibleProvider
                          CodexProvider                 (local or remote)
                             (CLI transport)             (HTTP transport)
```

## 1. The contract every provider speaks

`backend/app/llm/contracts.py` is the boundary. Above it a service builds an
`LLMRequest` out of typed messages and a `TaskPurpose`; below it an adapter turns that
request into whatever its transport needs — a CLI argv, an HTTP body — and returns an
`LLMResponse` or a stream of `LLMStreamEvent`s. Neither side names the other's world: a
service never sees a `base_url`, an adapter never sees a `CandidateProfile`.

`LLMProvider` is the `Protocol` the router holds — four members and nothing that
reveals a transport. A provider **describes itself** (`metadata`), **generates**
(`generate` one-shot, `stream` incremental), and **can be probed** (`healthcheck`).
`generate` and `stream` raise `LLMError` on failure, never a sentinel, so a caller
cannot mistake a failure for an empty answer; `healthcheck` returns a `ProviderHealth`
and raises nothing, because a provider being down is data a settings page renders, not
an exception it must catch.

Every value in the contract is a frozen Pydantic model with `extra="forbid"` — the
same discipline the domain uses, for the same reason: an LLM-produced payload that
invents a field fails rather than losing it silently. These are *infrastructure*
values, not domain models, so the module is free to be imported by adapters that also
import `httpx` or `subprocess`, which the domain purity test forbids under
`backend/app/domain`.

`LLMRequest` carries the messages, an optional `system` instruction, an optional
`model` (a connection carries a default; a request that names one overrides it), the
`TaskPurpose`, `max_output_tokens`/`temperature`/`reasoning_effort` hints, an optional
`StructuredOutputSpec`, tool definitions, a `SessionContext`, a timeout, and the
`prompt_name`/`prompt_version` provenance stamp. Its `required_capabilities()` is
**derived from its shape**, so the router filters on fact rather than a caller's
say-so: asking for structured output *is* requiring `STRUCTURED_OUTPUT`, and the two
cannot drift.

## 2. Transport is a classification, never a branch

`ProviderTransport` (in `contracts.py`) has three members — `CLI`,
`OPENAI_COMPATIBLE_API`, `LOCAL_OPENAI_COMPATIBLE`. Like `CompanyProviderType`, it is
**descriptive and never dispatched on**: the moment the router branched on it, the
"no hard-coded provider" rule would be back in another shape. It classifies for a
status page and decides nothing.

The physical tree the design called for is what shipped, configured rather than deeply
subclassed:

```
LLMProvider (Protocol)
  ├── ClaudeCodeProvider        (CLI transport, its own adapter)
  ├── CodexProvider             (CLI transport, a separate adapter — not a subclass)
  └── OpenAICompatibleProvider  (one adapter, configured local or remote)
```

The two CLI adapters are deliberately **not** related by inheritance: they share only
the `SafeCliRunner` boundary, and coupling them would make a change to Claude's
stream-json parsing silently reshape Codex. The two OpenAI-compatible transports —
a hosted gateway and a loopback server — are **one** adapter configured differently,
because Ollama, LM Studio, a self-hosted vLLM and a hosted gateway all speak the same
`/chat/completions` wire format. Native Anthropic/Gemini adapters are not in Phase 11;
`LLMProviderType` is where a later phase adds them.

## 3. Capability filtering

`backend/app/llm/capabilities.py` is the vocabulary a task's requirement is written
in. A résumé service does not ask "is this Claude?"; it asks "can this provider return
structured output?" and lets the router refuse a provider that cannot, so a mismatch
is a typed `CAPABILITY_NOT_SUPPORTED`, not a stack trace three layers down. The set:
`TEXT_GENERATION` (the floor every provider claims), `STREAMING`, `STRUCTURED_OUTPUT`,
`TOOLS`, `SESSION_RESUME`, `REASONING_CONTROL`, `SYSTEM_INSTRUCTIONS`, `TOKEN_USAGE`,
`COST_USAGE`, `LOCAL_EXECUTION`.

Capability is deliberately separate from health: a capability is a *static* fact about
an adapter's shape (a read-only CLI cannot honour a `response_schema`), health is the
*runtime* answer to "reachable now?". The router filters on capability first and
consults health second. The adapters declare honestly — the Claude CLI adapter does
**not** claim `STRUCTURED_OUTPUT`, `TOOLS` or `REASONING_CONTROL`, because it is run
read-only with stream-json text, and claiming a capability it does not implement would
route a task to a dead end.

## 4. The connection profile

`backend/app/llm/connection.py` is the persisted half: an `LLMConnection` is what a
user configured for one provider — which `LLMProviderType`, at which `base_url`, with
which `model`, and (for a hosted gateway) an API key kept **encrypted at rest**.
Two shape rules are enforced on the value object rather than left to the API, because
a value that can be constructed wrong is one a migration or a test will eventually
construct wrong:

- **A CLI connection carries no credential, no base URL and no custom headers.** Claude
  Code and Codex authenticate themselves and run a local binary; injecting a key or a
  URL would be meaningless and, for the `ANTHROPIC_*` case, would violate the §1
  security invariant. The model refuses it, so no code path stores one.
- **An API connection carries a base URL.** An OpenAI-compatible endpoint cannot be
  reached without one, and the hostname is never assumed — so it is required and never
  defaulted.

The credential is never a plaintext field. `encrypted_api_key` is the ciphertext and
`secret_version` names the key that produced it — both-or-neither, enforced by a
validator and by a database CHECK. The plaintext exists only for the instant the
factory decrypts it to build a provider.

V1's provider-specific `claude_session_id` in a generic conversation table is exactly
the leak this phase removes. `backend/app/llm/sessions.py`'s `ProviderSession` is the
replacement: one row per `(connection, conversation)` holding the provider's own
handle under the neutral name `external_session_id`. Its id is *derived* from the
connection and the conversation key, so resuming refreshes the one row rather than
appending a second — the idempotence the Claude CLI's stale-session recovery depends
on. A stateless provider simply never sets the handle.

## 5. Generic gateway support

`OpenAICompatibleProvider` (`backend/app/llm/providers/openai_compatible.py`) is the
one adapter for every OpenAI-compatible endpoint. What differs between a local server
and a hosted gateway is the transport classification and the credential:

- a **local** provider (`LOCAL_OPENAI_COMPATIBLE`) points at a loopback address, is
  keyless, and claims `LOCAL_EXECUTION` — the prompt never leaves the machine;
- a **remote** provider (`OPENAI_COMPATIBLE_API`) points at an `https` *public* host,
  carries a bearer credential, and does not claim `LOCAL_EXECUTION`.

The hostname is never assumed to be OpenAI's; `base_url`, `model` and optional custom
headers are all configurable. The SSRF gate (`net_policy`) runs in two stages, because
one static check cannot see what a hostname resolves to:

- **A static check at construction (`validate_base_url`).** Only `http`/`https`; the
  link-local/metadata (`169.254.169.254`), unspecified, multicast and reserved ranges
  refused for everyone; a remote provider forced onto `https` so a plaintext prompt
  never crosses a network; a `LOCAL_OPENAI_COMPATIBLE` provider forced onto a *loopback
  literal* (or `localhost`); and a remote provider refused a loopback or private-LAN
  *literal*, because a remote target must be public. A hostname cannot be classified
  without DNS, and a lookup here would be a side effect and a TOCTOU window, so it is
  left provisionally `REMOTE` and checked at call time instead.
- **A resolution check before every request (`resolve_and_validate_remote_host`).** A
  remote hostname is resolved through an injected `HostResolver` immediately before
  `generate`, `stream` and `healthcheck`, and the request is refused unless *every*
  resolved address is public. A loopback, private-LAN (RFC1918 / IPv6 ULA), link-local
  or metadata answer — even one among several — is a refusal, so a name pointed at
  `169.254.169.254` or `10.0.0.5` cannot reach inside the deployment. A resolver failure
  is a typed `PROVIDER_MISCONFIGURED` whose detail is composed from the table, never the
  resolver's own text, so a `gaierror` string cannot leak.

Two invariants follow:

- **Local means the same machine, not the same network.** Only a loopback address is
  `LOCAL_EXECUTION`, and therefore only loopback satisfies the `LOCAL_ONLY` privacy
  class. A private-LAN address is *not* local: it is off the box, so it is refused for a
  local provider, and it is not public, so it is refused for a remote one. `LOCAL_ONLY`
  is an honest "this prompt never leaves the machine", not "it stays on the LAN".
- **`LOCAL_EXECUTION` is set from the validated address class, never the label.** An
  "Ollama" connection whose URL resolves remote is remote, so a privacy filter reads
  the truth about where the data goes.

Redirects are disabled at the client (`follow_redirects=False`), so a `3xx` cannot
re-target a request at an address the policy never saw. A residual DNS-rebinding window
remains: resolution happens immediately before the request but the socket is not pinned
to the validated address, so a name that answers a public IP for the check and a private
IP for the connection is not fully closed — pinning the socket while preserving SNI is a
noted follow-up, and the layer does not claim complete rebinding protection.

Reserved headers (`host`, `content-length`, `authorization`, `content-type`) cannot be
overridden by a custom header — a user who set `Host` expecting it to route somewhere
is told it will not, rather than having it silently dropped. One contained
`httpx.AsyncClient` is built per call and closed with it, so a settings change takes
effect on the next request rather than after a restart. `http_transport` is the
`httpx.MockTransport` test seam, so the whole adapter is testable without a socket.

## 6. The provider router

`backend/app/llm/router.py` is the one place a request meets a provider. A service
builds an `LLMRequest` and a `RoutingPolicy` and calls `route`; the router filters the
registry by capability and privacy, orders the survivors by a fixed rule, and runs the
first.

**The ordering is deterministic.** Most specific first: (1) an explicit `provider_key`
pin on the policy; (2) the policy's `preferred_keys`, in the order given; (3)
everything else the filters left, by the registry's total order (priority, then key).
A request routed twice against an unchanged registry picks the same provider, because
every tie has a key tie-break — which is what makes a fallback chain reproducible
rather than a coin flip.

**Privacy is enforced before capability spends anything.** `PrivacyClass` is a
property of the *request*, decided by the caller from what the prompt carries, and is
required — there is no default, because guessing "external is fine" is exactly the
mistake that leaks a prompt:

- `LOCAL_ONLY` — the prompt may go only to a provider that runs on this machine *by
  validated address* — a loopback address or a CLI, never a private-LAN endpoint. The
  default for anything carrying candidate evidence.
- `EXTERNAL_ALLOWED` — the caller has judged the content safe to send off the machine.
- `SPECIFIC_CONNECTION_ONLY` — only the named connections, wherever they are, and never
  a fallback off them.

## 7. Fallbacks are explicit and recorded

Fallback is **off by default** (`allow_fallback=False`): switching models is a
decision a caller opts into, not something discovered after the fact. A provider whose
failure is *retryable* (a timeout, a rate limit, a transient outage) is retried within
itself a bounded number of times (`DEFAULT_MAX_ATTEMPTS_PER_PROVIDER = 2`); a provider
that still fails, or fails unretryably (a missing capability, a bad credential), hands
off to the next candidate **only if** the policy allowed it. When the winner was not
the first choice, the `RoutingOutcome` — and the telemetry run — record `fallback_from`
and a coded `fallback_reason`, so an operator can see the platform switched and why.

Streaming has a stricter rule: fallback is possible **only before the first byte**. A
stream that has already emitted a `TEXT_DELTA` cannot fall back, because the consumer
has seen tokens and replaying from a different provider would duplicate them.

A routing decision is inspectable without spending a call: `router.candidates(request,
policy)` returns the eligible providers in the order they would be tried.

## 8. Structured outputs

A `StructuredOutputSpec` asks a provider for JSON matching a schema, but the layer
**re-validates the result independently** — a provider's claim to have honoured a
schema is never taken on trust. A response that asked for structured output and got
nothing valid never becomes an `LLMResponse`; it raises `STRUCTURED_OUTPUT_INVALID`. A
single bounded repair attempt is allowed (`allow_repair`, default true) — one, never a
loop, because an unbounded repair turns a broken provider into a spend spiral. Invalid
output does not mutate state.

The document generator is where this rule earns its place: `LLMDocumentGenerator`
(`backend/app/documents/llm_generator.py`) re-validates the model's JSON into a
`ResumeDocument`/`CoverLetterDocument` and raises `STRUCTURED_OUTPUT_INVALID` on a
mismatch, never forwarding a `ValidationError`. The Evidence Guard stays *outside* the
provider — the service rejects invented candidate facts — so the truth guarantee does
not depend on the model behaving (see [ATS Documents](./ATS_DOCUMENTS.md)).

## 9. Tool permissions — the model proposes, the application executes

A `ToolDefinition` tells the model a tool exists and what shape its arguments take; a
`ToolCall` in a response (or a `TOOL_PROPOSAL` stream event) is a *request to act*, not
an action. Nothing in the LLM layer executes anything: the application validates a
proposal's authorization, current state, policy, limits and input schema, and only
then runs it. This is why the Claude CLI adapter runs with read-only tools
(`Read,Grep,Glob`) and Phase 11 does not widen that.

## 10. Prompt architecture

`backend/app/llm/prompts.py` versions prompts by task. A `PromptTemplate` records a
`name`, a `version`, a `schema_version` and model-independent instructions, and
renders into an `LLMRequest` stamped with `prompt_name`/`prompt_version` so an answer
can be traced to the prompt that produced it. Phase 11 ships `resume_tailoring/1.0`
and `cover_letter/1.0` (each carrying the truth rule and a JSON schema); the chat and
interview-prep purposes are declared in `TaskPurpose` as the vocabulary later phases
fill.

## 11. Failure normalization

`backend/app/llm/failures.py` turns *any* way a call can fail — a CLI subprocess that
exits non-zero, an HTTP 429, a local server that is not running, a response that does
not match its schema — into one of a fixed `LLMFailureCode` set. Business code above
the router catches `LLMError` and reads `.code`; it never inspects a provider-specific
exception, which keeps the "no `if provider == …`" rule true on the failure path too.
An unrecognised failure is `PROVIDER_INTERNAL_ERROR`, never a new ad-hoc string. The
code also tells the router whether a failure is `retryable`.

Two secret-safety rules hold at this boundary:

1. **A detail is composed from a fixed table, never forwarded from a provider.** A
   hosted API's 401 body can echo the very key that was rejected; a CLI's stderr can
   print the argv. `classify_provider_failure` reads the exception to pick a code and
   never copies its text out — the only thing ever appended is an HTTP status number.
2. **`redact_secrets` runs over any caller-supplied detail anyway** — belt and braces
   for the one string an adapter knew something specific enough to say — blanking
   declared credential values and the `sk-…`/`Bearer …`/`?api_key=` shapes a credential
   travels in.

## 12. Cost and token telemetry

`backend/app/llm/recorder.py` wraps `LLMRouter.route` and writes one `LLMRun`
(`backend/app/llm/telemetry.py`) per call. It lives *around* the router rather than
inside it, for the reason the router holds no repository: routing is a pure decision,
persistence is a side effect a service owns. Three properties hold:

- **Latency is measured on a monotonic clock, timestamps on the wall clock.** An NTP
  step mid-call must not make a call report a negative latency, so the duration comes
  from `time.monotonic` while `started_at`/`finished_at` come from the injected clock.
- **The unknown stays null.** Tokens and cost are copied straight off the response's
  `TokenUsage`, which is already null where a provider reported nothing — the recorder
  never substitutes a 0 that a dashboard would sum.
- **A failure is recorded, typed and secret-free, then re-raised.** The recorder does
  not swallow the error; a caller's own failure handling is unchanged by telemetry
  being on.
- **The run's id rides back on the outcome, so a caller can attribute provenance.** The
  recorder mints the `LLMRun`'s id, writes the row, and sets it on the `RoutingOutcome`
  as `run_id` — the caller reads it off the outcome rather than issuing a "latest run for
  this user" query a concurrent call could win. This is the seam Phase 14 uses to stamp
  the exact run onto a generated question, evaluation or summary: the interview adapter
  returns each artefact in an `InterviewLLMResult[T]` (value + `run_id`), and the service
  persists that id as a `SET NULL` FK to `llm_runs.id`. A run is honestly absent — `None`
  on the outcome — only where none was made (a call the router refused, or a deterministic
  fallback the platform authored without a provider).

A call the router *refused* before trying any provider (`NoProviderAvailable`) is not
an LLM call and gets no run — there was no provider to attribute one to. A `CANCELLED`
run is not counted against a provider, and a `TIMEOUT` is separated from a `FAILED`
because a deadline and a refusal are different operational facts.

## 13. Privacy and disclosure

The privacy classes above are the enforcement; the [LLM Connections](./LLM_CONNECTIONS.md)
settings surface is the disclosure. A user chooses local-only, one external provider or
a fallback set by configuring connections and a routing preference. **A sensitive
credential value is never returned to the frontend after storage** — the API surfaces
`has_api_key`, never the value or its ciphertext (see §21 below and the connections
doc). The credential is encrypted at rest with a master key that is never a database
column.

## 14. Migration from V1, and what is contained

The strangler order the design called for is what happened: the generic
request/response/capability contract came first, then the CLI and OpenAI-compatible
adapters wrapped V1's working mechanics behind it, then generic session persistence
replaced `claude_session_id`. V1's own `server/chat.py` is left untouched — the Phase
11 adapters are a *parallel* path over the same transport, and V1's chat, interview
prep and local-model streaming still run through V1's code. The parity that matters is
pinned by tests rather than asserted: the V1 subprocess and Ollama/LM Studio tests
still pass, and the Phase 10 prompt-injection regression still holds.

The **one** allowed provider-type switch is `backend/app/llm/factory.py`. Its
`LLMProviderFactory.create` is the only function permitted to branch on
`LLMProviderType`; it decrypts the stored credential at the instant a provider is built
and never before, and it builds a CLI provider without a base URL or a key — so there
is no path here that could inject the `ANTHROPIC_*` credential the §1 invariant forbids.
`backend/app/llm/bootstrap.py` composes a populated registry from a user's connections
per use, never a process-wide singleton, because a registry accumulates health as calls
run and a cached one would share that state between users.

## Security invariants (the non-negotiables)

- **§1 — the platform never injects `ANTHROPIC_API_KEY`.** `server/_env.py::child_env()`
  strips the entire `ANTHROPIC_*` namespace from every CLI subprocess, and no adapter
  passes a credential through the environment. The Claude CLI's auth stays CLI-managed.
  This is a V1 invariant Phase 11 must not weaken; a CLI connection is structurally
  incapable of storing a key.
- **Explicit argv, never a shell.** `SafeCliRunner` uses `create_subprocess_exec`, so a
  value inside a command can never be reinterpreted as a shell metacharacter. It also
  enforces a deadline, an output cap, and a kill-on-exit so a wedged binary cannot hold
  a request open or exhaust memory.
- **§21 — credentials encrypted at rest, key never in the database.** Fernet
  (AES-128-CBC + HMAC-SHA256) via `backend/app/llm/secrets.py`; the master key comes
  from `JOBSEARCH_LLM_SECRET_KEY` in the environment and is never a column, a response
  field, or a log line. A database dump holds ciphertext and a version tag and nothing
  that reads them. See [LLM Connections](./LLM_CONNECTIONS.md).
- **No raw-prompt endpoint.** There is no `POST /llm/complete` and no generic
  prompt-passthrough. A task that needs a model routes through its own service
  (documents, matching, chat), which builds a request from a versioned prompt.

## Tests

The layer is covered without a live LLM or a socket (CLAUDE.md §Testing): the router,
registry, capabilities, secrets, net_policy, failures and contracts have unit suites;
the OpenAI-compatible adapter is exercised through `httpx.MockTransport`; the CLI
adapters run against fake-binary scripts; and `tests/v2_llm.py::FakeProvider` is the
shared double a service test routes through. The API settings surface is tested in
`tests/test_v2_api_llm.py`, and the persistence layer on real PostgreSQL.
