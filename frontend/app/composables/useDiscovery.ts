import { computed, onScopeDispose, ref, toValue, watch } from 'vue'
import type { MaybeRefOrGetter } from 'vue'
import { useIntervalFn } from '@vueuse/core'
import type { DiscoveryQueued, DiscoveryRun } from '~/types/v2'
import type { DiscoveryState } from '~/utils/discovery-messages'
import { ApiError, apiGet, apiPost } from '~/utils/api-client'
import { discoveryLatest, discoveryRun, discoveryStart } from '~/utils/endpoints'
import { invalidate } from './useApiQuery'

const POLL_INTERVAL_MS = 3000
const MAX_POLLS = 40
const TERMINAL = new Set<DiscoveryState>(['SUCCEEDED', 'PARTIAL', 'FAILED'])

/** One saved search's manual collection; no implicit schedule or browser submission. */
export function useDiscoveryRun(profileId: MaybeRefOrGetter<string | null>) {
  const queued = ref<DiscoveryQueued | null>(null)
  const report = ref<DiscoveryRun | null>(null)
  const error = ref(false)
  const errorCode = ref<string | null>(null)
  const busy = ref(false)
  let generation = 0
  let polls = 0

  const status = computed<DiscoveryState>(() =>
    error.value ? 'ERROR' : (report.value?.status ?? queued.value?.status ?? 'IDLE'))
  const sourceHealth = computed(() => report.value?.source_health ?? [])
  const lastUpdated = computed(() => report.value?.finished_at ?? null)

  const timer = useIntervalFn(() => void poll(), POLL_INTERVAL_MS, { immediate: false })

  async function poll() {
    const task = queued.value
    if (!task) return
    const currentGeneration = generation
    if (++polls > MAX_POLLS) {
      timer.pause()
      error.value = true
      return
    }
    try {
      const result = await apiGet<DiscoveryRun>(discoveryRun(task.run_id))
      if (currentGeneration !== generation) return
      report.value = result
      if (TERMINAL.has(result.status)) {
        timer.pause()
        await invalidate('geo:saved')
        busy.value = false
      }
      else timer.resume()
    }
    catch (cause) {
      if (currentGeneration !== generation) return
      timer.pause()
      error.value = true
      errorCode.value = cause instanceof ApiError ? cause.code : null
      busy.value = false
    }
  }

  async function refresh() {
    const id = toValue(profileId)
    if (!id || busy.value) return
    const currentGeneration = ++generation
    timer.pause()
    busy.value = true
    error.value = false
    errorCode.value = null
    try {
      const result = await apiPost<DiscoveryQueued>(discoveryStart(id))
      if (currentGeneration !== generation) return
      queued.value = result
      report.value = null
      polls = 0
      await poll()
    }
    catch (cause) {
      if (currentGeneration === generation) {
        error.value = true
        errorCode.value = cause instanceof ApiError ? cause.code : null
      }
    }
    finally {
      // A queued/running discovery is still in progress after its first status
      // read. Keep the control busy until `poll` observes a terminal result so
      // the UI does not imply the refresh has already finished.
      if (currentGeneration === generation
          && (error.value || TERMINAL.has(status.value))) busy.value = false
    }
  }

  async function hydrate(id: string, currentGeneration: number) {
    try {
      const latest = await apiGet<DiscoveryRun>(discoveryLatest(id))
      if (currentGeneration !== generation) return
      report.value = latest
      if (!TERMINAL.has(latest.status)) {
        queued.value = { run_id: latest.run_id, status: 'QUEUED' }
        polls = 0
        timer.resume()
      }
    }
    catch (cause) {
      if (currentGeneration === generation && !(cause instanceof ApiError && cause.status === 404))
        error.value = true
    }
  }

  watch(() => toValue(profileId), (id) => {
    generation++
    timer.pause()
    queued.value = null
    report.value = null
    error.value = false
    errorCode.value = null
    busy.value = false
    if (id) void hydrate(id, generation)
  }, { immediate: true })
  onScopeDispose(() => timer.pause())
  return { refresh, status, sourceHealth, lastUpdated, report, busy, error, errorCode }
}
