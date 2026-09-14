// The `/api/v2/geo/*` surface: two open reads and one saved-search read.
//
// A file of its own, like useCompanies.ts, and for the same reason — none of this is
// scoped to the account. Opportunities and employers placed in space are shared facts
// (docs/GEO_SEARCH.md §Sharing), so the two open reads have no "me" in their keys. The
// third does, but the scoping is the saved search's, not the map's: the owner is the
// session's, never a value the map sends (docs/AUTHENTICATION.md §Authorization).
//
// Two things here are decisions rather than plumbing.
//
// **The filters are the cache key, and they are built once, here.** As with the jobs
// board and the company directory, a page of full-time roles within 25 km and a page of
// everything are different answers; one entry for both would serve whichever landed
// last. `geo:` is the prefix. `geoQuery` is exported because the key is built from it,
// so a test can assert the key and the request URL agree instead of inferring one from
// the other.
//
// **The array params are repeated, not joined.** `radius`, `country`, `opportunity_type`
// and `workplace_mode` each appear once per value (`?radius=…&radius=…`), which is what
// FastAPI's `Query(list)` reads and what the generated types describe. `bounds` and
// `remote`, by contrast, are single-valued. Nothing here filters by geography — PostGIS
// already did (§34); this only shapes the request.
import { computed, toValue } from 'vue'
import type { MaybeRefOrGetter } from 'vue'
import type {
  CompanyGeoResponse,
  OpportunityGeoResponse,
  OpportunityType,
  RemoteSelection,
  WorkplaceMode,
} from '~/types/v2'
import { ApiError, apiGet } from '~/utils/api-client'
import { V2_ENDPOINTS, searchProfileOpportunities } from '~/utils/endpoints'
import { useApiQuery } from './useApiQuery'

/** The default page size, matching `DEFAULT_GEO_LIMIT` on the backend (max 200). */
export const GEO_PAGE_SIZE = 50

/** Gate a geo read on a condition the page owns — which mode is showing, mostly. */
export interface GeoQueryOptions {
  enabled?: MaybeRefOrGetter<boolean>
}

/** Everything the two open geo reads accept. Arrays are repeated params; the rest single. */
export interface GeoFilters {
  /** Each `"lat,lng,km"` with an optional `,label`. Repeated as `radius`. */
  radii?: string[]
  /** ISO-3166 alpha-2 codes. Repeated as `country`. */
  countries?: string[]
  /** `"north,south,east,west"`. A single `bounds`; the map's current viewport when panning. */
  bounds?: string | null
  /** `exclude | include | only`. Absent means the backend default, `exclude`. */
  remote?: RemoteSelection
  /** Codes that bound a country-restricted remote role. Repeated as `remote_country`. */
  remoteCountries?: string[]
  opportunityTypes?: OpportunityType[]
  workplaceModes?: WorkplaceMode[]
  limit?: number
  offset?: number
}

/**
 * The query string for one set of filters, empty ones left out. Exported so the cache
 * key and the request URL are provably the same string.
 */
export function geoQuery(filters: GeoFilters): string {
  const params = new URLSearchParams()
  // Repeated params: one entry per value, in list order, so the key is deterministic.
  for (const radius of filters.radii ?? []) params.append('radius', radius)
  for (const country of filters.countries ?? []) params.append('country', country)
  for (const code of filters.remoteCountries ?? []) params.append('remote_country', code)
  for (const type of filters.opportunityTypes ?? []) params.append('opportunity_type', type)
  for (const mode of filters.workplaceModes ?? []) params.append('workplace_mode', mode)
  // Single-valued. `bounds` is omitted when absent — an empty string is not a viewport.
  if (filters.bounds) params.set('bounds', filters.bounds)
  if (filters.remote) params.set('remote', filters.remote)
  params.set('limit', String(filters.limit ?? GEO_PAGE_SIZE))
  params.set('offset', String(filters.offset ?? 0))
  return params.toString()
}

/** One page of opportunities placed in space. Every filter is part of the key. */
export function useGeoOpportunitiesQuery(
  filters: MaybeRefOrGetter<GeoFilters> = {},
  options: GeoQueryOptions = {},
) {
  const query = computed(() => geoQuery(toValue(filters)))
  return useApiQuery<OpportunityGeoResponse>(
    computed(() => `geo:opportunities:${query.value}`),
    () => apiGet<OpportunityGeoResponse>(`${V2_ENDPOINTS.geoOpportunities}?${query.value}`),
    { enabled: options.enabled },
  )
}

/** One page of employers placed in space. */
export function useGeoCompaniesQuery(
  filters: MaybeRefOrGetter<GeoFilters> = {},
  options: GeoQueryOptions = {},
) {
  const query = computed(() => geoQuery(toValue(filters)))
  return useApiQuery<CompanyGeoResponse>(
    computed(() => `geo:companies:${query.value}`),
    () => apiGet<CompanyGeoResponse>(`${V2_ENDPOINTS.geoCompanies}?${query.value}`),
    { enabled: options.enabled },
  )
}

/** What the saved-search read accepts: a viewport and a window, nothing else. */
export interface SavedGeoFilters {
  bounds?: string | null
  limit?: number
  offset?: number
}

/** The saved-search query string. Narrow on purpose — the scope is the saved search's. */
export function savedGeoQuery(filters: SavedGeoFilters): string {
  const params = new URLSearchParams()
  if (filters.bounds) params.set('bounds', filters.bounds)
  params.set('limit', String(filters.limit ?? GEO_PAGE_SIZE))
  params.set('offset', String(filters.offset ?? 0))
  return params.toString()
}

/**
 * The opportunities of one saved search, run with its own radius/country/remote/type
 * logic on the backend — the map never reimplements it (§15). `null` is a saved search
 * id nothing is stored under, mapped from `search_profile_not_found` exactly as
 * useCompanies maps a missing company: a stale id in a shared URL is an ordinary screen
 * to render, not a failure, and it has to be read here while it is still an `ApiError`
 * (useAsyncData wraps a thrown error in a `NuxtError`).
 *
 * `enabled` guards the absent id: the page reads it from the URL, and a request against
 * `/search-profiles/none/opportunities` would be a 404 rather than a page.
 */
export function useSavedSearchOpportunitiesQuery(
  profileId: MaybeRefOrGetter<string | null>,
  filters: MaybeRefOrGetter<SavedGeoFilters> = {},
  options: GeoQueryOptions = {},
) {
  const id = computed(() => toValue(profileId))
  const query = computed(() => savedGeoQuery(toValue(filters)))
  return useApiQuery<OpportunityGeoResponse | null>(
    computed(() => `geo:saved:${id.value ?? 'none'}:${query.value}`),
    async () => {
      try {
        return await apiGet<OpportunityGeoResponse>(
          `${searchProfileOpportunities(id.value as string)}?${query.value}`,
        )
      }
      catch (error) {
        if (error instanceof ApiError && error.code === 'search_profile_not_found') return null
        throw error
      }
    },
    // Both gates must hold: a real id, and the page still showing the saved-search view.
    { enabled: computed(() => id.value !== null && (options.enabled === undefined || toValue(options.enabled))) },
  )
}
