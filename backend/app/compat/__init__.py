"""V1 compatibility adapters.

Everything in this package exists to let V2 code read V1 data without V1 having
to change, and every module here is expected to be deleted once V1 is retired —
not when persistence lands, but when nothing needs the old tracker any more. Two
rules keep that promise honest:

- the dependency arrow points one way only. Nothing here imports `pipeline` or
  `server`; a mapper takes plain data in (`dict(sqlite3.Row)`), so the strict
  type checker never has to follow into leniently-typed V1 modules. `v1_import`
  is the one module allowed `sqlite3` itself, since opening V1's *file* is
  precisely its job, and tests/test_v2_domain_purity.py draws that line module
  by module rather than package-wide;
- nothing here writes to V1. These are readers and mappers, and the V1 database
  is opened read-only.
"""
