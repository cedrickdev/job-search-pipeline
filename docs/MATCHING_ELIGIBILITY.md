# V2 Matching and Eligibility

Built in Phase 9. This document describes the two deterministic engines that turn
a candidate profile and an opportunity into two independent verdicts:

```
CandidateProfile ─┐                    ┌─> MatchEvaluation   (a score, 0.0–1.0, + coverage)
                  ├─> match engine ────┘
Opportunity ──────┤
                  ├─> eligibility ─────┐
CountryPack ──────┘   engine           └─> EligibilityResult (a gate verdict, never a score)
```

The point of the phase in one sentence: **compatibility and permission are two
different questions, and the answer to one may never change the answer to the
other.** `Match: 92% / INELIGIBLE` and `Match: 61% / ELIGIBLE` are both valid
outcomes, and the system is built so that neither number can silently move the
other. A great fit the candidate is not allowed to take is still a great fit; a
mediocre fit they are perfectly entitled to is still mediocre.

Neither engine uses an LLM, an embedding, or a similarity threshold. Both are pure
functions of structured data: the same pair produces the same verdicts on every
machine and every run, and the reason a verdict came out the way it did is a typed
code, not a model's opinion.

## The invariants

Seven claims, each held by a test rather than by review:

- **The two axes are two records.** `evaluate` runs the match engine and the
  eligibility engine independently and stores each in its own repository. Neither
  is a field of the other; a re-run of one never disturbs the other. The service
  never passes an eligibility verdict into the match engine or vice versa — the
  separation is structural, not a discipline
  (`tests/test_v2_assessment_service.py`).
- **A dimension nobody can evaluate is omitted, never scored zero.** The match
  engine scores only the dimensions real structured data supports. `overall` is
  the weighted mean over the dimensions that were *evaluated* —
  `weighted_sum / sum(weights of evaluated dims)` — so an unassessable axis lowers
  *coverage*, not the score. A pair with nothing scorable returns `None`, which the
  UI renders as UNKNOWN, not as 0% (`tests/test_v2_match_engine.py`).
- **Coverage is a separate axis from the score.** `evidence_confidence` answers
  "how much of the profile's weight could we assess?", which is a different fact
  from "how well did what we assessed fit?". A perfect score over one evaluable
  dimension is a confident-looking number with low coverage, and the two travel as
  two fields.
- **Missing evidence is INCOMPLETE, an unknown is REVIEW_REQUIRED, neither is a
  refusal.** The eligibility engine never turns silence into INELIGIBLE. An
  unrecorded work authorization, an undeclared language, an unknown status — all
  route to "we don't know yet" or "a human should look", never to a block.
- **Operator-maintained pack data cannot refuse on its own (§legal safety).** Every
  blocking decision is routed through `permitted_block_status`, so a Country Pack
  rule whose authority is `OPERATOR_CONFIG` (the default) can only ever reach
  REVIEW_REQUIRED. Only a `VERIFIED` rule, or a candidate's own declaration, may
  produce INELIGIBLE. The Swiss student-permit 15h/week cap is the canonical case,
  and it has a named regression proving it cannot by itself block
  (`test_operator_config_permit_cap_cannot_by_itself_produce_ineligible`).
- **The owner comes from the session, never the body.** A `MatchEvaluation` and an
  `EligibilityResult` are user-owned; every read is scoped by `user_id`. A verdict
  another account produced reads as absent, and the three empty-read cases (no
  profile, not evaluated, not yours) are one indistinguishable 404 so an id cannot
  be probed.
- **A verdict carries only typed, UI-safe reasons.** Every non-ELIGIBLE check
  carries at least one `Reason` — an unexplained refusal is forbidden — and a
  reason is an uppercase code plus a plain sentence, never a serialized exception,
  a credential or an environment value.

V1 is untouched. The persisted shape of a `MatchEvaluation`, its dimension scores
and the eligibility tables reuse the Phase 1 domain models and the Phase 2
persistence conventions.

## Layout

```
backend/app/domain/matching.py       the scoring vocabulary: MatchDimension,
                                     DimensionScore, MatchEvaluation, MatchProfile,
                                     MatchClassification, to_percent
backend/app/domain/eligibility.py    the gate vocabulary: EligibilityRequirement,
                                     EligibilityStatus, RuleAuthority,
                                     DeterminationSource, EligibilityCheck,
                                     EligibilityResult (status is a property)
backend/app/matching/
  engine.py          evaluate_match — the deterministic compatibility engine
  skills.py          normalize_skill — the standalone, deterministic normalizer
  skill_ontology.py  the canonical/alias/family table (no distance metric)
backend/app/eligibility/
  engine.py          evaluate_eligibility — the deterministic gate engine
  policy.py          ELIGIBILITY_POLICY_VERSION, permitted_block_status
backend/app/services/assessment.py   the orchestrator: run both engines, store both
backend/app/api/routes/matches.py     the three authenticated endpoints
backend/app/api/schemas.py            the Phase 9 response/request models
backend/migrations/versions/rev_0006_phase_9_eligibility.py   the eligibility tables
```

## The match engine

`evaluate_match(profile, opportunity, *, pack, now, match_profile=DEFAULT_MATCH_PROFILE)
-> MatchEvaluation | None`.

### Dimensions

`MatchDimension` names six axes. Phase 9 can evaluate three of them, because those
are the three the structured data supports today:

- **LANGUAGE_FIT** — graded from the posting's language requirements against the
  candidate's declared proficiencies. A required language below the minimum is a
  *low score*, not a refusal — the gate is eligibility's.
- **LOCATION_FIT** — graded categorically by administrative proximity (same city >
  same country > different country); a remote posting is a full fit because it does
  not constrain where the candidate lives. It reads the administrative fields only
  — **it never recomputes distance**, which is Phase 7's job (§location).
- **SCHEDULE_FIT** — the candidate's availability band against the posting's
  workload. A workload stated only in percent needs a pack to convert to weekly
  hours (`PackMetadata.weekly_hours_for_percent`); with `pack=None` and a
  percent-only workload, the dimension is *omitted*.

SKILLS_FIT, EXPERIENCE_FIT and EDUCATION_FIT are named in the enum and weighted in
the profile, but no Phase 9 data feeds them, so they are omitted from every
evaluation today. They count against coverage, not against the score.

### Scoring and weighting

Scores are canonical `0.0–1.0` internally and rendered `0–100` at presentation
through `to_percent` (half-up, in one place, so two surfaces cannot disagree about
whether 0.715 is 71 or 72). `DEFAULT_MATCH_PROFILE` weights:

| Dimension      | Weight |
|----------------|--------|
| SKILLS_FIT     | 0.30   |
| EXPERIENCE_FIT | 0.20   |
| LANGUAGE_FIT   | 0.15   |
| LOCATION_FIT   | 0.15   |
| EDUCATION_FIT  | 0.10   |
| SCHEDULE_FIT   | 0.10   |

`overall` normalizes over the weights of the dimensions that were *scored*:
`sum(score * weight) / sum(weight)` across evaluated dimensions only. So a pair
where only LANGUAGE_FIT and LOCATION_FIT are evaluable is scored on those two
alone, and the missing four show up as reduced `evidence_confidence`.

Classification is served, never re-derived: `MatchProfile.classify` maps a score to
a `MatchClassification` band, and the API returns the band so a client never
re-implements a threshold that would drift.

### Evidence coverage

`evidence_confidence = sum(weight of evaluated dims) / sum(all weights)`. It is
`None` only when a `MatchEvaluation` is absent (nothing scorable). It is the answer
to a question the score cannot answer: a 100% match over one dimension has
`evidence_confidence` near 0.15, and the pair of numbers says "confident about what
we saw, and we saw very little" honestly.

## The eligibility engine

`evaluate_eligibility(profile, opportunity, *, pack, now) -> EligibilityResult`.
Always returns a result — WORK_AUTHORIZATION is emitted unconditionally — so the
"at least one check" the domain requires is always met, even when the honest answer
to every gate is "we don't know".

### States

`EligibilityStatus` has four values, and the two middles are not interchangeable:

- **ELIGIBLE** — the gate passed.
- **INCOMPLETE** — the platform does not yet know; evidence is missing.
- **REVIEW_REQUIRED** — the platform found something a human must confirm before it
  counts against the candidate (most importantly an unverified rule).
- **INELIGIBLE** — a definite refusal, permitted only on a `VERIFIED` rule or the
  candidate's own declaration.

`EligibilityResult.status` is a **property**, not a stored field: it is the worst-of
aggregate over the checks (`INELIGIBLE > REVIEW_REQUIRED > INCOMPLETE > ELIGIBLE`),
so a stored aggregate can never drift out of step with the checks it summarizes. The
persistence layer denormalizes a copy for filtering, but the property is the one
source of truth. `is_blocking` is true only for INELIGIBLE.

### Reasons

Every non-ELIGIBLE check carries at least one `Reason`, structurally enforced by the
`EligibilityCheck` validator — the model cannot be constructed the wrong way round.
A reason is a typed `code` (uppercase, e.g. `LANGUAGE_BELOW_MINIMUM`,
`PERMIT_HOURS_CAP_EXCEEDED`, `WORK_AUTHORIZATION_NEEDS_SPONSORSHIP`), a plain
`detail` sentence, and an `impact`. The vocabulary is closed and safe for a UI: no
secret, environment value, or exception text can reach it.

### Legal-policy safety

`RuleAuthority` — `VERIFIED`, `SOURCE_DECLARED`, `OPERATOR_CONFIG`, `UNKNOWN` — is
carried on each check and decides how far a rule may go. `permitted_block_status`
is the single function that turns authority into a ceiling: `VERIFIED` may reach
INELIGIBLE, everything weaker raises REVIEW_REQUIRED. The `EligibilityCheck` domain
validator enforces the same rule a second time, so a `COUNTRY_PACK_RULE` that is
INELIGIBLE without `VERIFIED` authority is a value error at construction.

This is why **no Swiss permit rule is hard-coded in core Python**: the numbers live
in `country_packs/ch/eligibility.yaml` as operator configuration, default to
`OPERATOR_CONFIG`, and therefore can only ever trigger review. The named regression
loads the real CH pack and proves the B_STUDENT 15h/week cap cannot by itself
produce INELIGIBLE.

### Schedule and work authorization

- **Schedule/workload** is two answers about the same numbers, and both are correct:
  a 15h permit cap against a 20h posting *lowers* SCHEDULE_FIT (matching) *and*
  raises a PERMIT_HOURS_CAP review (eligibility). A permit hours cap is a gate, not
  a low score, and a low score is not a gate.
- **Work authorization** is the baseline gate. The candidate's own declaration is a
  `CANDIDATE_DECLARATION` — strong enough to refuse (NOT_AUTHORIZED → INELIGIBLE) or
  route to review (REQUIRES_SPONSORSHIP → REVIEW_REQUIRED) — while silence is
  INCOMPLETE. A posting with no country cannot be assessed and says so. Schedule,
  language and permit gates that are UNKNOWN never become INELIGIBLE.

## SearchProfile integration

The engines read a `CandidateProfile` and an `Opportunity`; a `SearchProfile`'s
preferred workplace modes, contract types and workloads are *preferences* that shape
matching, never eligibility gates. MANDATORY requirements route to eligibility;
PREFERRED ones inform the match score. A preference the candidate stated but a
posting does not meet lowers a dimension; it does not close a gate.

## Persistence and versioning

Both verdicts are keyed idempotently on the `(candidate_profile_id, opportunity_id)`
pair via `match_evaluation_id` and `eligibility_result_id` (both `uuid5` of the
pair). Re-evaluating a pair replaces its verdicts rather than accumulating rows.

Provenance is persisted so a re-evaluation under changed rules is auditable:
`MatchEvaluation.evaluator_key` (`deterministic-match/1+<match-profile-version>`)
and `EligibilityResult.policy_version` (`eligibility-policy/1.0`). The Phase 9
migration `rev_0006_phase_9_eligibility` adds the eligibility tables and reuses the
existing `match_evaluations` / `match_dimension_scores` tables; it applies cleanly
both fresh→head and 0005→head.

`OpportunityRequirement` is a shared fact (no owner); `MatchEvaluation` and
`EligibilityResult` are user-scoped. Cross-user isolation is enforced in the
repositories and covered by tests.

## Ranking

A list of assessments preserves both axes and is **chronological, not ranked**: an
INELIGIBLE pair is never pushed down by pretending its match score is low. A client
that wants to rank by fit or filter by eligibility has both numbers and does so
itself — the API refuses to collapse two axes into one order.

## API

Three authenticated endpoints under `/api/v2`, owner always from the session:

- `POST /matches/evaluate` — assess one posting for the account's profile on both
  axes and store both verdicts. `200` (not `201`: a re-run replaces, keyed on the
  pair). `404 candidate_profile_not_found` (go to onboarding) or
  `404 opportunity_not_found`.
- `GET /opportunities/{opportunity_id}/match` — the stored assessment for one pair,
  a pure read that never runs an engine. `404 match_not_evaluated` covers "not
  evaluated", "no profile" and "not this account's" alike.
- `GET /matches` — this account's assessed pairs, most recently determined first.

The wire format keeps `match` (nullable) and `eligibility` (always present) as two
independent objects; each score is emitted on both scales (`overall` +
`overall_percent`), with the served classification and the separate
`evidence_confidence`.

## Frontend

Phase 9 is backend-only. The score and verdict badges, and any assessment view in
the Nuxt app, are deferred to a later phase; the API and its generated types are in
place for that work to build on.

## What Phase 10 will add

The skill normalizer (`normalize_skill`, `SkillOntology`) is built and tested now,
standalone and unused by the current engine, because Phase 10's extraction pipeline
is where skills and evidence arrive. It is deterministic by design — a lookup over
canonical names and explicit aliases, with **no distance metric**, so `java` and
`javascript` never merge — and an unknown token is preserved verbatim rather than
snapped to a neighbour. When Phase 10 populates candidate skills and opportunity
requirements, SKILLS_FIT and the other dormant dimensions become evaluable and the
weights above start to bind.
