# Engineering Standards

## Runtime

Backend:
- Python 3.12+
- FastAPI
- Pydantic v2
- asyncio where appropriate
- PostgreSQL/PostGIS target
- SQLAlchemy 2.x
- Alembic

Frontend:
- Nuxt 4
- Vue 3
- TypeScript
- Tailwind CSS
- Nuxt UI
- Pinia
- VueUse
- MapLibre GL JS
- ECharts
- Vitest
- Vue Test Utils
- Playwright for E2E

## Quality gates

Backend target:
- pytest;
- Ruff;
- mypy in strict mode for new V2 modules;
- migration tests;
- deterministic unit tests for domain logic.

Frontend target:
- TypeScript strict mode;
- Nuxt typecheck;
- Vitest;
- Vue Test Utils;
- API contract/schema validation;
- Playwright critical-path E2E.

## Test pyramid

1. domain unit tests;
2. application-service tests;
3. adapter contract tests with fixtures/mocks;
4. API integration tests;
5. limited E2E flows.

Do not make tests depend on live job boards or live LLM providers by default.

## Source adapter tests

Every source adapter should have fixture-based tests covering:

- normal result;
- empty result;
- changed/missing optional fields;
- HTTP failure;
- malformed response;
- deduplication identity.

## Application adapter tests

Do not submit real applications in automated tests.

Use controlled pages/fixtures and test:

- field mapping;
- unsupported field detection;
- file attachment;
- requires-human flow;
- success confirmation recognition;
- typed failures.

## LLM tests

LLM-dependent features require:

- schema validation tests;
- deterministic mocked responses;
- prompt regression fixtures;
- evidence/hallucination guards;
- provider contract tests.

Live-provider tests should be opt-in.

## Database rules

- migrations are forward-only and reviewed;
- no `except Exception: pass` migration strategy in V2;
- foreign keys enforced;
- timestamps UTC;
- UUID ids;
- user ownership explicit;
- spatial indexes for geo queries;
- idempotency keys for background/import operations where needed.

## Security

- never commit secrets;
- encrypt stored API/site credentials;
- redact secrets from errors/logs;
- validate all LLM action proposals;
- enforce user authorization server-side;
- do not bypass MFA/CAPTCHA;
- audit autonomous submissions.

## Observability

Use structured logs.

Every background run gets a run/correlation id.

Failures should use stable codes such as:

- `SOURCE_UNAVAILABLE`
- `SOURCE_RATE_LIMITED`
- `LLM_INVALID_OUTPUT`
- `LLM_PROVIDER_UNAVAILABLE`
- `APPLICATION_REQUIRES_HUMAN`
- `APPLICATION_UNSUPPORTED`
- `GEOCODING_FAILED`
- `ELIGIBILITY_INCOMPLETE`

## Definition of done

A roadmap phase is done only when:

- acceptance criteria are met;
- tests pass;
- lint/type checks required by the phase pass;
- migrations are documented;
- no new secret is committed;
- documentation is updated;
- backward-compatibility implications are stated;
- the agent stops and asks the user to validate.


## Containerization

Docker and Docker Compose are mandatory V2 infrastructure components once PostgreSQL/PostGIS is introduced.

Required standards:

- reproducible local environment;
- pinned major service versions;
- explicit health checks;
- non-root containers where practical;
- no secrets baked into images;
- deterministic migration command;
- separate browser-worker image when Playwright isolation is introduced.
