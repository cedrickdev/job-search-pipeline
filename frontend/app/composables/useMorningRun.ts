import { onScopeDispose, ref } from 'vue'
import { useIntervalFn } from '@vueuse/core'
import type { MorningRunBatch, MorningRunLatest, MorningRunStatus, MorningSchedule, MorningScheduleRequest } from '~/types/v2'
import { ApiError, apiGet, apiPost, apiPut } from '~/utils/api-client'
import { V2_ENDPOINTS } from '~/utils/endpoints'
import { invalidate } from './useApiQuery'

const ACTIVE = new Set(['QUEUED', 'RUNNING'])
const PREPARING = new Set(['DISCOVERED', 'MATCHED', 'CV_RENDERED'])
const QUEUE_VISIBILITY_GRACE_MS = 15_000

export function isMorningRunActive(run: MorningRunStatus): boolean {
  if (ACTIVE.has(run.status)) return true
  return (run.preparations ?? []).some((item) => {
    if (item.task_status === 'DEAD_LETTERED') return false
    if (PREPARING.has(item.stage)) return true
    if (item.stage !== 'DOCUMENTS_RENDERED') return false
    if (item.application_state === 'SUBMITTED'
      || item.application_state === 'SUBMISSION_STATE_UNKNOWN'
      || item.application_state === 'REQUIRES_HUMAN'
      || item.application_state === 'READY_FOR_REVIEW') return false
    return item.task_status === 'QUEUED' || item.task_status === 'RUNNING'
      || item.application_state === 'PREPARING'
      || item.application_state === 'SUBMITTING'
      || item.submission_task_status === 'QUEUED'
      || item.submission_task_status === 'RUNNING'
  })
}

export function useMorningRun() {
  const schedule = ref<MorningSchedule | null>(null)
  const runs = ref<MorningRunStatus[]>([])
  const busy = ref(false)
  const saving = ref(false)
  const error = ref<string | null>(null)
  const scheduleError = ref<string | null>(null)
  let live = true
  let polling = false
  let queuedAt: number | null = null

  function explain(cause: unknown): string {
    if (cause instanceof ApiError && cause.code === 'discovery_source_unapproved')
      return cause.code
    return cause instanceof ApiError && typeof cause.detail === 'string'
      ? cause.detail : 'network_error'
  }

  const timer = useIntervalFn(() => void refreshRuns(), 3000, { immediate: false })

  async function refreshRuns() {
    if (polling || !live) return
    polling = true
    try {
      const latest = await apiGet<MorningRunLatest>(V2_ENDPOINTS.morningRunsLatest)
      if (!live) return
      let queueConfirmationMissing = false
      if (latest.runs.length === 0 && queuedAt !== null && runs.value.some(run => ACTIVE.has(run.status))) {
        if (Date.now() - queuedAt < QUEUE_VISIBILITY_GRACE_MS) {
          error.value = null
          timer.resume()
          return
        }
        queueConfirmationMissing = true
      }
      queuedAt = null
      runs.value = latest.runs
      error.value = queueConfirmationMissing ? 'queue_confirmation_missing' : null
      if (runs.value.some(isMorningRunActive)) timer.resume()
      else {
        timer.pause()
        busy.value = false
        await invalidate('geo:saved')
      }
    }
    catch (cause) {
      if (live) {
        error.value = explain(cause)
        timer.pause()
        busy.value = false
      }
    }
    finally { polling = false }
  }

  async function load() {
    try {
      const result = await apiGet<MorningSchedule>(V2_ENDPOINTS.morningSchedule)
      if (live) schedule.value = result
    }
    catch (cause) { if (live) scheduleError.value = explain(cause) }
    await refreshRuns()
  }

  async function saveSchedule(payload: MorningScheduleRequest) {
    saving.value = true
    scheduleError.value = null
    try {
      const result = await apiPut<MorningSchedule>(V2_ENDPOINTS.morningSchedule, payload)
      if (live) schedule.value = result
      return result
    }
    catch (cause) { if (live) scheduleError.value = explain(cause); return null }
    finally { saving.value = false }
  }

  async function runNow(): Promise<MorningRunBatch | null> {
    if (busy.value) return null
    busy.value = true
    error.value = null
    try {
      const queued = await apiPost<MorningRunBatch>(V2_ENDPOINTS.morningRuns)
      if (!live) return queued
      runs.value = queued.runs.map(run => ({
        run_id: run.run_id, search_profile_id: run.search_profile_id,
        status: run.status === 'DEAD_LETTERED' ? 'FAILED' : run.status,
        seen_count: 0, matched_count: 0, source_health: [], warning_codes: [],
        preparations: [], preparations_truncated: false,
      }))
      queuedAt = runs.value.some(run => ACTIVE.has(run.status)) ? Date.now() : null
      if (runs.value.length && runs.value.some(run => ACTIVE.has(run.status))) timer.resume()
      else busy.value = false
      return queued
    }
    catch (cause) {
      if (live) error.value = explain(cause)
      busy.value = false
      return null
    }
  }

  void load()
  onScopeDispose(() => { live = false; timer.pause() })
  return { schedule, runs, busy, saving, error, scheduleError, saveSchedule, runNow, refreshRuns }
}
