import { computed, onScopeDispose, ref, toValue, watch } from 'vue'
import type { MaybeRefOrGetter } from 'vue'
import { useIntervalFn } from '@vueuse/core'
import type { DiscoveryQueued, DiscoveryRun } from '~/types/v2'
import type { DiscoveryState } from '~/utils/discovery-messages'
import { apiGet, apiPost } from '~/utils/api-client'
import { discoveryRun, discoveryStart } from '~/utils/endpoints'
import { invalidate } from './useApiQuery'

const POLL_INTERVAL_MS = 3000
const MAX_POLLS = 40
const TERMINAL = new Set<DiscoveryState>(['SUCCEEDED', 'PARTIAL', 'FAILED'])

/** One saved search's manual collection; no implicit schedule or browser submission. */
export function useDiscoveryRun(profileId: MaybeRefOrGetter<string | null>) {
  const queued = ref<DiscoveryQueued | null>(null)
  const report = ref<DiscoveryRun | null>(null)
  const error = ref(false)
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
      }
      else timer.resume()
    }
    catch {
      if (currentGeneration !== generation) return
      timer.pause()
      error.value = true
    }
  }

  async function refresh() {
    const id = toValue(profileId)
    if (!id || busy.value) return
    const currentGeneration = ++generation
    timer.pause()
    busy.value = true
    error.value = false
    try {
      const result = await apiPost<DiscoveryQueued>(discoveryStart(id))
      if (currentGeneration !== generation) return
      queued.value = result
      report.value = null
      polls = 0
      await poll()
    }
    catch {
      if (currentGeneration === generation) error.value = true
    }
    finally {
      if (currentGeneration === generation) busy.value = false
    }
  }

  watch(() => toValue(profileId), () => {
    generation++
    timer.pause()
    queued.value = null
    report.value = null
    error.value = false
    busy.value = false
  })
  onScopeDispose(() => timer.pause())
  return { refresh, status, sourceHealth, lastUpdated, report, busy, error }
}
