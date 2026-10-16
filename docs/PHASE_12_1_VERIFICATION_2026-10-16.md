# Phase 12.1 — Playwright acceptance, 16 October 2026

## Scope and verdict

The local fixture acceptance for Greenhouse, Lever and Ashby passes. This
acceptance covers browser execution and its safety boundaries; it does not
activate real employer submissions or establish the readiness of the entire V2.
No real application was sent. Phase 17 was not started.

Work resumed on `phase-12-1-real-playwright-ats` from
`31966a85daba300e4fb94867ef533f495766c65b`, preserving the existing working tree.
The drivers and worker already existed; they were repaired incrementally.

## Committed checkpoints

| Commit | Behavior |
| --- | --- |
| `0beced8` | CV/letter uploads use recognized document slots, not DOM order; unknown slots receive no candidate file, duplicates are refused and candidate filenames are retained. |
| `a6fb2a6` | Confirmation requires a visible application receipt; generic text, hidden/empty placeholders and URL substrings do not establish success. Delayed receipts are awaited; an existing receipt prevents another click. |
| `10d53f6` | CAPTCHA, MFA, login and form structure are checked again after filling and before the click. |
| `5358605` | Extraction, filling, uploads and submit controls share one uniquely identified application form. Other forms on the page do not receive candidate data. |
| `d2a3974` | The existing interrupted policy-brake fix is checkpointed separately: policy writes serialize with the final authority check and browser action. Its PostgreSQL regression test is included. |
| `4215a29` | Docker and CI explicitly execute all five browser regression modules. |

Tests for each new defect failed before its fix and passed afterward. Existing
unrelated changes were not swept into these commits. Several files were staged
by exact hunk to preserve unrelated work in the same file.

## Final checks

| Check | Result |
| --- | --- |
| Full Python suite on the current working tree | 4,348 passed, 1 skipped, 2 dependency deprecation warnings |
| All five real Chromium regression modules, host | 66 passed |
| Fresh Docker browser-test image | 66 passed, no skipped browser cases; one WeasyPrint dependency warning |
| Nuxt unit tests, typecheck and production build | passed |
| Frontend Playwright suite | 244 passed |
| Ruff and mypy | passed; mypy checked 340 source files |
| OpenAPI JSON and generated TypeScript comparison | passed |
| Compose config, CI YAML parsing and whitespace checks | passed |
| Clean export of committed code at `4215a29` | 112 browser/engine/persistence tests plus 48 loader/API/worker tests passed |

The clean export was created with `git archive`, and its imported driver path
was checked before testing. It has no untracked files from the main working
tree. It proves the Phase 12.1 checkpoints independently of the later uncommitted
features. The full-tree and Docker results above include those preserved
features and must not be represented as a clean full-V2 release build.

The skipped test is the PostgreSQL backup/restore integration: `pg_dump`,
`pg_restore` and `psql` are absent from the host PATH. Browser checks did run.
The Nuxt build also reports a large client chunk; that warning did not fail it.

## Supported targets and hand-offs

| Target | V2 acceptance status |
| --- | --- |
| Greenhouse, Lever, Ashby | Dedicated drivers verified against local fixtures. Live employer variants remain unverified. |
| Umantis, Migros, Jobup, Welcome to the Jungle | Legacy implementations exist, but are not registered as V2 drivers. The V2 executor hands off without navigating an unsupported target. No automatic port was claimed. |
| Workday, LinkedIn, other hosts | Human hand-off through the unsupported-channel result. No login, CAPTCHA or MFA bypass. |

The Phase 12.1 plan limits automated fixture support to the three dedicated ATS
drivers. Registry nomination is not authority to submit: the owner policy,
document pins, reviewed-form controls and live-send switch remain applicable.
An unconfirmed click remains unknown and must never trigger an automatic retry.

## Local runtime and persistence

The browser-worker image was rebuilt and the local browser worker restarted
after checking there was no running browser task. Its environment still has
`JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED=false`. The Docker images were built
from the preserved working tree, not from the clean export. The candidate
database remains at `0032`; this continuation introduced no migration and did
not reset or remove candidate data. No API or general-worker rollout was needed
for the driver-only changes.

The pre-existing broader V2 changes remain in the working tree for separate
review and checkpointing. This report does not label that whole tree released,
nor does it approve any source or live employer form.
