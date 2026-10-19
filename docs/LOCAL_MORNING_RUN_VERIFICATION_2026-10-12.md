# Swiss V2 morning-run verification — 12 October 2026

## Verdict

The owner-scoped V2 schedule, queue, worker boundary, and FR/EN Settings controls
are implemented and verified with synthetic data. **This is not approval for live
multi-jobboard discovery or production-ready applications.** The source-access
review in `SWISS_SOURCE_REVIEW_2026-10-12.md` has approved no CH board yet. The
resident scheduler defaults to disabled. A queued discovery is not a completed
CV, cover letter, or candidature.

## Checks and evidence

| Check | Result |
| --- | --- |
| Python full suite | 4,204 passed, 1 skipped, 2 dependency warnings after the source gate and quality fixes. |
| Nuxt Vitest full suite | 636 passed, 69 files. |
| Playwright full suite | 232 passed after the Settings isolation change. |
| Nuxt typecheck and production build | Both passed. Build warns about a >500 kB chunk. |
| FastAPI OpenAPI JSON and generated TS comparison | Both matched. `npm run check:api` cannot run through npm on this macOS setup because the nested Python process loses the WeasyPrint library path; equivalent commands ran directly. |
| Ruff / mypy repository-wide | Both clean: Ruff 0 errors; mypy 0 errors in 321 backend source files. |
| `git diff --check`; Compose configuration | Passed. |

The browser suite uses strict fixture routing: unexpected `/api` requests fail.
Settings was checked at 320, 390, 768, and 1440 px in both locales, including
queued acknowledgement, partial source failure, map link, and terminal panel.
The simulated local candidate journey remained review-only, with no submission.
The six review cases (Swiss DST, schedule update, partial source failure, owner
isolation, unsupported form, and queued-versus-complete UI) have targeted tests
in the Python and frontend suites. This is fixture evidence, not live-source
evidence.

## Disposable Docker rehearsal

An initial attempt using only a different Compose project name exposed a
configuration flaw: fixed volume names caused the test Postgres container to
mount the existing local database volume. The original PostgreSQL restarted.
The attempt was stopped **before any migration or test write**. Afterwards the
original service reported healthy, accepted connections, and a read-only table
count succeeded; this does not prove comprehensive data integrity. The incident
is why `JOBSEARCH_VOLUME_PREFIX` now isolates *all* named Compose volumes while
preserving the normal names by default. `docs/OPERATIONS.md` warns against
project-name-only isolation.

A second rehearsal used a verified fresh volume prefix and separate ports.
Alembic migrated a new PostGIS database through revision 0029. Two synthetic
users, two active CH searches, and two due schedules were inserted. A one-shot
`--dry-run` counted two schedules and rolled back all queue writes (0 task rows).
The enabled one-shot scheduler then queued exactly two `OPPORTUNITY_DISCOVERY`
tasks on the `GENERAL` lane, and no browser task or application. Searches were
restricted to a deliberately nonexistent fixture source key, so no board was
selected. The one-shot worker attempted both and returned retryable
`UNEXPECTED_ERROR`; the discovery rows were `FAILED`, while the task rows
remained queued for retry. Thus this rehearsal proves container startup,
migration, owner-separated enqueue, and a safe failure boundary — **not** a
successful real collection or terminal success state. The isolated synthetic
containers, network, and volumes were removed. The original DB was checked
healthy afterwards.

## Remaining gates

1. Obtain and document authorization or official API eligibility for at least
   two independent Swiss job-board/aggregator feeds, including quotas, storage
   rules, and CH coverage. Then perform a bounded, non-submitting dry-run and
   record source keys, timestamp, valid result counts, and health. No such run
   was made here. The resident scheduler is disabled by default, but an
   authenticated manual discovery action is now refused by the server unless an
   operator allow-list selects reviewed sources. Operators must not enable live
   collection against unapproved sources.
2. Rehearse a successful fixture worker path in an isolated container with a
   registered fake source; the deliberately unknown source above tested failure
   only. Keep CAPTCHA/MFA and unsupported forms as human stops.
3. Complete the later gated LLM, safe application routing, local acceptance,
   and document-quality repairs before declaring the V2 application workflow
   ready. Real submissions were not tested or authorized in this verification.

## Files and migration notes

This milestone added the schedule domain/repository, V2 morning-run service and
routes, scheduler CLI, Compose service, typed Nuxt client and Settings section,
tests, source review, operations note, and this report. The new migration is
`backend/migrations/versions/rev_0029_morning_schedules.py`; it depends on the
currently untracked revisions 0026–0028. The working tree contains extensive
uncommitted user work, so no partial commit or PR was created. Review the full
working tree and migration ancestry before landing.

## Follow-up: server-side source gate

The manual API, map refresh, per-user CLI and scheduler now check a default-empty
`JOBSEARCH_DISCOVERY_ALLOWED_SOURCES` before enqueue. The general worker checks
again before contacting any source and limits unrestricted saved searches to
allowed keys. A queued task is not a permit. This removes the manual bypass
identified above, but approves no CH feed and does not validate applications.
The historical Docker rehearsal predates this gate; it is not evidence of a
successful live-source collection.

Verification after this follow-up: 4,204 Python tests passed, 1 skipped;
636 Nuxt tests passed; 232 Playwright tests passed; Nuxt typecheck and build,
the OpenAPI/TypeScript contract comparison, Docker Compose configuration,
`git diff --check`, Ruff and mypy all passed. No live source was queried and
no application was submitted. The earlier isolated Docker rehearsal was not
repeated after the guard; this remains an explicit acceptance limitation.
