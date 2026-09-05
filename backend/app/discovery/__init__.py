"""Discovery — the source plugin framework (Phase 5).

Seven modules, in dependency order:

- `capabilities` — what a source can be asked to do. A leaf: it imports nothing.
- `contracts` — `SourceMetadata`, `DiscoveryRequest`, `DiscoveryResult`,
  `SourceHealth` and the `OpportunitySource` Protocol itself.
- `failures` — turning an adapter's exception into a code and a *secret-free*
  detail. Nothing else in this package is allowed to format an exception.
- `normalization` — a source payload plus a Country Pack becomes an
  `Opportunity`, with provenance preserved.
- `registry` — lookup and filtering by key, country, capability and priority.
- `requests` — `SearchProfile` → `DiscoveryRequest`.
- `orchestrator` — one sweep: select, run concurrently, isolate failures,
  aggregate.

`adapters/` holds the concrete V1-wrapping sources and `bootstrap` is the
composition root. Neither `registry` nor `orchestrator` may import either of
them: the whole point is that adding a source means writing an adapter and
registering it, never editing the algorithm (docs/COUNTRY_PACKS.md, and
tests/test_v2_discovery_boundaries.py asserts it).

The dependency on `country_packs` points one way — this package reads a pack,
a pack never reads this package, apart from the capability vocabulary a
`sources.yaml` names by string.
"""
