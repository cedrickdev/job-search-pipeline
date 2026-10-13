# Swiss Morning Run: safe browser routing and submission — design

Date: 2026-10-13. Status: written specification for user review.

## Purpose and phase boundary

The candidate wants an active Swiss Morning Run to continue beyond the
evidence-checked CV and cover letter: open a traceable application, inspect the
actual ATS form, and submit unattended only when the candidate's standing policy
and a narrowly approved form variant both authorize it. Every other form goes
to a clear human hand-off. This phase proves that route with local Greenhouse
and Lever fixtures; it does not activate live employer submissions or claim
Swiss-wide source coverage.

The user approved this architecture in conversation on 2026-10-13. This written
version must be reviewed before an implementation plan is written. The phase
remains gated by `docs/IMPLEMENTATION_PLAN.md`: finish, test, report, obtain
validation, and stop before a subsequent live-activation or acceptance phase.

## Existing system and gap

The Morning Run already persists Swiss discovery results, matching decisions,
and exact rendered CV/letter versions in an owner/run/opportunity checkpoint.
`ApplicationService` already owns create, reversible prepare, human approval,
submission reservation, submit-time rechecks, audit events, and unknown-outcome
recovery. Its browser adapter is deliberately `SUPPORTED_WITH_REVIEW`. The
browser lane has separate workers, per-account Chromium profiles, Greenhouse
and Lever drivers, and a live-send switch that defaults to off. The morning
checkpoint does not yet create or prepare an `Application`, and no specific ATS
form variant can currently receive narrowly scoped unattended authority.

The ATS board catalog and discovery allow-list remain default-denied. The
2026-10-12 Swiss source review approves neither the held Swiss job boards nor
live Greenhouse/Lever employer feeds. Public listing visibility, driver support,
and permission to submit are three separate facts. None implies another.

## Chosen approach and alternatives

Extend the durable owner-scoped workflow, reusing the existing application
service and browser lane. Add an operator-controlled, default-deny capability
registry for *specific* ATS form variants. The ordinary browser adapter remains
review-only. A small, typed safety resolver grants `FULLY_SUPPORTED` for one
prepared application only when its exact target, driver, form fingerprint and
operator-reviewed conditions match an enabled capability. The user policy and
execution gate still decide whether unattended submission is allowed.

Rejected approaches:

- Marking the browser adapter or an entire Greenhouse/Lever host family
  `FULLY_SUPPORTED` would turn one fixture pass into authority over changed
  employer forms and is unsafe.
- Keeping all applications in manual review would be safe but would not meet
  the candidate's approved unattended objective on genuinely supported forms.
- Letting a model or browser agent decide when to submit would bypass the
  deterministic policy, evidence, and operator gates.

## Capability registry and activation

An enabled form capability identifies the ATS family and driver version,
employer board identity, canonical HTTPS origin and bounded path shape,
approved form fingerprint/version, allowed required fields and upload slots,
review provenance and time, and explicit collection/form-access decision. It
contains no candidate data, credentials or page body. Configuration is owned by
the operator, not by the candidate or LLM; unknown, expired, disabled, or
nonmatching entries return review-only. Redirects, target URL changes and a
new form fingerprint cannot inherit authority.

Use a versioned, operator-controlled deployment catalog, following the existing
ATS board-catalog pattern. Its packaged default is empty; a runtime override
must name an explicitly mounted reviewed file, and the worker reloads its
decision before a send. Each activation records a review identifier and date;
the application audit records the matched capability identifier. Candidate
settings cannot edit this catalog. An application can be prepared from an
unactivated form, but it cannot be queued for unattended submission on that
basis. This phase may add fixture-only capability entries; production entries
stay disabled until a separate review of applicable terms, specific employer
form behavior, non-sending preflight and operator rollout.

## Workflow and authoritative state

1. After a checkpoint reaches `DOCUMENTS_RENDERED`, a general-lane task reads
   the owner, completed/partial Swiss run membership, current profile, current
   decision and exact CV/letter version IDs. It creates or reuses one
   idempotent `Application` for the canonical target. A duplicate Morning tick
   or another search finding the same opportunity cannot create a second
   application or submission attempt.
2. The reversible browser preparation uses the existing service/driver to read
   the form, resolve evidence-backed answers, and pin the exact guarded
   documents. Login, CAPTCHA, MFA, sensitive or unknown required fields,
   unsupported uploads, and ambiguous form identity become `REQUIRES_HUMAN`.
   Provider errors or absence of rendered documents cannot be hidden by a
   generic template or treated as a successful preparation.
3. The capability resolver matches the prepared form and returns a scoped
   safety level plus fixed reason codes. The application service evaluates the
   current user policy, opportunity type, score/evidence/eligibility, daily and
   weekly limits, and approval brake. A form capability never overrides a
   stricter policy or a human-required answer. The UI distinguishes document
   readiness, application preparation, queueing and confirmed submission.
4. Only an approved, owner-scoped application can enter the browser lane. Queue
   acknowledgement is `QUEUED`, never `CONFIRMED`. The browser worker commits
   a rate/attempt reservation before any possible click, reloads the current
   owner/policy/capability/target/fingerprint and pinned versions, and checks
   the deployment live-send switch. The task carries identifiers only. The
   existing `ApplicationService` remains the sole submission authority;
   Playwright executes, while an LLM can only propose typed answers.
5. A positive ATS confirmation records `SUBMITTED` with its receipt. A click
   without positive confirmation, a process death after reservation, or a
   disputed outcome becomes `SUBMISSION_STATE_UNKNOWN` and is never retried
   automatically. A pre-click refusal releases or settles the reservation
   according to the existing accounting rules and records a typed blocker.

The next implementation plan must define the smallest durable join from the
Morning Run checkpoint to the application and its task identity. If a schema
change is needed, it is a new migration after `0031`, never an edit to applied
history. The application aggregate and attempt table, not the UI or checkpoint,
remain authoritative for whether a submission occurred.

## Concurrency, pause and limits

The per-user daily limit is configured by the candidate and counted by durable
attempt reservations across scheduled and manual triggers, not by the number of
matching offers. A concurrent pair of workers must not take the last slot twice.
Policy pause, cap reduction, source/form deactivation, or a changed candidate
profile after discovery must remove authority at the final pre-click check.

A database read followed by an external browser click is not an atomic pause
barrier. This phase must test that race and document its operational semantics.
It must not enable unattended live sends until a reviewed synchronization or
equivalent safe activation protocol exists. The UI must not promise that a pause
reverses an attempt already across the irreversible boundary. In-flight and
unknown attempts remain visible and require reconciliation.

## Privacy, security and failure behavior

- Ownership comes from the authenticated session or task owner; no user ID from
  a model, URL or task payload grants access. Every preparation, document,
  capability and application read is scoped and tested across two accounts.
- Canonical target validation, HTTPS/host/path checks and redirect handling
  precede navigation and precede the final click. No arbitrary URL is promoted
  by a form fingerprint alone.
- Browser profiles remain per account. The browser worker receives no LLM API
  key, terminal key or database-wide secret beyond its minimum runtime needs.
  Logs, task payloads and audit reasons use fixed, secret-free codes, not raw
  page text, answers, cookies or candidate documents.
- CAPTCHA/MFA/login walls and anti-bot challenges are not bypassed. A changed
  form or unsupported employer host cannot fall back to a generic click.
- Source acquisition remains separately default-denied. LinkedIn and held
  JobCloud/Indeed sources are not enabled by this design. Switzerland is the
  only active country; country expansion remains its own gate.
- No automated test contacts a real employer, uses a real LLM credential or
  sends an actual application. Fixture-only `live_enabled` injection cannot
  leak into deployment defaults.

## API, UI and observability

Expose the application identity, preparation state, fixed blocker codes,
capability activation state, pinned CV/letter links, queue task state, remaining
daily allowance, and audit trail through owner-scoped V2 endpoints. The Morning
Run view links to the existing application view rather than inventing a second
submission state machine. FR/EN copy describes `REQUIRES_HUMAN`, queued, unknown,
and confirmed outcomes accurately; keyboard and screen-reader announcements
and 320/390/768/desktop layouts cover the new states. A disabled live-send
switch is visible, not a silent no-op.

Metrics and operator logs distinguish prepared, queued, pre-click refused,
confirmed, unknown and human-required outcomes without leaking applicant data.
A kill switch prevents new sends; reconciliation is required before any manual
retry of an unknown attempt.

## Acceptance and non-claims

The phase passes only when synthetic Greenhouse and Lever fixtures each drive
discovery member → match/eligibility → exact guarded CV/letter → application
prepare → scoped capability and policy check → one simulated browser-lane send
→ confirmed or unknown audit state. Tests also cover two-user isolation,
duplicate runs, a changed form, wrong target, policy pause, cap concurrency,
CAPTCHA/MFA/sensitive questions, no confirmation, crash after reservation,
and no second click. Full Python, frontend, type/lint/build, API-contract,
Docker/Chromium fixture and migration checks must pass, or failures must be
reported by name.

Passing fixtures does **not** prove either live Swiss employer feed is approved,
that a real ATS form permits automation, or that the V2 is ready for unattended
live applications. A later, separately approved operator review and bounded
non-sending live rehearsal must establish those facts before activation.
