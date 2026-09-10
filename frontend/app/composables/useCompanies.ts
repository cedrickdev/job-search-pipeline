// The `/api/v2/companies` surface: two reads and one write.
//
// A file of its own rather than lines in useAccount.ts, and the reason is the same
// one that keeps `user_id` off the companies table: none of this is scoped to the
// session's account. An employer is a shared fact — two accounts asking for the same
// company get the same answer — so there is no "me" in any of these keys and no owner
// in any of these paths (docs/COMPANY_DISCOVERY.md §Sharing).
//
// Two things here are decisions rather than plumbing.
//
// **The filters are part of the cache key.** As with the jobs board, a page of
// Greenhouse employers and a page of every employer are different answers, and one
// entry for both would serve whichever landed last. `companies:` is the prefix, so
// one `invalidate('companies')` after a discovery pass refreshes every filtered
// variant and the detail pages with it.
//
// **A discovery pass sends no seeds.** `POST /company-discovery/run` accepts a
// country and a provider selection, and there is deliberately no way to add an
// employer through it: companies are shared, so a request body that carried one would
// let any account write into every other account's directory. New seeds are
// configuration.
import { computed, toValue } from 'vue'
import type { MaybeRefOrGetter } from 'vue'
import type {
  AtsPlatform,
  CompanyDetail,
  CompanyDiscoveryRun,
  CompanyList,
  SpontaneousSupport,
} from '~/types/v2'
import { ApiError, apiGet, apiPost } from '~/utils/api-client'
import { V2_ENDPOINTS, company as companyPath } from '~/utils/endpoints'
import { invalidate, useApiQuery } from './useApiQuery'
import { useMutation } from './useMutation'

/** The default page size, matching `DEFAULT_PAGE_SIZE` on the backend. */
export const COMPANY_PAGE_SIZE = 20

/** The five filters `GET /companies` accepts, plus the window. */
export interface CompanyFilters {
  text?: string
  country?: string
  ats_platform?: AtsPlatform | null
  spontaneous_support?: SpontaneousSupport | null
  has_opportunities?: boolean | null
  limit?: number
  offset?: number
}

/**
 * The query string for one set of filters, with the empty ones left out.
 *
 * Exported because it is what the cache key is built from, so a test can assert the
 * two agree instead of inferring it from a request URL.
 */
export function companyQuery(filters: CompanyFilters): string {
  const params = new URLSearchParams()
  if (filters.text) params.set('text', filters.text)
  if (filters.country) params.set('country', filters.country)
  if (filters.ats_platform) params.set('ats_platform', filters.ats_platform)
  if (filters.spontaneous_support) {
    params.set('spontaneous_support', filters.spontaneous_support)
  }
  // Presence, not truthiness: `false` is a filter — "employers with no active
  // opportunity" is the question §28 exists to answer — and `if (…)` would drop it.
  if (filters.has_opportunities !== null && filters.has_opportunities !== undefined) {
    params.set('has_opportunities', String(filters.has_opportunities))
  }
  params.set('limit', String(filters.limit ?? COMPANY_PAGE_SIZE))
  params.set('offset', String(filters.offset ?? 0))
  return params.toString()
}

/** One page of employers. Every filter is part of the key. */
export function useCompaniesQuery(filters: MaybeRefOrGetter<CompanyFilters> = {}) {
  const query = computed(() => companyQuery(toValue(filters)))
  return useApiQuery<CompanyList>(
    computed(() => `companies:list:${query.value}`),
    () => apiGet<CompanyList>(`${V2_ENDPOINTS.companies}?${query.value}`),
  )
}

/**
 * One employer with its aliases, careers endpoints and provenance, or `null`.
 *
 * `null` is a company id nothing is stored under, mapped from
 * `company_not_found` exactly as useAccount.ts maps a missing profile: an id that
 * came out of a stale link is an ordinary screen to render, not a failure, and
 * `useAsyncData` wraps a thrown error in a `NuxtError` — so the code has to be read
 * here, while it is still an `ApiError`, or the page could only say "something went
 * wrong".
 *
 * `enabled` guards the missing id: the detail page reads it from the route, and a
 * request for `/companies/undefined` would be a 422 rather than a page.
 */
export function useCompanyQuery(companyId: MaybeRefOrGetter<string | null>) {
  const id = computed(() => toValue(companyId))
  return useApiQuery<CompanyDetail | null>(
    computed(() => `companies:detail:${id.value ?? 'none'}`),
    async () => {
      try {
        return await apiGet<CompanyDetail>(companyPath(id.value as string))
      }
      catch (error) {
        if (error instanceof ApiError && error.code === 'company_not_found') return null
        throw error
      }
    },
    { enabled: computed(() => id.value !== null) },
  )
}

/**
 * Run a discovery pass, then refresh every company view.
 *
 * The pass is idempotent (`created: 0` on a second identical call), so this is safe
 * to trigger twice — but it is still a write against shared data, which is why it is
 * a mutation with a visible pending state rather than something a page does on mount.
 */
export function useRunCompanyDiscovery() {
  return useMutation<void, CompanyDiscoveryRun>(
    () => apiPost<CompanyDiscoveryRun>(V2_ENDPOINTS.companyDiscoveryRun),
    { onSuccess: () => invalidate('companies') },
  )
}
