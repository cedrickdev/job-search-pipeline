# Swiss ATS morning autopilot — design

Date: 2026-10-12. Status: written specification for user review; not an implementation approval.

## Purpose and scope

The candidate wants a local, Switzerland-only Morning Run that discovers relevant
jobs, internships, apprenticeships and training opportunities, ranks them,
prepares evidence-grounded ATS-compatible CVs and motivation letters, and sends
eligible applications automatically through Playwright where a supported form
and the candidate's policy permit it. The user chooses the daily application
limit in Settings. Every discovered opportunity and every application outcome
must be visible in the V2 UI; a queued task must never be represented as a sent
application.

The candidate has approved replacing the previous acceptance demand for two
independent Swiss job boards with two distinct public employer/ATS feeds that
contain Swiss opportunities. This is a narrower coverage claim, not a claim to
search the entire Swiss labour market. LinkedIn and the held JobCloud/Indeed
adapters remain out of scope. No CAPTCHA/MFA bypass, anti-bot evasion, invented
candidate claim, or real submission in automated tests is permitted.

This design extends the already implemented CH V2 morning schedule. It does not
replace the provider-neutral LLM, evidence, matching, document, or application
engines. It does not change the V1 compatibility routes.

## Current state and boundary

`MorningRunService.enqueue_active_ch` currently queues only
`OPPORTUNITY_DISCOVERY` for active CH searches. The 2026-10-12 verification
report explicitly did not validate live discovery, generation, or submission.
`config/companies.yaml` has empty Greenhouse, Lever and Ashby lists. Merely
allowing `greenhouse` and `lever` source keys would therefore yield no ATS jobs.
The browser adapter is currently `SUPPORTED_WITH_REVIEW`, so an AUTOPILOT policy
alone does not authorize unattended browser submission. This safety ceiling
must not be bypassed by a Morning Run shortcut.

Source access is default-denied by `JOBSEARCH_DISCOVERY_ALLOWED_SOURCES`.
The source review leaves `jobup`, `jobscout24`, and `indeed_ch` on hold. An
ATS job-listing endpoint being publicly readable is not authorization to
collect without limits or to submit an application at that employer. Operator
review of applicable conditions is a separate activation gate.

## Chosen approach and alternatives

Use one durable, owner-scoped workflow on the existing V2 queue. Discovery
selects reviewed ATS employer boards; downstream stages consume persisted
opportunity IDs and invoke existing V2 services. A type-checked stage transition
and owner-scoped idempotency key join each step. The application service alone
may authorize the irreversible submit, rechecking current policy, eligibility,
document versions, source/form permission, and the daily/weekly budget at send
time. Playwright executes a form; an LLM can produce structured proposals, but
neither is an authority to submit.

Rejected alternatives:

- A generic web-scraping/browser agent roaming Swiss boards would not establish
  permission, provenance, stable identities, or reliable form safety.
- Running the entire pipeline inside a Nuxt request would lose progress on
  refresh/time-out and create ambiguous retries.
- Keeping only a manual per-offer approval would not meet the candidate's
  unattended-application objective, though it remains the fallback for
  unsupported sites and sensitive questions.

## Source acquisition and normalization

1. Keep an operator-reviewed catalog of employer ATS boards, with source key,
   employer identity, board token, country, provenance URL, access decision,
   collection/storage conditions, request budget, and review timestamp. It may
   be seeded by existing company-discovery data or operator curation, but no
   discovered board becomes enabled merely because a page links to it.
2. The existing ATS source adapters fetch only approved, enabled boards at
   bounded rates. A separate source-access check runs when a task is enqueued
   and again before the worker's external request. Unsupported or unreviewed
   boards return a typed, visible source warning; no live fetch occurs.
3. Persist normalized V2 `Opportunity` records with source key, employer,
   original listing URL, verified application URL if present, external posting
   ID, observation time, language, and location confidence. Do not fabricate
   Swiss coordinates or silently infer CH from a generic company address.
   Dedupe by source posting ID and verified canonical target where available.
4. Feed persisted CH opportunities to list and map. An opportunity without
   trustworthy coordinates remains in the list. Counts and health distinguish
   no matches from no eligible source, partial results, and run failure.
5. Initial acceptance needs at least one reviewed employer board on each of
   Greenhouse and Lever, each returning a valid CH posting in a bounded,
   timestamped, non-submitting live discovery check. This proves two ATS feeds,
   not two job boards or broad market coverage. If conditions or results do not
   allow it, report the gate as unverified and keep the source disabled.

## Per-opportunity workflow

The Morning Run has explicit, durable stages:

`DISCOVERED -> MATCHED -> ELIGIBILITY_CHECKED -> DOCUMENTS_RENDERED ->
APPLICATION_PREPARED -> SUBMISSION_QUEUED -> SUBMITTING -> CONFIRMED`.

The terminal alternatives include `SKIPPED`, `REQUIRES_HUMAN`, `FAILED`, and
`SUBMISSION_STATE_UNKNOWN`; they do not collapse into `CONFIRMED`.

- Matching and eligibility use their existing deterministic V2 results and
  record score, evidence coverage, exclusions, and policy version. An unknown
  or legally sensitive answer cannot become an inferred positive answer.
- The provider-neutral LLM receives only the necessary candidate evidence and
  listing content. It may generate typed draft documents and answer proposals.
  The evidence guard checks every candidate claim; only rendered, guard-cleared
  and pinned CV/letter versions may be uploaded. Provider unavailability yields
  a visible stage failure or review requirement, never a fabricated fallback.
- The application service creates an idempotent record for the user,
  opportunity and channel. Preparing a browser application validates the
  actual target URL, recognized driver, form fingerprint, required fields,
  candidate contact data, uploads, and any human-required question.
- The existing browser adapter's review-only safety ceiling stays in force by
  default. Promotion of a particular ATS form variant to fully supported
  requires documented access conditions, fixture tests, controlled non-send
  preflight evidence, explicit operator activation, and a narrowly scoped
  adapter capability. Unknown variants remain review-only. A platform-wide
  change from `SUPPORTED_WITH_REVIEW` to `FULLY_SUPPORTED` is forbidden.
- Before the one irreversible Playwright action, the worker rereads current
  ownership, candidate policy, approval brake, allowed opportunity type,
  match/eligibility result, source/form activation, pinned documents,
  per-user rate reservation and live-send switch. A policy changed after
  discovery can only reduce authority. The worker reserves and audits the
  attempt before clicking. No unverified retry follows a click.
- A positive confirmation makes an application `CONFIRMED`. A click without
  confirmation is `SUBMISSION_STATE_UNKNOWN` and requires human reconciliation;
  it must not be retried automatically.

## Safety, privacy and recovery

- Default deployment state is discovery and live submission disabled until
  their separate operator gates are reviewed. The candidate's AUTOPILOT policy
  and configured daily limit are necessary but not sufficient for live sends.
- CAPTCHA, MFA, login walls, unknown required fields, sensitive declarations,
  changed or ambiguous forms, and unsupported hosts become `REQUIRES_HUMAN`.
  No bypass or anti-bot evasion is attempted.
- User ownership is derived from the authenticated session, never supplied by
  a model or URL parameter. Worker tasks and all artifacts are owner-scoped.
  Logs and source-health reports use fixed, secret-free codes; raw page bodies,
  credentials and candidate evidence are not copied into error messages.
- Retries are limited to reversible stages. Source failures preserve results
  from other sources. A stranded in-flight submission is reconciled as
  unknown, never assumed failed and resent.
- Each user's daily cap counts durable submission reservations/attempts across
  searches and scheduled/manual triggers, with concurrency-safe enforcement.
  The UI shows the cap, consumption, and why an opportunity was skipped.

## UI and observability

Settings exposes the CH schedule, daily cap, source/form activation status,
LLM readiness, and a clear distinction between discovery, preparation and live
submission. The run detail shows per-source health and per-opportunity stages,
links to the pinned documents and application audit trail, and offers a human
handoff where needed. Map/list updates reflect persisted backend data. All new
copy is fully FR/EN; mobile layouts, keyboard navigation, focus, and live status
announcements are tested. A 202 response is displayed as queued, not success.

## Implementation and acceptance boundaries

The implementation plan must split work into one approved repository phase at
a time, respecting `docs/IMPLEMENTATION_PLAN.md`. Suggested order: (1) reviewed
CH employer-board catalog and non-submitting discovery; (2) durable downstream
matching/document orchestration; (3) narrowly activated safe browser routing;
(4) local end-to-end acceptance and UI polish. A phase stops for user validation
before the next begins.

Acceptance evidence requires:

1. Synthetic two-user CH runs, duplicate ticks, Zurich DST and multiple search
   profiles never duplicate opportunities, documents or submission attempts.
2. Fixture Greenhouse and Lever feeds each yield CH offers; source failure is
   reported without losing other results. A separate bounded live discovery
   check records actual source names, timestamps, counts and conditions when
   permitted. No test submits to an employer.
3. A fixture opportunity traverses scoring, eligibility, evidence-grounded
   document rendering, browser preparation, policy/rate checks, and a simulated
   confirmed submission. Fixture CAPTCHA/MFA/ambiguous question and absent
   confirmation take the human/unknown paths.
4. At 320, 390, 768 and desktop widths, FR and EN UI exposes truthful states
   for Settings, opportunity list/map, document review and application trail.
5. Targeted and full Python/frontend/browser suites, lint/typecheck/build,
   migration checks and isolated Docker local smoke pass on the final tree.
   A passing fixture suite alone is not a production-ready declaration.

No real employer contact or application submission is included in automated
acceptance. Claiming local production readiness additionally requires explicit
operator review of source/form conditions and an observed real environment
rehearsal that does not send without the candidate's live-send configuration.

## Review focus

The critical design decisions are the replacement of two independent job
boards with Greenhouse plus Lever Swiss employer feeds; the empty starting
employer catalog; the review-only current browser adapter ceiling; the split
between public listing access and authorized form submission; and the rule
that an ambiguous click is never retried. None may be hidden by a generic
"Morning Run successful" status.
