"""Geocoding adapters: the provider-neutral half of Phase 7 (§4–§6).

`backend.app.domain.geo` holds the *vocabulary* — what a request is, what an
outcome means, what a resolved place carries. This package holds the **port** and
the implementations behind it, for the reason that module's own comment gives: a
Protocol whose methods are `async` is an infrastructure concern, and putting it in
the domain would make the domain purity test wrong about what a domain is.

The shape mirrors `backend.app.discovery` and `backend.app.companies`, so an
operator's mental model transfers:

- `contracts` — the `Geocoder` port, its settings, and the narrow HTTP seam that
  makes every adapter testable without a network (§38);
- `nominatim` — the one concrete provider Phase 7 ships, proving the abstraction
  is real rather than aspirational (§5);
- `caching` — a `Geocoder` that wraps another one and consults the
  `geocoding_cache` table first (§23);
- `bootstrap` — the only module that names a provider class.

Nothing here writes to a repository other than the cache, and nothing here decides
whether a location *should* be replaced: that is
`backend.app.services.geo_enrichment`'s judgement, and §7's rule about not
overwriting stronger coordinates lives there with the rest of the policy.
"""
