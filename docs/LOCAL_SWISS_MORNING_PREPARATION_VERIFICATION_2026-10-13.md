# Swiss Morning Run preparation — local verification (13 October 2026)

## Scope and result

This phase joins a Swiss discovery run to owner-scoped matching, policy decision,
and evidence-checked CV and cover-letter preparation. It does **not** send an
application. The fixture rehearsal used two synthetic accounts, a healthy ATS
source and an unavailable source; discovery remained `PARTIAL`, its healthy
opportunity advanced through the general worker, and a simulated interruption
after CV rendering resumed with the same CV version and one letter version.
There were zero `APPLICATION_SUBMISSION` tasks and zero browser-lane tasks.
The foreign account could not read the preparation.

## Checks on the final implementation

| Check | Result |
| --- | --- |
| Python suite on isolated PostgreSQL/PostGIS test database | 4,264 passed, 1 skipped, 2 dependency deprecation warnings |
| Synthetic worker restart rehearsal | 1 passed; one CV and one letter version |
| Ruff | passed |
| mypy (`backend country_packs`) | passed, 337 source files |
| Nuxt unit tests | 640 passed in 69 files |
| Nuxt typecheck and production build | passed |
| OpenAPI JSON and generated TypeScript comparison | passed |
| Playwright fixture suite | 240 passed; no real employer submission |
| `docker compose config --quiet` | passed |
| `git diff --check` | passed |

The OpenAPI comparison was run directly with the local WeasyPrint library path.
The npm wrapper does not preserve that macOS loader setting on this machine;
the JSON and generated types both match. The synthetic database was migrated
from empty to Alembic revision `0031`; `alembic_version` was checked in the
isolated container. The isolated Compose project used the explicit
`jobsearch-prep-20261013` volume prefix, not the candidate's existing volume.

## Relevant changes

- `backend/app/domain/morning_preparation.py`,
  `backend/app/repositories/morning_preparation.py`, and
  `backend/migrations/versions/rev_0031_morning_preparation.py` add the
  owner/run/opportunity checkpoint. Equal-time stage transitions now persist
  the supplied domain clock instead of inheriting the database update clock.
- `backend/app/services/discovery_catalog.py`,
  `backend/app/services/morning_preparation.py`,
  `backend/app/services/morning_documents.py`, the task handlers and worker
  wiring provide a durable discovery-to-document path. CV and letter have
  separate committed checkpoints; retries use pinned version IDs.
- `backend/app/api/routes/morning_run.py`, the document-version download route,
  `frontend/app/composables/useMorningRun.ts`, and the settings view expose
  owner-scoped progress, terminal task failures, and exact rendered PDFs in
  English and French. They do not label preparation as submission.
- The preparation, API, discovery, and worker integration tests cover partial
  discovery, retry, ownership, policy changes, and the absence of browser work.

Migration `0031` depends on the still-untracked `0026`–`0030` chain. It must
not be committed or deployed alone. This report does not claim a clean
worktree; existing uncommitted changes were preserved.

## Remaining gates and risks

- Swiss live ATS boards are not yet approved for a bounded, non-submitting
  source check. Fixture success is not evidence that live sites will work.
- No real application has been sent or validated. Browser preparation,
  site-specific form handling, safety review, and a separate submission phase
  are still required before end-to-end automatic candidacy can be claimed.
- Document generation currently holds a database row lock while awaiting the
  provider. A slow or unavailable provider can lengthen lock contention.
  Policy re-evaluation also records a decision at each document stage, so
  audit volume should be reviewed before sustained live use.
- A crash after artifact bytes are written but before the database transaction
  commits can leave orphaned storage objects; the pinned checkpoint prevents
  duplicate *committed* CV versions, not all temporary bytes.
- The two suite warnings concern Starlette/httpx and AnyIO deprecations. They
  did not fail this run but should be tracked during dependency upgrades.

The repository's phase gate requires user validation before starting browser
preparation or any real application-submission work.
