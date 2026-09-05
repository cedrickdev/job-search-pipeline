// The data layer, tested where it differs from `useAsyncData`.
//
// This file exists because of a bug it now pins. Nuxt 4 resolves
// `experimental.granularCachedData` to `true`, which means `getCachedData` is
// consulted on *every* execute — not only the first one — and `refreshNuxtData`
// does not clear the payload before re-running it. A `getCachedData` that only
// asked "is this payload younger than the staleness window?" therefore answered
// every post-write `invalidate()` from the payload that write had just
// invalidated: the request never left, and the screen kept rendering the state
// from before the save. Nuxt's own default refuses the cache for the two refresh
// causes, and so does ours now.
//
// A component is mounted rather than calling the composable directly because
// `useApiQuery` registers itself for prefix expansion through the mounted
// registry, and that registration is half of what `invalidate` does.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import { clearCached, invalidate, keysMatching, useApiQuery } from '~/composables/useApiQuery'

/** A counter of real fetcher calls, and the query that increments it. */
function counted() {
  const state = { calls: 0 }
  const fetcher = async () => {
    state.calls += 1
    return { calls: state.calls }
  }
  return { state, fetcher }
}

/** A component whose only job is to hold one query open. */
function holder(key: string, fetcher: () => Promise<unknown>) {
  return defineComponent({
    setup() {
      const { data } = useApiQuery(key, fetcher)
      return () => h('output', JSON.stringify(data.value ?? null))
    },
  })
}

async function mountQuery(key: string, fetcher: () => Promise<unknown>) {
  const wrapper = await mountSuspended(holder(key, fetcher))
  await flushPromises()
  return wrapper
}

describe('useApiQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  // The regression. Every write in the app depends on this: `useMutation`'s
  // `onSuccess` calls `invalidate`, and a save that does not refetch leaves the
  // screen showing the state it just changed.
  it('refetches a key that was fetched a moment ago', async () => {
    const { state, fetcher } = counted()
    const wrapper = await mountQuery('probe:fresh', fetcher)

    expect(state.calls).toBe(1)

    await invalidate('probe')
    await flushPromises()

    expect(state.calls).toBe(2)
    expect(wrapper.text()).toBe(JSON.stringify({ calls: 2 }))
  })

  // TanStack's partial matching, which is the whole reason the registry exists:
  // one `invalidate("me")` has to reach `me:profile` and `me:searches:all` alike.
  it('expands a prefix to every mounted key under it', async () => {
    const one = counted()
    const two = counted()
    await mountQuery('acct:one', one.fetcher)
    await mountQuery('acct:two', two.fetcher)

    expect(keysMatching('acct').sort()).toEqual(['acct:one', 'acct:two'])

    await invalidate('acct')
    await flushPromises()

    expect(one.state.calls).toBe(2)
    expect(two.state.calls).toBe(2)
  })

  // "jobs" must not match "jobsX" — prefix, not `startsWith`.
  it('does not treat a longer name as the same prefix', async () => {
    const mine = counted()
    const other = counted()
    await mountQuery('run', mine.fetcher)
    await mountQuery('runner', other.fetcher)

    await invalidate('run')
    await flushPromises()

    expect(mine.state.calls).toBe(2)
    expect(other.state.calls).toBe(1)
  })

  it('is a no-op for a prefix nothing is mounted under', async () => {
    const { state, fetcher } = counted()
    await mountQuery('kept', fetcher)

    await invalidate('nothing-here')
    await flushPromises()

    expect(state.calls).toBe(1)
  })

  /**
   * `clearCached` is the opposite of `invalidate`: the payload goes, and nothing
   * is requested in its place. That is what a sign-out needs — the previous
   * account's data must not survive, and refetching it would only produce 401s.
   */
  it('drops a cached payload without asking for it again', async () => {
    const { state, fetcher } = counted()
    await mountQuery('acct:profile', fetcher)

    clearCached('acct')
    await flushPromises()

    expect(state.calls).toBe(1)
    expect(useNuxtData('acct:profile').data.value).toBeUndefined()
  })

  it('leaves a payload outside the cleared prefixes alone', async () => {
    await mountQuery('acct:searches', counted().fetcher)
    await mountQuery('jobs', counted().fetcher)

    clearCached('acct')

    expect(useNuxtData('acct:searches').data.value).toBeUndefined()
    expect(useNuxtData('jobs').data.value).toEqual({ calls: 1 })
  })

  // `null` is a payload, not a miss: it is how `useProfileQuery` spells "this
  // account has not saved a profile yet". Read with `??`, a cached `null` looked
  // absent and every mount asked the API again.
  it('serves a cached null instead of fetching again', async () => {
    useNuxtData('acct:none').data.value = null
    const { state, fetcher } = counted()

    const wrapper = await mountQuery('acct:none', fetcher)

    expect(state.calls).toBe(0)
    expect(wrapper.text()).toBe('null')
  })
})

/**
 * V1's `staleTime: 15_000`, which the `cause` check above must not have thrown
 * away: leaving a screen and coming straight back is not a reason to re-request,
 * and coming back later is.
 */
describe('useApiQuery · the staleness window', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('reuses the payload when a screen is remounted inside the window', async () => {
    const { state, fetcher } = counted()
    const first = await mountQuery('win:fresh', fetcher)
    first.unmount()

    await mountQuery('win:fresh', fetcher)

    expect(state.calls).toBe(1)
  })

  it('asks again when the payload is older than the window', async () => {
    const { state, fetcher } = counted()
    const first = await mountQuery('win:stale', fetcher)
    first.unmount()

    // The clock, not a timer: staleness is measured against `Date.now()`, and
    // moving real timers would disturb the poll interval and the flushing here.
    vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 60_000)
    await mountQuery('win:stale', fetcher)

    expect(state.calls).toBe(2)
  })
})
