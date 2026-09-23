# Career Chat — a safe control plane over V2

> Phase 13. The career chat lets a signed-in candidate talk to an LLM about their
> search, their applications and their documents, and — with one confirmation — drive
> the same application services the rest of the platform exposes. It is a *control
> plane*, not an autopilot, and this document is the contract that makes that word safe.

## The one rule: prose has zero authority

A language model produces text. In this system that text can *describe*, *explain* and
*propose*, but it can never *do*. Nothing an assistant writes in a sentence mutates
anything on the platform. The only thing that can cause a change is a **typed action
proposal** — a validated `ChatAction`, parsed out of the reply, shown to the user on a
confirm-gated card, and executed only after the user clicks Confirm.

Everything in this phase is an elaboration of that sentence. The prompt states it to the
model (`backend/app/chat/prompts.py`); the parser enforces it mechanically
(`backend/app/chat/parsing.py`); the validator and executor enforce it again at run time
(`backend/app/chat/validators.py`, `backend/app/chat/executor.py`); the frontend renders
prose and proposals in different places and treats them differently
(`frontend/app/pages/chat.vue`, `frontend/app/components/ChatActionCard.vue`).

## proposal != permission

A proposal is a *request to act*, never a grant. The model emitting one, and even a human
confirming one, does not by itself authorize anything. Between "Confirm" and any mutation,
a confirmed proposal is re-checked against the state of the world:

- **ownership** — every entity the action names is loaded through a `user_id`-scoped
  repository, so a proposal about another account's application, search or document reads
  as absent and is refused;
- **coarse domain state** — the validator rejects what is closed for good (a terminal
  application) before a service is ever called;
- **the service's own authoritative rules** — the executor calls the *same* application
  services the HTTP routes call, so a `SUBMIT_APPLICATION` re-runs the entire Phase 12
  execution gate (policy, eligibility, the rate budget, the submission gate) with no
  override and no shortcut; the evidence guard still governs a document generation.

The chat is one more caller of those boundaries, never a way around them.

## What the model is never given

The safety of the phase is a property of *wiring*, not of the model's good behaviour. The
assistant is handed a bounded, user-scoped situation snapshot and the recent transcript,
and nothing else. It has **no** direct database access, **no** shell, **no** filesystem
write, **no** unrestricted HTTP, **no** browser or Playwright tools, and **no** access to
secrets, LLM connection rows, sessions or tokens. The context builder
(`backend/app/chat/context.py`) holds only four read-side repositories, so "the chat
cannot leak a secret" is guaranteed by the *type* of what it can read, not by a habit of
the caller. The only ids that ever reach the model are the ones the snapshot puts there,
and the only way those ids turn into an effect is a confirmed, re-authorized proposal.

## The turn pipeline

One turn of the chat runs this path (`backend/app/chat/conversation.py`):

1. **Persist the user's message.** Ownership is checked first — a conversation that is not
   this account's raises `ConversationNotFound` before anything is written. The user's
   message is stored at the next sequence and the thread's activity time is stamped, so
   even a turn whose model call later fails has recorded that the user spoke.
2. **Compose the request.** The versioned `CAREER_CHAT_V1` system prompt + a bounded,
   user-scoped situation snapshot (`ChatContextBuilder.render()`) + up to
   `_MAX_HISTORY_MESSAGES` (20) prior turns replayed oldest-first. The snapshot and the
   user's words are separated by a `=== USER MESSAGE ===` heading so the model can tell
   its situation from the user's instruction. There is no `structured_output` — the chat
   streams prose — and no provider-held session; a turn is composed wholly from stored rows.
3. **Stream.** The turn streams through the telemetry recorder
   (`LLMTelemetryRecorder.stream`), which records an `LLMRun` while forwarding token
   deltas. Whether a turn may reach a *remote* provider is decided by the `RoutingPolicy`
   the service was constructed with — the privacy decision lives in bootstrap, never here.
4. **Parse.** When the stream ends, the accumulated text is split by `parse_turn` into
   prose (stored as the assistant message) and typed proposals (stored `PROPOSED`).
5. **Propose, never execute.** Each proposal is persisted with status `PROPOSED`. Nothing
   in this service ever runs one — that is the executor's job, reached only after a human
   confirms.

## Parsing: where the rule becomes mechanical

`backend/app/chat/parsing.py` is the authority boundary. The model streams one markdown
answer; inside it there may be a single fenced ```` ```proposal ```` block. The parser
pulls that block out *independently of the prose*, parses the JSON, and validates every
action against the domain `ChatAction` union with `CHAT_ACTION_ADAPTER`. Three properties
make it a boundary rather than a suggestion:

- **The domain union is the only gate.** An unknown `kind`, a missing id, an out-of-range
  radius or an invented argument fails to validate into a `ChatAction` and is dropped with
  a secret-free diagnostic — it never becomes a half-understood command. The parser is
  lenient about the *envelope* (where the list lives, minor JSON slips, one deterministic
  bracket-slice repair) and strict about the *action*.
- **It is total and bounded.** Malformed input yields a `ParsedTurn` with prose and
  diagnostics, never an exception; a turn is capped at `MAX_PROPOSALS_PER_TURN` (5), and
  everything beyond the cap is dropped rather than silently accepted.
- **The block never reaches the stored message.** `prose` is the answer with every
  proposal fence removed — a control payload is not a message a user reads or replays.

Parsing decides only what was *said*. Whether what was said may *run* is the validator's
job, at confirm time.

## The closed action vocabulary

`ChatActionKind` (`backend/app/domain/chat.py`) is a closed enum of exactly 11 members in
four families. The model may propose only these; anything else fails to parse.

| Family | Kinds | Effect |
|---|---|---|
| Documents | `GENERATE_RESUME`, `GENERATE_COVER_LETTER` | Tailor the candidate's own materials to a posting (evidence guard applies) |
| Applications | `CREATE_APPLICATION`, `PREPARE_APPLICATION`, `APPROVE_APPLICATION`, `SUBMIT_APPLICATION`, `CANCEL_APPLICATION` | Drive the Phase 12 application lifecycle; each step is re-checked by the application engine |
| Search preferences | `SET_SEARCH_RADIUS`, `UPDATE_SEARCH_KEYWORDS` | Bounded edits to one saved search (`radius_km` in (0, 500]) |
| Read / navigate | `NAVIGATE`, `OPEN_INTERVIEW_PREP` | Client-side hints that change **nothing** on the server |

`READ_ONLY_ACTION_KINDS = {NAVIGATE, OPEN_INTERVIEW_PREP}`: these mutate nothing, so the
executor records a side-effect-free `SUCCEEDED` for them without calling any service. Chat
interview support covers only the existing V1 surface (`OPEN_INTERVIEW_PREP`, and
`NAVIGATE → INTERVIEW_PREP`); it does not add an interview simulator.

Each `ChatAction` is a discriminated union member on `kind` (`CHAT_ACTION_ADAPTER`), so a
new kind added without a matching executor/validator branch is a *type* error via
`assert_never`, not a silent gap.

## The last gate

*(This is the section `ChatActionCard.vue` and `stores/chat.ts` link to as
"§The last gate".)*

Executing a confirmed proposal happens in exactly one place —
`ChatActionExecutor.execute` (`backend/app/chat/executor.py`) — and it is the far end of
"prose has zero authority". The parser decided what the model *said*; the validator
decides whether what it said *may* run; and the executor is where a proposal that survived
both, and that a human confirmed, finally reaches a service. `execute` runs this sequence:

1. **Load scoped by owner.** The proposal is read through a `user_id`-scoped repository, so
   a confirm naming another account's proposal reads as absent → `ChatProposalNotFound`
   (HTTP 404 — "no such proposal" and "not yours" are one answer, so an id cannot be probed).
2. **Idempotency by derived id.** The execution row's id is derived from the proposal
   alone (`chat_action_execution_id`). If an execution already exists it is returned
   unchanged, so a double-clicked Confirm or a retried request collapses onto the one row
   and the underlying action never runs twice.
3. **Open-status guard.** A proposal no longer `PROPOSED` (a dismissed one, with no
   execution to return) raises `ChatProposalNotActionable` (HTTP 409).
4. **Re-authorize.** The `ProposalValidator` re-checks ownership and coarse domain state.
   A refusal is recorded as a `REJECTED` execution carrying the validator's secret-free
   code and detail — never executed. `proposal != permission`, restated at the moment of action.
5. **Dispatch.** A read-only action is recorded `SUCCEEDED` with no service call. A
   mutating action is dispatched to the one service it maps to (`DocumentService`,
   `ApplicationService`, `OnboardingService`) inside a guard.

The guard maps outcomes cleanly and the audit can never drift from the proposal's status:

| Outcome | When | Proposal status |
|---|---|---|
| `SUCCEEDED` | The service returned normally (or a read-only hint) | `EXECUTED` |
| `REJECTED` | A typed precondition refusal — not permitted right now | `REJECTED` |
| `FAILED` | Permitted, but the service call raised unexpectedly | `FAILED` |

Two safety properties round it out:

- **Every attempt is audited, refusal included.** A `ChatActionExecution` row is written
  for `SUCCEEDED`, `REJECTED` *and* `FAILED`, so "the platform refused this, and why" is
  data the chat renders, not a dropped attempt. The audit is written before the proposal's
  status is advanced.
- **A detail never leaks a secret.** A refusal surfaces the service's own typed,
  domain-composed message (ids, states, a failure code — secret-free by construction). An
  *unexpected* failure is recorded with a fixed generic sentence and the raw exception text
  is dropped, because it could quote a driver, a URL or a token.

### Dismiss

`dismiss` is the user's other move: `PROPOSED → DISMISSED`. It writes no execution because
nothing was attempted; it only moves the proposal out of the open set so it can never later
be confirmed. It is scoped and guarded exactly as `execute`'s opening steps.

## The validator: ownership by reading

`ProposalValidator.validate` (`backend/app/chat/validators.py`) is the load-bearing half
of `proposal != permission`, and it is deliberately narrow:

- **Ownership by reading, not by trusting the id.** Every entity an action names is loaded
  through a `user_id`-scoped repository. A proposal about another account's application or
  search reads as absent and is rejected — never trusted because the id was well-formed or
  the model wrote it. An opportunity is the one shared fact (a posting belongs to no one)
  and is only checked to *exist*.
- **A coarse state guard, never the state machine.** For the application lifecycle it
  rejects only the terminal case no operation can touch (`is_terminal`); the exact
  per-operation rule ("submit needs APPROVED", the whole Phase 12 gate) stays in
  `ApplicationService`. Re-encoding that rule here would be a second copy free to drift, so
  the validator does the drift-free check and leaves the authoritative one to its owner.
- **Read-only actions skip every check** and are always permitted.

It returns a `ProposalValidation` (a value, never an exception): a refusal carries a stable
`ProposalRejectionCode` (`OPPORTUNITY_NOT_FOUND`, `APPLICATION_NOT_FOUND`,
`APPLICATION_CLOSED`, `SEARCH_NOT_FOUND`, `SEARCH_HAS_NO_RADIUS`) so the frontend can render
each refusal and telemetry can count them. Every message names only ids and states — the
user's own data — so a rejection detail can never carry a secret.

## The SSE turn contract

`POST /api/v2/chat/conversations/{id}/messages` is the one streaming endpoint. Ownership
and empty-message checks run *before* the stream is returned, so a foreign thread is an
ordinary `404` and an empty message a `422` — JSON errors, never an event mid-stream. Once
past them the response is a `text/event-stream` of `ChatStreamEventResponse`s, one per
`data:` line, emitted verbatim as `data: {json}\n\n` (no `event:` line). The event types:

| `type` | Payload | Meaning |
|---|---|---|
| `TOKEN` | `text` | A chunk of assistant prose to append to the live preview |
| `COMPLETED` | `message`, `proposals` | The turn succeeded: the stored assistant message and its `PROPOSED` proposals |
| `ERROR` | `error_code`, `error_detail` | The turn reached a provider and did not succeed — a typed, secret-free failure |

A provider fault that raises rather than yielding (most notably `NoProviderAvailable` on an
empty route) is caught and surfaced as a terminal `ERROR`, so a caller iterating the stream
always sees a terminal event and never an exception mid-stream. A failed turn writes no
assistant message; the user's message is already persisted.

## Proposal lifecycle and statuses

A `ChatActionProposal` is born `PROPOSED` and moves once, terminally:

```
                    confirm →  EXECUTED   (SUCCEEDED execution)
   PROPOSED ────────────────→  REJECTED   (validator/service refused)
        │           confirm →  FAILED     (permitted, service raised)
        └── dismiss ────────→  DISMISSED  (declined; no execution row)
```

Only a `PROPOSED` proposal is actionable. The card shows Confirm/Dismiss only while open;
once terminal it shows a status badge and no buttons, so a stale card can never act twice.
`ChatActionExecution.outcome` is `SUCCEEDED | REJECTED | FAILED`, and the outcome→status
map is a single source of truth on both sides of the wire (`_STATUS_FOR_OUTCOME` in the
executor, `OUTCOME_STATUS` in the store).

## Navigation is a hint, not an open redirect

A `NAVIGATE` action changes nothing on the server; the executor records a side-effect-free
`SUCCEEDED` whose `result_ref` is the target. The *client* decides where — and whether — to
go. `navigationRoute` (`frontend/app/utils/v2-chat.ts`) maps only the `NavigationTarget`
members this app owns a route for:

| Target | Route |
|---|---|
| `OPPORTUNITIES` | `/map` |
| `APPLICATIONS` | `/applications` |
| `DOCUMENTS` | `/documents` |
| `COMPANIES` | `/companies` |
| `SETTINGS` | `/settings` |

`MATCHES` and `INTERVIEW_PREP` have no standalone V2 route yet, so they map to `null` and
the client stays put. A target the client does not recognise is simply ignored, never
followed — which is what keeps a chat action from ever being an open redirect. The page
navigates only after a `NAVIGATE` proposal was *confirmed* and the server returned
`SUCCEEDED`; a rejected or failed confirm navigates nowhere.

## Persisted entities

Chat state is durable (migration `rev_0010_phase_13_career_chat`). Four entities, all
user-scoped, with ids derived for idempotency:

- **`Conversation`** — a thread: title (a truncated caption, never model authority),
  archived flag, activity timestamps.
- **`ChatMessage`** — one turn's prose. `content` holds prose *only* — the proposal block
  is parsed out and never stored here. An assistant message carries the `llm_run_id` and
  `provider_key` of the run that produced it. The id derives from
  `(conversation, sequence)`, so re-finalizing a turn writes the same row.
- **`ChatActionProposal`** — one proposed action: its typed `action`, a human `summary`,
  its status. The id derives from `(message, ordinal)`, so a re-finalize is idempotent.
- **`ChatActionExecution`** — the audit of one confirmed proposal: outcome, secret-free
  detail, optional `result_ref`. The id derives from the proposal, so a double-confirm
  collapses to one row.

## HTTP surface (`/api/v2/chat`)

The owner is never in the path or body — it is the account resolved from the session, so no
request can read, confirm or dismiss on another user's conversation or proposal. Reads
answer `404` for "no such thread" and "not yours" alike.

| Method + path | Purpose |
|---|---|
| `POST /chat/conversations` | Open a new thread (`201`) |
| `GET /chat/conversations` | This account's threads, recent activity first |
| `GET /chat/conversations/{id}` | One thread's caption/activity (`404` if not yours) |
| `GET /chat/conversations/{id}/messages` | Transcript, oldest first |
| `GET /chat/conversations/{id}/proposals` | Proposals with current status |
| `POST /chat/conversations/{id}/messages` | Send a turn; stream the reply as SSE |
| `POST /chat/proposals/{id}/confirm` | Execute — the last gate (`404`/`409` as above) |
| `POST /chat/proposals/{id}/dismiss` | Decline an open proposal |

## Frontend

The screen (`frontend/app/pages/chat.vue`, behind the `auth` middleware) is built around
the rule: prose and proposals live in different places and are treated differently.
Assistant prose renders as Markdown and can say anything; it changes nothing. A proposal is
a separate, confirm-gated `ChatActionCard`, whose label is derived from the *typed* action
via `describeChatAction` — never from the model's summary prose, which is shown only as
plain text. The card never acts; Confirm/Dismiss emit to the page, which asks the store,
which asks the server. The Pinia store (`frontend/app/stores/chat.ts`) holds streamed prose
and proposals but never decides an outcome — a card's new status is read off the server's
audited execution, never assumed from the click. The page's one self-driven side effect is
following a confirmed, server-permitted `NAVIGATE`.

## Testing

Per `CLAUDE.md`, tests never touch a live board or a live LLM. Backend tests drive the
parser, validator, executor, conversation service and routes against fakes and fixtures;
frontend Vitest specs stub `fetch` (including the SSE wire format `data: {json}\n\n`) and
mock `navigateTo`; the Playwright e2e answers every `/api/**` from a table and proves, in a
real browser, that a *confirmed* `NAVIGATE` moves the app while prose and unconfirmed
proposals do not.

## Module map

Backend: `backend/app/domain/chat.py` (entities, closed action union), `chat/prompts.py`
(versioned system prompt + grammar), `chat/context.py` (bounded snapshot),
`chat/parsing.py` (authority boundary), `chat/validators.py` (re-authorization),
`chat/executor.py` (the last gate), `chat/conversation.py` (streaming turn service),
`api/routes/chat.py` (HTTP + SSE).

Frontend: `app/pages/chat.vue`, `app/components/ChatActionCard.vue`, `app/stores/chat.ts`,
`app/utils/v2-chat.ts` (transport + display helpers + navigation map).
