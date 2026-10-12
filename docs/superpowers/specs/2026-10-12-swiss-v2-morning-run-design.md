# Swiss V2 morning run — design

Date: 2026-10-12. Status: written specification for user review.

## Purpose and delivery boundary

The candidate wants one honest local Swiss morning run that searches several
sources, records what each source actually returned, and presents the results in
the V2 opportunity list and map. The run must not claim that discovering a job
also generated a tailored dossier or submitted an application. This is the first
of five ordered repairs: V2 morning run, backend LLM connection, safe application
routing, full local acceptance, and real-profile document quality. Each repair
has its own implementation and validation gate under `docs/IMPLEMENTATION_PLAN.md`.

The agreed application boundary is multi-source discovery with automatic sending
only through explicitly supported and authorized ATS forms. Other boards require
a human hand-off. No real application is submitted in tests. CAPTCHA, MFA, login
walls, ambiguous questions, and unknown required fields never receive automated
answers or bypasses.

Only Switzerland is active. LinkedIn remains disabled. This design does not
activate `jobs.ch`, add another country, or assert that every existing source is
permitted, healthy, or covers every Swiss vacancy. Source-specific use must be
reviewed before live activation; an adapter's presence is not permission to use
it. The candidate's previously exposed gateway credential must not be copied to
fixtures, logs, commits, or the design.

## Existing system and problem

The CH Country Pack registers multiple discovery adapters. V2 can enqueue a
`MORNING` discovery task for an active, owned CH search and its general worker can
persist opportunities, source health, and warnings. The CLI must currently be
scheduled explicitly; it has no built-in morning timer. The Settings page's
schedule and “full pipeline” button still call the separate V1 run manager. A V1
success banner therefore says nothing about V2 discovery, documents, or
applications. `MANUAL` discovery from the map is a different V2 route.

## Chosen approach and alternatives

Use the persisted V2 task queue as the single morning-run authority. A
user-owned schedule selects active CH saved searches and enqueues one MORNING task
per eligible search for each Swiss local calendar day. The existing V2 discovery
worker performs collection. The UI shows the per-search queued/running/partial/
failed/completed state and source-level coverage. It does not turn an enqueue
acknowledgement into a “pipeline completed” notification.

Keeping the V1 scheduler and copying its results into V2 would preserve two
sources of truth and two idempotency systems. Running a long collection directly
in a Nuxt request would lose durability on refresh or timeout. Neither is
selected. Existing V1 routes remain functional during migration but are clearly
labelled as legacy; they cannot silently activate V2 automation.

## Components and data flow

1. **Schedule and trigger.** FastAPI stores an owner-scoped V2 schedule with
   enabled state, local time, cadence, and CH scope. A scheduler enqueues through
   the existing `DiscoveryEnqueueService`, never through `server.runs.RunManager`.
   Manual V2 “run now” uses the same owner-scoped search selection but an explicit
   trigger; it cannot change the persisted schedule.
2. **Idempotency.** Enqueue uses the existing Swiss-day MORNING idempotency key
   per user and saved search. Concurrent scheduler replicas and repeated clicks
   must not produce duplicate work. Time-zone conversion is `Europe/Zurich`,
   including daylight-saving changes. A disabled schedule or inactive/non-CH
   search enqueues nothing.
3. **Collection.** The general worker uses the CH Country Pack registry, records
   each source's health and warnings, deduplicates and normalizes opportunities,
   and writes them under the correct owner/search. A failed source cannot make
   other healthy sources' results disappear. Sources requiring configuration
   report that state rather than pretending to return zero vacancies.
4. **Presentation.** Settings shows the V2 schedule and action separately from
   legacy controls. The map/list reads persisted V2 opportunities and run status.
   It distinguishes no matches from partial coverage, failure, and still queued.
   Its result counts and markers are backend-derived; unlocated offers remain
   visible in the list without invented coordinates. FR/EN status, retry guidance,
   progress, and accessible live feedback cover all states.
5. **Downstream boundary.** Matching, LLM documents, and application preparation
   remain independently observable stages until their later repairs pass. The
   morning run may show those stages as unavailable or pending, not successful.
   It never queues a browser submission merely because an opportunity was found.

## Source eligibility

The run reports the actual enabled/selected sources, attempted sources, and each
source's health. “Several sources” means at least two independently successful,
reviewed CH discovery sources in the local acceptance evidence; merely having
two names in YAML is insufficient. If fewer than two meet that bar, the UI says
multi-source coverage is unverified and the V2 milestone is not approved.

Fixtures prove orchestration, not live source availability. Before claiming
multi-jobboard operation, a separate bounded local dry-run must record at least
two reviewed CH job-board or aggregator sources returning valid Swiss listings.
If current access conditions or source health prevent that, report the blocker
and seek an authorized integration; do not quietly substitute two employer ATS
boards for the promised multi-jobboard coverage.

Discovery and application capabilities are separate. A listing URL is never
promoted to a verified application URL. Only a supported ATS driver and current
user policy can permit an irreversible submit later; all other targets retain a
clear manual hand-off. Source rate limits and access conditions are checked
before enabling live collection, with no anti-bot evasion.

## Failure and security behavior

- Missing owner, inactive search, or non-CH search: typed refusal with no task.
- Duplicate same-day MORNING request: return the existing task identity and
  status; do not create a second collection or duplicate opportunity.
- Worker outage: show queued/stalled status, not a successful run.
- Partial or misconfigured source: preserve valid prior/other-source offers and
  show the warning and source name without leaking upstream responses or secrets.
- API outage: localized error and retry; no optimistic success toast.
- Schedule save and run trigger: CSRF/session and owner checks, bounded inputs,
  and no cross-user task, search, result, or schedule visibility.
- Automated tests: synthetic source responses and local fixtures only; no real
  LLM credential, external ATS, or application submission.

## Acceptance for this repair

1. A synthetic local user can activate a Swiss morning schedule, observe one
   MORNING task per active CH search on its due day, and see no duplicate on a
   second tick. Another user cannot see or trigger that user's tasks or results.
2. The general worker persists opportunities from at least two independent
   CH source fixtures, preserves partial results when another source fails,
   and exposes source health and warning codes in the API and UI. A separate
   bounded, authorized dry-run records actual Swiss listings from at least two
   reviewed job-board or aggregator sources before live multi-source operation
   is claimed.
3. The Settings trigger and schedule use V2 endpoints only. V1 controls, if
   retained, are visibly legacy. The map/list reflects the V2 persisted results.
4. FR/EN desktop and mobile checks cover queued, running, empty, partial, failed,
   and completed states, including keyboard and screen-reader feedback.
5. Full targeted and project suites, type/lint checks, Docker/local worker smoke
   tests, and a fixture-only browser scenario pass. Any unavailable check is
   reported by name rather than treated as success.
6. No real application is sent; no claim of a production-ready full application
   workflow is made until the later LLM, submission, acceptance, and document
   quality gates pass.

## Review focus

The highest-risk edges are a DST boundary producing duplicate days; a schedule
update while a task is queued; a single source failing after others succeeded;
two users with similarly named saved searches; a job with a listing URL but no
verified application form; and a front-end success notification before the
worker has completed. Each must have a regression test in the implementation
plan.
