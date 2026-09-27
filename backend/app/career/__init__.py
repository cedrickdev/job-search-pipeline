"""The Career Intelligence Loop — observe, measure, recommend, and (only then) change (Phase 15).

Phase 15's whole design is one sentence the code is shaped to make unbreakable:

    observe → measure → recommend → user approves → an existing service executes.

Each link is deliberately weaker than the next reads as. An `ApplicationOutcome` only
*observes* a real-world hiring fact — it can never drive a Phase 12 `ApplicationState`, so a
recruiter's rejection is not an execution failure (§2, §84). `CareerAnalytics` only *measures*:
a deterministic, versioned, censoring-aware funnel that no provider is anywhere near (§24-25).
A `CareerRecommendation` only *suggests*, and only with cited evidence that clears a sample
floor — it holds zero mutation authority (§26-33). The single link allowed to touch the
platform is a `StrategyChangeProposal`, and even it changes nothing until a human approves it,
at which point the typed change is re-validated against the live target and handed to the
**same** application service that already owns that edit (`OnboardingService` for searches, a
minimal `ApplicationPolicyService` for policies). Nothing here writes a `SearchProfile` or an
`ApplicationPolicy` directly.

The modules, in the order the loop flows through them:

- `errors` — the one typed refusal (`CareerError`) every service in the loop raises, carrying
  a stable `CareerErrorCode` the API maps to a status once;
- `outcomes` — recording, correcting and retracting the real-world facts, append-only and
  idempotent by the outcome's derived id, never touching an application's execution state;
- `roles` — the deterministic role-family classification (and a human's manual correction) the
  by-role analytics groups on;
- `analytics` — the deterministic aggregation that turns effective outcomes into a
  self-describing `CareerAnalytics` report;
- `recommendations` — the engine that reasons over *that report* (never raw rows) and emits
  evidence-backed suggestions, with an optional wording seam that a provider may only polish;
- `strategy` — the proposal service and its approval executor: the one place a confirmed,
  re-validated change reaches an existing service, audited whatever the outcome.

The acceptance the phase is judged on lives here: a recommendation always cites internal
metrics, and the system never silently expands the user's application policy — a loosening
edit is sensitive by construction and demands an explicit second confirmation.
"""
