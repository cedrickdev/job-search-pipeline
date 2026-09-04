// Write composables, one per V1 mutation hook. The invalidation sets are copied
// from webapp/src/api/hooks.ts deliberately — which caches a given action
// refreshes is observable behaviour, and three of them are counter-intuitive
// enough that changing them would be a silent parity break:
//
//   * `generatePrep` writes the server's response into the cache instead of
//     refetching, because a fail-closed draft is NOT persisted and a GET would
//     return the old prep and drop the warning;
//   * `triggerFull` refreshes only the run status, because a full run changes
//     nothing synchronously;
//   * `draftFollowup` invalidates nothing, because a draft is not state.
import { toValue } from 'vue'
import type { MaybeRefOrGetter } from 'vue'
import type { FollowupDraft, PrepData, Settings, SettingsResponse } from '~/types/domain'
import { apiPost, apiPut } from '~/utils/api-client'
import { ENDPOINTS, job as jobPath } from '~/utils/endpoints'
import { invalidate } from './useApiQuery'
import { useMutation } from './useMutation'

export function useSaveSettings() {
  return useMutation<Settings, SettingsResponse>(
    settings => apiPut<SettingsResponse>(ENDPOINTS.settings, settings),
    { onSuccess: () => invalidate('settings') },
  )
}

/** Every action bound to one job. `jobId` may be null while no drawer is open. */
export function useJobActions(jobId: MaybeRefOrGetter<number | null>) {
  const id = () => toValue(jobId) as number
  const refresh = async () => {
    const current = toValue(jobId)
    await invalidate('jobs', 'overview', ...(current === null ? [] : [`job:${current}`]))
  }
  const onSuccess = { onSuccess: refresh }

  return {
    go: useMutation<void, unknown>(() => apiPost(jobPath(ENDPOINTS.jobGo, id())), onSuccess),
    applied: useMutation<void, unknown>(
      () => apiPost(jobPath(ENDPOINTS.jobApplied, id()), {}),
      onSuccess,
    ),
    skip: useMutation<void, unknown>(() => apiPost(jobPath(ENDPOINTS.jobSkip, id())), onSuccess),
    setStatus: useMutation<string, unknown>(
      status => apiPost(jobPath(ENDPOINTS.jobStatus, id()), { status }),
      onSuccess,
    ),
    regen: useMutation<string, unknown>(
      notes => apiPost(jobPath(ENDPOINTS.jobRegen, id()), { notes }),
      onSuccess,
    ),
    applyNow: useMutation<void, unknown>(
      () => apiPost(jobPath(ENDPOINTS.jobApplyNow, id()), {}),
      onSuccess,
    ),
  }
}

/**
 * Board drag-and-drop status change. Unlike `useJobActions` (bound to one job)
 * this takes the id per call, so a single board-level mutation serves every card.
 */
export function useSetJobStatus() {
  return useMutation<{ jobId: number, status: string }, unknown>(
    ({ jobId, status }) => apiPost(jobPath(ENDPOINTS.jobStatus, jobId), { status }),
    { onSuccess: () => invalidate('jobs', 'overview') },
  )
}

export function usePrepMutations(jobId: MaybeRefOrGetter<number>) {
  const id = () => toValue(jobId)
  const onSuccess = { onSuccess: () => invalidate(`prep:${id()}`) }

  return {
    saveNotes: useMutation<string, unknown>(
      notesMd => apiPut(jobPath(ENDPOINTS.jobPrepNotes, id()), { notes_md: notesMd }),
      onSuccess,
    ),
    addInterview: useMutation<{ round_label: string, scheduled_for?: string }, unknown>(
      body => apiPost(jobPath(ENDPOINTS.jobInterviews, id()), body),
      onSuccess,
    ),
  }
}

/**
 * Generate a prep pack. The result is written straight into the prep cache
 * rather than invalidated: a draft that fails the mandate gate comes back with
 * `mandate_ok: false` and is NOT saved server-side, so refetching would replace
 * it with the old prep and lose the warning.
 */
export function useGeneratePrep(jobId: MaybeRefOrGetter<number>) {
  return useMutation<void, PrepData>(
    () => apiPost<PrepData>(jobPath(ENDPOINTS.jobPrepGenerate, toValue(jobId))),
  )
}

export function useFollowupActions() {
  const onSuccess = { onSuccess: () => invalidate('overview') }
  return {
    snooze: useMutation<{ jobId: number, days?: number }, unknown>(
      ({ jobId, days }) => apiPost(jobPath(ENDPOINTS.jobFollowupSnooze, jobId), { days: days ?? 7 }),
      onSuccess,
    ),
    dismiss: useMutation<number, unknown>(
      jobId => apiPost(jobPath(ENDPOINTS.jobFollowupDismiss, jobId)),
      onSuccess,
    ),
    // A draft is text handed to the user, not state: nothing to invalidate.
    draft: useMutation<number, FollowupDraft>(
      jobId => apiPost<FollowupDraft>(jobPath(ENDPOINTS.jobDraftFollowup, jobId)),
    ),
  }
}

/** Manual discovery sweep. Refreshes run status, the board and the overview. */
export function useTriggerDiscovery() {
  return useMutation<void, unknown>(() => apiPost(ENDPOINTS.runsDiscover), {
    onSuccess: () => invalidate('runStatus', 'jobs', 'overview'),
  })
}

/** Manual full (agentic) run. Only the run status changes synchronously. */
export function useTriggerFull() {
  return useMutation<void, unknown>(() => apiPost(ENDPOINTS.runsFull), {
    onSuccess: () => invalidate('runStatus'),
  })
}
