# Career Intelligence — learning from real outcomes

> Phase 15. The platform can now *learn* from what happens after an application is sent.
> It records the real-world hiring milestones a search produces, measures them into a
> funnel, derives evidence-backed advice from that funnel, and turns a piece of advice into
> a change to a search or a policy **only** when a human approves it. It is a closed loop
> with one mutation, and this document is the contract that keeps that loop honest.

## The spine: one loop, one mutation

Everything in this phase is a link in a single chain:

```
observe → measure → recommend → user approves → an existing domain service executes
```

Each arrow is a boundary, and the boundaries are the point:

- **observe** writes facts about the outside world (a screen, an interview, a rejection).
- **measure** reads those facts into a report. It computes; it never writes.
- **recommend** reads the report into advice. Advice cites evidence and carries no lever.
- **user approves** is the only step that can change the system, and only a human can take it.
- **execute** is the *same* domain service the rest of the platform already uses — the loop
  is one more caller of it, never a private back door.

The two rules below are what make the words above safe. Read them first.

## the separation — outcomes never move execution state

This is the load-bearing invariant of the phase.

`Application.state` (Phase 12) is a **platform execution** lifecycle: it tracks what the
engine did — planned, prepared, gate-evaluated, submitted, failed. A Phase 15 **outcome**
is a fact about the **real world** — a recruiter acknowledged, screened, interviewed,
made an offer, or said no.

These are different axes and they never cross:

- Recording an outcome can never write an `ApplicationState`. A recruiter's rejection is a
  `REJECTED` **outcome**; it does **not** set the application to `FAILED`. `FAILED` means
  the engine failed to submit — a different fact entirely.
- Analytics may **read** execution state (an application had to be `SUBMITTED` to have an
  outcome worth counting) but may never **write** it.

The invariant is enforced by absence: nothing in `backend/app/career/outcomes.py`,
`backend/app/domain/outcome.py`, or the frontend `OutcomeTimeline.vue` /
`useOutcomes.ts` can reach a lifecycle transition. There is no code path from an outcome
to a state change, so there is no way to violate the rule by mistake.

## observe — the outcome record

An outcome is an append-only fact recorded against one application
(`backend/app/domain/outcome.py`, served by `backend/app/api/routes/outcomes.py`).

- **Kinds** are the hiring milestones: `ACKNOWLEDGED`, `SCREEN`, `ASSESSMENT`,
  `INTERVIEW`, `OFFER_RECEIVED`, `OFFER_ACCEPTED`, `OFFER_DECLINED`, `REJECTED`,
  `WITHDRAWN`.
- **Sources** record where the fact came from: `MANUAL_USER`, `EMAIL`, `ATS`, `IMPORTED`.
- **Nothing is deleted.** A mistake is *corrected* — a new outcome supersedes the old one
  and points back at it — or *retracted* — the outcome flips to `RETRACTED`. Every status
  (`EFFECTIVE`, `SUPERSEDED`, `RETRACTED`) stays in the record, because "we believed this,
  then took it back" is part of the audit.

The frontend surfaces this per application, on demand: expanding an application row on the
applications page mounts `OutcomeTimeline.vue`, which fetches only that application's
timeline (`useOutcomes.ts`), the same lazy shape as the execution trail beside it.
## measure — the funnel report

The report is a pure read over an account's applications and their outcomes
(`backend/app/domain/analytics.py`, computed by `backend/app/career/analytics.py`, served
by `backend/app/api/routes/career.py`, surfaced by `useCareer.ts` and `career.vue`).

It has four parts:

- **the funnel** — counts narrowing from `SUBMITTED` down through the milestones to
  `ACCEPTED`;
- **conversion rates** — response, interview-conversion, offer-conversion and acceptance,
  each carrying its numerator, denominator and sample size so a rate is never a naked
  percentage;
- **timing** — median (and quartile) days to first response, to interview, to offer, to
  decision;
- **breakdowns** — the same numbers cut by dimension: role family, source, opportunity
  type, document strategy.

**Maturity, not optimism.** An application too young to have plausibly resolved is not
counted as a failure. The report applies an observation horizon and reports *censoring*:
how many applications are mature enough to have resolved versus still too recent to count.
A thin matured sample yields a `null` rate — an honest dash — not a misleading `0%`.

## recommend — evidence-backed advice

A recommendation is advice derived from the report, and it has **zero authority**
(`backend/app/domain/recommendation.py`, `backend/app/career/recommendations.py`, rendered
by `RecommendationCard.vue`).

- Every recommendation **cites the internal evidence it rests on** — the rate or timing,
  with its sample size — so it can be audited against the funnel it came from. This is the
  phase's acceptance rule: *recommendations cite internal evidence/metrics.*
- Generation recomputes the report and **appends** a fresh set to a write-once store; it
  edits no prior recommendation and it does **not** mutate the report (the analytics cache
  is deliberately left untouched, `useCareer.ts`).
- A recommendation carries nothing it can execute. Acting on one is a separate, human step.

## the approval gate — the only mutation

Turning advice into a change is the single place this phase writes anything, and it is
gated behind a human (`backend/app/domain/strategy_change.py`,
`backend/app/career/strategy.py`, served by `backend/app/api/routes/strategy.py`, surfaced
by `useStrategyProposals.ts`, `strategy.vue` and `StrategyProposalCard.vue`).

A **strategy-change proposal** is a typed, bounded description of one edit to one search or
one application policy. It shows the whole change as a `before → after` diff — never the
raw levers — and it changes nothing until approved. On approval, the confirmed change is
handed to the **same service that already owns that edit** (e.g.
`backend/app/services/application_policy.py`); the gate never edits state directly.

Two protections live here:

- **Optimistic concurrency.** A proposal carries the `target_version` it was drawn
  against; if the target moved since, approval is refused as stale rather than clobbering a
  newer edit.
- **The sensitive second gate.** A change that *loosens a safety brake* — lowering the
  score floor, raising a cap, widening what the engine may apply to — is refused with
  `sensitive_confirmation_required` unless the caller sends an explicit `confirm_sensitive`.
  The UI catches exactly that refusal and asks for a deliberate second "yes"; only that
  second acknowledgement sends the flag. This is the acceptance rule made mechanical:
  **the system never silently expands the user's application policy.**

## roles — a supporting dimension

Role-family classification (`backend/app/domain/role.py`, `backend/app/career/roles.py`,
`backend/app/api/routes/roles.py`) is not a link in the spine; it is the lookup that lets
the funnel be cut by role family, so a breakdown can say "software-engineering roles
respond best" with a number behind it.

## What this phase does not do

- It does not act on advice automatically. There is no autopilot; the approval gate is the
  only mutation and only a human can pass it.
- It does not move, reinterpret, or override the Phase 12 execution lifecycle.
- It does not fabricate outcomes. An outcome is a fact a human or an integration recorded,
  never one the system inferred to make a funnel look better.

## Where it lives

| Concern | Backend | Frontend |
| --- | --- | --- |
| observe (outcomes) | `domain/outcome.py`, `career/outcomes.py`, `api/routes/outcomes.py` | `useOutcomes.ts`, `OutcomeTimeline.vue` |
| measure (analytics) | `domain/analytics.py`, `career/analytics.py`, `api/routes/career.py` | `useCareer.ts`, `pages/career.vue` |
| recommend | `domain/recommendation.py`, `career/recommendations.py` | `RecommendationCard.vue` |
| approve (strategy) | `domain/strategy_change.py`, `career/strategy.py`, `services/application_policy.py`, `api/routes/strategy.py` | `useStrategyProposals.ts`, `pages/strategy.vue`, `StrategyProposalCard.vue` |
| roles | `domain/role.py`, `career/roles.py`, `api/routes/roles.py` | (dimension only) |

Migration: `backend/migrations/versions/rev_0014_phase_15_career_intelligence.py`.
