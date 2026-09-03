"""V1 compatibility adapters.

Everything in this package exists to let V2 code read V1 data without V1 having
to change, and every module here is expected to be deleted once persistence
moves in Phase 2. Two rules keep that promise honest:

- the dependency arrow points one way only. Nothing here imports `pipeline`,
  `server` or `sqlite3`; a caller passes plain data in (`dict(sqlite3.Row)`), so
  the strict type checker never has to follow into leniently-typed V1 modules;
- nothing here writes. These are readers and mappers.
"""
