# Swiss application autopilot — design

Date: 2026-10-03. Status: proposed for user review. Scope: Switzerland only.

## Outcome and agreed authority

The candidate configures active saved searches and a candidate profile once. For
Swiss jobs and internships matching those searches, the system discovers offers,
assesses fit and eligibility, prepares an evidence-grounded résumé for each offer,
and submits on supported application forms without per-offer approval. The
candidate has approved a minimum overall match of 0.85 and a maximum of ten
application attempts per Swiss local day. Ten is a ceiling, not a target.

The candidate must explicitly activate the standing policy after a successful
dry-run review. No real application is sent by automated tests. An LLM may propose
document text and non-sensitive form answers, but cannot change the policy or
authorize submission. Playwright executes a previously authorized submission;
it does not make the decision.

## Existing foundation and gaps

- V2 has Swiss source adapters and a discovery orchestrator, but its discovery
  CLI reports results without persisting them for saved searches.
- V2 has deterministic matching and eligibility assessment. An overall score can
  have low evidence coverage, so score alone is insufficient authority.
- V2 can generate per-opportunity résumé and cover-letter versions and guard
  candidate claims. The API currently wires the deterministic generator, not the
  provider-neutral LLM generator.
- V2 has application policies, a decision service, an execution gate, an audit
  trail, a browser adapter, ATS drivers for Greenhouse/Lever/Ashby, and a browser
  task lane. The API currently uses a manual-only fallback registry; the
  submission path from UI to the browser lane is not connected.
- The V1 Jobs and Settings screens use a separate SQLite tracker. Their
  auto-apply and full-run controls must not be presented as activating V2.

## Canonical flow

1. A general-lane run loads one user's active saved searches and the CH Country
   Pack. It discovers Swiss offers, records source health and warnings, persists
   canonical opportunities, and deduplicates the same posting across sources.
   A failed source does not abort the rest of the run. Inactive searches and
   postings outside their requested type, location, work pattern or terms cannot
   authorize an application.
2. The account-scoped assessment service scores each candidate–opportunity pair
   and determines eligibility independently. Automatic submission requires an
   overall score >= 0.85, evidence confidence >= 0.60, an affirmative eligibility
   verdict, a classified allowed opportunity type, and a match to an active CH
   search. A missing score, missing required fact or ambiguous eligibility is
   held for review, never counted as a positive match.
3. The existing decision service forms typed intent under the user's persisted
   policy. The default remains MANUAL. The autopilot policy must be active, use
   AUTOPILOT mode, turn off per-submission approval, allow only explicitly chosen
   employment/internship types, keep the score floor at or above 0.85, and limit
   volume to ten per day and at most seventy per week. Only the owner can
   activate, pause or edit it; LLM and strategy recommendations cannot silently
   expand it.
4. A provider-neutral LLM connection generates a résumé tailored to the posting
   from the user's recorded evidence. Typed content is validated by the evidence
   guard, rendered to PDF and versioned. The exact rendered version is pinned to
   the application. A cover letter is generated and pinned when the form requires
   one. If the provider is unavailable, output is invalid, or evidence is
   insufficient, this application waits; the run continues.
5. Preparation selects a supported channel and inspects the form. The model may
   propose answers only where candidate evidence supports them. Sensitive,
   legally consequential or unknown required fields require the candidate.
   CAPTCHA, MFA, login walls, changed forms and unsupported hosts put only that
   application in a human-required state; the campaign proceeds with other offers.
6. A durable browser-lane task invokes the existing application service's
   submit-time gate and browser adapter. Immediately before the irreversible
   click it atomically reserves one of the owner's ten daily slots. A confirmed
   submission records the receipt; an ambiguous outcome becomes
   SUBMISSION_STATE_UNKNOWN, consumes its reserved slot and is never retried
   automatically. A failure before the click releases the slot. Idempotency and
   a canonical target identity prevent duplicate sends across retries and
   duplicate source listings.
7. The V2 UI shows the standing policy, activation/pause control, daily remaining
   slots, discovered matches, document versions, application state, blockers and
   audit trail. V1 remains usable during migration, but its controls are clearly
   labelled as legacy and cannot activate V2 by accident.

## Safety and privacy

- All reads and mutations are scoped to the authenticated account. Candidate
  evidence, documents, LLM connections, policy and application history cannot
  cross account boundaries.
- The submission gate is checked again in the browser worker, using current
  policy, eligibility, document versions and quotas. Queue placement is not
  authority. Pausing a policy prevents new irreversible clicks.
- No CAPTCHA/MFA bypass or anti-bot evasion. A normal existing authenticated
  session may be reused, but an authentication challenge requires hand-off.
- The candidate can view the exact evidence-backed document and submission
  history. Unsupported sites get a manual hand-off, not a false success.
- No real application submission, account creation on an employer site, or
  sensitive-data transmission occurs in automated tests.

## Validation and activation

Tests exercise the full local flow with a fake user, saved search, synthetic CH
offers, a fake LLM provider and local ATS fixtures. They verify score/coverage
and eligibility gates, duplicate suppression, atomic daily cap under concurrency,
policy pause, provider failure, sensitive questions, CAPTCHA/MFA, changed forms,
exact pinned uploads, a single submit click, confirmed receipt and STATE_UNKNOWN.
Backend tests, frontend tests, lint/type checks and Docker smoke tests pass.

A separate dry-run processes real or fixture offer metadata but stops before any
irreversible submit; it reports what would be sent and why others were held. The
owner reviews that report before explicitly activating the policy. Live ATS
success cannot be claimed from fixture tests. On activation, monitoring exposes
submitted, blocked, unknown and failed counts, and the owner can pause instantly.

## Delivery boundary

This is one product design, not permission to implement multiple roadmap phases
in one run. The current approved Phase 12.1 may wire and verify the V2 browser
submission path and its safe task-lane integration. The discovery persistence,
per-opportunity LLM document wiring and V2 policy/UI integration are dependent
deliverables to plan and validate at the appropriate phase gates. Stop at each
phase completion, report changed files, migrations, checks and risks, and ask for
explicit user validation before continuing. Phase 17 country expansion is out of
scope.
