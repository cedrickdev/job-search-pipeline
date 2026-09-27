// The billing data layer, tested at the wire where a bug would be invisible on screen.
//
// Three things here are load-bearing and asserted from the request rather than the rendered page:
//
//   * The three reads key under the prefixes the page mounts (`billing:plans`,
//     `billing:subscription`, `billing:usage`), so a stale panel would be a caught regression.
//   * A checkout sends only the plan slug in the body — the success and cancel URLs are the
//     server's, and a body that named one would 422 (§18). It resolves to the provider's
//     `redirect_url`, which is the whole output the page acts on.
//   * A portal open on a fresh account surfaces `billing_customer_missing` as an `ApiError` with
//     that code, not a thrown `NuxtError` — the page needs the code to say what went wrong.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import {
  useOpenCheckout,
  useOpenPortal,
  usePlansQuery,
  useSubscriptionQuery,
  useUsageQuery,
} from '~/composables/useBilling'
import { keysMatching } from '~/composables/useApiQuery'
import { stubFetch } from '../support/http'
import { planList, subscriptionOverview, usageSnapshot } from '../support/v2-fixtures'

/**
 * Run a composable once, inside a component's `setup`, and return its handle.
 *
 * The component stays mounted for the rest of the test so the query stays in the `useApiQuery`
 * registry (which is what `keysMatching` reads). The composable runs in `setup`, not the render
 * function, so its fetch fires once rather than on every re-render.
 */
async function run<T>(composable: () => T): Promise<T> {
  let handle!: T
  await mountSuspended(defineComponent({
    setup() {
      handle = composable()
      return () => h('div')
    },
  }))
  return handle
}

describe('usePlansQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('reads the catalogue and keys it under billing:plans', async () => {
    const http = stubFetch([{ match: '/api/v2/billing/plans', json: planList() }])
    const q = await run(() => usePlansQuery())
    await flushPromises()

    expect(http.callsTo('/api/v2/billing/plans')).toHaveLength(1)
    expect(q.data.value?.plans.map(p => p.slug)).toEqual(['free', 'pro', 'scale'])
    expect(keysMatching('billing:plans')).toContain('billing:plans')
  })
})

describe('useSubscriptionQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('reads the overview and keys it under billing:subscription', async () => {
    const http = stubFetch([{
      match: '/api/v2/billing/subscription',
      json: subscriptionOverview(),
    }])
    const q = await run(() => useSubscriptionQuery())
    await flushPromises()

    expect(http.callsTo('/api/v2/billing/subscription')).toHaveLength(1)
    expect(q.data.value?.plan.slug).toBe('free')
    expect(q.data.value?.is_paid).toBe(false)
    expect(keysMatching('billing:subscription')).toContain('billing:subscription')
  })
})

describe('useUsageQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('reads the snapshot and keys it under billing:usage', async () => {
    const http = stubFetch([{ match: '/api/v2/billing/usage', json: usageSnapshot() }])
    const q = await run(() => useUsageQuery())
    await flushPromises()

    expect(http.callsTo('/api/v2/billing/usage')).toHaveLength(1)
    expect(q.data.value?.lines).toHaveLength(2)
    expect(keysMatching('billing:usage')).toContain('billing:usage')
  })
})

describe('useOpenCheckout', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('POSTs only the plan slug and resolves to the redirect URL', async () => {
    const http = stubFetch([{
      match: '/api/v2/billing/checkout',
      method: 'POST',
      status: 201,
      json: { redirect_url: 'https://billing.example/checkout/cs_fake' },
    }])
    const checkout = await run(() => useOpenCheckout())

    const res = await checkout.mutateAsync('pro')

    expect(res.redirect_url).toBe('https://billing.example/checkout/cs_fake')
    // Only the slug travels — no success/cancel URL, which the server builds itself (§18).
    expect(http.bodyOf('/api/v2/billing/checkout')).toEqual({ plan_slug: 'pro' })
  })

  it('surfaces plan_not_purchasable as an ApiError with that code', async () => {
    stubFetch([{
      match: '/api/v2/billing/checkout',
      method: 'POST',
      status: 409,
      json: { error: 'plan_not_purchasable', detail: 'the free tier is not purchasable' },
    }])
    const checkout = await run(() => useOpenCheckout())

    const error = await checkout.mutateAsync('free').catch((e: unknown) => e)
    expect((error as { code: string }).code).toBe('plan_not_purchasable')
  })
})

describe('useOpenPortal', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('POSTs with no body and resolves to the portal redirect URL', async () => {
    const http = stubFetch([{
      match: '/api/v2/billing/portal',
      method: 'POST',
      status: 201,
      json: { redirect_url: 'https://billing.example/portal/ps_fake' },
    }])
    const portal = await run(() => useOpenPortal())

    const res = await portal.mutateAsync()

    expect(res.redirect_url).toBe('https://billing.example/portal/ps_fake')
    expect(http.bodyOf('/api/v2/billing/portal')).toBeUndefined()
  })

  it('surfaces billing_customer_missing as an ApiError with that code', async () => {
    stubFetch([{
      match: '/api/v2/billing/portal',
      method: 'POST',
      status: 409,
      json: { error: 'billing_customer_missing', detail: 'no provider customer' },
    }])
    const portal = await run(() => useOpenPortal())

    const error = await portal.mutateAsync().catch((e: unknown) => e)
    expect((error as { code: string }).code).toBe('billing_customer_missing')
  })
})
