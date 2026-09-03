# Job Search Pipeline — V2 Documentation

This directory is the source of truth for the V2 product and implementation plan.

## Documents

1. [V2 Product Specification](./V2_SPECIFICATION.md)
2. [Technical Architecture](./ARCHITECTURE.md)
3. [LLM Provider Architecture](./LLM_PROVIDER_ARCHITECTURE.md)
4. [Implementation Plan](./IMPLEMENTATION_PLAN.md)
5. [Frontend Architecture](./FRONTEND_ARCHITECTURE.md)
6. [Engineering Standards](./ENGINEERING_STANDARDS.md)
7. [V2 Persistence](./PERSISTENCE.md) — PostgreSQL/PostGIS, timezone policy, migrations, Docker and the V1 import, built in Phase 2
8. [V1 Baseline](./V1_BASELINE.md) — what V1 is and what its checks report, measured in Phase 0

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
