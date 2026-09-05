// Your account: the screen that owns everything about it after onboarding.
//
// Three of these cases carry a requirement rather than a preference.
//
// **Pausing must not empty the search.** `PUT /me/search-profiles/{id}` replaces
// the search, and this is the one place that writes one without showing the form,
// so the assertion is on the whole request body — every area and keyword list back
// as it was, with `is_active` alone flipped. A request carrying `{is_active}` on
// its own would silently reset where the discovery runs look.
//
// **Deleting is not recoverable.** There is no undo endpoint, so the `DELETE` must
// not leave on a click alone; the gate is asserted from both sides — cancelling
// sends nothing, confirming sends one.
//
// **The screen shows account facts, never credentials.** It renders the session's
// expiry, which is the closest thing here to session state, and the token behind it
// must not appear anywhere in the DOM.
//
// These run against the fake in support/account-api.ts, because a save here is
// followed by a refetch and "the row now says paused" is only a real assertion if
// the list comes back from a server that noticed the write.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mockNuxtImport, mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import ProfilePage from '~/pages/profile.vue'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import { accountApi } from '../support/account-api'
import { profile, search, searchDraft } from '../support/v2-fixtures'
import type { AccountApi } from '../support/account-api'
import type { FetchStub, RecordedCall } from '../support/http'
import type { SearchProfileDraft } from '~/types/v2'
import type { VueWrapper } from '@vue/test-utils'

const SEARCHES = '/api/v2/me/search-profiles'

const { navigateTo } = vi.hoisted(() => ({ navigateTo: vi.fn() }))
mockNuxtImport('navigateTo', () => navigateTo)

let api: AccountApi
let http: FetchStub

/**
 * Mount the page as the guard leaves it: the session already loaded, because
 * `middleware: 'auth'` has run before this component renders.
 */
async function mountProfile() {
  http = stubFetch(api.routes)
  await useSessionStore().ensure()
  const wrapper = await mountSuspended(ProfilePage, { route: '/profile' })
  await flushPromises()
  return wrapper
}

function callsWith(method: string, fragment = SEARCHES): RecordedCall[] {
  return http.callsTo(fragment).filter(call => call.method === method)
}

/** The body of the last request of that method, as the server received it. */
function lastBody(method: string, fragment = SEARCHES): SearchProfileDraft {
  const call = callsWith(method, fragment).at(-1)
  expect(call, `no ${method} to ${fragment}`).toBeTruthy()
  return JSON.parse(String(call!.init?.body ?? '{}')) as SearchProfileDraft
}

function button(wrapper: VueWrapper, label: string) {
  return wrapper.get(`[aria-label="${label}"]`)
}

function rows(wrapper: VueWrapper): string[] {
  return wrapper.findAll('.acct-search-name').map(row => row.text())
}

describe('profile page · the account', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    navigateTo.mockClear()
    clearNuxtData()
    useSessionStore().$reset()
    api = accountApi()
    api.profile = profile()
    api.searches = [search()]
  })

  it('states the facts about the account, and none about signing in', async () => {
    const wrapper = await mountProfile()

    const facts = wrapper.findAll('.acct-facts dd').map(dd => dd.text())
    expect(facts[0]).toBe('candidate@example.invalid')
    expect(facts[1]).toBe('Test Candidate')
    expect(facts[2]).toBe(new Date('2026-01-02T09:00:00Z').toLocaleString())
    // The session's own expiry, read from the store rather than from a cookie.
    expect(facts.at(-1)).toBe(new Date('2026-01-09T09:00:00Z').toLocaleString())
    expect(wrapper.get('.settings-card .auth-hint').text())
      .toContain('not available yet')
  })

  // The screen names a session; it must not carry the secret that identifies it.
  it('renders no session token and no password box', async () => {
    const wrapper = await mountProfile()

    expect(wrapper.find('input[type="password"]').exists()).toBe(false)
    expect(wrapper.html()).not.toMatch(/token|csrf|session_id|secret/i)
    expect(useSessionStore().$state).not.toHaveProperty('token')
  })

  it('signs out, then leaves for the login form', async () => {
    const wrapper = await mountProfile()

    await wrapper.findAll('button').find(b => b.text() === 'Sign out')!.trigger('click')
    await flushPromises()

    expect(http.callsTo('/api/v2/auth/logout')).toHaveLength(1)
    expect(useSessionStore().isAuthenticated).toBe(false)
    expect(navigateTo).toHaveBeenCalledWith('/login')
  })

  it('confirms a saved profile', async () => {
    const wrapper = await mountProfile()

    await wrapper.get('#profile-display-name').setValue('Renamed Candidate')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(api.profile?.profile.display_name).toBe('Renamed Candidate')
    expect(wrapper.get('.settings-ok').attributes('role')).toBe('status')
  })
})

describe('profile page · the saved searches', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    navigateTo.mockClear()
    clearNuxtData()
    useSessionStore().$reset()
    api = accountApi()
    api.profile = profile()
    api.searches = [search()]
  })

  it('lists each search with where it looks and whether it runs', async () => {
    const wrapper = await mountProfile()

    expect(rows(wrapper)).toEqual(['Backend roles'])
    expect(wrapper.get('.acct-search-meta').text()).toContain('CH')
    expect(wrapper.get('.acct-live').text()).toBe('active')
    expect(wrapper.find('.ov-empty').exists()).toBe(false)
  })

  it('says so when there are none', async () => {
    api.searches = []
    const wrapper = await mountProfile()

    expect(wrapper.get('.ov-empty').text()).toBe('No saved searches yet.')
  })

  /**
   * The wholesale-`PUT` case, asserted on the wire.
   *
   * Everything the form would have sent has to be in this body: `PUT` replaces the
   * search, so a field left out of a pause is a field reset to its default.
   */
  it('pauses a search by sending it back whole', async () => {
    api.searches = [search({
      search: searchDraft({
        queries: ['backend engineer'],
        excluded_keywords: ['sales'],
        contract_types: ['PERMANENT'],
        posting_languages: ['fr'],
        source_keys: ['jobup'],
        workload: { min_percent: 80, max_percent: 100 },
      }),
    })]
    const wrapper = await mountProfile()

    await button(wrapper, 'Pause Backend roles').trigger('click')
    await flushPromises()

    expect(callsWith('PUT')).toHaveLength(1)
    expect(lastBody('PUT')).toEqual({
      ...api.searches[0]!.search,
      is_active: false,
    })
    expect(lastBody('PUT').queries).toEqual(['backend engineer'])
    expect(lastBody('PUT').workload).toEqual({ min_percent: 80, max_percent: 100 })
    // The row is the refetched list's answer, not an optimistic guess.
    expect(api.searches[0]?.search.is_active).toBe(false)
    expect(wrapper.get('.acct-paused').text()).toBe('paused')
  })

  it('resumes a paused one the same way', async () => {
    api.searches = [search({ search: searchDraft({ is_active: false }) })]
    const wrapper = await mountProfile()

    await button(wrapper, 'Resume Backend roles').trigger('click')
    await flushPromises()

    expect(lastBody('PUT').is_active).toBe(true)
    expect(wrapper.get('.acct-live').text()).toBe('active')
  })

  it('edits a search in a form seeded with it, then closes', async () => {
    const wrapper = await mountProfile()

    await button(wrapper, 'Edit Backend roles').trigger('click')
    expect(button(wrapper, 'Edit Backend roles').attributes('aria-expanded')).toBe('true')
    expect(wrapper.get<HTMLInputElement>('#search-name').element.value)
      .toBe('Backend roles')

    await wrapper.get('#search-name').setValue('Platform roles')
    await wrapper.get('.acct-search form').trigger('submit')
    await flushPromises()

    expect(callsWith('POST')).toHaveLength(0)
    expect(api.searches).toHaveLength(1)
    expect(rows(wrapper)).toEqual(['Platform roles'])
    expect(wrapper.find('#search-name').exists()).toBe(false)
  })

  it('creates a second search from the new-search form', async () => {
    const wrapper = await mountProfile()

    await wrapper.findAll('button').find(b => b.text().includes('Add a search'))!.trigger('click')
    await wrapper.get('#search-name').setValue('Data roles')
    await wrapper.get('[aria-label="Area 1 country"]').setValue('FR')
    await wrapper.get('.acct-new form').trigger('submit')
    await flushPromises()

    expect(callsWith('POST')).toHaveLength(1)
    expect(api.searches).toHaveLength(2)
    expect(rows(wrapper)).toEqual(['Backend roles', 'Data roles'])
    expect(wrapper.find('.acct-new').exists()).toBe(false)
  })

  it('keeps the form open with its message when a save is refused', async () => {
    api.routes = [
      {
        match: '/api/v2/me/search-profiles/',
        method: 'PUT',
        status: 503,
        json: { error: 'database_unavailable', detail: 'no database' },
      },
      ...api.routes,
    ]
    const wrapper = await mountProfile()

    await button(wrapper, 'Edit Backend roles').trigger('click')
    await wrapper.get('#search-name').setValue('Platform roles')
    await wrapper.get('.acct-search form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('.acct-search .auth-error').text())
      .toBe('The service is temporarily unavailable. Try again in a moment.')
    // What was typed is still there to retry with.
    expect(wrapper.get<HTMLInputElement>('#search-name').element.value)
      .toBe('Platform roles')
  })
})

/** Deleting: no undo endpoint exists, so the gate is the feature. */
describe('profile page · deleting a search', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    navigateTo.mockClear()
    clearNuxtData()
    useSessionStore().$reset()
    api = accountApi()
    api.profile = profile()
    api.searches = [search()]
  })

  it('asks before it deletes anything', async () => {
    const wrapper = await mountProfile()

    await button(wrapper, 'Delete Backend roles').trigger('click')

    const bar = wrapper.get('[role="alertdialog"]')
    expect(bar.text()).toContain('Backend roles')
    expect(callsWith('DELETE')).toHaveLength(0)
  })

  it('sends nothing when the question is declined', async () => {
    const wrapper = await mountProfile()

    await button(wrapper, 'Delete Backend roles').trigger('click')
    await wrapper.findAll('[role="alertdialog"] button')
      .find(b => b.text() === 'Cancel')!.trigger('click')
    await flushPromises()

    expect(callsWith('DELETE')).toHaveLength(0)
    expect(wrapper.find('[role="alertdialog"]').exists()).toBe(false)
    expect(rows(wrapper)).toEqual(['Backend roles'])
  })

  it('deletes once when it is confirmed', async () => {
    const wrapper = await mountProfile()

    await button(wrapper, 'Delete Backend roles').trigger('click')
    await wrapper.findAll('[role="alertdialog"] button')
      .find(b => b.text() === 'Confirm')!.trigger('click')
    await flushPromises()

    expect(callsWith('DELETE')).toHaveLength(1)
    expect(api.searches).toHaveLength(0)
    expect(wrapper.get('.ov-empty').text()).toBe('No saved searches yet.')
  })

  // Another tab got there first. The list is refetched either way, so the row goes
  // — the sentence is what explains why.
  it('says why a refused delete changed nothing', async () => {
    api.routes = [
      {
        match: '/api/v2/me/search-profiles/',
        method: 'DELETE',
        status: 404,
        json: { error: 'search_profile_not_found', detail: 'gone' },
      },
      ...api.routes,
    ]
    const wrapper = await mountProfile()

    await button(wrapper, 'Delete Backend roles').trigger('click')
    await wrapper.findAll('[role="alertdialog"] button')
      .find(b => b.text() === 'Confirm')!.trigger('click')
    await flushPromises()

    expect(wrapper.get('.auth-error').attributes('role')).toBe('alert')
    expect(wrapper.get('.auth-error').text()).toBe('That saved search no longer exists.')
    expect(api.searches).toHaveLength(1)
  })
})
