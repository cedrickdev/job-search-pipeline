# Swiss safe browser routing — local verification, 13 October 2026

## Verdict

The Morning Run can hand an exact, rendered Swiss dossier to an owner-scoped
browser application, preserve the pinned CV and letter versions, and queue a
submission only for a fixture-approved Greenhouse or Lever form when the
deployment switch is explicitly enabled. This is a **synthetic acceptance**, not
authorization to contact or apply to a real employer. No real application was
submitted during these tests.

The packaged `config/ats_approved_forms.yaml` contains `forms: []`, the Swiss
ATS board catalog has no reviewed board, and Compose defaults
`JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED` to `false`. Later on 13 October,
the candidate's local PostgreSQL database was backed up, restoration-tested,
and migrated from `0028` to `0032`; API and worker images were rebuilt and
restarted. This removes the local code/schema mismatch, but the V2 must not
be called ready for automatic real applications.

## Verification

| Check | Result |
| --- | --- |
| Python full suite, dedicated `jobsearch_test` database | 4,307 passed, 1 skipped, 2 dependency deprecation warnings after the policy-brake fix |
| Two independent PostgreSQL connections, two Morning Runs, same offer | 1 passed: one application, one queue task, zero premature attempts |
| Existing daily-cap reservation race and synthetic Greenhouse/Lever outcomes | 5 focused tests passed |
| Nuxt unit/component tests | 650 passed in 69 files |
| Nuxt typecheck and production build | passed; build warned about a large client chunk |
| Full Playwright browser fixture suite | 244 passed; no employer contacted |
| Ruff and mypy | passed; mypy checked 340 files |
| OpenAPI JSON and generated TypeScript | both match, with direct Python check using the macOS WeasyPrint library path |
| Compose configuration and whitespace check | passed |
| Local stack after migration and rebuild | API OpenAPI 200, frontend login 200, unauthenticated Morning Run 401, API healthy; API and browser-worker live switches false |
| Rebuilt Docker Chromium fixture image | 25 passed, 1 WeasyPrint deprecation warning; no employer contacted |
| Policy pause versus click regression | failed before the fix, passed afterward on independent PostgreSQL sessions; policy update waits for the in-flight browser action |

The full browser suite covers FR/EN layout at phone, tablet and desktop widths.
It uses fixture API and map responses; passing does not establish that live map
tiles, every real job feed or employer forms will work. The direct OpenAPI check
was necessary because the nested npm wrapper loses `DYLD_FALLBACK_LIBRARY_PATH`
on this macOS host. The initial concurrent Python runs collided on the *test*
database and produced one invalid migration result; the final full suite was
rerun serially. Neither run touched `jobsearch_dev`.

Before the coordinated local upgrade, a custom PostgreSQL dump was saved as
`var/backups/jobsearch-pre-v2-20261013.dump` with mode `0600` and restored into
the distinct scratch database `jobsearch_verify_20261013`. The restored
revision (`0028`) and counts (34 users, 358 opportunities, 2 applications,
6 tasks) matched the source. The scratch database was then removed. After
migrations `0029`–`0032`, the local database reports `0032` with those same
counts; the application containers were rebuilt and restarted. The backup is
still retained locally. This is a schema and service smoke check, not an
authenticated end-to-end live candidature test.

An earlier isolated PostgreSQL/PostGIS rehearsal in this phase migrated a new
database from empty to `0032`, downgraded to `0031`, then reapplied `0032`;
four synthetic browser integration scenarios passed on that revision. A
separate rebuilt browser-worker image passed 25 Chromium fixture checks. The
temporary synthetic volumes were removed after their identities were checked.

## Changes and migration chain

- `backend/app/application_engine/browser/form_capabilities.py` and
  `config/ats_approved_forms.yaml`: exact, default-deny reviewed-form authority.
- `backend/app/services/applications.py`, `backend/app/services/application_queue.py`,
  `backend/app/services/morning_application.py` and browser worker/handlers:
  owner-scoped preparation, exact document pins, live-switch and final-form
  rechecks, durable reservation and unknown-outcome handling.
- `backend/app/tasks/handlers.py` and
  `backend/app/repositories/sqlalchemy_repositories.py`: a shared per-owner
  advisory lock now serializes policy writes against the final authority check
  and browser action. The policy-pause acknowledgement cannot overtake the
  irreversible click; this adds one held database connection per active send.
- `frontend/app/pages/settings.vue`, `frontend/app/pages/applications.vue`,
  `frontend/app/composables/useMorningRun.ts`, `frontend/openapi.json` and
  `frontend/app/types/api.d.ts`: bilingual, owner-scoped progress and audit links.
- `tests/test_v2_morning_browser_integration.py` and focused persistence/browser
  tests: synthetic end-to-end and independent-connection duplicate-run checks.
- `backend/migrations/versions/rev_0032_swiss_browser_routing.py` adds the
  reviewed-form identifiers and browser preparation task kind. It depends on
  the still-untracked `0026`–`0031` chain; none of these migrations should be
  landed or deployed alone. This worktree contains many pre-existing changes
  that were preserved, not swept into a commit.

## Remaining live-activation gates

1. Review actual Swiss employer feed terms and approve bounded source entries;
   review exact employer form variants and conditions before any catalog entry.
   LinkedIn and unapproved job boards remain out of scope.
2. Review the remaining live-activation boundary: the policy-brake race is
   serialized for writes through the policy repository, but a form catalog or
   employer page can still change after a check, and an external write bypassing
   that repository does not participate in the lock. Keep the live switch off
   until this operational protocol and exact target forms are reviewed.
3. Complete an authenticated local candidate rehearsal on the rebuilt stack:
   sign-in, discovery from a reviewed source, documents and browser preflight
   without sending. The database migration and coordinated service rollout are
   complete, but this user journey is not yet proven on real postings.
4. Reconcile any `SUBMISSION_STATE_UNKNOWN` attempt manually; it is deliberately
   never auto-retried. CAPTCHA, MFA, login walls and ambiguous fields require a
   human hand-off, not bypass or speculative submission.

Per `AGENTS.md`, this phase stops for user validation before live activation or
the next phase. Its fixture success is not a claim of V2 production readiness.
