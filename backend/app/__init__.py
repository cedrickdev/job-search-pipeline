"""V2 application package (docs/ARCHITECTURE.md §2).

Phase 1 populated two subpackages; Phase 2 adds three more:

- `domain` — pure models, enums, value objects and validation rules;
- `compat` — the V1-facing mapping and import layer, deletable once V1 retires;
- `core` — process configuration (today: how the database URL is resolved);
- `infrastructure` — adapters that talk to the outside world, starting with
  PostgreSQL/PostGIS persistence;
- `repositories` — the contracts application code depends on, plus their
  SQLAlchemy implementations.

The dependency direction is one-way and enforced by tests:
`infrastructure` and `repositories` import `domain`; `domain` imports neither.

The api/, matching/, geo/ and llm/ packages the architecture targets arrive with
the phases that need them; creating them empty now would only advertise
structure that nothing enforces.
"""
