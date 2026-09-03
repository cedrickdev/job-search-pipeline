# V1 Baseline — Phase 0

Written during Phase 0 of [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md). It
records what V1 *is* and what its checks *currently report*, so that from Phase 1
on, any new failure is distinguishable from inherited debt.

Nothing in V1 was modified to produce this document. Phase 0 added tooling
configuration only: `[tool.ruff]` / `[tool.mypy]` in `pyproject.toml` and
`.github/workflows/ci.yml`.

Measured on 2026-09-03, commit `8ac28fd`, macOS, Python 3.14.7, Node 22.22.2.

Phase 0.1 amended §2, §9 and §10: the suite no longer needs the operator's
private profile, so a clean clone runs all 460 tests with nothing deselected.
It touched three V1 files, all behaviour-preserving at the default path
(`pipeline/paths.py`, `pipeline/cv_render.py`, `tests/conftest.py`).

## 1. Reproducing the checks

```bash
.venv/bin/python -m pip install -e ".[dev]"   # pytest, ruff, mypy, stubs
.venv/bin/ruff check .                        # lint          — must pass
.venv/bin/mypy                                # V1 types      — baseline, see §8
.venv/bin/python -m pytest -q                 # backend tests — 460, no setup
npm --prefix webapp ci
npm --prefix webapp test                      # frontend tests
npm --prefix webapp run build                 # tsc --noEmit && vite build
```

`requires-python` is `>=3.12` and Ruff/mypy target `py312`; the local venv runs
3.14.7 and CI runs 3.12, so both ends of the supported range are exercised.

## 2. Baseline results

| Check | Working tree | Clean `git clone` |
|---|---|---|
| `pytest` | 460 passed | 460 passed — §9 |
| `ruff check .` | passes (151 files) | passes |
| `mypy` | 22 errors / 15 files | 22 errors / 15 files |
| `vitest` | 75 passed, 16 files | 75 passed, 16 files |
| `npm run build` | passes | passes |

One warning is expected and harmless: Starlette announces that `TestClient` on
`httpx` is deprecated in favour of `httpx2`.

Those numbers are the **V1** baseline and stay fixed as V2 grows: they are what a
regression is measured against. From Phase 1 on, `pytest` reports more — 877
passed = these 460 plus 417 in `tests/test_v2_*.py` — and `mypy` still reports 22
errors in 15 files because a bare run checks `pipeline`, `server` and `dashboard`
only. `mypy backend` is a separate, and clean, check (see pyproject.toml).

No check contacts a job board or an LLM provider: every source adapter test reads
a file from `tests/fixtures/`, and the copilot tests inject a fake subprocess or
an `httpx.MockTransport`.

## 3. Architecture as built

V1 is a single-user, local-only tool. There is no authentication layer anywhere,
because there is no second user and nothing listens beyond `127.0.0.1` — the
first structural gap between V1 and the multi-user V2 target.

```text
config/searches.yaml ─┐
config/companies.yaml ─┴─> pipeline.discover ──> jobs (SQLite)
                                                  │
   scoring / tailoring, driven by the /morning-run skill
                                                  │
                                      scores, cv_versions, applications
                                                  │
   server/ (FastAPI) ──> webapp/ (React SPA, served from the same process)
```

| Layer | Location | Files / lines |
|---|---|---|
| Deterministic core | `pipeline/` | 28 / 2959 |
| Source adapters | `pipeline/sources/` | 15 / 638 |
| Application adapters | `pipeline/apply/` | 12 / 1570 |
| HTTP layer | `server/` | 16 / 1776 |
| Route modules | `server/routes/` | 12 / 599 |
| Static export | `dashboard/` | 2 / 139 |
| Operator scripts | `scripts/` | 6 / 933 |
| Frontend | `webapp/src/` | 47 TS/TSX files |

`server/app.py::create_app(db_path, settings_path, spa_dist)` is the only HTTP
entry point. It takes all three paths as arguments, which is why the test suite
can run a real app against a `tmp_path` database — worth preserving in V2.

Routers are registered first and the SPA fallback last, so `/api` 404s keep their
JSON body while deep links fall back to `index.html`.

Two run kinds share one `RunManager` guard (`server/runs.py`): `discovery` execs
`python -m pipeline.discover`, and `full` execs `scripts/morning_run.sh`, which
drives Claude Code through the `/morning-run` skill. A second trigger while one
is in flight returns HTTP 409.

The application status machine is a fixed 16-state list in `pipeline/statuses.py`:
Discovered, Scored, Borderline, Ready to apply, Approved, Applied, Phone Screen,
Needs you, Duplicate, Recruiter reply, Interview scheduled, Offer, Rejected,
Ghosted, Withdrawn, Archived.

## 4. Database schema

SQLite, created by `pipeline/db.py::init_db()` (idempotent, also called from
`create_app`). Dumped from `init_db` rather than from `data/tracker.db`, so this
reflects the code and not local drift. Integer autoincrement keys throughout.

| Table | Cols | Columns |
|---|---|---|
| `applications` | 17 | id, job_id, cv_version_id, cover_letter_path, status, channel, submitted_at, confirmation_screenshot, recruiter_email, created_at, scored_at, tailored_at, approved_at, phone_screen_at, interview_at, offer_at, rejected_at |
| `apply_requests` | 8 | id, job_id, status, detail, channel, screenshot_path, created_at, resolved_at |
| `chat_messages` | 6 | id, scope, scope_id, role, text, created_at |
| `chat_sessions` | 4 | scope, scope_id, claude_session_id, updated_at |
| `cv_versions` | 8 | id, job_id, language, pdf_path, content_hash, diff_summary, created_at, phone_screen_pct |
| `events` | 7 | id, application_id, job_id, event_type, detail, source, created_at |
| `followup_overrides` | 4 | application_id, snooze_until, dismissed, updated_at |
| `interview_log` | 7 | id, application_id, round_label, scheduled_for, outcome, notes, created_at |
| `jobs` | 15 | id, source, company, title, url, location, remote_policy, contract_type, salary, description, language, posted_date, dedup_hash, discovered_date, track |
| `prep_cache` | 5 | application_id, likely_questions_json, company_research_json, talking_points_json, generated_at |
| `prep_notes` | 3 | application_id, notes_md, updated_at |
| `regen_requests` | 8 | id, job_id, notes, status, created_at, resolved_at, creativity, detail |
| `runs` | 5 | id, kind, started_at, finished_at, summary |
| `scores` | 7 | id, job_id, score, reasoning, red_flags, scorer_version, created_at |

One explicit index, `idx_chat_messages_scope`. Everything else relies on implicit
primary-key indexes, including the `jobs.dedup_hash` lookup that every discovery
sweep performs per posting — cheap at the current scale, a Phase 2 concern at any
other.

Observations that matter for the V2 migration:

- **No `user_id` anywhere.** Every table is implicitly owned by the single local
  user. Phase 2 has to add ownership to all 14 tables, not just the top ones.
- **`chat_sessions.claude_session_id` is provider-specific.**
  [LLM_PROVIDER_ARCHITECTURE.md](./LLM_PROVIDER_ARCHITECTURE.md) §4 requires
  generic tables to carry no provider-specific field; this column names its
  provider in the schema. Phase 11 follow-up, not a Phase 0 fix.
- **Dates are ISO strings, not typed timestamps**, and are written with naive
  local-time `datetime.now()`. The PostgreSQL move must pick an explicit tz
  policy rather than inherit this one.
- `jobs` has no geographic columns beyond free-text `location`, so the PostGIS
  work in Phase 2/geo has no V1 data to migrate — only text to geocode.

## 5. Adapter inventory

### Discovery sources — 13 adapters + `_common.py`

`pipeline/discover.py` holds two registries mapping a source name to a callable:

- `ATS_SOURCES` (company-keyed, one call per company in `config/companies.yaml`):
  `greenhouse`, `lever`, `ashby` — all JSON APIs.
- `QUERY_SOURCES` (query-keyed, one call per search in `config/searches.yaml`):
  `wtj`, `linkedin`, `indeed`, `indeed_ch`, `jobup`, `jooble`, `migros`, `coop`,
  `jobscout24`, `manpower`.

`indeed_ch` reuses `indeed`'s Mosaic parser with a different host, and both parse
an embedded JSON blob rather than HTML. `pipeline/sources/_common.py` supplies
`normalize()`, which rejects any key outside `JOB_COLUMNS` — the reason a source
adapter cannot silently invent a column. `strip_html()` flattens descriptions.

Every adapter is a pure parser plus a fetch call, so every one is tested against a
fixture. That separation is what makes the V2 `Opportunity` mapping tractable:
only `normalize()` has to change shape, not 13 parsers.

### Application channels — 9 adapters + `generic` fallback

`pipeline/apply/__init__.py::_get_handler()` dispatches on
`_common.detect_platform()`: `linkedin`, `wtj`, `jobup`, `umantis`, `migros`,
`greenhouse`, `lever`, `ashby`, plus `email_apply`; anything unrecognized falls
through to `generic`. Browser work runs through `pipeline/applier.py`, which owns
the single Playwright launch configuration (`fr-CH` / `Europe/Zurich`, matching
`scripts/wtj_login.py` so the saved session and the replay agree).

This dispatcher is already the `ApplicationStrategy` registry Phase 12 asks for,
in an untyped form — an adapt-not-rewrite candidate.

## 6. HTTP surface — 29 operations

Generated from `create_app().openapi()`; the source decorators were cross-checked
and match one-for-one, so there are no hidden routes. All are unauthenticated and
unscoped by design (§3).

| Router | Operations |
|---|---|
| `overview` | `GET /api/overview` |
| `analytics` | `GET /api/analytics` |
| `approved` | `GET /api/approved` |
| `settings` | `GET /api/settings`, `PUT /api/settings` |
| `jobs` | `GET /api/jobs`, `GET /api/jobs/{job_id}`, `GET /api/jobs/{job_id}/fit` |
| `actions` | `POST /api/jobs/{job_id}/` + `go`, `applied`, `skip`, `status`, `regen`, `apply-now`, `draft_followup`, `followup/snooze`, `followup/dismiss` |
| `files` | `GET /api/files/cv/{cv_id}` |
| `prep` | `GET /api/jobs/{job_id}/prep`, `POST .../prep/generate`, `PUT .../prep/notes`, `POST .../interviews`, `PATCH /api/interviews/{interview_id}` |
| `chat` | `POST /api/chat` (SSE), `GET /api/chat/history` |
| `transcribe` | `POST /api/transcribe` |
| `runs` | `POST /api/runs/discover`, `POST /api/runs/full`, `GET /api/runs/status` |

`webapp/src/api/schema.d.ts` is generated from this schema by `npm run gen:api`
against a live server, so it can drift; it is not regenerated by any check.

## 7. LLM and chat behaviour as built

**The invariant:** V1 never calls a hosted Anthropic API. It shells out to the
local `claude` CLI, which authenticates from its own stored credentials.
`server/_env.py::child_env()` enforces this by stripping the entire `ANTHROPIC_*`
namespace — a prefix, not a list, so a newly invented credential variable cannot
leak through a stale allowlist. Every subprocess spawn in `server/` must pass
`env=child_env()`.

**Backends** (`pipeline/settings.py::LLM_BACKENDS`): `claude_cli` (default),
`ollama`, `lmstudio`. `_validate_local_url()` rejects any `llm_base_url` that is
not `localhost` or a loopback address, so no backend choice can ship job data
off-box. There is deliberately no remote-API option — the single largest
behavioural gap against the provider-neutral V2 target.

**Turn flow** (`server/chat.py`):

1. `build_context(conn, scope, scope_id)` assembles context **server-side** from
   the database: a digest for `global` scope, a compact dict for `job` scope. The
   `POST /api/chat` body has no `context` field, on purpose — a client-supplied
   context could smuggle real client names or invented figures past the gate.
2. `build_prompt()` prepends `_PREAMBLE`, which fixes the action-block grammar and
   restates the standing mandates.
3. `stream_turn()` dispatches: `stream_chat()` spawns
   `claude --print --output-format stream-json --allowedTools Read,Grep,Glob`
   (read-only tools, 120 s timeout, 256 KiB output cap, killed on exit), or
   `stream_local_model()` posts to an OpenAI-compatible `/chat/completions`.
4. `parse_action_blocks()` extracts ```` ```action ```` blocks and validates each
   against `_ALLOWED_ACTION_TYPES` = {`regen`, `set_status`, `mark_applied`,
   `draft_followup`, `skip`}, re-checking `set_status` targets against `STATUSES`.
   Prose therefore cannot mutate state; only a typed, server-validated proposal
   can. Phase 12 keeps this property, it does not need to invent it.
5. `finalize_turn()` runs the §6.2 mandate gate: `mandate.apply_fixes()` rewrites
   aliases and "GenAI" → "Generative AI", then `mandate.check_prose()` returns the
   verdict. **It fails closed**: with no redaction config it reports
   `anonymization_config_missing` and `mandate_ok=False`. When a corpus is passed,
   it also rejects numbers and glossary terms absent from that corpus — the truth
   gate `EvidenceGuard` generalizes.
6. Streamed `token` events are an ephemeral preview; the sanitized `text` on the
   `done` event is authoritative and the UI must replace the preview with it.

Both turns are persisted (`chat_messages`) regardless of `mandate_ok`, because in
a keyless setup the gate commonly fails closed and dropping those replies would
reopen the "conversation vanished" bug. A stale `--resume` id that dies before
emitting anything is retried once with a fresh session.

Only two callers reach the model: `server/routes/chat.py` (streaming) and
`server/routes/prep.py` via `chat.collect_turn()` (non-streaming). Follow-up
drafting (`server/followups.py::draft_followup`) is template-based, not generated.
That narrow call surface is why Phase 11 can introduce a provider registry without
touching business logic.

## 8. Lint and type baselines

### Ruff — green, with V1 debt declared in configuration

`ruff check .` covers 151 files and passes. The rule set (`E`, `W`, `F`, `I`, `UP`,
`B`, `C4`, `ASYNC`, `RUF100`, `S105`–`S107`, `S608`) applies everywhere; the
violations V1 already has are listed per directory in
`[tool.ruff.lint.per-file-ignores]`. No V1 file was reformatted, and new code under
`backend/` or `country_packs/` gets the undiluted set.

Debt tolerated, by directory, at the time of writing — 199 findings in total,
measured by re-running the same rule set with `per-file-ignores` emptied:

| Directory | Count | Dominant rules |
|---|---|---|
| `pipeline/**` | 70 | E501 21, I001 18, F401 7, UP\* 9 |
| `server/**` | 45 | B904 17, UP045 9, E501 7, UP035 3 |
| `tests/**` | 68 | E501 24, I001 22, F401 16 |
| `scripts/**` | 16 | E501 9, F841 2, F401 2 |
| `dashboard/**` | 0 | — |

`dashboard/**` needs no ignores at all. Two findings are exonerations rather than
debt, both reviewed line by line: `S105` in `pipeline/site_credentials.py` flags
`_PASSWORD_ENV = "SITE_ACCOUNT_PASSWORD"`, which is an environment-variable *name*;
the five `S608` hits build column lists from the fixed `JOB_COLUMNS` whitelist and
bind every value as a parameter. `B008` is switched off through
`flake8-bugbear.extend-immutable-calls` because FastAPI's `Depends()` is a call in
a default argument by design.

The Ruff **formatter** is intentionally not enabled: running it would rewrite the
entire V1 tree, which Phase 0 forbids.

### mypy — 22 errors, measured and regression-gated

Two tiers, since `strict` is a global-only flag and cannot be set per module: V1 is
checked leniently (`disallow_untyped_defs` and `check_untyped_defs` off, because
enabling either would flag most V1 functions and amount to demanding the rewrite
Phase 0 forbids), while `backend.*` and `country_packs.*` have the full strict flag
set spelled out ahead of Phase 1.

| Code | Count |
|---|---|
| `union-attr` | 7 |
| `arg-type` | 7 |
| `return-value` | 4 |
| `type-var` | 3 |
| `var-annotated` | 1 |

Spread over 15 files, the largest being `pipeline/email_inbox.py` and
`pipeline/applier.py` at 3 each. Adding `types-PyYAML` removed a further 7
`import-untyped` errors with no code change.

CI does not merely tolerate this count — it pins it. The workflow records
`BASELINE: 22` and fails if the number rises, so inherited debt stays visible while
a newly introduced type error still breaks the build.

## 9. Test profile strategy (Phase 0.1)

`cv/base_cv.yaml` holds the operator's real identity and is correctly
gitignored, so it does not exist in a clean clone. Phase 0 measured the damage:
**31 of 460 tests failed with `FileNotFoundError`**, 18 of them only incidentally
(`/api/jobs/{id}/fit` and the chat context build gap analysis, which loads the
profile). Copying the tracked `cv/base_cv.template.yaml` over it fixed 25; the
remaining six assert on the profile's **density** — that the library fills a page,
that an overstuffed CV drops priority-3 bullets first, that fill rises with
content — and a one-bullet placeholder cannot satisfy them. Phase 0 therefore
deselected those six by name in CI, which left the safety net with a hole in
exactly the area V1 is most fragile.

Phase 0.1 closed it with one seam and one fixture:

- **`pipeline/paths.py` gained `BASE_CV_PATH`**, defaulting to
  `CV_DIR / "base_cv.yaml"` — the path production already used.
- **`load_base_cv()` reads `paths.BASE_CV_PATH` per call** instead of rebuilding
  the literal, so the attribute can be redirected. Production resolution is
  unchanged; `python -m pipeline.cv_render` still renders the operator's own CV.
- **`tests/fixtures/base_cv.yaml`** is a tracked, fully synthetic library: 3
  invented employers, 15 bullets, `example.test` (RFC 2606) e-mail and an
  all-zero phone number. It shares no name, employer, school, address or contact
  value with any real profile.
- **`tests/conftest.py` redirects `paths.BASE_CV_PATH` to it, autouse and
  unconditional.** Not "use the real one if present": that would make the suite
  assert different things on a developer's machine than in CI, which is the
  reproducibility bug being fixed.
- **CI dropped both the `cp` step and the six `--deselect` flags**; the pytest
  step is now plain `python -m pytest -q`.

The fixture is dense by construction, because three of the six tests pin it
between hard bounds. The window is narrow: with `H` the rendered content height,
`b` one bullet line (6.4 mm) and `f` one 2-line filler bullet (11.7 mm), the
overstuffed test only drops all 15 fillers while `H - b + f > 265 mm`, i.e.
`H > 259.6 mm`, and one page means `H <= 265 mm`. Measured `H = 264.1 mm`
(fill 0.9966 in both languages), and the library carries exactly **one**
priority-3 bullet, since a second would put `H - 2b + f` back under one page and
let a filler survive.

Two properties make those numbers platform-independent, which matters because CI
runs Ubuntu with only `fonts-dejavu-core` while the file was tuned on macOS
(Helvetica):

1. `cv/style.css` sets a **unitless** `line-height: 1.45`, so every block's
   height is a multiple of the font *size*, never of its metrics. Only a
   different line *count* can move the fill.
2. No line comes close to filling its width: the widest bullet, skill and
   education line measures **67%** of the available width, so DejaVu's ~12%
   wider glyphs cannot rewrap them. The only block near a boundary is the
   two-line summary, and it re-wraps in the safe direction — DejaVu is wider,
   never narrower, so `H` can only grow, and growth is absorbed by dropping the
   single priority-3 bullet (fill 0.993, still above the 0.92 floor).

Editing the fixture means re-checking those bounds. `python -m pipeline.cv_render`
prints pages and fill for whichever profile `BASE_CV_PATH` resolves to, and is
also the way to sanity-check a freshly onboarded *real* profile now that no test
reads it.

## 10. Follow-ups this baseline surfaced

Recorded here rather than fixed, because Phase 0 changes no behaviour:

1. ~~**Synthetic CV fixture** to close §9 and restore a 460-test CI.~~ Done in
   Phase 0.1; §9 documents the result. What is left is the *other* half of what
   the old §9 covered: nothing now checks a freshly onboarded **real** profile,
   because the suite no longer reads it. `python -m pipeline.cv_render` does it
   by hand; an `/onboard` step or an operator script could do it automatically.
2. **`pipeline/applier.py` viewport typing** — 3 of the 22 mypy errors are
   `dict[str, int]` passed where Playwright wants `ViewportSize`; annotating the
   module constant clears them. Behaviour-neutral, so it belongs to whichever phase
   next touches that file.
3. **`chat_sessions.claude_session_id`** names a provider in a generic table,
   against [LLM_PROVIDER_ARCHITECTURE.md](./LLM_PROVIDER_ARCHITECTURE.md) §4.
   Phase 11.
4. **No `user_id` on any of the 14 tables** — Phase 2 must add ownership
   everywhere, and every one of the 29 endpoints needs authorization scoping.
5. **Naive local-time timestamps stored as ISO strings.** Decide the timezone
   policy in Phase 2 rather than inheriting it.
6. **`npm run e2e` is configured but there is no `webapp/e2e/` directory**, so no
   Playwright E2E runs today and CI has no E2E job. Note that
   [FRONTEND_ARCHITECTURE.md](./FRONTEND_ARCHITECTURE.md) migrates this frontend to
   Nuxt 4 / Vue 3, so the 75 React tests are transitional; CI will need a second
   frontend job while both stacks coexist.
7. **`webapp/src/api/schema.d.ts` can drift** — it is generated from a running
   server by hand and no check verifies it against `openapi.json`.
8. **The CI workflow has never executed.** The remote push is still blocked by a
   GitHub account mismatch, so `.github/workflows/ci.yml` is verified only by local
   simulation of its steps (§1, both bash guards dry-run under `bash -e`) and by
   YAML parsing. The Ubuntu WeasyPrint font stack remains the one unproven part:
   the page-count and fill assertions now run there instead of being deselected,
   so §9 states the two properties that make them font-independent and the
   direction the remaining risk points in. The first green CI run is what
   confirms it.
