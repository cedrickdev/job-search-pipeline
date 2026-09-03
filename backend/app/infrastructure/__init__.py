"""Adapters that talk to the outside world (docs/ARCHITECTURE.md §1).

Phase 2 populates `database` only: SQLAlchemy models, custom column types, the
engine factories and the ORM-to-domain mappers. Anything here may import
`backend.app.domain`; nothing in the domain may import anything here, which
`tests/test_v2_domain_purity.py` enforces.
"""
