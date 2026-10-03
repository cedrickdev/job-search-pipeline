# Swiss application autopilot — design

Date: 2026-10-03. Status: revised design for user review. Initial live scope:
Switzerland only. Other countries are a later, separately validated rollout.

## Outcome and agreed authority

The candidate configures active saved searches and a candidate profile once. For
Swiss jobs and internships matching those searches, the system discovers offers,
assesses fit and eligibility, prepares an evidence-grounded résumé for each offer,
and submits on supported application forms without per-offer approval. The
candidate has approved a minimum overall match of 0.85 and a maximum of ten
application attempts per Swiss local day. Ten is a ceiling, not a target. The
candidate may pause the campaign at any time and inspect every submitted artifact,
answer, receipt and blocker.

The candidate must explicitly activate the standing policy after a successful
dry-run review. No real application is sent by automated tests. An LLM may propose
document text and non-sensitive form answers, but cannot change the policy or
authorize submission. Playwright executes a previously authorized submission;
it does not make the decision.

The product is bilingual: French and English are complete interface locales, and
the language of application documents is selected from the opportunity's needs
independently of the interface locale. Provider connections support official API
keys and a generic HTTPS gateway. Subscription-based sign-in is an optional,
capability-gated integration only when the provider officially supports this
application and the account is eligible; it is never simulated by browser login,
cookie reuse or scraping a consumer chat interface.

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
   overall score >= 0.85, sufficient evidence coverage under a documented and
   tested threshold, an affirmative eligibility verdict, a classified allowed
   opportunity type, and a match to an active CH search. A missing score, missing
   required fact or ambiguous eligibility is held for review, never counted as a
   positive match. The initial evidence-coverage candidate is 0.60; it must be
   verified with realistic fixtures before live activation, not treated as an
   already-approved user preference.
3. The existing decision service forms typed intent under the user's persisted
   policy. The default remains MANUAL. The autopilot policy must be active, use
   AUTOPILOT mode, turn off per-submission approval, allow only explicitly chosen
   employment/internship types, keep the score floor at or above 0.85, and limit
   volume to ten per day, with a weekly ceiling no greater than seventy as a
   backstop rather than additional authorization. Only the owner can
   activate, pause or edit it; LLM and strategy recommendations cannot silently
   expand it.
4. A provider-neutral LLM connection generates a résumé tailored to the posting
   from the user's recorded evidence. Typed content is validated by the evidence
   guard, rendered to PDF and versioned. The exact rendered version is pinned to
   the application. A cover letter is generated and pinned when the form requires
   one. If the provider is unavailable, output is invalid, or evidence is
   insufficient, this application waits; the run continues. Provider fallback
   cannot silently send candidate data to a different operator: the owner must
   authorize the destination and the routing decision is recorded.
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

## Model connections

The existing provider-neutral router and user-owned connection store remain the
business boundary. Add first-class official OpenAI and Anthropic API adapters only
behind the same typed request/response and capability checks as the existing
OpenAI-compatible gateway. A connection records owner, provider, model, transport
capabilities and secret metadata; the request routing policy supplies the privacy
class. Keys remain write-only to the browser, encrypted
at rest, rotatable and absent from logs. Health checks send no candidate evidence.

The generic gateway accepts `base_url + api_key + model` over validated HTTPS. Its
egress policy must close the documented DNS-rebinding gap before an untrusted URL is
enabled in a production multi-user deployment; redirects and access to private,
loopback and cloud-metadata addresses remain disallowed. A deployment-local model
may use a separately classified loopback endpoint; it is not a remote gateway.

An official subscription connector is optional and disabled by default. Enable it
only when the provider offers a supported authorization flow for this app, the
deployment and user are eligible, the granted scopes permit the exact task, and
revocation, expiry, storage and auditing are implemented. If any condition fails,
the UI says "not available" and offers the API connection instead. Existing
self-authenticating developer CLIs are not assumed to authorize a hosted,
background candidate-data workflow. No application submission depends on a
subscription connector being available.

## Internationalization

Nuxt presents all navigation, settings, forms, validation errors, statuses,
notifications, accessibility labels and policy explanations in `fr` and `en` from
versioned translation catalogs. No user-facing string is assembled from an LLM
response. The account stores a locale preference; a first visit may use browser
preference, with a visible language switcher and a deterministic fallback. Dates,
times and quantities are locale-formatted while daily policy accounting remains
anchored to the Swiss time zone for the initial CH campaign.

Résumé and cover-letter language is a separate, per-opportunity decision. An
ambiguous posting is held for the candidate to inspect and select a language before
that application can be sent. A missing translation fails visibly in development and falls back to
English in production; translation coverage is checked in CI. French and English
tests cover the complete critical path, including safety and error states.

## Country expansion contract

The core workflow uses a country code and Country Pack registry, never Swiss-only
branches in the domain services. Only CH is activated for discovery and autopilot
initially. Future packs must supply source capabilities and policy review,
terminology, opportunity types, geographic validation, eligibility metadata,
locale/date/currency handling, adapter fixtures and documented limitations before
the UI offers them as selectable countries. Each country is a separate phase gate;
choosing a country with no validated pack cannot activate a campaign. A candidate's
searches and autopilot policy are explicitly country-scoped so adding another pack
cannot silently broaden an existing CH authorization.

## Safety and privacy

- All reads and mutations are scoped to the authenticated account. Candidate
  evidence, documents, LLM connections, policy and application history cannot
  cross account boundaries.
- Authentication, CSRF, role/owner authorization and account-scoped repository
  queries protect both API and workers. Browser execution runs isolated from API,
  LLM credentials and other users' sessions, with bounded network egress, timeouts,
  resource limits and redacted logs. A browser task carries identifiers, not secrets.
- Stored connection secrets and any future authorization tokens use versioned
  encryption with operator-controlled rotation. Documents and form answers have
  explicit retention and deletion paths. Audit events record decisions and hashes or
  references to exact artifacts without copying unnecessary personal data into logs.
- The submission gate is checked again in the browser worker, using current
  policy, eligibility, document versions and quotas. Queue placement is not
  authority. Pausing a policy prevents new irreversible clicks.
- No CAPTCHA/MFA bypass or anti-bot evasion. A normal existing authenticated
  session may be reused, but an authentication challenge requires hand-off.
- The candidate can view the exact evidence-backed document and submission
  history. Unsupported sites get a manual hand-off, not a false success.
- No real application submission, account creation on an employer site, or
  sensitive-data transmission occurs in automated tests.
- Before production activation, require a threat-model review, tenant-isolation
  tests, concurrency/idempotency tests, dependency and secret scanning, SSRF and
  prompt-injection tests, backup/restore rehearsal, monitored dry-run and an
  operator-visible kill switch. Passing unit tests alone is not a production claim.

## Validation and activation

Tests exercise the full local flow with a fake user, saved search, synthetic CH
offers, a fake LLM provider and local ATS fixtures. They verify score/coverage
and eligibility gates, duplicate suppression, atomic daily cap under concurrency,
policy pause, provider failure, sensitive questions, CAPTCHA/MFA, changed forms,
exact pinned uploads, a single submit click, confirmed receipt and STATE_UNKNOWN.
Backend tests, frontend tests, lint/type checks and Docker smoke tests pass.

Security and i18n acceptance also require two-user isolation tests for every new
API/worker path, refusal of hostile gateway URLs and redirects, redaction tests,
key-rotation tests, locale-catalog completeness and French/English browser flows.
Tests of subscription connectors use provider fakes and prove a disabled or expired
connector cannot be selected; no test signs into a real subscription.

A separate dry-run processes real or fixture offer metadata but stops before any
irreversible submit; it reports what would be sent and why others were held. The
owner reviews that report before explicitly activating the policy. Live ATS
success cannot be claimed from fixture tests. On activation, monitoring exposes
submitted, blocked, unknown and failed counts, and the owner can pause instantly.

## Delivery boundary

This is one product design, not permission to implement multiple roadmap phases
in one run. The current approved Phase 12.1 may wire and verify the V2 browser
submission path and its safe task-lane integration. The discovery persistence,
per-opportunity LLM document wiring, native API/subscription adapters,
internationalization and V2 policy/UI integration are dependent deliverables to
plan and validate at the appropriate phase gates. Stop at each phase completion,
report changed files, migrations, checks and risks, and ask for explicit user
validation before continuing. Phase 17 country expansion is out of scope for the
CH release but is supported by the country-neutral seams above.
