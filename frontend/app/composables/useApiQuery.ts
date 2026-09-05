// The data layer. `useAsyncData` plus the two behaviours V1 got from TanStack
// Query and Nuxt does not give for free.
//
// 1. Prefix invalidation. TanStack matches `invalidateQueries(["jobs"])` against
//    every key that *starts with* ["jobs"], so one call refreshes the board, the
//    table and every filtered variant. `refreshNuxtData` only takes exact keys.
//    The registry below tracks which keys are currently mounted so a prefix can
//    be expanded to the exact keys that exist right now.
//
// 2. Conditional polling. V1's `refetchInterval` was a function of the current
//    data — 2s while a run is active, 10s while a regen or apply is in flight,
//    off otherwise. `useAsyncData` has no equivalent, so the interval is driven
//    by a VueUse `useIntervalFn` that re-arms whenever the predicate's answer
//    changes.
//
// Keys are strings rather than arrays because that is what `useAsyncData` takes;
// the V1 array key ["analytics", 30] becomes "analytics:30". The `:` separator is
// what makes prefix matching work, so it must not appear inside a segment.
import { computed, toValue, watch } from 'vue'
import type { MaybeRefOrGetter } from 'vue'
import { useIntervalFn } from '@vueuse/core'

/** Every key currently mounted somewhere in the app, for prefix expansion. */
const mounted = new Map<string, number>()

/**
 * When each key was last fetched, for the staleness window below.
 *
 * Module scope, not per-call-site: TanStack tracks `dataUpdatedAt` on the shared
 * cache entry, so age survives a component unmounting. Holding it inside
 * `useApiQuery` would reset it on every remount, and since a missing timestamp
 * means "not stale", a remounted query would then serve the cached payload
 * forever — the opposite of a 15s window.
 */
const fetchedAt = new Map<string, number>()

const STALE_MS = 15_000

function retain(key: string) {
  mounted.set(key, (mounted.get(key) ?? 0) + 1)
}

function release(key: string) {
  const n = (mounted.get(key) ?? 0) - 1
  if (n <= 0) mounted.delete(key)
  else mounted.set(key, n)
}

/**
 * Expand a key prefix to the mounted keys it covers, mirroring TanStack's
 * partial key matching. "jobs" matches "jobs" and "jobs:{...}" but not "jobsX".
 */
export function keysMatching(prefix: string): string[] {
  const out: string[] = []
  for (const key of mounted.keys()) {
    if (key === prefix || key.startsWith(`${prefix}:`)) out.push(key)
  }
  return out
}

/**
 * Refetch every mounted query under these prefixes. The Nuxt equivalent of
 * `queryClient.invalidateQueries`. Unmounted keys are a no-op, exactly as an
 * unmounted TanStack query is not refetched either.
 */
export async function invalidate(...prefixes: string[]): Promise<void> {
  const keys = [...new Set(prefixes.flatMap(keysMatching))]
  if (keys.length) await refreshNuxtData(keys)
}

/**
 * Drop every cached payload under these prefixes without refetching.
 *
 * The counterpart to `invalidate`, and Phase 4 is why it exists: when a session
 * ends, the previous account's profile and saved searches must not sit in the cache
 * waiting for the next one. `invalidate` is the wrong tool for that — it *refetches*
 * the keys, which after a logout means a burst of 401s, and it only reaches keys
 * that are still mounted. A logout navigates away first, so by the time this runs
 * the components are gone and the registry is empty; matching on the cache's own
 * keys instead is what makes the payloads actually disappear.
 */
export function clearCached(...prefixes: string[]): void {
  const matches = (key: string) =>
    prefixes.some(prefix => key === prefix || key.startsWith(`${prefix}:`))
  for (const key of [...fetchedAt.keys()]) {
    if (matches(key)) fetchedAt.delete(key)
  }
  clearNuxtData(matches)
}

export interface ApiQueryOptions<T> {
  /** Poll interval in ms as a function of the latest data; false to stop. */
  pollInterval?: (data: T | null) => number | false
  /** Skip fetching entirely while this is false (V1's `enabled`). */
  enabled?: MaybeRefOrGetter<boolean>
}

/**
 * A cached, invalidatable GET.
 *
 * `getCachedData` reproduces V1's `staleTime: 15_000`: a remount inside the
 * window reuses the payload instead of refetching. V1 also set
 * `refetchOnWindowFocus: false`, which is already Nuxt's default.
 *
 * The `cause` check is what keeps that window from swallowing invalidation.
 * `refreshNuxtData` does not clear the payload — it re-runs `execute`, and Nuxt
 * 4 consults `getCachedData` on every run (`experimental.granularCachedData`
 * defaults to true). A window-only implementation would therefore answer a
 * post-write `invalidate()` from the very payload the write just invalidated.
 * Nuxt's own default refuses the cache for exactly these two causes.
 */
export function useApiQuery<T>(
  key: MaybeRefOrGetter<string>,
  fetcher: () => Promise<T>,
  options: ApiQueryOptions<T> = {},
) {
  const state = useAsyncData<T>(key, async () => {
    const resolved = toValue(key)
    const data = await fetcher()
    fetchedAt.set(resolved, Date.now())
    return data
  }, {
    immediate: options.enabled === undefined ? true : toValue(options.enabled),
    getCachedData(cacheKey, nuxtApp, context) {
      // A refresh is a request for the server's answer, not for this one.
      if (context.cause === 'refresh:manual' || context.cause === 'refresh:hook') {
        return undefined
      }
      // Presence, not truthiness: `null` is a real payload here — it is how
      // useAccount.ts spells "this account has no profile yet" — and `??` would
      // read it as a miss and refetch on every remount.
      const cached = cacheKey in nuxtApp.payload.data
        ? nuxtApp.payload.data[cacheKey]
        : nuxtApp.static.data[cacheKey]
      if (cached === undefined) return undefined
      const at = fetchedAt.get(cacheKey)
      if (at !== undefined && Date.now() - at > STALE_MS) return undefined
      return cached as T
    },
  })

  // Track the resolved key so `invalidate()` can find it. A reactive key (jobs
  // filters, analytics window) moves the registration with it.
  watch(
    () => toValue(key),
    (next, previous) => {
      if (previous !== undefined) release(previous)
      retain(next)
    },
    { immediate: true },
  )
  onScopeDispose(() => release(toValue(key)))

  // V1's `enabled: jobId !== null` — fetch as soon as the guard opens.
  if (options.enabled !== undefined) {
    watch(
      () => toValue(options.enabled!),
      (on) => {
        if (on) void state.refresh()
      },
    )
  }

  if (options.pollInterval) {
    // `useAsyncData` types its data as `PickFrom<T, KeysOf<T>>`, which is exactly
    // `T` once `T` is concrete but does not reduce while it is still generic —
    // hence the cast, which the concrete call sites in useQueries.ts do check.
    const delay = computed(() =>
      options.pollInterval!((state.data.value ?? null) as T | null))
    const timer = useIntervalFn(
      () => void state.refresh(),
      computed(() => (delay.value === false ? 0 : delay.value)),
      { immediate: false },
    )
    watch(
      delay,
      (ms) => {
        if (ms === false) timer.pause()
        else timer.resume()
      },
      { immediate: true },
    )
  }

  return state
}
