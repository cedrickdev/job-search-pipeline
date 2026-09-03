"""Process configuration for the V2 backend.

Phase 2 needs exactly one thing configured: where PostgreSQL is. It lives here
rather than in `infrastructure/database` so that a module which opens a
connection and a module which runs a migration read the same resolution rules,
and so those rules can be tested without a database.

Nothing in this package holds a secret of its own: every value comes from the
environment, and the one URL that is written down is the local development DSN
that `docker-compose.yml` publishes on the loopback interface.
"""
