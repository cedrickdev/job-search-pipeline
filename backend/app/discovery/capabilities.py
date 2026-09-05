"""`SourceCapability` — what a source can actually be asked to do.

The orchestrator cannot know whether a source is a JSON API, an HTML listing, an
Algolia index or a browser session (docs/ARCHITECTURE.md §7). What it must know
is which parts of a `DiscoveryRequest` a given source can honour, because the
alternative is what V1 does: pass every argument to every source and let four of
them silently ignore it. `pipeline/sources/migros.py` takes `query` and
`location` and fetches one hardcoded Vaud listing regardless; nothing in V1 says
so except a docstring.

Two rules keep this enum honest:

**A capability is claimed only if the implementation really has it.** Six members
below currently have no holder at all — no V1 source paginates, filters by
opportunity type, filters by remote, does native radius, or returns a structured
salary or a structured location. They are declared because they are the
vocabulary a request and a degradation policy are written in: `RADIUS_SEARCH`
with zero holders is exactly what makes "this profile asks for 25 km and no
source can do it" a typed, tested outcome instead of a silent lie
(docs/COUNTRY_PACKS.md §Radius).

**Accepting an argument is not the same as filtering on it.** Welcome to the
Jungle folds the location into its free-text query and jobup.ch does the same
with its `term`; both change the *ranking* and neither restricts the result set.
Those sources declare `LOCATION_SEARCH` in `SourceMetadata.capabilities` and
*also* in `advisory_capabilities`, and the adapter emits a warning rather than
letting a caller believe the result set was narrowed.
"""
from enum import StrEnum


class SourceCapability(StrEnum):
    """One thing a source can be asked to do, as a stable machine name.

    A `StrEnum`, so a Country Pack's `sources.yaml` can name one as a string and
    a typo fails at load time with `COUNTRY_PACK_INVALID_CAPABILITY` instead of
    quietly selecting nothing.
    """

    # --- request shaping ---------------------------------------------------
    KEYWORD_SEARCH = "KEYWORD_SEARCH"
    LOCATION_SEARCH = "LOCATION_SEARCH"
    # No holder today. Phase 7 owns database-backed radius semantics; a source
    # that grows a native radius parameter claims this and stops being degraded.
    RADIUS_SEARCH = "RADIUS_SEARCH"
    # No holder today: not one V1 source accepts a remote or a contract-type
    # filter, which is why both are applied downstream instead.
    REMOTE_FILTER = "REMOTE_FILTER"
    OPPORTUNITY_TYPE_FILTER = "OPPORTUNITY_TYPE_FILTER"
    COMPANY_FILTER = "COMPANY_FILTER"

    # --- result shaping ---------------------------------------------------
    # No holder today: every V1 adapter reads page one and stops.
    PAGINATION = "PAGINATION"
    # A source that can be asked for "only what changed since". LinkedIn's
    # `f_TPR` and Indeed's `fromage` are exactly this; the rest sweep everything.
    INCREMENTAL_DISCOVERY = "INCREMENTAL_DISCOVERY"

    # --- payload richness -------------------------------------------------
    # No holder today. Ashby's `compensationTierSummary`, Indeed's
    # `salarySnippet.text` and Jooble's `salary` are all human strings — a range
    # parsed out of one would be a fabricated fact about a real employer.
    STRUCTURED_SALARY = "STRUCTURED_SALARY"
    # Three Swiss sources publish a machine-readable activity rate ("80%").
    STRUCTURED_WORKLOAD = "STRUCTURED_WORKLOAD"
    # No holder today: every V1 source gives one free-text location line, which
    # is why `Location.raw` is set and `city`/`country`/`point` are not.
    STRUCTURED_LOCATION = "STRUCTURED_LOCATION"
    # The posting URL *is* the apply URL, rather than a page linking to one.
    DIRECT_APPLY_URL = "DIRECT_APPLY_URL"
    # The payload identifies the applicant-tracking system behind the posting.
    ATS_METADATA = "ATS_METADATA"

    # --- operations -------------------------------------------------------
    # A source that can be probed without running a whole discovery sweep.
    HEALTHCHECK = "HEALTHCHECK"
