// Onboarding: the wizard that is not a wizard.
//
// The step on screen is derived from `GET /api/v2/onboarding` on every load, so the
// cases worth testing are the ones where a write changes what the server says next.
// That is why these run against the fake in support/account-api.ts rather than a
// table of fixed responses — "saving the profile advances the step" is only a real
// assertion if the step comes back from a server that noticed the save.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mockNuxtImport, mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import OnboardingPage from '~/pages/onboarding.vue'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import { accountApi } from '../support/account-api'
import { account, profile, profileDraft, search, searchDraft } from '../support/v2-fixtures'
import type { AccountApi } from '../support/account-api'
import type { FetchStub } from '../support/http'
import type { VueWrapper } from '@vue/test-utils'

const { navigateTo } = vi.hoisted(() => ({ navigateTo: vi.fn() }))
mockNuxtImport('navigateTo', () => navigateTo)

let api: AccountApi
let http: FetchStub

/** Mount the page against the fake's current state. */
async function mountOnboarding() {
  http = stubFetch(api.routes)
  const wrapper = await mountSuspended(OnboardingPage, { route: '/onboarding' })
  await flushPromises()
  return wrapper
}

/** The step list, as a reader sees it: which one is current, which are done. */
function steps(wrapper: VueWrapper): string[] {
  return wrapper.findAll('.onb-steps li').map(li => li.attributes('class') ?? '')
}

function submit(wrapper: VueWrapper) {
  return wrapper.get('form').trigger('submit')
}

describe('onboarding page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    navigateTo.mockClear()
    clearNuxtData()
    useSessionStore().$reset()
    api = accountApi()
  })

  it('opens on the profile step for a new account', async () => {
    const wrapper = await mountOnboarding()

    expect(steps(wrapper)).toEqual(['onb-step current', 'onb-step', 'onb-step'])
    expect(wrapper.get('h2').text()).toBe('Who you are')
    expect(wrapper.find('#profile-display-name').exists()).toBe(true)
    expect(wrapper.get('button[type="submit"]').text()).toBe('Save and continue')
  })

  it('advances to the search step once the profile is saved', async () => {
    const wrapper = await mountOnboarding()

    await wrapper.get('#profile-display-name').setValue('Test Candidate')
    await submit(wrapper)
    await flushPromises()

    expect(http.callsTo('/api/v2/me/profile')
      .filter(call => call.method === 'PUT')).toHaveLength(1)
    expect(api.profile?.profile.display_name).toBe('Test Candidate')
    expect(steps(wrapper)).toEqual(['onb-step done', 'onb-step current', 'onb-step'])
    expect(wrapper.get('h2').text()).toBe('What you are looking for')
  })

  it('opens on the search step when a profile already exists', async () => {
    api.profile = profile()
    const wrapper = await mountOnboarding()

    expect(wrapper.get('h2').text()).toBe('What you are looking for')
    expect(wrapper.find('#search-name').exists()).toBe(true)
  })

  it('creates the first search and reaches the finish step', async () => {
    api.profile = profile()
    const wrapper = await mountOnboarding()

    await wrapper.get('#search-name').setValue('Backend roles')
    await wrapper.get('[aria-label="Area 1 country"]').setValue('CH')
    await submit(wrapper)
    await flushPromises()

    expect(http.callsTo('/api/v2/me/search-profiles')
      .filter(call => call.method === 'POST')).toHaveLength(1)
    expect(api.searches).toHaveLength(1)
    expect(steps(wrapper)).toEqual(['onb-step done', 'onb-step done', 'onb-step current'])
    expect(wrapper.get('h2').text()).toBe('Ready')
  })

  it('finishes, stamps the account and leaves for the command center', async () => {
    api.profile = profile()
    api.searches = [search()]
    const wrapper = await mountOnboarding()

    await wrapper.get('button.btn-primary').trigger('click')
    await flushPromises()

    expect(http.callsTo('/api/v2/onboarding/complete')).toHaveLength(1)
    expect(api.completedAt).not.toBeNull()
    // The store holds the stamped account, or the guard would send them back here on
    // the next navigation.
    expect(useSessionStore().needsOnboarding).toBe(false)
    expect(navigateTo).toHaveBeenCalledWith('/')
  })
})

/**
 * A saved-but-paused search: the one state that needs the counts rather than a
 * boolean. The writing is done and onboarding still cannot finish, and the fix is a
 * checkbox on the search that exists — not a second search.
 */
describe('onboarding page · a paused search', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    navigateTo.mockClear()
    clearNuxtData()
    useSessionStore().$reset()
    api = accountApi()
    api.profile = profile()
    api.searches = [search({ search: searchDraft({ is_active: false }) })]
  })

  it('stays on the search step and says why', async () => {
    const wrapper = await mountOnboarding()

    expect(steps(wrapper)).toEqual(['onb-step done', 'onb-step current', 'onb-step'])
    expect(wrapper.get('.onb-panel .auth-hint').text())
      .toContain('paused')
  })

  it('loads that search into the form instead of an empty one', async () => {
    const wrapper = await mountOnboarding()

    expect(wrapper.get<HTMLInputElement>('#search-name').element.value).toBe('Backend roles')
    expect(wrapper.get<HTMLInputElement>('.acct-form > .acct-check input').element.checked)
      .toBe(false)
  })

  it('resumes it with a PUT rather than creating a second search', async () => {
    const wrapper = await mountOnboarding()

    await wrapper.get('.acct-form > .acct-check input').setValue(true)
    await submit(wrapper)
    await flushPromises()

    const calls = http.callsTo('/api/v2/me/search-profiles')
    expect(calls.filter(call => call.method === 'POST')).toHaveLength(0)
    expect(calls.filter(call => call.method === 'PUT')).toHaveLength(1)
    expect(api.searches).toHaveLength(1)
    expect(api.searches[0]?.search.is_active).toBe(true)
    expect(wrapper.get('h2').text()).toBe('Ready')
  })
})

describe('onboarding page · refusals and the way out', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    navigateTo.mockClear()
    clearNuxtData()
    useSessionStore().$reset()
    api = accountApi()
  })

  // The counts this page rendered can be out of date — another tab, or a search
  // paused elsewhere. The server re-reads them, and 409 is what it answers.
  it('shows the server sentence when finishing is refused, and stays put', async () => {
    api.profile = profile()
    api.searches = [search()]
    const wrapper = await mountOnboarding()

    // The state the page is holding says it may finish; the server disagrees by the
    // time the button is pressed.
    api.searches = [search({ search: searchDraft({ is_active: false }) })]

    await wrapper.get('button.btn-primary').trigger('click')
    await flushPromises()

    expect(wrapper.get('.auth-error').text())
      .toBe('Save a profile and at least one active search first.')
    expect(navigateTo).not.toHaveBeenCalled()
    expect(api.completedAt).toBeNull()
  })

  it('keeps a refused profile save on the same step, with its message', async () => {
    api.routes = [
      {
        match: '/api/v2/me/profile',
        method: 'PUT',
        status: 503,
        json: { error: 'database_unavailable', detail: 'no database' },
      },
      ...api.routes,
    ]
    const wrapper = await mountOnboarding()

    await wrapper.get('#profile-display-name').setValue('Test Candidate')
    await submit(wrapper)
    await flushPromises()

    expect(wrapper.get('.auth-error').text())
      .toBe('The service is temporarily unavailable. Try again in a moment.')
    expect(steps(wrapper)).toEqual(['onb-step current', 'onb-step', 'onb-step'])
  })

  it('names the account it is setting up, and can sign out of it', async () => {
    const wrapper = await mountOnboarding()
    await useSessionStore().ensure()
    await flushPromises()

    expect(wrapper.get('.auth-alt').text()).toContain('Test Candidate')

    await wrapper.findAll('button').find(b => b.text() === 'Sign out')!.trigger('click')
    await flushPromises()

    expect(http.callsTo('/api/v2/auth/logout')).toHaveLength(1)
    expect(useSessionStore().isAuthenticated).toBe(false)
    expect(navigateTo).toHaveBeenCalledWith('/login')
  })

  // Nothing on this page is a place to type a password, and none of the three reads
  // it makes answers with one. Cheap to assert, and it is the requirement.
  it('renders nothing that looks like a credential', async () => {
    api.profile = profile({ profile: profileDraft({ display_name: 'Test Candidate' }) })
    api.searches = [search()]
    api.completedAt = null
    const wrapper = await mountOnboarding()

    expect(wrapper.html()).not.toMatch(/password|token|csrf|session_id/i)
    expect(wrapper.find('input[type="password"]').exists()).toBe(false)
    expect(useSessionStore().$state).not.toHaveProperty('token')
    expect(account()).not.toHaveProperty('password_hash')
  })
})
