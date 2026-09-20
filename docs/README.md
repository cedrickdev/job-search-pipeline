# Job Search Pipeline — V2 Documentation

This directory is the source of truth for the V2 product and implementation plan.

## Documents

1. [V2 Product Specification](./V2_SPECIFICATION.md)
2. [Technical Architecture](./ARCHITECTURE.md)
3. [LLM Provider Architecture](./LLM_PROVIDER_ARCHITECTURE.md)
4. [Implementation Plan](./IMPLEMENTATION_PLAN.md)
5. [Frontend Architecture](./FRONTEND_ARCHITECTURE.md) — Nuxt 4 + Vue 3, the active frontend since Phase 3
6. [Engineering Standards](./ENGINEERING_STANDARDS.md)
7. [V2 Persistence](./PERSISTENCE.md) — PostgreSQL/PostGIS, timezone policy, migrations, Docker and the V1 import, built in Phase 2
8. [V2 Authentication](./AUTHENTICATION.md) — accounts, server-side sessions, the two cookies, CSRF and the onboarding gate, built in Phase 4
9. [Country Packs and Source Plugins](./COUNTRY_PACKS.md) — the pack contract, the source contract, capabilities, health, the Swiss reference pack, built in Phase 5
10. [Company Discovery](./COMPANY_DISCOVERY.md) — companies as first-class discovery targets, identity resolution, ATS detection, career pages, spontaneous applications, built in Phase 6
11. [Geo Opportunity Explorer](./GEO_SEARCH.md) — the geo search query, remote policy, the provider-neutral geocoder, the enrichment pass and its cache, built in Phase 7
12. [Interactive Map Explorer](./MAP_EXPLORER.md) — the MapLibre map and list over the geo API, the one-selection store, the pan-is-not-a-query rule, sharable URLs and the offline test map, built in Phase 8
13. [Matching and Eligibility](./MATCHING_ELIGIBILITY.md) — the two deterministic engines, the score-versus-verdict separation, evidence coverage, the legal-safety rule and the assessment API, built in Phase 9
14. [Candidate Evidence Store](./CANDIDATE_EVIDENCE.md) — the attested-facts substrate a document is built from, the citation invariant, the evidence/claim API and screen, built in Phase 10
15. [ATS Documents](./ATS_DOCUMENTS.md) — the résumé/cover-letter generator, the evidence guard's four gates, WeasyPrint rendering, the version lifecycle and the document API, built in Phase 10
16. [V1 Baseline](./V1_BASELINE.md) — what V1 is and what its checks report, measured in Phase 0

## Product principle

The user configures their career search once. The system then continuously discovers, ranks, prepares and tracks opportunities while minimizing repetitive manual work.

“100% automated” means that repetitive search, CV adaptation, cover-letter generation, supported form filling, tracking and follow-up preparation are automated. A provider may still require user action for CAPTCHA, MFA, legally required declarations, unsupported portals or policy-restricted workflows.

## Critical execution rule

No coding agent may implement multiple roadmap phases in one run.

For each phase:

1. inspect the repository and relevant documentation;
2. present the phase plan;
3. implement only the current phase;
4. run the required checks;
5. summarize changed files, migrations, tests and risks;
6. stop and explicitly ask the user to validate the phase;
7. continue only after explicit user approval.
