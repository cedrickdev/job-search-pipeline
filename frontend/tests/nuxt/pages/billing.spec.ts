import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import BillingPage from '~/pages/billing.vue'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import { plan, planList, signedIn, subscriptionOverview, usageSnapshot } from '../support/v2-fixtures'

describe('billing page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    useSessionStore().$reset()
    document.cookie = 'app_locale=en; path=/; max-age=0'
  })

  it('translates plan details and usage labels into French', async () => {
    document.cookie = 'app_locale=fr; path=/'
    stubFetch([
      { match: '/api/v2/billing/plans', json: planList() },
      { match: '/api/v2/billing/subscription', json: subscriptionOverview() },
      { match: '/api/v2/billing/usage', json: usageSnapshot() },
      { match: '/api/v2/auth/session', json: signedIn() },
    ])
    await useSessionStore().ensure()
    const wrapper = await mountSuspended(BillingPage, { route: '/billing' })
    await flushPromises()

    expect(wrapper.text()).toContain('Facturation')
    expect(wrapper.text()).toContain('Gratuit')
    expect(wrapper.text()).toContain('Découvrez toutes les fonctionnalités')
    expect(wrapper.text()).toContain('recherches actives')
    expect(wrapper.text()).toContain('candidatures envoyées')
  })

  it('labels local unlimited workflow access without presenting it as a paid plan', async () => {
    stubFetch([
      { match: '/api/v2/billing/plans', json: planList() },
      {
        match: '/api/v2/billing/subscription',
        json: subscriptionOverview({
          plan: plan({
            slug: 'local-development',
            name: 'Local development',
            description: 'Unlimited local workflow testing. Not a paid subscription.',
          }),
        }),
      },
      { match: '/api/v2/billing/usage', json: usageSnapshot() },
      { match: '/api/v2/auth/session', json: signedIn() },
    ])
    await useSessionStore().ensure()
    const wrapper = await mountSuspended(BillingPage, { route: '/billing' })
    await flushPromises()

    expect(wrapper.text()).toContain('Local workflow mode is enabled')
    expect(wrapper.text()).toContain('Local development')
    expect(wrapper.find('[data-test="plans"]').exists()).toBe(false)
  })
})
