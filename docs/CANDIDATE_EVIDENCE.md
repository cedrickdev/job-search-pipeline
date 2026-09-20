# V2 Candidate Evidence Store

Built in Phase 10, alongside the generated documents that draw on it (see [ATS
Documents](./ATS_DOCUMENTS.md)). This document describes the layer that holds the
candidate's *attested facts* — the set a résumé or cover letter is allowed to be
built from:

```
onboarding profile ─┐
manual entry        ─┼─> CandidateEvidence ─┐
imported CV / base  ─┘   (a filed record)   ├─> CandidateProfile (the aggregate)
                                            │     └─ _claims_rest_on_held_evidence
       CandidateClaim ──cites──> evidence_ids┘        (an assertion resting on records)
```

The point of the layer in one sentence, quoted from `backend/app/domain/documents.py`
and `CLAUDE.md` because it is the rule the whole phase serves:

> **The system may rewrite, reorder, shorten, emphasize or omit truthful candidate
> information, but it may never invent new candidate facts.**

Evidence is what makes "truthful candidate information" a *set* rather than a
feeling. A record here is a fact the candidate has attested — a CV bullet, a diploma,
a language assessment, a self-declaration — filed with where it came from. A claim is
an assertion ("Senior Backend Engineer", "speaks French at C1") that names the
evidence it rests on. A generated document may select from and re-order these; it may
never state something no record supports (docs/V2_SPECIFICATION.md §9,
docs/ARCHITECTURE.md §5). The guard that enforces that at generation time is Phase
10's other half, documented separately.

## The invariant

Four claims, each held by a test rather than by review:

- **A claim cannot rest on nothing.** `CandidateClaim.evidence_ids` is
  `Field(min_length=1)` — an unsupported claim is *unconstructible*, not merely
  discouraged. This is the domain form of V1's "unknown bullet id" truth gate
  (`pipeline/tailor_io.py`), promoted from a validation function to a type
  constraint. The same shape recurs on the document side (`EvidenceBackedText`,
  `ResumeEntry`), which is why the guard has no `MISSING_CITATION` code to check —
  an uncited line cannot be built in the first place.
- **A claim may only cite evidence the profile already holds.** The rule lives in
  *three* layers so no single one is the only guard: the aggregate invariant
  `CandidateProfile._claims_rest_on_held_evidence` (checked on every construction,
  including on read from the database), the service's pre-write check that raises
  `ClaimCitesUnknownEvidence` → 422 with the offending ids, and the database's
  non-empty-array CHECK. A claim and the evidence it cites are written together in
  one upsert, so a half-written state where a claim outlives its evidence cannot be
  persisted.
- **There is no `LLM_GENERATED` provenance, by design.** `EvidenceProvenance` has
  eight members and none of them means "a model inferred it": a generated sentence
  is never evidence (§3), so a document pipeline that wanted to promote its own
  output into the store would find no provenance to file it under. `SYSTEM_DERIVED`
  is the one machine origin, and it is *deterministic derivation* from other stored
  facts — a language proficiency turned into a `LANGUAGE` claim — never generation.
  A test asserts the enum contains `SELF_DECLARATION` and does *not* contain
  `LLM_INFERENCE`, `INFERRED`, `ASSUMED` or `GENERATED`.
- **The owner comes from the session, never the body.** Every write is scoped to the
  account behind the cookie: the service takes a `user_id`, loads *that* account's
  profile, and stamps every record with it. The evidence and claim rows carry no
  `user_id` column at all — they hang off the profile, and the profile's owner is the
  owner (docs/ENGINEERING_STANDARDS.md §Security).

V1 is untouched. The base-CV library it already had is bridged, not replaced: an
evidence record's `reference_key` carries V1's stable bullet id (e.g.
`acme-checkout`) so a Phase 10 document can trace a line back to the V1 source it came
from.

## Layout

```
backend/app/domain/candidate.py          the evidence vocabulary: EvidenceKind,
                                          EvidenceProvenance, ClaimType, CandidateEvidence,
                                          CandidateClaim, and the CandidateProfile aggregate
                                          that holds them and enforces the citation invariant
backend/app/domain/identifiers.py         EvidenceId, ClaimId, new_evidence_id(), new_claim_id()
backend/app/services/evidence.py          CandidateEvidenceService — add_evidence, add_claim,
                                          the EvidenceDraft/ClaimDraft inputs, the
                                          ClaimCitesUnknownEvidence pre-write check
backend/app/api/routes/documents.py       POST /me/evidence, POST /me/claims, GET /me/evidence
backend/app/api/schemas.py                AddEvidenceRequest, AddClaimRequest, and the
                                          CandidateEvidence/Claim/List response models
backend/app/api/errors.py                 the slug → HTTP status mapping
backend/app/infrastructure/database/
  models.py                               CandidateEvidenceRow, CandidateClaimRow
  mappers.py                              evidence/claim ↔ row, reconciled with the profile
backend/migrations/versions/rev_0007_phase_10_documents.py
frontend/app/composables/useDocuments.ts  useEvidenceQuery, useEvidenceActions
frontend/app/pages/evidence.vue            the /evidence screen: record a fact, assert a claim
```

The evidence models live in `domain/candidate.py`, not `domain/documents.py`: they
are candidate facts, and the document types are what is *built* from them. Imports run
one way — `domain/candidate` ← `services/evidence` ← the routes — and none of the
domain depends on a service.

## The vocabulary

`EvidenceKind` says what *sort* of record a fact is, and `EvidenceProvenance` says
which *pipeline* filed it. The two are orthogonal on purpose: the kind tells a reader
how much weight a fact carries, the provenance is what makes an accepted document
auditable (§45).

| `EvidenceKind` | |
| --- | --- |
| `CV_BULLET` | one bullet from the CV, carrying V1's `reference_key` |
| `CV_SUMMARY` | the profile summary line |
| `EMPLOYMENT_RECORD` | a role held, with dates |
| `DIPLOMA`, `CERTIFICATE` | an awarded qualification |
| `LANGUAGE_ASSESSMENT` | a proficiency, ideally with a source |
| `PORTFOLIO_ITEM`, `REFERENCE` | a shown work, a named referee |
| `PERMIT_DOCUMENT` | a work-authorization document |
| `SELF_DECLARATION` | the weakest member — a fact the candidate states about themselves |

`SELF_DECLARATION` is deliberately *not* a loophole: it is still an attributed source
(the candidate), which is a different and stronger thing than a sentence a model
produced. "No member of this enum means 'an LLM inferred it'."

| `EvidenceProvenance` | |
| --- | --- |
| `CANDIDATE_PROFILE` | filed from the onboarding profile |
| `BASE_CV` | from the V1 base-CV library |
| `MANUAL_USER_INPUT` | typed into the evidence screen |
| `IMPORTED_CV` | parsed from an uploaded CV |
| `PROJECT`, `EMPLOYMENT_RECORD`, `EDUCATION_RECORD` | derived from a structured profile section |
| `SYSTEM_DERIVED` | deterministic derivation from other stored facts — the one machine origin, and never generation |

`ClaimType` is the kind of assertion a claim makes: `SKILL`, `EXPERIENCE`,
`EDUCATION`, `CERTIFICATION`, `LANGUAGE`, `AVAILABILITY`, `WORK_AUTHORIZATION`,
`ACHIEVEMENT`.

## The records

`CandidateEvidence` is one attested fact:

- `kind` and `provenance` as above;
- `summary` — a non-empty sentence, the fact itself;
- `reference_key` — optional, the bridge to V1's base-CV bullet id;
- `detail` — optional elaboration;
- `issued_on` / `valid_until` — an optional validity window, and
  `_validity_window_is_ordered` rejects a `valid_until` that precedes `issued_on`
  (same day is allowed — a one-day certificate is a fact, not a contradiction);
- `source_document` — a *label* (a path or URL), never file content: the store holds
  the claim that a document exists, not the bytes;
- `recorded_at` — when it entered the store.

`CandidateClaim` is an assertion resting on evidence:

- `claim_type` and a non-empty `label`;
- `detail` — optional;
- `evidence_ids` — a tuple of at least one `EvidenceId`. This is the whole point of a
  claim: it is what lets matching report a meaningful `evidence_confidence` instead of
  taking a keyword on faith.

Both live inside the `CandidateProfile` aggregate, whose validators hold the
invariants: identities are unique, every record's owner matches the profile's owner
(`_identities_are_unique_and_owned`), and every claim's — and every work
authorization's — cited evidence is present in the profile
(`_claims_rest_on_held_evidence`). Because those run on *construction*, they run again
when a profile is rebuilt from the database: a stored claim citing an id no evidence
row provides fails loudly on read rather than silently rendering an unsupported line.

## The service

`CandidateEvidenceService` is the write side, kept apart from `OnboardingService`
because the two answer to different rules: onboarding saves identity and preferences
*whole*; evidence is the attested record, *grown* over time. It holds only the profile
repository, because evidence and claims are child collections reconciled by the
profile upsert.

- `add_evidence(user_id, EvidenceDraft, *, now)` — loads the account's profile (404
  `candidate_profile_not_found` if none), appends a freshly-identified
  `CandidateEvidence` stamped with the owner, upserts, and returns the saved record.
- `add_claim(user_id, ClaimDraft, *, now)` — loads the profile, then checks the
  cited ids against the evidence the profile *holds* **before writing**. Any id that
  names no held record raises `ClaimCitesUnknownEvidence`, carrying the offending ids
  — a 422 with a message the screen can show, rather than the 500 that the aggregate's
  own re-check would otherwise produce. The early check is not the only guard; it is
  the one that gives the useful answer.

The drafts (`EvidenceDraft`, `ClaimDraft`) are frozen inputs with no id, no owner and
no timestamp: those are the service's to assign, not the caller's to claim.

## Persistence

Revision `0007`, `revises 0006`, additive throughout. Two tables and two column
additions:

- **`candidate_evidence`** and **`candidate_claims`** — child tables of
  `candidate_profiles`, each with `id`, `profile_id` (FK, `ON DELETE CASCADE`), an
  `ordinal` that preserves the domain tuple order, the record's own columns, and
  timestamps. **Neither has a `user_id`** — the owner is the profile's owner, and a
  second copy of it would be a second thing to keep in agreement. `candidate_claims`
  stores `evidence_ids` as a `TEXT[]` with two CHECKs: no null or empty element, and
  `array_length(evidence_ids, 1) >= 1` — the `min_length=1` invariant made physical.
  `candidate_evidence` carries the `issued_on <= valid_until` CHECK. Both have a
  `UNIQUE (profile_id, ordinal)` and an index on `profile_id`.
- **`evidence_ids` on `candidate_work_authorizations` and `eligibility_checks`** —
  Phase 9's checks and the profile's work authorizations get back the evidence
  citations they always modelled, as `TEXT[]` columns (default `'{}'`) with the
  element-present CHECK. A work authorization's citations are re-checked against the
  profile on read; an eligibility check's are provenance only.

Evidence and claims **cite by array, not by foreign key**: PostgreSQL has no
array-of-foreign-keys, and a link table would have to be joined across on every read
of a profile. The integrity that a foreign key would give is instead the aggregate's
`_claims_rest_on_held_evidence`, re-run on every load. Enums are `VARCHAR(32)` + CHECK
(never native `ENUM`), and the migration writes each member list out verbatim so a
schema-drift test fails if `models.py` and the migration disagree. Ids are
domain-supplied (`uuid4` via `new_evidence_id()` / `new_claim_id()`), not
database-generated, so a record has the same id in the domain and the row.

`downgrade` drops the two tables and the two added columns; it is destructive, because
the records are the phase's own data and nothing earlier held them.

## The API

Three endpoints, all under `/api/v2`, all behind an authenticated session. The two
writes need the `X-CSRF-Token` header (the check is inside the session dependency, so
any authenticated unsafe method is covered); the read needs only the cookie.

| Operation | Status | Meaning |
| --- | --- | --- |
| `POST /me/evidence` | 201 | file one attested fact (`AddEvidenceRequest` → `CandidateEvidenceResponse`) |
| `POST /me/claims` | 201 | assert one claim citing held evidence (`AddClaimRequest` → `CandidateClaimResponse`) |
| `GET /me/evidence` | 200 | the whole attested record — evidence and claims (`CandidateEvidenceListResponse`) |

The owner is never in the path: `/me` *is* the authorization model, resolved from the
session. `AddEvidenceRequest` constrains every optional string to `min_length=1`, so a
blank `reference_key` or `detail` must be sent as absent (null), not as `""` — the
frontend turns an empty field into null for exactly this reason. `AddClaimRequest`
carries `evidence_ids` with `min_length=1`, so the "a claim cites at least one record"
rule is enforced at the edge as well as in the domain.

The error slugs a client branches on:

| Exception | Status | Slug |
| --- | --- | --- |
| `CandidateProfileNotFound` | 404 | `candidate_profile_not_found` |
| `ClaimCitesUnknownEvidence` | 422 | `claim_cites_unknown_evidence` (body also carries the offending `evidence_ids`) |

`candidate_profile_not_found` is the same code `GET /me/profile` answers before
onboarding has saved a profile, so a client reads it the same way on both: not an
error, but "finish onboarding first". The `claim_cites_unknown_evidence` ids are the
client's own, echoed back to say *which* citation failed — not a secret, because the
caller sent them.

## The frontend

One screen, `/evidence` (`app/pages/evidence.vue`), and two composable helpers in
`useDocuments.ts`:

- `useEvidenceQuery()` reads `GET /me/evidence` and maps
  `candidate_profile_not_found` to `null` — the "no profile yet" state, told apart
  from "a profile with no evidence yet" so the page can point at onboarding rather
  than render forms that would 404 on submit.
- `useEvidenceActions()` exposes `addEvidence` and `addClaim`, each invalidating the
  `me:evidence` cache so a new record or claim shows without a reload.

The screen is the write side of the truth guarantee made operable. It is two forms in
a deliberate order — record a fact, *then* assert a claim on it — because a claim can
only cite evidence already stored. The claim form's evidence picker lists the records
above it; asserting is disabled until at least one is checked, so the "a claim rests
on evidence" rule is visible in the UI, not just enforced on the server. A rejected
claim (`claim_cites_unknown_evidence`) becomes a sentence under the form, not a thrown
error.

## Tests

No test in the default suite touches a live LLM or a live job board (CLAUDE.md
§Testing). The domain invariants run in-process; the persistence tests run against a
real PostgreSQL.

| Suite | Covers |
| --- | --- |
| `tests/test_v2_candidate.py` | the domain invariants — the empty-summary and empty-`evidence_ids` refusals, the ordered validity window, a claim resolving to its records in profile order, a profile refusing another user's records, and the assertion that no `EvidenceKind` means "a model inferred it" |
| `tests/test_v2_documents.py` | the evidence *service* — a fact filed under the account, a claim that cites held evidence stored, a claim citing unknown evidence refused *before* the write, and evidence refused before a profile exists |
| `tests/test_v2_persistence_repositories.py` | the profile repository round-tripping its evidence and claims |
| `tests/test_v2_persistence_constraints.py` | the row-level CHECKs — non-empty `evidence_ids`, the ordered validity window |
| `tests/test_v2_persistence_mappers.py` | evidence/claim ↔ row fidelity |
| `tests/test_v2_persistence_schema.py` | schema drift between `models.py`, the metadata and the migration's enum lists |
| `tests/test_v2_persistence_migrations.py` | the upgrade and downgrade of the Phase 10 tables |
| `tests/test_v2_api_surface.py` | the evidence and claim endpoints under the prefix, authenticated, the writes carrying CSRF |
| `frontend/tests/nuxt/composables/useDocuments.spec.ts` | the read mapping a missing profile to null, the writes hitting their endpoints and invalidating `me:evidence` |
| `frontend/tests/nuxt/pages/evidence.spec.ts` | the screen — recording a fact with typed enums and null-not-empty optionals, the claim button disabled until evidence is cited, a rejected claim shown in its own words, the no-profile state |
| `frontend/tests/e2e/documents.spec.ts` | the browser flow — the screen reachable from the nav, a record written with its `X-CSRF-Token`, the anonymous redirect to `/login` |

## Known risks

- **A claim's citations are integrity-checked in Python, not by a foreign key.** The
  `TEXT[]` design means the database enforces only that `evidence_ids` is non-empty;
  that each id names a *held* record is the aggregate's job on read. A row edited
  outside the application could hold a dangling citation, and it would surface as a
  loud failure when the profile is next loaded rather than as silent corruption — but
  the database alone does not prevent it.
- **Nothing yet imports evidence in bulk.** A CV upload that parses into many records
  is a future pass; today the store is filled from onboarding and the manual form, so
  a candidate with a long history types or is migrated in, rather than dropping a PDF.
- **`source_document` is a label the store trusts.** It records that a document exists
  and where; it does not fetch or verify it. An evidence record can name a diploma the
  store never saw, and the weight a reader gives that is the `EvidenceKind`'s job to
  signal, not the store's to check.

## Not in this layer

The generation and validation of documents from this evidence — the reference
generator, the `CandidateEvidenceGuard`, PDF rendering and the document surface — are
Phase 10's other half; see [ATS Documents](./ATS_DOCUMENTS.md). Contact PII (email,
phone, postal address) is deliberately absent from the evidence models
(docs/ENGINEERING_STANDARDS.md §Security). Any promotion of generated text into the
store: there is no provenance for it, by design.
