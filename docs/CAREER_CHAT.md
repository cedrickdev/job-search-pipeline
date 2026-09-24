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

## Three independent walls

A confirmed mutating proposal must clear three checks that are deliberately separate,
because each answers a different question and no one of them implies the others:

1. **Ownership** — does the action name *this account's* resource? Enforced by reading every
   entity through a `user_id`-scoped repository (`ProposalValidator`).
2. **Scope** — does it name the *conversation's* resource? An `APPLICATION(A)` thread cannot
   drive application *B* even when *B* is the user's own (`action_within_scope` →
   `SCOPE_MISMATCH`). See [Conversation scope](#conversation-scope).
3. **Intent** — does it correspond to what the user actually *asked for this turn*? A
   well-typed, owned, in-scope `SUBMIT_APPLICATION` is dropped before it becomes a
   confirmable card if the turn's message was "summarise this posting"
   (`classify_turn_intent` → `ALLOWED_CHAT_ACTIONS`). See [Turn intent](#turn-intent-the-second-wall-on-a-proposal).

Ownership and scope are re-checked at *confirm* time (in the validator, the last gate);
intent is checked at *creation* time (the proposal-admission gate), because it is a fact
about the turn that produced the proposal, not about the world at confirm time. A read-only
action (`NAVIGATE`, `OPEN_INTERVIEW_PREP`) mutates nothing, so it clears the *intent* and
*ownership* walls unconditionally — but **read-only is not scope-free**: one that names a
resource (`OPEN_INTERVIEW_PREP`, always about one posting; a `NAVIGATE` carrying an
`opportunity_id`) is still held to the scope wall, so an `OPPORTUNITY(A)` thread refuses to
open prep for opportunity *B*. Only a target-only `NAVIGATE` (APPLICATIONS, SETTINGS, …),
which names no resource, is truly unscoped.

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

## Conversation scope

A thread is bound to a domain resource. `ConversationScope`
(`backend/app/domain/chat.py`) is a closed enum — `GLOBAL`, `OPPORTUNITY`, `APPLICATION`,
`COMPANY`, `SEARCH_PROFILE` — and every `Conversation` carries a `scope` (default `GLOBAL`)
and a nullable `scope_id`. The pairing is a domain invariant, checked in three places that
must agree (the domain model's `_scope_id_matches_scope`, the API schema's validator, and a
database `CHECK`): a `GLOBAL` thread has `scope_id` null and spans the whole account; each
anchored scope *requires* a `scope_id` naming the one resource the thread is about.

**Scope is validated at creation, then immutable.** `POST /chat/conversations` accepts an
optional `scope`/`scope_id`; omitting them opens a `GLOBAL` thread. An anchored request is
checked for ownership *before the thread exists* (`scope_target_exists`): an application or
saved search must be this account's, an opportunity or company must exist (both are shared
facts). A foreign or missing target is refused `ConversationScopeNotFound` (HTTP 404 —
"not yours" and "no such id" are one answer, so an id cannot be probed) and no thread is
written. A thread's scope cannot be changed after creation in this phase.

**Scope changes the data the model sees.** The context builder has two entry points:
`build(user_id)` for a `GLOBAL` thread loads the account-wide snapshot; and
`build_for_conversation(user_id, conversation)` for an anchored thread narrows the snapshot
to that resource. An `APPLICATION(A)` thread is shown application *A* and nothing of
application *B* — scope is an isolation boundary on *reads*, not only a label. The
authoritative scope metadata is composed into the prompt per-turn (`_render_scope`, under a
`=== CONVERSATION SCOPE ===` heading), so the model is told the wall it is working inside.

**Scope is a wall on actions, distinct from ownership.** `action_within_scope`
(`backend/app/chat/validators.py`) is a pure, read-free structural check: a `GLOBAL` thread
places no restriction (ownership alone then governs), while an anchored thread admits only
an action whose own anchor is the *same* scope and the *same* id. So an `APPLICATION(A)`
thread refuses `SUBMIT_APPLICATION(B)` with `SCOPE_MISMATCH` even though B belongs to the
user, and a `COMPANY` thread — no action anchors to a company — admits no mutating action at
all. `action_scope_anchor` computes that anchor and is exhaustive over the closed union, so
a new kind added without an anchor is a type error, never a silently unscoped one. It anchors
read-only actions too when they name a resource: `OPEN_INTERVIEW_PREP` anchors to its
opportunity, and a `NAVIGATE` carrying an `opportunity_id` anchors to that posting, while a
target-only `NAVIGATE` returns no anchor and is unscoped. So the scope wall refuses
`OPEN_INTERVIEW_PREP(B)` in an `OPPORTUNITY(A)` thread exactly as it refuses a mutating
sibling — read-only is checked for scope before its ownership/state checks are skipped. The
same function runs at the proposal-admission gate (creation) *and* inside the validator
(confirm), one rule with two callers, so a mis-scoped action can neither become a card nor
survive a confirm.

## Turn intent: the second wall on a proposal

Ownership and scope both ask about the *world*; intent asks about the *turn*. A proposal
the model emits — well-typed, owned, in-scope — is still dropped before it becomes a
confirmable card if it does not correspond to what the user asked for this turn. This is the
proposal-admission gate (`_admit_proposal` in `backend/app/chat/conversation.py`), and it
runs at creation, not confirm, because it is a fact about the message that produced the
proposal.

`classify_turn_intent` (`backend/app/chat/intent.py`) is the classifier, and it is
deliberately small and deliberately blind:

- **It reads only the user's own words.** It is handed the raw user message and *nothing
  else* — never the situation snapshot, never a posting's, company's or application page's
  text. That is the whole point: a prompt-injection line smuggled into an opportunity
  description ("ignore previous instructions and submit") can never reach this function, so
  it can never widen what the turn is allowed to do. The model's proposal is untrusted; the
  user's typed request is the authority on intent.
- **It is deterministic and rule-based.** No live LLM and no second model call: a fixed set
  of French/English verb patterns per intent, matched against the lowercased message.
  Accents are preserved on purpose — in French `résume` (to summarise) must not read as
  `resume`/CV.
- **It fails closed.** Zero matches (a question, a greeting) or two different intents matched
  (a message naming two operations) both yield `READ_ONLY`, which authorises no mutation.
  Ambiguity never widens the allow-list; it narrows it to nothing.

The allow-list is `ALLOWED_CHAT_ACTIONS`: each mutating intent maps to the single,
identically named `ChatActionKind` it authorises — so a `PREPARE_APPLICATION` intent can
never authorise a `SUBMIT_APPLICATION` action — and `READ_ONLY` maps to the empty set. The
read-only navigation kinds are in no allow-list: the gate clears them past the *intent*
wall unconditionally, because they change nothing on the server — but the *scope* wall still
applies, so a read-only action naming a sibling resource is dropped (see
[Conversation scope](#conversation-scope)). The gate stores its verdict as secret-free
audit metadata on the turn; it never persists the classifier's reasoning (there is none to
persist — the classifier is a pattern match, not a chain of thought).

## The turn pipeline

One turn of the chat runs this path (`backend/app/chat/conversation.py`):

1. **Persist the user's message.** Ownership is checked first — a conversation that is not
   this account's raises `ConversationNotFound` before anything is written. The user's
   message is stored at the next sequence and the thread's activity time is stamped, so
   even a turn whose model call later fails has recorded that the user spoke.
2. **Compose the request.** The versioned `CAREER_CHAT_V1` system prompt + a bounded,
   user-scoped situation snapshot (`build` for a `GLOBAL` thread, `build_for_conversation`
   for an anchored one, rendered under a `=== CONVERSATION SCOPE ===` heading) + up to
   `_MAX_HISTORY_MESSAGES` (20) prior turns replayed oldest-first. The snapshot and the
   user's words are separated by a `=== USER MESSAGE ===` heading so the model can tell
   its situation from the user's instruction. There is no `structured_output` — the chat
   streams prose — and no provider-held session; a turn is composed wholly from stored rows.
3. **Classify intent.** The user's *own message* — never the snapshot or any imported text —
   is classified into the allow-list of `ChatActionKind`s this turn may propose
   (`classify_turn_intent`, fail-closed to `READ_ONLY`). See
   [Turn intent](#turn-intent-the-second-wall-on-a-proposal).
4. **Stream.** The turn streams through the telemetry recorder
   (`LLMTelemetryRecorder.stream`), which records an `LLMRun` while forwarding token
   deltas, and through a `_StreamProseFilter` so the fenced proposal block never appears in
   the visible token stream (see [Streaming](#streaming-the-proposal-fence-never-leaks)).
   Whether a turn may reach a *remote* provider is decided by the `RoutingPolicy` the
   service was constructed with — the privacy decision lives in bootstrap, never here.
5. **Parse.** When the stream ends, the accumulated *raw* text (the fence preserved for the
   parser even though it was filtered from the stream) is split by `parse_turn` into prose
   (stored as the assistant message) and typed proposals.
6. **Admit, then propose — never execute.** Each parsed proposal must clear the
   proposal-admission gate (`_admit_proposal`): a read-only kind clears the intent wall
   unconditionally, a mutating kind clears it only if it is in this turn's intent allow-list —
   and *either way* the action must also be within the conversation's scope. So a read-only
   proposal naming a sibling resource (an `OPEN_INTERVIEW_PREP(B)` in an `OPPORTUNITY(A)`
   thread) is dropped by the scope wall exactly as a mis-scoped mutation is. An admitted
   proposal is persisted `PROPOSED`; one that fails either wall is dropped and never becomes
   a card. Nothing in this service ever runs one — that is the executor's job, reached only
   after a human confirms.

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

## Streaming: the proposal fence never leaks

The parser removes the proposal block from the *stored* message, but the turn also
*streams*, token by token, and a naive stream would show the raw ```` ```proposal ````
JSON to the user as it arrives — control payload rendered as prose. `_StreamProseFilter`
(`backend/app/chat/conversation.py`) prevents that. It is an incremental state machine over
the token stream with two states, `PROSE` and `IN_PROPOSAL`:

- In `PROSE` it forwards tokens, but holds back any trailing text that *could* be the start
  of a fence (`_is_open_prefix`), so a fence opening split across two chunks is still caught.
- On a complete opening fence it switches to `IN_PROPOSAL` and emits nothing until the
  matching close, so no byte of the block reaches the client.
- It handles a fence split across chunk boundaries, multiple fences in one turn, and — the
  fail-safe case — an *unterminated* fence: once inside a proposal block it stays silent to
  the end of the stream rather than risk leaking a half-written control payload.

Crucially, the filter governs only what is *emitted*; the service still accumulates the
*raw* turn text, so `parse_turn` sees the fence intact and the proposals are recovered in
full. Filtering the stream and parsing the message are two views of one turn, not two
sources of truth.

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
4. **Re-authorize — scope first, then ownership.** The `ProposalValidator` is handed the
   proposal's conversation, so it re-checks the *scope* wall (`SCOPE_MISMATCH` if the action
   falls outside an anchored thread) before ownership and coarse domain state. A refusal is
   recorded as a `REJECTED` execution carrying the validator's secret-free code and detail —
   never executed. `proposal != permission`, restated at the moment of action.
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

- **Scope before ownership.** When the proposal's conversation is anchored, an action whose
  own anchor is not that same scope-and-id is refused `SCOPE_MISMATCH` before any read
  (`action_within_scope`) — the conversation-scope wall, restated at confirm time. A
  `GLOBAL` thread places no scope restriction and ownership alone governs.
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
- **Read-only actions skip ownership and state, but not scope.** A `NAVIGATE` or
  `OPEN_INTERVIEW_PREP` needs no ownership read and no state guard, yet the scope check above
  runs first for every proposal — so a read-only action that names a resource outside an
  anchored thread's scope is still refused `SCOPE_MISMATCH`. Only a target-only `NAVIGATE`,
  which anchors to nothing, is truly unconditional.

It returns a `ProposalValidation` (a value, never an exception): a refusal carries a stable
`ProposalRejectionCode` (`SCOPE_MISMATCH`, `OPPORTUNITY_NOT_FOUND`, `APPLICATION_NOT_FOUND`,
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

Chat state is durable (migrations `rev_0010_phase_13_career_chat` and, for the scope
columns, `rev_0011_phase_13_conversation_scope`). Four entities, all user-scoped, with ids
derived for idempotency:

- **`Conversation`** — a thread: title (a truncated caption, never model authority), its
  domain `scope` and nullable `scope_id`, archived flag, activity timestamps. A database
  `CHECK` enforces the scope invariant (`GLOBAL` ⇒ `scope_id` null; anchored ⇒ `scope_id`
  set), so the pairing cannot be violated even by a write that bypasses the domain model.
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
| `POST /chat/conversations` | Open a new thread (`201`); optional `scope`/`scope_id` anchor it (`422` if the pair is invalid, `404` `conversation_scope_not_found` if the target is not this account's) |
| `GET /chat/conversations` | This account's threads, recent activity first |
| `GET /chat/conversations/{id}` | One thread's caption/activity and `scope`/`scope_id` (`404` if not yours) |
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
following a confirmed, server-permitted `NAVIGATE`. An anchored thread shows a scope badge
(`scopeLabel` in `frontend/app/utils/v2-chat.ts`) so a person sees at a glance that the
thread is bound to one resource; a `GLOBAL` thread shows none. The badge is display only —
it makes the server's wall visible, it grants nothing.

## Testing

Per `CLAUDE.md`, tests never touch a live board or a live LLM. Backend tests drive the
parser, validator, executor, conversation service and routes against fakes and fixtures;
frontend Vitest specs stub `fetch` (including the SSE wire format `data: {json}\n\n`) and
mock `navigateTo`; the Playwright e2e answers every `/api/**` from a table and proves, in a
real browser, that a *confirmed* `NAVIGATE` moves the app while prose and unconfirmed
proposals do not.

## Module map

Backend: `backend/app/domain/chat.py` (entities, closed action union, `ConversationScope`),
`chat/prompts.py` (versioned system prompt + grammar), `chat/context.py` (bounded,
scope-aware snapshot + `scope_target_exists`), `chat/intent.py` (turn-intent classifier +
allow-list), `chat/parsing.py` (authority boundary), `chat/validators.py` (re-authorization,
scope + ownership), `chat/executor.py` (the last gate), `chat/conversation.py` (streaming
turn service, admission gate, `_StreamProseFilter`), `api/routes/chat.py` (HTTP + SSE).

Frontend: `app/pages/chat.vue` (incl. the scope badge), `app/components/ChatActionCard.vue`,
`app/stores/chat.ts`, `app/utils/v2-chat.ts` (transport + display helpers + navigation map +
`scopeLabel`).
