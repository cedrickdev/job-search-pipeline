# Swiss ATS discovery foundation — local verification, 12 October 2026

## Verdict

The Swiss V2 discovery foundation is implemented and fixture-tested. It is **not**
live-source validated and does **not** make the end-to-end application workflow
production-ready. `config/ats_reviewed_boards.yaml` has no approved Greenhouse or
Lever board. The source allow-list remains default-empty. The API refuses a V2
Morning Run whose selected ATS source has no reviewed board. No application
submission was made during this verification.

## Evidence on the current working tree

| Check | Result |
| --- | --- |
| Python full suite | 4,241 passed, 1 skipped after the worker exit-code fix; 2 dependency deprecation warnings. |
| Nuxt unit/component suite | 637 passed in 69 files. |
| Targeted map and Settings browser suite | 17 passed across the map and Settings files after adding FR/EN API-outage cases; synthetic API and tile fixtures only. |
| Map and Settings responsive smoke | 16 passed: English and French at 320, 390, 768 and 1440 px, no document overflow. |
| Full Playwright suite | First run: 235 passed, 1 intermittent local-candidate-flow failure under 6 workers. That test passed alone; complete final rerun with 3 workers: 238 passed. |
| Nuxt typecheck and build | Passed. |
| OpenAPI snapshot and generated TypeScript type comparison | Passed using the direct Python command with `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`; the nested `npm run check:api` command loses that macOS library path on this host. |
| Ruff and mypy | Passed; mypy checked 325 backend files. |
| Docker Compose configuration and `git diff --check` | Passed. |

The focused database integration fixture persists one CH Greenhouse opportunity,
rejects an explicitly French posting, preserves the successful offer when Lever
times out, reports partial source health, enforces account isolation and leaves
the application-submission task count at zero. The map test keeps an unlocated
Swiss offer with its textual place in the list and does not assign a point.
The Settings browser tests show queue, partial coverage, zero healthy matches,
and the unapproved-source refusal without calling the V1 full run or claiming a
sent application. These are fixture results, not evidence of live employer
coverage.

## Disposable database rehearsal

An isolated Compose project (`jobsearchatsverify20261012b`) used a separately
verified new volume name, port 56433 for PostgreSQL/PostGIS and port 57380 for
Redis. Host integration tests targeted that project's `jobsearch_test` database
on the final tree: 30 passed, covering discovery persistence, ATS request
budgets and non-sending browser automation. The disposable project and
synthetic volume were removed. The original `jobsearch-postgres-1` was healthy
afterwards.

A second isolated project (`jobsearchatsworker20261012`) built the current
backend image, migrated a new PostgreSQL/PostGIS volume through revision 0030,
and ran the actual general-worker process with `--once`. Its synthetic Swiss
search selected Greenhouse while the reviewed board catalog stayed empty.
The task ended `DEAD_LETTERED` after one attempt with permanent reason
`DISCOVERY_SOURCE_UNAPPROVED`; the process exited 0 after processing one task.
Database checks found zero browser tasks, discovery runs and opportunities.
The same disposable Compose project was recreated with a fresh volume and a
read-only fixture script mounted outside the runtime image. It replaced the
Greenhouse and Lever fetch functions in memory before invoking the actual
`run_worker.main` entry point; no network request to an employer was possible.
The process exited 0 after one task. The task was `SUCCEEDED`, its discovery
run `PARTIAL`, with one CH Greenhouse offer in the owner's catalog, a Lever
timeout reflected as unavailable health, one durable request reservation per
source, no other-owner run, one list-only offer with no fabricated map point,
and zero browser tasks. The rehearsal script is
`tests/fixtures/ats_worker_container_rehearsal.py`. This proves the container
worker's fixture-wired path, not live employer coverage.
The original local Compose stack was not rebuilt or restarted during these
isolated rehearsals; its database remained healthy. The corrected worker image
must be rolled into that stack through the normal migration/restart procedure
after the untracked migration chain is reviewed.

## Source and migration gates

- Greenhouse and Lever here are ATS feed families, not two independent Swiss
  job boards. Both reviewed board lists remain empty; no live request or
  source-condition approval was performed. A bounded live discovery check is
  **UNVERIFIED**. JobCloud, Indeed and LinkedIn remain outside this activation.
- Migration `rev_0030_ats_request_budget.py` adds the durable per-board Swiss-day
  request ledger and depends on `rev_0029_morning_schedules.py`. Revisions
  0026–0029 and 0030 are all untracked in this working tree. The tests exercise
  the current files, but this is not a committed, deployable migration chain.
- Source budgets reserve before HTTP and fail closed on exhaustion or storage
  outage. A red-to-green regression test caught V1's hidden three-attempt
  retry loop; V2 reviewed ATS fetches now disable it so one reservation cannot
  cover multiple HTTP attempts. V1 keeps its prior retry behavior. This does
  not authorize collection under any employer's terms.
- An independent review raised a possible API hot-reload gap. The live server
  loads the immutable reviewed catalog in its lifespan before serving requests;
  a regression test confirms that an edit before the first request does not
  widen that process's snapshot. A restart remains required to adopt changes.
- The isolated worker rehearsal exposed a CLI exit-code defect: `--once` returned
  the number of drained tasks as the process code, so a nonempty successful
  drain appeared to fail. A failing regression test preceded the fix; the
  rebuilt image now exits 0 after draining one task.

## Files in this discovery-foundation work

New source and budget files: `config/ats_reviewed_boards.yaml`,
`backend/app/discovery/ats_board_catalog.py`,
`backend/app/discovery/adapters/budgeted_ats.py`,
`backend/app/repositories/ats_request_budget.py`, and
`backend/migrations/versions/rev_0030_ats_request_budget.py`.
`pipeline/http_fetch.py` adds a scoped retry override used only by the V2 ATS
adapter; the ordinary V1 default remains unchanged.
Composition and policy changes: `backend/app/discovery/bootstrap.py`,
`backend/app/discovery/contracts.py`, `backend/app/discovery/failures.py`,
`backend/app/discovery/normalization.py`,
`backend/app/services/discovery_source_access.py`, API startup, discovery,
scheduler and worker entry points, plus the CH country-pack metadata.
UI/contract changes: `frontend/app/composables/useMorningRun.ts`,
`frontend/app/pages/settings.vue`, `frontend/app/utils/discovery-messages.ts`,
`frontend/openapi.json`, `frontend/app/types/api.d.ts`, and their Nuxt and
Playwright tests. Source review and operations instructions were updated.
Many of these files were already modified before this work; no mixed commit
was made, and the full working-tree diff needs review before landing.
The container-worker follow-up also changes `backend/app/cli/run_worker.py`
and `tests/test_v2_worker_cli.py` for the exit-code correction, and adds the
read-only fixture harness `tests/fixtures/ats_worker_container_rehearsal.py`.

## Next validation gates

1. Review conditions for at least one Swiss-posting employer board on each of
   Greenhouse and Lever; only approved entries may be added to the catalog.
   Perform a bounded, non-submitting live check and record source, time,
   request count, CH results and warnings.
2. Review and commit migrations 0026–0030 together with their dependencies.
3. Plan and validate the later evidence-grounded matching/document and
   narrowly activated browser-submission phases separately. CAPTCHA, MFA and
   ambiguous forms remain human stops; no real submission belongs in tests.
