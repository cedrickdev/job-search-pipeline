# V2 ATS Documents

Built in Phase 10, the generation half of the phase whose other half is the
[Candidate Evidence Store](./CANDIDATE_EVIDENCE.md). This document describes the
layer that turns a candidate's *attested facts* into an ATS-safe résumé or cover
letter for a specific posting — and the guard that stands between the two, refusing
any version that would state something the evidence does not carry:

```
CandidateProfile ─┐
(evidence+claims) ─┼─> generator ─> DocumentContent ─> CandidateEvidenceGuard ─┐
Opportunity       ─┘   (composes,      (structured,       (four gates)         │
                        never invents)   still typed)                          │
                                                             ┌──── ok? ─────────┤
                                                        yes  │             no   │
                                                    render (WeasyPrint)    keep as
                                                    + store PDF            REJECTED
                                                    → RENDERED version     (audited, §45)
```

The rule the whole phase serves, quoted from `backend/app/documents/guard.py` and
`CLAUDE.md`:

> **The system may rewrite, reorder, shorten, emphasize or omit truthful candidate
> information, but it may never invent new candidate facts.**

The evidence store makes "truthful candidate information" a *set*; this layer is
what is *built* from that set. A generator composes a document by selecting and
re-ordering evidence-backed lines. The guard then checks the composed content
against the same evidence, gate by gate, and only a version that clears every gate
is rendered to a PDF and offered as the candidate's. A version that fails is kept,
not deleted — the reason it failed is the record (docs/ENGINEERING_STANDARDS.md
§45).

## The invariant

Four claims, each held by a type or a test rather than by review:

- **An uncited line is unconstructible, so the guard never has to catch one.**
  `EvidenceBackedText` and `ResumeEntry` carry `evidence_ids` with
  `Field(min_length=1)`: a summary sentence, an experience bullet or a cover-letter
  paragraph cannot exist without citing at least one evidence id. This is why
  `DocumentViolationCode` has no `MISSING_CITATION` — the type answers that question.
  The guard's job is the harder one the type cannot: whether the cited evidence
  *exists*, and whether the *words* are supported.
- **Nothing is rendered or stored until the content clears the guard.** The guard is
  pure and deterministic — no I/O, no LLM, no clock — so the service runs it *before*
  touching the renderer or the artifact store. A passing report is rendered and its
  PDF stored; a failing one becomes a `REJECTED` version with no artifact. Because
  the render-and-store happens inside version construction, after the guard and before
  the caller persists the document, a `RENDERED` version can never exist without the
  bytes it points at — the domain's `_status_agrees_with_verdict_and_artifact`
  invariant, and a database CHECK, both refuse the contradiction.
- **A refused version is auditable, not erased.** A caught fabrication becomes a
  `REJECTED` version carrying the guard's whole report: every violation's `code`, a
  human `detail`, and the `offending_text` that tripped the rule. The rejected attempt
  and *why* it was rejected survive to the API and the screen, because a silent rewrite
  is exactly what the guard exists to prevent (§45).
- **The owner comes from the session, never the body.** Every generate and every read
  is scoped to the account behind the cookie: the service loads *that* account's
  default profile and stamps the document with the `user_id`. `GET /documents/{id}`
  raises one error for "no such document" and "not yours" alike, so a caller cannot
  learn another account holds a document by asking for its id.

V1 is honored, not discarded. The guard is a faithful port of V1's
`pipeline.tailor_io.truth_violations` from tailored CV YAML onto the structured
`DocumentContent` domain — the same numeric-token tokenizer, the same skill-library
rule — so a fact that passed V1 passes here unchanged. The renderer inherits V1's
`pipeline.cv_render` rules (one page, ATS-readable text, the fill floor), restated for
the structured document.

## Layout

```
backend/app/domain/documents.py           the document vocabulary: CandidateDocumentType,
                                           DocumentStatus, DocumentViolationCode, the content
                                           models (ResumeDocument, CoverLetterDocument,
                                           EvidenceBackedText, ResumeEntry, ResumeSkillGroup),
                                           DocumentGuardReport/Violation, DocumentArtifactRef,
                                           DocumentVersion and the CandidateDocument aggregate
backend/app/documents/guard.py             CandidateEvidenceGuard — the four gates, pure and
                                           deterministic; the numeric_tokens tokenizer
backend/app/documents/generator.py         DocumentGenerator protocol, DeterministicDocumentGenerator
                                           (key deterministic-reference/1), InsufficientEvidence
backend/app/documents/render.py            render_document → RenderedDocument (WeasyPrint,
                                           key ats-weasyprint/1, one-page trim, fill floor 0.92)
backend/app/documents/artifacts.py         DocumentArtifactStore protocol, LocalDocumentArtifactStore,
                                           StoredArtifact, ArtifactNotFound, the path-traversal guard
backend/app/services/documents.py          DocumentService — generate/guard/render/store/version,
                                           and the owner-scoped reads and download
backend/app/api/routes/documents.py        POST .../resume, .../cover-letter, GET /documents,
                                           GET /documents/{id}, GET /documents/{id}/download
backend/app/api/schemas.py                 GenerateDocumentRequest and the document response models
backend/app/api/errors.py                  the slug → HTTP status mapping
backend/app/infrastructure/database/
  models.py                                CandidateDocumentRow, DocumentVersionRow
  mappers.py                               document/version ↔ row, the flattened artifact columns
backend/migrations/versions/rev_0007_phase_10_documents.py
frontend/app/composables/useDocuments.ts   useDocumentsQuery, useDocumentQuery, useGenerateDocument,
                                           useDownloadDocument
frontend/app/pages/documents/index.vue      the /documents list
frontend/app/pages/documents/[id].vue       one document, its version history and guard verdicts
frontend/app/components/DocumentGenerateActions.vue  the generate buttons on an opportunity
```

The imports run one way — `domain/documents` ← `documents/{guard,generator,render,
artifacts}` ← `services/documents` ← the routes — and none of the domain depends on a
service or a renderer. The generator and the artifact store are *protocols* the
service is handed, not concretes it reaches for, which is what keeps generation
provider-neutral (docs/LLM_PROVIDER_ARCHITECTURE.md §3) and lets a flow test run
against fakes.

## The vocabulary

`CandidateDocumentType` is `RESUME` or `COVER_LETTER`. A résumé and a cover letter for
the same posting are *two* documents, not two faces of one: they have independent
version histories, so a candidate can regenerate the letter after an interview without
touching the résumé. The value is part of the `candidate_document_id`, so the split is
also what keeps their rows distinct.

`DocumentStatus` is where one version sits in its lifecycle. The path is one-way
through the guard:

| `DocumentStatus` | |
| --- | --- |
| `DRAFT` | a generator has proposed content, not yet checked |
| `VALIDATING` | the guard is running |
| `VALIDATED` | cleared the guard, not yet rendered |
| `REJECTED` | the guard caught a fabrication — kept, with the reason (§45) |
| `RENDERED` | cleared the guard and rendered to a stored PDF |
| `ARCHIVED` | superseded by a newer version |

`is_usable` is true for `VALIDATED` and `RENDERED` only — the two states in which
content has passed the guard. A `DRAFT` has not been checked and a `REJECTED` one
failed, so a surface may show either for transparency but never as "your résumé". The
deterministic generator today produces a version that is either `RENDERED` (passed) or
`REJECTED` (failed) in one pass; the intermediate states exist for an asynchronous
generator that proposes a `DRAFT`, runs the guard while `VALIDATING`, and renders a
`VALIDATED` version later.

`DocumentViolationCode` is why the guard refused a version. There are five, each a way
generated text could assert something the evidence does not carry:

| `DocumentViolationCode` | |
| --- | --- |
| `UNKNOWN_EVIDENCE` | a line cites an evidence id the profile does not hold (V1's "unknown bullet id") |
| `INVENTED_NUMBER` | a numeric token in a line is absent from the evidence that line cites |
| `INVENTED_TERM` | a claimable hard-skill term appears in a line but nowhere in the candidate's evidence corpus |
| `UNSUPPORTED_SKILL` | a skill listed on the résumé matches no `SKILL` claim (compared through the skill ontology) |
| `ALTERED_IDENTITY` | the name on the document is not the candidate's own `display_name` |

There is deliberately no `MISSING_CITATION`: an uncited line cannot be built in the
first place (`EvidenceBackedText`), so the guard has nothing to catch there.

## The guard

`CandidateEvidenceGuard.review(content, *, profile, opportunity)` returns a
`DocumentGuardReport` — an `ok` bool tied by a validator to its `violations`, so a
report cannot claim to pass while carrying a violation, nor fail while carrying none.
The four gates, each a port of a V1 truth rule:

- **`UNKNOWN_EVIDENCE`.** Every `EvidenceBackedText` line cites at least one id (the
  type guarantees it); the guard checks each cited id names a record the *profile*
  holds. The type guarantees the citation is *present*; the guard guarantees it is
  *real*.
- **`INVENTED_NUMBER`.** A numeric token in a line must appear in the evidence that
  line cites, compared as a multiset through the very same `numeric_tokens` tokenizer
  V1 uses — so French "2,5 M€" and "€2.5M" still compare equal, and a metric no
  evidence supports (`grew revenue 300%`) is caught.
- **`INVENTED_TERM`.** A claimable hard-skill term — drawn from the skill ontology, the
  posting's requirements, or the candidate's own claims — that appears in a line but is
  found nowhere in the candidate's evidence corpus is a fabrication. This is V1's
  base-library qualification rule.
- **`UNSUPPORTED_SKILL`.** A skill listed on the résumé that matches none of the
  candidate's `SKILL` claims, compared through the deterministic skill ontology so
  "Node.js" and "node" are one skill.

A fifth check, `ALTERED_IDENTITY`, guards the one field a document may never rewrite:
the candidate's name. V1 froze contact and identity verbatim; the structured form keeps
the profile's `display_name` and refuses a substitution.

The guard is *pure and deterministic* — no I/O, no LLM, no clock — which is precisely
what lets the service run it before it touches a database or a renderer, giving the
same no-partial-state guarantee V1 has.

## The pipeline

`DocumentService.generate(user_id, opportunity_id, type, *, now, language)` is the
whole workflow, in one method whose steps are ordered so a fabrication never reaches
storage:

1. **Resolve.** Load the account's default profile (404 `candidate_profile_not_found`
   if none) and the posting (404 `opportunity_not_found`). The owner is the session's,
   never the body's.
2. **Language.** Use the explicit `language` override if given, else the posting's own
   `posting_language`, else the candidate's first declared language. It never *guesses*
   a language the candidate did not state.
3. **Compose.** Dispatch to the generator's `generate_resume` or `generate_cover_letter`
   for the type. `InsufficientEvidence` (409) here when the profile carries too little
   to build a truthful document — the honest answer, not invented filler.
4. **Guard.** `guard.review(...)` returns the verdict, before anything is rendered or
   written.
5. **Version and build.** Compute the next version number from the existing document for
   the `(profile, opportunity, type)` triple. If the report passed, render the PDF, store
   it, and build a `RENDERED` version referencing the artifact; if it failed, build a
   `REJECTED` version with the report and no artifact.
6. **Persist.** Append the version to the document (creating it on the first attempt)
   and upsert. The whole document is returned so a client sees the new attempt in its
   history.

The document id and the version id are *derived* from the triple and the count
(`candidate_document_id`, `document_version_id`), so a retried call after a failed flush
reuses the same rows rather than accreting a duplicate — idempotent per version number.
A fresh call after a *successful* one is a new attempt and a new version: that is
regeneration, and it is meant to grow the history.

## The generator

`DocumentGenerator` is a protocol with `generate_resume` and `generate_cover_letter`
and a `key`. The service stamps that key on every version and otherwise never learns
which generator produced the content, which is what keeps generation provider-neutral —
a Claude, Codex or OpenAI-compatible generator is a drop-in that satisfies the same
protocol.

`DeterministicDocumentGenerator` (key `deterministic-reference/1`) is the reference
implementation and the test default: it composes strictly from the profile's evidence
and claims — every line it emits cites the records it rests on, so it *cannot* invent —
and never fills a gap with prose. A thin profile yields a plain, short document, and it
raises `InsufficientEvidence` rather than padding one. It is deterministic, so the same
profile and posting compose the same content, which is what makes the flow tests exact.

## The renderer

`render_document(content, *, language)` produces a `RenderedDocument` — the PDF bytes,
a `page_count`, a `fill` fraction and the renderer key `ats-weasyprint/1`. Its rules
are V1's `pipeline.cv_render`, restated:

- **ATS-readable text, not a picture of text.** A single column of semantic headings and
  bullet lists — no multi-column tables, no text boxes, no images. The extraction test
  reads the PDF back with `pypdf` and asserts the candidate's facts survive, the only
  honest way to prove an ATS can read it.
- **One page.** A résumé that overflows drops its lowest-priority bullet and re-renders,
  exactly as V1 does. "Lowest priority" is document order: the generator emits entries
  newest-first and bullets most-important-first, so the bullet dropped is the last one of
  the last entry that still has more than one — a job is never stripped to zero bullets,
  and a cover letter is never trimmed.
- **Fill floor.** The fraction of the page the content covers is measured and reported
  against `FILL_FLOOR = 0.92`. Unlike V1 this is *advisory*, not fatal: V2's generator
  selects from real evidence and does not over-trim, so a sparse résumé reflects thin
  evidence rather than a bug — the service surfaces the number, it does not refuse to
  render.

Rendering is *deterministic*: the PDF's producer string and creation date are fixed, so
the same content renders to the same bytes. The producer names this renderer honestly; it
does not impersonate Word the way V1 did to appease a specific ATS. WeasyPrint is imported
at module load, which is why any command that imports the app needs
`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` on macOS to find libgobject.

## The artifact store

`DocumentArtifactStore` is a protocol — `key_for`, `put`, `get` — so the service is
handed a store rather than knowing where bytes live. `LocalDocumentArtifactStore` writes
under a root directory; a production store over object storage satisfies the same protocol.

- **The key is id-derived and opaque.** `key_for(document_id, version_id)` returns
  `documents/{document_id}/{version_id}.pdf`, so re-rendering a version overwrites its own
  artifact rather than leaking a file. The `storage_key` is deliberately *absent* from every
  API response: a client downloads through the document id, and a storage locator in a body
  is an internal path a UI has no use for and an attacker might.
- **A key can never escape the root.** `_resolve` joins the key onto the resolved root and
  refuses any result that is not the root or under it — so a `..` or an absolute key is a loud
  `ValueError`, not an arbitrary file read or write. This is not paranoia about the store's own
  keys, which are id-derived and safe; it is the store refusing to *ever* be the component that
  lets a bad key out of the directory.
- **A missing file is `ArtifactNotFound`.** `get` on a key the store no longer holds raises it,
  which the service leaves to propagate as a 500 `artifact_unavailable` — a storage fault, not a
  client error, and never disguised as "not rendered yet".

## Persistence

Revision `0007`, `revises 0006`, additive throughout (the same revision that adds the
evidence tables; see [Candidate Evidence](./CANDIDATE_EVIDENCE.md#persistence)). Two
document tables:

- **`candidate_documents`** — one row per `(candidate_profile_id, opportunity_id,
  document_type)` triple, enforced by a `UNIQUE` named explicitly to stay within
  PostgreSQL's 63-character limit. It carries a `user_id` *alongside* the profile id —
  denormalized for the reason every user-owned table states: each scoped read is `WHERE
  user_id = ?`. All three foreign keys (`user`, `profile`, `opportunity`) cascade, so a
  deleted account, profile or posting leaves no document behind. Indexed on
  `(user_id, updated_at)` for the "most recently updated first" list and on
  `opportunity_id`.
- **`document_versions`** — one row per attempt, `UNIQUE (document_id, version)`,
  cascading from the document. `content` and `guard_report` are `JSONB`; `guard_ok` is a
  boolean the mapper lifts out of the report so a CHECK can read the verdict without a
  JSONB path expression. The five `artifact_*` columns are the flattened
  `DocumentArtifactRef`, present exactly when `status = 'RENDERED'`. `created_at` is
  domain-supplied (the instant the attempt was made), so it is NOT NULL with no default,
  unlike the `updated_at` bookkeeping column.

Three CHECKs on `document_versions` are the lifecycle made physical — verbatim copies of
the `_status_agrees_with_verdict_and_artifact` invariant, each written as the implication
it is (a CHECK fails only on FALSE): a `VALIDATED`/`RENDERED` version carries a passing
verdict, a `REJECTED` one a failing verdict, and only a `RENDERED` version references an
artifact. `version >= 1`, `language ~ '^[a-z]{2}$'`, and non-negative artifact sizes round
out the row-level guards.

Enums are `VARCHAR(32)` + CHECK (never native `ENUM`, so widening a member set is an
ordinary migration, not DDL that cannot share a transaction with a table rewrite), and the
migration writes each member list out verbatim so a schema-drift test fails if `models.py`
and the migration disagree. Ids are domain-supplied, so a row has the same id as the domain
object. `downgrade` drops both tables (and the evidence tables and columns) — destructive,
because the documents are the phase's own data.

## The API

Five endpoints, all under `/api/v2`, all behind an authenticated session. The two
generates need the `X-CSRF-Token` header (an unsafe method); the three reads need only
the cookie. The owner is never in the path.

| Operation | Status | Meaning |
| --- | --- | --- |
| `POST /opportunities/{id}/resume` | 200 | append a résumé version for the posting (`GenerateDocumentRequest` → `CandidateDocumentResponse`) |
| `POST /opportunities/{id}/cover-letter` | 200 | append a cover-letter version, same guard, same shape |
| `GET /documents` | 200 | this account's documents, most recently updated first (`CandidateDocumentListResponse`) |
| `GET /documents/{id}` | 200 | one document with its whole version history (`CandidateDocumentResponse`) |
| `GET /documents/{id}/download` | 200 | stream the newest `RENDERED` PDF — a binary `Response`, `Content-Disposition: attachment` |

The generates are a `POST`, not a `PUT`: each call is a new attempt that grows the
history, not an idempotent replace. `CandidateDocumentResponse` returns the versions
newest-*last* with strictly increasing numbers, plus `latest_usable_version` — the number
of the newest version fit to show as the candidate's, `null` when every attempt is a draft
or rejected, which a UI renders as "not generated yet" rather than showing an unchecked
draft. Each `DocumentVersionResponse` carries the structured `content` (the exact thing the
guard checked, never a blob), the `guard_report` once the guard has run, and an `artifact`
descriptor for a `RENDERED` version — `media_type`, `byte_size`, `page_count`,
`rendered_at`, but never the `storage_key`.

The error slugs a client branches on:

| Exception | Status | Slug | Meaning |
| --- | --- | --- | --- |
| `CandidateProfileNotFound` | 404 | `candidate_profile_not_found` | no profile yet — finish onboarding |
| `OpportunityNotFound` | 404 | `opportunity_not_found` | no such posting |
| `InsufficientEvidence` | 409 | `insufficient_evidence` | too little evidence to build a truthful document — the honest refusal |
| `DocumentNotFound` | 404 | `document_not_found` | no such document, or not this account's (one code for both) |
| `DocumentArtifactMissing` | 409 | `document_not_rendered` | the document is real but no version has cleared the guard and rendered yet |
| `ArtifactNotFound` | 500 | `artifact_unavailable` | the row references bytes the store no longer holds — a storage fault |

`insufficient_evidence` is a 409, not a 422: the request is well-formed, the *state* is the
conflict, and the sentence is the generator's own fixed explanation, never user input.
`document_not_rendered` is a 409 rather than a 404 because the resource is real and this
account's — the state is temporary, not the id wrong.

## The frontend

Four composables in `useDocuments.ts` and three screens:

- `useDocumentsQuery()` reads `GET /documents` for the list.
- `useDocumentQuery(id)` reads one document and maps `document_not_found` to `null` — an id
  out of a stale link is a screen to render ("no such document"), not an error to throw.
- `useGenerateDocument()` exposes the résumé and cover-letter mutations; each is a `POST`
  that invalidates the whole `documents` surface, so the list and any open detail both pick
  up the new attempt. `insufficient_evidence` becomes a line pointing at the evidence page.
- `useDownloadDocument()` streams the PDF through the document id (never a storage key),
  failing with `document_not_rendered` when nothing has cleared the guard yet.

`app/pages/documents/index.vue` is the read-only list. `app/pages/documents/[id].vue` shows
one document's version history newest-first, and for a rejected version it renders the guard
verdict in full — each violation's `code`, its `detail`, and the `offending_text` quoted — so
the refusal is visible, named and quoted rather than hidden behind a rewrite (§45). Download is
disabled unless some version is `RENDERED`. `DocumentGenerateActions.vue` is the entry point
from an opportunity (on the map result card): two buttons that generate and route to the new
document, turning an `insufficient_evidence` refusal into a link to `/evidence` rather than a
dead end. All three screens are behind `middleware: 'auth'`.

## Tests

No test in the default suite touches a live LLM or a live job board (CLAUDE.md §Testing).
The guard and generator are pure and run in-process; the renderer runs WeasyPrint locally;
the persistence tests run against a real PostgreSQL.

| Suite | Covers |
| --- | --- |
| `tests/test_v2_documents.py` | the guard's four gates and the identity gate; the deterministic generator composing only from evidence and raising `InsufficientEvidence`; the renderer's one-page trim, fill reporting, and a `pypdf` round-trip proving the facts survive extraction; the service pipeline — a passing version rendered and stored, a failing one kept as `REJECTED` with its report, regeneration growing the history, and the owner-scoped reads and download |
| `tests/test_v2_persistence_repositories.py` | the document repository round-tripping a document and its versions, including the flattened artifact columns |
| `tests/test_v2_persistence_constraints.py` | the row-level CHECKs — the status/verdict/artifact agreement, positive version, the language format |
| `tests/test_v2_persistence_mappers.py` | document/version ↔ row fidelity, and `guard_ok` lifted out of the JSONB report |
| `tests/test_v2_persistence_schema.py` | schema drift between `models.py`, the metadata and the migration's enum and CHECK lists |
| `tests/test_v2_persistence_migrations.py` | the upgrade and downgrade of the Phase 10 tables |
| `tests/test_v2_api_surface.py` | the five endpoints under the prefix, authenticated, the generates carrying CSRF, and the binary download's `Content-Disposition` |
| `frontend/tests/nuxt/composables/useDocuments.spec.ts` | the reads, the missing-document→null mapping, the generates invalidating the surface, the download through the id |
| `frontend/tests/nuxt/pages/documents.spec.ts` | the list and the detail — résumé content rendered, a rejected attempt kept with the rule it broke, download disabled when nothing is rendered, the no-such-document state |
| `frontend/tests/nuxt/components/DocumentGenerateActions.spec.ts` | a successful generation routing to the new document, the cover-letter endpoint targeted, `insufficient_evidence` shown as a link to evidence with no navigation |
| `frontend/tests/e2e/documents.spec.ts` | the browser flow — the screens reachable from the nav, a rejected version on screen with the rule it broke and the line that broke it, the anonymous redirect to `/login` |

## Known risks

- **The intermediate lifecycle states are unexercised today.** `DRAFT`, `VALIDATING`,
  `VALIDATED` and `ARCHIVED` are modelled and constrained, but the deterministic generator
  produces only `RENDERED` or `REJECTED` in one synchronous pass. An asynchronous generator
  that proposes a draft and renders later would use them; until then the states are a
  contract the current engine does not walk.
- **The fill floor is advisory, not enforced.** A sparse résumé renders and reports a low
  `fill` rather than being refused, on the premise that thin output reflects thin evidence.
  If a future generator over-trims, the number is surfaced but nothing fails on it — a
  reviewer, not the pipeline, decides whether 0.60 is too empty to send.
- **The guard is a lexical, deterministic check, not a semantic one.** It catches invented
  numbers, unknown citations and unsupported skills by comparing tokens and skills against
  the cited evidence. A sentence that is *misleading* while using only supported tokens —
  a true fact framed to imply more — is beyond it. The guarantee is "no invented facts",
  not "no misleading emphasis".
- **The local artifact store is a filesystem, not durable object storage.** It is correct
  and path-safe, but a production deployment across multiple workers needs a shared store
  behind the same protocol; the local store is the development and single-node default.

## Not in this layer

The candidate evidence store the documents are built from — the evidence and claim records,
the citation invariant, the write screen — is Phase 10's other half; see [Candidate
Evidence](./CANDIDATE_EVIDENCE.md). The matching that consumes the same evidence to report an
`evidence_confidence` is Phase 9's (docs/ARCHITECTURE.md). Any LLM-backed generator: the
`DocumentGenerator` protocol is the seam it plugs into, and it is subject to the same guard —
there is no path by which a generated sentence becomes evidence, by design.
