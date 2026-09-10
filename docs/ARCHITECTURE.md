# Job Search Pipeline — V2 Technical Architecture

## 1. Architectural direction

V2 should be a modular monolith first, with explicit domain boundaries and infrastructure adapters. Do not prematurely split into microservices.

Target conceptual layers:

- API / presentation;
- application services;
- domain;
- infrastructure adapters;
- background workers.

## 2. Proposed repository direction

A gradual target:

```text
backend/
  app/
    api/
    core/
    domain/
    users/
    profiles/
    companies/
    opportunities/
    discovery/
    matching/
    eligibility/
    geo/
    resumes/
    applications/
    interviews/
    conversations/
    intelligence/
    llm/
    infrastructure/
frontend/
docs/
country_packs/
```

Existing `pipeline/` and `server/` modules should be migrated incrementally, not rewritten in one large change.

## 3. Core entities

### User
Owns all candidate-specific data.

### CandidateProfile
Canonical professional and administrative profile.

### CandidateEvidence
Evidence backing a claim used in CVs, letters or matching.

### SearchProfile
One autonomous search configuration.

### SearchArea
Country/radius/polygon/remote constraints.

### Company
Canonical company identity. Realized in Phase 6: a first-class discovery target
rather than a name printed on a posting, carrying the comparison forms identity
resolution reads, a typed ATS detection, an evidence-backed spontaneous-application
verdict and an identity status. A company exists whether or not it is hiring.
See [Company Discovery](./COMPANY_DISCOVERY.md).

### CompanyAlias
Another label the same employer is published under, with the source that reported it.

### CareerSite
One careers endpoint of one company — corporate page, ATS board, spontaneous form.

### CompanyDiscoveryRecord
How one provider came to know about one employer. Survives the company it points at.

### CompanyLocation
Physical location, preferably PostGIS geometry.

### Opportunity
Normalized professional opportunity.

### OpportunitySourceRecord
Source-specific representation and provenance.

### MatchEvaluation
Multidimensional scoring result.

### Application
Lifecycle record for a candidate pursuing an opportunity.

### ApplicationArtifact
CV, cover letter, answers, generated research or other files.

### ApplicationEvent
Append-only audit event.

### InterviewSession
Interactive interview practice session.

### Conversation
Chat conversation with scoped context.

### AgentRun / WorkflowRun
Auditable background execution.

## 4. Persistence

Target:

- PostgreSQL;
- PostGIS;
- SQLAlchemy 2.x;
- Alembic;
- UTC timestamps;
- UUID identifiers;
- JSONB only for flexible metadata, not as a substitute for domain modeling.

SQLite may remain supported for local development during migration, but PostgreSQL is the target source of truth for SaaS.

## 5. Multi-tenancy

All candidate-owned records must include or resolve to `user_id`.

Repository/query APIs must make accidental cross-user reads difficult.

Avoid accepting arbitrary `user_id` values directly from frontend payloads. Resolve identity from authenticated context.

Phase 4 realized this for the first user-owned tables (`candidate_profiles`, `search_profiles`). The owner is never in a path and never in a request body — `extra="forbid"` rejects one that invents a `user_id` — it comes from the session, and the repository puts it in the `WHERE` clause rather than filtering rows in Python. Another account's row is therefore absent rather than forbidden, and answers the same 404 as one that never existed. See [V2 Authentication](./AUTHENTICATION.md) §Authorization.

The converse is equally deliberate: a **shared fact carries no owner**. Companies,
their aliases, their careers endpoints, their discovery provenance and the
opportunities themselves have no `user_id`, so two accounts asking the same question
get the same answer. Those endpoints still require a session — the directory is not
public — but authorization has nothing to scope, and adding an owner column to make a
read *look* scoped would be a lie about who the data belongs to. See
[Company Discovery](./COMPANY_DISCOVERY.md) §Sharing.

## 6. Background execution

The current in-process FastAPI scheduler is suitable for V1 but not the long-term SaaS runtime.

Introduce a task abstraction before selecting a concrete queue.

Concept:

```python
class TaskDispatcher(Protocol):
    async def enqueue(self, task: TaskSpec) -> TaskId: ...
```

Possible future implementation:

- Redis + Dramatiq;
- Redis + Celery;
- ARQ;
- workflow engine if orchestration complexity justifies it.

Task families:

- discovery;
- company discovery;
- geocoding;
- matching;
- CV generation;
- application;
- recruiter inbox synchronization;
- interview preparation;
- analytics.

## 7. Source plugin contract

Implemented in Phase 5. See [Country Packs and Source Plugins](./COUNTRY_PACKS.md)
for the full contract, the capability model and the health semantics.

```python
class OpportunitySource(Protocol):
    @property
    def metadata(self) -> SourceMetadata: ...

    async def discover(self, request: DiscoveryRequest) -> DiscoveryResult: ...

    async def healthcheck(self) -> SourceHealth: ...
```

Three members, none of which mentions HTTP, HTML, an ATS or a browser: the
orchestrator cannot tell one transport from another.

`metadata` carries identity and declared capabilities — country support, query
shape, traversal, which fields come back typed, and downstream use such as direct
apply URLs and ATS metadata. Capabilities a source honours only as a ranking hint
are declared separately as advisory, so a caller can be told the answer was not
filtered.

`discover` returns rather than raises: a failing source reports a non-HEALTHY
`SourceHealth` inside its `DiscoveryResult`, which is the only shape that can also
carry the postings it did collect. Detail fetching is not part of the Phase 5
contract; a later phase adds it as its own capability rather than as a mandatory
method.

### 7b. Company discovery provider contract

Implemented in Phase 6, and deliberately a **separate** protocol. See
[Company Discovery](./COMPANY_DISCOVERY.md).

```python
class CompanyDiscoveryProvider(Protocol):
    @property
    def metadata(self) -> CompanyProviderMetadata: ...

    async def discover(self, request: CompanyDiscoveryRequest) -> CompanyDiscoveryResult: ...

    async def healthcheck(self) -> ProviderHealth: ...
```

The same three members, and the same "returns rather than raises" rule, over a
different question: an `OpportunitySource` answers "what is open right now?" and a
`CompanyDiscoveryProvider` answers "which employers exist?". One protocol serving both
would force every provider to invent postings it has never seen and every source to
promise a company identity it cannot resolve. Health, failure classification and
secret redaction are Phase 5's, reused rather than reimplemented.

## 8. Application adapter contract

```python
class ApplicationAdapter(Protocol):
    key: str

    async def can_handle(self, opportunity: Opportunity) -> bool:
        ...

    async def prepare(self, context: ApplicationContext) -> PreparedApplication:
        ...

    async def submit(self, prepared: PreparedApplication) -> SubmissionResult:
        ...
```

Adapters include ATS-specific, browser, email and manual-required implementations.

## 9. LLM boundary

No domain module may import Anthropic/OpenAI/Codex/Claude-specific SDK code directly.

All LLM access flows through the provider-neutral layer specified in `LLM_PROVIDER_ARCHITECTURE.md`.

## 10. Typed LLM outputs

Critical decisions must use typed schemas, preferably Pydantic v2.

Examples:

- `OpportunityAnalysis`;
- `MatchExplanation`;
- `ApplicationDecision`;
- `ResumeRewrite`;
- `InterviewEvaluation`;
- `ChatActionProposal`.

Never parse irreversible business actions from unconstrained prose.

## 11. Geo architecture

Target data model:

- `CompanyLocation.location` → PostGIS `POINT`;
- `Opportunity.location` or relation to company location;
- `SearchArea.center` + radius or polygon.

Queries should use spatial indexes and database-side distance predicates.

## 12. Frontend

The V2 frontend target is **Nuxt 4 + Vue 3 + TypeScript**.

Phase 3 migrated the React + Vite frontend to Nuxt for parity and then removed it, so `frontend/` is the only frontend. Onboarding, maps and interview simulation are built on top of it rather than beside it.

### Target frontend stack

- Nuxt 4;
- Vue 3;
- TypeScript;
- Tailwind CSS;
- Nuxt UI;
- Pinia for durable client/global UI state;
- `useFetch`, `useAsyncData` and `$fetch` for FastAPI server state;
- VueUse;
- Zod for shared frontend validation where useful;
- MapLibre GL JS for the interactive map;
- ECharts for analytics/visualization;
- Vitest + Vue Test Utils;
- Playwright for critical E2E flows.

### Frontend/backend boundary

Nuxt is the presentation layer. FastAPI remains the single source of truth for business logic.

Do not duplicate matching, eligibility, application policy, LLM routing or persistence logic in Nitro server routes.

Nuxt server/Nitro may be used for frontend-specific concerns only when justified.

Suggested routes:

- `/`
- `/onboarding`
- `/dashboard`
- `/opportunities`
- `/opportunities/map`
- `/companies`
- `/applications`
- `/resumes`
- `/interviews`
- `/chat`
- `/analytics`
- `/profile`
- `/settings`
- `/billing`

Suggested structure:

```text
frontend/
  app/
    assets/
    components/
    composables/
    layouts/
    middleware/
    pages/
    stores/
    types/
    utils/
  nuxt.config.ts
  package.json
```

### State rules

Use FastAPI as source of truth for domain/server state.

Prefer:

- `useFetch` / `useAsyncData` / `$fetch` for opportunities, applications, companies, profiles and analytics;
- Pinia for shared client/UI state such as map filters, selected opportunity, onboarding wizard state and panel preferences.

Do not mirror the entire backend database into Pinia.

## 13. Containerization

Docker is a V2 architecture requirement.

Target local development should eventually support:

```bash
docker compose up
```

Core services:

- `frontend` — Nuxt 4;
- `api` — FastAPI / Python 3.12;
- `postgres` — PostgreSQL + PostGIS;
- `redis` — queue/cache infrastructure when introduced;
- `worker` — background execution;
- `browser-worker` — Playwright workload when isolated execution is introduced.

Containerization is introduced incrementally rather than as a prerequisite to Phase 0.

### Docker principles

- development images must support fast iteration;
- production images must use multi-stage builds where useful;
- secrets must come from environment/secret stores, never images;
- health checks are required for production services;
- database migrations must be explicit;
- browser automation should eventually be isolated from ordinary workers.

## 14. Observability

Every workflow run should expose:

- correlation/run id;
- user id;
- task type;
- source/adapter/provider;
- duration;
- retries;
- result;
- structured failure code;
- token usage where relevant;
- LLM cost where available.

Never log raw credentials.

## 15. Security boundaries

Sensitive data includes:

- API keys;
- gateway tokens;
- site credentials;
- CV/profile information;
- contact data;
- application answers;
- recruiter messages.

Requirements:

- encryption at rest for provider/site credentials;
- secrets excluded from logs;
- minimal browser-session persistence;
- explicit user authorization for application actions;
- audit log for autonomous submissions;
- server-side authorization on every candidate-owned resource.

Phase 4 built the session half of this: Argon2id passwords, server-side sessions whose tokens are stored only as SHA-256 digests, a double-submit CSRF check compared against the session row, and cookies whose `Secure` flag is a deployment setting rather than an inference from the request scheme. [V2 Authentication](./AUTHENTICATION.md) is the reference, including what it deliberately leaves out.

## 16. Migration philosophy

Do not perform a big-bang rewrite.

Use strangler-style migration:

1. create new domain abstractions;
2. adapt V1 services to them;
3. migrate persistence;
4. move endpoints;
5. delete obsolete modules only after parity and tests.
