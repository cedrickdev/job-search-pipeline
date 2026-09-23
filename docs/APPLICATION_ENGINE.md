# The Application Engine (Phase 12)

The application engine turns a *decided* opportunity into a *submitted, audited*
application — safely. It is the last stage of the funnel: discovery finds a posting,
matching and eligibility judge it, the decision records intent, and this engine
executes that intent through a channel adapter, under a policy gate, leaving an
append-only trail behind.

Its one non-negotiable rule (§1): **nothing is submitted that a human — or an
explicit autopilot policy — has not cleared, and the gate is re-checked at the moment
of submission.** A high match score, a model's confidence, or an approval given this
morning is never, on its own, authority to submit this afternoon.

Playwright is an execution adapter here, never the decision engine, and the platform
never solves a CAPTCHA, completes an MFA challenge, or invents a candidate fact to
fill a field.

## The flow

```
ApplicationDecision (intent)
   → ApplicationExecutionGate.evaluate(...)  ← re-run at submission time
      → ApplicationAdapterRegistry.resolve(channel)
         → adapter.prepare(context)          (reversible; reads the form)
            → resolve answers deterministically; pin exact document versions
               → human checkpoint if REQUIRES_HUMAN / REQUIRES_APPROVAL
                  → adapter.submit(context)   (the one irreversible act)
                     → ApplicationEvent + SubmissionAttempt (audit)
```

## Layers

### Domain (`backend/app/domain/`, pure, dependency-light)

- **`application_channel`** — `ApplicationChannel` (ATS_API, ATS_FORM, DIRECT_FORM,
  BROWSER, EMAIL, MANUAL, UNSUPPORTED); `AdapterSafetyLevel`, a *one-directional
  ceiling* (§59) an adapter declares — the gate takes the minimum of it and the
  policy, so an adapter can lower autonomy but never raise it; `HumanRequiredReason`,
  the closed set of reasons a run stops for a person (CAPTCHA, MFA, login, an unknown
  required field, a sensitive question, an ambiguous or changed form, an unresolved
  upload, an unsupported channel).
- **`application_failure`** — `ApplicationFailureCode` and `ApplicationError`, a
  normalized, secret-free failure vocabulary. A detail is composed from a fixed table,
  never forwarded from an adapter's own message (§83, §85); `classify_adapter_failure`
  drops any exception text.
- **`application_answer`** — `ApplicationQuestion`, `ApplicationAnswerProposal`
  (what a model may *suggest*), `ApplicationAnswer` (what an adapter may *fill*), and
  `resolve_answer`, the one deterministic bridge between them (§28-29): a sensitive
  question is always a human hand-off, a required question with no trustworthy answer
  becomes `UNKNOWN_REQUIRED_FIELD` (never guessed as N/A/0/Yes/No, §25), and a model
  proposal fills a field only above a confidence bar.
- **`execution_gate`** — `ApplicationExecutionGate.evaluate(...)`, deterministic and
  clock-free, returning one `ExecutionAuthorization` with an outcome
  (`PERMITTED`/`REQUIRES_APPROVAL`/`REQUIRES_HUMAN`/`BLOCKED`) and the reasons behind
  it. The load-bearing safety mechanism (§4-7).
- **`application`** — `ApplicationState` and the closed `ALLOWED_TRANSITIONS` graph
  (§17-18); `build_idempotency_key` and the derived `application_id` (§36);
  `PinnedDocument` (§14-16); `SubmissionResult`; the `Application` aggregate, whose
  every state change goes through `transition_to` and whose idempotency key is a
  validated function of its target and channel.
- **`application_event`** — `ApplicationEvent` (the append-only trail, §41) and
  `SubmissionAttempt` (one try at the irreversible act, written in flight before the
  send so a crash leaves evidence, §88).

### The execution gate (§4-7)

`evaluate` gathers `(outcome, reason)` findings from independent checks and returns
the least-autonomous outcome, keeping every reason:

- **BLOCKED** — a non-submitting decision; an inactive policy; a `MANUAL`/
  `COPY_ASSISTED` policy mode (the §5 regression: a policy that became MANUAL between
  approval and submission blocks the send); a disallowed opportunity type; an
  INELIGIBLE verdict (§6); an INCOMPLETE verdict when the policy does not allow it; a
  match below a policy floor (§7); an exhausted rate budget (§49).
- **REQUIRES_HUMAN** — a missing or REVIEW_REQUIRED eligibility; a match the policy
  needs but nobody scored; a `MANUAL_ONLY`/`UNSUPPORTED` adapter; any human-required
  reason preparation surfaced.
- **REQUIRES_APPROVAL** — a `SUPPORTED_WITH_REVIEW` adapter, or a policy with its
  approval brake on.
- **PERMITTED** — an autopilot policy with the brake off *and* a `FULLY_SUPPORTED`
  adapter *and* everything else clear.

The gate is pure: the caller passes the current policy and the day's/week's
submission counts, so the same inputs always yield the same verdict, and the
submit-time re-evaluation reads the policy *as it is now*.

### Ports and adapters (`backend/app/application_engine/`)

- **`contracts`** — the `ApplicationAdapter` Protocol (`capabilities`, async
  `prepare`, async `submit`), plus `AdapterCapabilities`, `AdapterPreparation` and
  `ApplicationContext`. Dispatch is on a typed channel, never a platform string (§8).
- **`registry`** — `ApplicationAdapterRegistry`, one adapter per channel with a
  generic fallback so an unserved channel degrades to a human hand-off (§12-13).
- **`task_dispatcher`** — the `TaskDispatcher` Protocol that keeps the browser worker
  behind an interface (§44-48). The V1 `flock` browser lock is *reused* (`browser_lock`
  on `data/browser_state/.lock`), so V1 and V2 serialize on the shared profile. A
  crashed SUBMIT worker resolves to `STATE_UNKNOWN`, never a retryable failure (§88).
  `SubprocessTaskDispatcher` runs the isolated worker; `FakeTaskDispatcher` is the test
  double.
- **`adapters/`** — `GenericManualAdapter` (the conservative fallback: prepares what
  it can, submits nothing, hands off to a human, §13); `BrowserApplicationAdapter`
  (drives a page through a `TaskDispatcher`, `SUPPORTED_WITH_REVIEW`); and
  `EmailApplicationAdapter` (applies to an *explicit* recipient only, §62, composing a
  neutral body and attaching pinned rendered documents).
- **`bootstrap`** — `build_application_registry`, the one place concrete adapters are
  named. The API composes a fallback-only registry (safe by default); a worker
  deployment composes one with a dispatcher and a mail sender.
- **`browser_worker`** — the isolated worker entrypoint the subprocess dispatcher
  spawns. It takes the shared browser lock and, until Playwright page-driving lands,
  reports a `REQUIRES_HUMAN` hand-off rather than claim a submission it did not make.

### Services (`backend/app/services/`)

- **`application_decisions.ApplicationDecisionService`** — forms the deterministic
  `ApplicationDecision` for a pair (§30-32), worst-first: ineligible → SKIP, below a
  floor → SKIP, review/incomplete → REQUIRE_REVIEW, else the policy mode's intent
  (MANUAL → SAVE, COPY_ASSISTED → PREPARE, SUPERVISED/AUTOPILOT → AUTO_APPLY).
- **`applications.ApplicationService`** — the lifecycle: `create` (idempotent, §36),
  `prepare` (reversible, routes by the gate, pins RENDERED versions, resolves answers),
  `approve` (§52), `submit` (re-evaluates the gate, re-checks pinned documents, writes
  an in-flight attempt before the irreversible act, records the typed outcome), `cancel`
  and `recover_in_flight` (§88). It holds no clock; each method takes `now`.

### Persistence (migration `rev_0009`)

Five tables: `application_policies`, `application_decisions`, `applications`,
`application_events`, `submission_attempts`. `applications` has a UNIQUE
`idempotency_key` (§36); `submission_attempts` a UNIQUE `(application_id,
attempt_number)`. Every CHECK restates a domain validator, so a row written by a
migration or by psql cannot assert a state the engine could never have produced.

### API (`/api/v2/applications`)

Idempotent, session-scoped (the owner is never in the path or body):

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/applications` | open from the stored decision |
| GET | `/applications` | this account's applications |
| GET | `/applications/{id}` | one application (404 if not yours) |
| POST | `/applications/{id}/prepare` | prepare and route by the gate |
| POST | `/applications/{id}/approve` | a human's approval |
| POST | `/applications/{id}/submit` | the irreversible submission |
| POST | `/applications/{id}/cancel` | abandon before sending |
| GET | `/applications/{id}/events` | the append-only trail |

`ApplicationError` maps to a status by code (duplicate → 409, rate-limited → 429,
everything else → 409); the service exceptions map to 404 (not found), 409 (decision
missing, not actionable). The body's `error` is the code lowercased — the same closed
vocabulary the engine branches on.

### Frontend (`/applications`)

A list operating the lifecycle: each row shows the server's state and offers the one
action it allows — Prepare a planned one, "Approve & submit" a reviewed one (the
review screen's single decision, two audited acts, §89), Submit an approved one,
Cancel anything not yet sent. An application's append-only trail is shown on demand.
There is no "ignore safeguards" control, and the default policy is MANUAL.

## Security invariants

- No irreversible submission from match/LLM alone; the gate is re-checked at
  submission time (§1, §5).
- CAPTCHA and MFA are never solved or bypassed — meeting one is an immediate typed
  human hand-off (§21).
- An unknown required answer is never guessed; a sensitive/demographic/legal question
  is never inferred (§25-26).
- Only RENDERED, guard-cleared document versions are submitted, and the exact pinned
  version is sent — never "the latest" (§14-16).
- Idempotency is physical: the id is derived from the key and the DB UNIQUE constraint
  is the second defence (§36-40).
- Failure details are composed from a fixed table, never an adapter's or a page's own
  text (§83, §85).
- An ambiguous submission is `SUBMISSION_STATE_UNKNOWN`, never a blind retry (§38, §88).

## Tests

No live boards, LLMs, browsers or CAPTCHAs (CLAUDE.md §Testing). The gate, the
decision service and the full lifecycle are pinned over in-memory fakes
(`tests/test_v2_application_engine.py`); the five tables are exercised on real
PostgreSQL (`tests/test_v2_persistence_application_engine.py`); the HTTP contract,
ownership and error map are covered by `tests/test_v2_api_applications.py`; and the
frontend by `useApplications.spec.ts`, `applications.spec.ts` and an e2e.
