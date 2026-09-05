"""The only place in `backend/` that is allowed to import `pipeline`.

§7 asks for `existing V1 implementation → V2 adapter → OpportunitySource`, which
means *something* has to import V1. Confining that import to this package is what
keeps the rest of V2 honest: `backend/app/domain` and `backend/app/compat` are
guarded by `tests/test_v2_domain_purity.py`, and
`tests/test_v2_discovery_boundaries.py` extends the same check to the discovery
framework — `contracts`, `capabilities`, `registry`, `requests`, `normalization`,
`failures` and `orchestrator` may not name `pipeline`, and only this package may.

Two modules:

- `v1_sources` — the two adapter shapes (a query source and an ATS board) that
  turn a V1 module into an `OpportunitySource`.
- `v1_catalog` — the declarations: which V1 module, under which key, with which
  capabilities. It is the one file in the system that names a board, and it is
  composition, not orchestration.

Deleting a V1 module is therefore a two-file change and never a core change,
which is the strangler seam docs/ARCHITECTURE.md asks for: when a native V2
implementation of jobup exists, its adapter replaces the wrapper under the same
`source_key` and nothing else in the platform notices.
"""
