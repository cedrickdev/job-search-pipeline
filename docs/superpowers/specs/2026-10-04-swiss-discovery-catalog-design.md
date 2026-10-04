# Swiss discovery catalog — focused design

Date: 2026-10-04. Status: proposed for user review. Country: CH only.

## Outcome and boundaries

A candidate with active Swiss saved searches can run discovery manually or through
the morning run, then browse the collected opportunities in the existing V2 map
and list. The catalog survives process restarts. Results include source provenance,
freshness and an honest account of partial source failures. This is a discovery
delivery, not permission to submit applications. The existing submission safety
gate and its default-disabled live behavior remain unchanged.

The first catalog contains employment opportunities, student jobs, internships,
apprenticeships and work-study roles using the existing `Opportunity` vocabulary.
Courses, certifications and schools without employment contracts belong to a
separate training catalog and are not shoehorned into `Opportunity`. They receive
their own later design, plan and validation gate. LinkedIn is excluded from this
delivery: no credentials, authenticated crawling, email-alert import or browser
login. The user's application contact address is a separate onboarding/settings
concern; this discovery phase does not need mailbox access and does not store a
mail password. Other countries remain disabled pending their own Country Pack
validation.

This is one gated implementation increment alongside the roadmap in
`docs/IMPLEMENTATION_PLAN.md`, not a claim that the whole Swiss autopilot or Phase
17 is complete. The prior Phase 12.1 working-tree changes must be preserved.

## Existing seams and chosen approach

Reuse `SearchProfile` → CH Country Pack → `DiscoveryOrchestrator.sweep()` and its
typed `SweepReport`; add a persistence service and owner-scoped run interface
around them. Reuse the PostgreSQL/PostGIS `Opportunity` repository and the Phase
7/8 geo API/map rather than creating a second job tracker or moving business
logic into Nuxt. Keep source selection declarative through the Country Pack.

The alternatives considered were (a) an ephemeral map fed directly by each
sweep, which cannot retain results or explain historical source failure, and
(b) a separate Swiss-only jobs database, which would duplicate the canonical V2
opportunity model. Both are rejected. A persisted, country-neutral catalog using
the existing model is the recommended and approved direction.

## Collection and persistence flow

1. An authenticated candidate selects an active saved search they own, or a
   morning run enumerates that candidate's active CH searches. No inactive or
   other-user search can be run on their behalf. A user without an active CH
   search gets an explicit empty-state response, not a country-wide sweep.
2. The existing request mapper and CH pack select enabled, reviewed sources. The
   LinkedIn binding is disabled for this delivery; source failures, 403/429
   responses and parsing warnings are reported, not bypassed. Additional boards
   such as jobs.ch require a source-specific capability and usage review before
   enablement; this delivery makes no promise to cover every Swiss board.
3. The sweep runs with bounded concurrency and returns opportunities, warnings,
   metrics and per-source health. A single failed source does not erase usable
   results from others. The run records start/end time, owning user and search,
   country, status, counts, source health and sanitized warning codes. Do not
   persist credentials, cookies or sensitive candidate data in run payloads.
4. Persist source observations idempotently by `(source_key, external_id)` where
   present, with a normalized canonical URL fallback. Preserve every public
   source URL, fetch time and raw public posting metadata as provenance. A
   conservative canonical-target key may group cross-board copies; title and
   company alone are not enough to merge two vacancies. Ambiguous pairs remain
   separate. A repeated sweep refreshes last-seen and changed public fields
   without generating a new visible offer. Database uniqueness and transaction
   handling enforce this under concurrent runs.
5. Associate each run's matched opportunity IDs with the owner's saved search so
   the UI can show why an offer was found. Public job postings may share one
   catalog record across users, but search membership, history and any later
   score/application state are user-owned. Filtering is server-side according to
   the saved search; collection alone does not authorize a future application.
6. An offer absent from a partial sweep remains available with its prior
   last-seen timestamp. Expiry requires reliable positive evidence or a separately
   documented stale policy, never one incomplete or failed sweep. A user can
   distinguish newly seen, updated and previously seen offers.

## API, map and execution

Expose owner-scoped endpoints to start a run, inspect its status and source
report, and list results for a saved search. Run requests are idempotent and
bounded: concurrent duplicate requests coalesce or reject predictably, and
rate/volume limits protect source sites and workers. The same service handles
manual and morning-run triggers; the trigger type is recorded. If the existing
task infrastructure can host the run, use its general lane and durable status.
Scheduling must be explicitly configured per user and deployment; no hidden
background schedule is enabled just by opening the UI.

The existing geo search and map remain the display path. Results must stay in
sync between the list and map. Server-provided coordinates may create pins;
unknown, remote-only or unresolved locations remain in the list without fabricated
coordinates. A collection failure does not break browsing the last successful
catalog. The UI displays last refresh, incomplete-coverage warnings, a manual
refresh action and actionable source states in French and English. It must not
imply that zero results from an incomplete run means zero jobs exist.

## Security, reliability and testing

Authorize every run/status/result operation by `user_id` and search ownership,
including worker re-entry and pagination. Public opportunity records are not a
backdoor to another user's search, candidate profile, match score or application.
Sanitize source text before display, treat descriptions as untrusted data, and
prevent unsafe link schemes. Network calls use finite timeouts, bounded parallelism
and explicit source policies. Do not add CAPTCHA/MFA bypass, anti-bot evasion,
LinkedIn sessions or broad mail access.

Tests use synthetic sources and fixtures: saved-search request mapping; owner
isolation; manual/morning-run parity; repeated and concurrent sweep idempotency;
multi-source provenance; conservative deduplication; partial failures and source
health; persistence after restart; no false expiry; map/list agreement and
unlocated offers; French/English states; and no application submission. Migration
tests cover upgrade and rollback of any added catalog/run tables. Verify the local
Docker database, API, frontend build and relevant test suites. A live-source smoke
test, if run, is read-only and reports source limitations without promising
coverage or production readiness.

## Completion boundary

At the end of this increment, report changed files, migrations, tests and known
source gaps, request explicit validation, and stop. Training catalog, LinkedIn,
mailbox access, new countries and autonomous live submission are separate gated
deliveries.
