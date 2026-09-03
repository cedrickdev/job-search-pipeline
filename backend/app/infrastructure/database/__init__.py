"""PostgreSQL/PostGIS persistence for the V2 domain.

Five modules, in dependency order:

- `types` — the column types the schema is built from (`UtcDateTime`,
  `GeographyPoint`) plus the PostGIS expressions that query them;
- `base` — the declarative base, its naming convention and the
  annotation-to-column mapping every model inherits;
- `models` — the tables;
- `mappers` — ORM row ↔ domain model translation, the only place that knows
  both shapes;
- `engine` — engine and session factories.

The tables are created by Alembic (`backend/migrations`), never by
`metadata.create_all()`: docs/IMPLEMENTATION_PLAN.md Phase 2 requires that a
clean database be constructible from migrations alone, and a schema that can
also appear out of `create_all` drifts from its migrations without anyone
noticing. A test compares the two definitions instead.
"""
