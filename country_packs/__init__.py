"""Country Packs — everything about a country that is not about the platform.

A Country Pack answers questions the generic domain must not know the answer to:
what a full-time week is, what "apprentissage" means in universal terms, which
boards are worth sweeping, what a permit called "B" allows. V1 has no such
concept — `pipeline/discover.py` hardcodes twelve Swiss boards and
`config/searches.yaml` hardcodes two Vaud towns — which is why adding France
would today mean editing the discovery algorithm
(docs/V2_SPECIFICATION.md §5, docs/COUNTRY_PACKS.md).

Four modules and one pack:

- `errors` — the `COUNTRY_PACK_*` vocabulary and the single exception type. A
  leaf.
- `contracts` — the typed shape of a pack. Data only.
- `loader` — YAML on disk becomes those models, or raises with the file, the key
  and the accepted values (§16: malformed configuration must fail loudly).
- `registry` — packs by country code, with duplicate registration refused.
- `ch/` — Switzerland, the reference pack: five YAML files and a loader call.

**A pack is data, never behaviour.** No LLM call, no browser automation, no HTTP
client, and above all no credential: `sources.yaml` names the *environment
variable* a source needs (`JOOBLE_API_KEY`), never its value, and
`SourceBinding` constrains that field to a shape a real key does not have.

**The dependency direction.** This package imports the universal vocabulary it
maps *onto* — `backend.app.domain.*`, the leaf enum
`backend.app.discovery.capabilities`, and from `backend.app.discovery.contracts`
the two constrained aliases `SourceKey` and `EnvVarName` — and nothing else from
`backend`. Those aliases are imported rather than redeclared on purpose: a pack
that defined its own idea of a source key could name a source the registry would
refuse, and `EnvVarName`'s pattern is what makes it impossible to write a
credential *value* into `sources.yaml`. Both modules are data, so the direction
still only ever points at vocabulary.

In particular this package never imports `registry`, `orchestrator` or any
adapter: the discovery framework reads a pack, a pack never runs discovery. That
is a module-level rule, not a package-level one, and
`tests/test_v2_discovery_boundaries.py` asserts it by walking the imports rather
than trusting review.
"""
