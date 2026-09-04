// Read composables — one per V1 query hook, keeping the query keys recognisable
// (["analytics", 30] became "analytics:30") so a reader can line these up against
// webapp/src/api/hooks.ts.
import { computed, toValue } from 'vue'
import type { MaybeRefOrGetter } from 'vue'
import type {
  Analytics,
  JobDetail,
  JobsResponse,
  Overview,
  PrepData,
  RunStatus,
  SettingsResponse,
} from '~/types/domain'
import { apiGet } from '~/utils/api-client'
import { ENDPOINTS, job as jobPath } from '~/utils/endpoints'
import { useApiQuery } from './useApiQuery'

export function useSettingsQuery() {
  return useApiQuery<SettingsResponse>('settings', () =>
    apiGet<SettingsResponse>(ENDPOINTS.settings))
}

export function useOverview() {
  return useApiQuery<Overview>('overview', () => apiGet<Overview>(ENDPOINTS.overview))
}

export function useAnalytics(days: MaybeRefOrGetter<number> = 30) {
  return useApiQuery<Analytics>(
    computed(() => `analytics:${toValue(days)}`),
    () => apiGet<Analytics>(`${ENDPOINTS.analytics}?days=${toValue(days)}`),
  )
}

export interface JobsParams {
  status?: string
  q?: string
  sort?: string
  view?: string
}

export function useJobs(params: MaybeRefOrGetter<JobsParams>) {
  const query = computed(() => {
    const qs = new URLSearchParams()
    for (const [k, v] of Object.entries(toValue(params))) {
      if (v) qs.set(k, v)
    }
    return qs.toString()
  })
  return useApiQuery<JobsResponse>(
    // The serialized params are part of the key, as ["jobs", params] was in V1,
    // so switching view or filter is a different cache entry rather than a
    // silent overwrite. `invalidate('jobs')` still covers all of them.
    computed(() => `jobs:${query.value}`),
    () => apiGet<JobsResponse>(ENDPOINTS.jobs + (query.value ? `?${query.value}` : '')),
  )
}

/**
 * Transitional alias. `Opportunity` is the V2 domain abstraction, but the V1
 * routes this phase migrates still speak `job`, and renaming the wire vocabulary
 * is not a frontend-migration change. This exists so V2 surfaces can be written
 * against the target name from the start and the rename becomes one file.
 */
export const useOpportunities = useJobs

export function useJobDetail(jobId: MaybeRefOrGetter<number | null>) {
  return useApiQuery<JobDetail>(
    computed(() => `job:${toValue(jobId) ?? 'none'}`),
    () => apiGet<JobDetail>(jobPath(ENDPOINTS.job, toValue(jobId) as number)),
    {
      enabled: computed(() => toValue(jobId) !== null),
      // While a CV regen is queued, poll so the drawer clears the "regenerating"
      // state and shows the new CV as soon as the background run renders it.
      // Also poll while an apply is in flight so the banner settles. Idle: off.
      pollInterval: (data) => {
        const applyActive
          = data?.last_apply?.status === 'pending' || data?.last_apply?.status === 'in_progress'
        return data?.pending_regen || applyActive ? 10_000 : false
      },
    },
  )
}

export function usePrep(jobId: MaybeRefOrGetter<number>) {
  return useApiQuery<PrepData>(
    computed(() => `prep:${toValue(jobId)}`),
    () => apiGet<PrepData>(jobPath(ENDPOINTS.jobPrep, toValue(jobId))),
  )
}

/** Poll run status only while a run is active (every 2s); stop polling at idle. */
export function useRunStatus() {
  return useApiQuery<RunStatus>('runStatus', () => apiGet<RunStatus>(ENDPOINTS.runsStatus), {
    pollInterval: (data) => (data?.state === 'running' ? 2000 : false),
  })
}
