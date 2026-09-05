// The sign-in screen. Ported in spirit from nothing — V1 had no accounts — so every
// case here is new, and the two that carry a security argument are the redirect
// handling and the wording of a refusal.
//
// `navigateTo` is mocked because the assertion is *where the page decides to go*;
// there is no second page to arrive at in a unit test. The route is set through
// `mountSuspended`'s `route` option, which is how `?redirect=` gets into `useRoute`.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mockNuxtImport, mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import LoginPage from '~/pages/login.vue'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import { account, signedIn } from '../support/v2-fixtures'
import type { Route } from '../support/http'
import type { VueWrapper } from '@vue/test-utils'

const SESSION = '/api/v2/auth/session'
const LOGIN = '/api/v2/auth/login'

const { navigateTo } = vi.hoisted(() => ({ navigateTo: vi.fn() }))
mockNuxtImport('navigateTo', () => navigateTo)

/** No session by default: this page is for visitors who do not have one. */
const ANONYMOUS: Route = { match: SESSION, status: 401, json: { error: 'not_authenticated' } }

function routes(...extra: Route[]): Route[] {
  return [...extra, ANONYMOUS]
}

async function mountLogin(route = '/login') {
  const wrapper = await mountSuspended(LoginPage, { route })
  await flushPromises()
  return wrapper
}

/** Fill the form and submit it, as a user would. */
async function signIn(wrapper: VueWrapper, email = 'candidate@example.invalid', password = 'a-long-enough-passphrase') {
  await wrapper.get('#email').setValue(email)
  await wrapper.get('#password').setValue(password)
  await wrapper.get('form').trigger('submit')
  await flushPromises()
}

describe('login page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    navigateTo.mockClear()
    clearNuxtData()
    useSessionStore().$reset()
  })

  it('renders the form', async () => {
    stubFetch(routes())
    const wrapper = await mountLogin()

    expect(wrapper.get('h1').text()).toBe('Sign in')
    expect(wrapper.get('#email').attributes('autocomplete')).toBe('email')
    expect(wrapper.get('#password').attributes('autocomplete')).toBe('current-password')
    expect(wrapper.get('button[type="submit"]').text()).toBe('Sign in')
    expect(wrapper.find('.auth-error').exists()).toBe(false)
  })

  it('signs in and lands on the overview', async () => {
    const http = stubFetch(routes({ match: LOGIN, json: signedIn() }))
    const wrapper = await mountLogin()

    await signIn(wrapper)

    expect(http.bodyOf(LOGIN)).toEqual({
      email: 'candidate@example.invalid',
      password: 'a-long-enough-passphrase',
    })
    expect(useSessionStore().isAuthenticated).toBe(true)
    expect(navigateTo).toHaveBeenCalledWith('/')
  })

  it('returns the user to the page the guard sent them from', async () => {
    stubFetch(routes({ match: LOGIN, json: signedIn() }))
    const wrapper = await mountLogin('/login?redirect=/profile')

    await signIn(wrapper)

    expect(navigateTo).toHaveBeenCalledWith('/profile')
  })

  /**
   * An open redirect, refused.
   *
   * `/login?redirect=https://evil.example` in a mail would bounce a freshly signed-in
   * user off site with `/login` in the address bar the whole way. Only a path of this
   * app is honoured, and `//host` is a path in name only — it is protocol-relative,
   * and a browser reads it as another origin.
   */
  it.each([
    ['https://evil.example', 'an absolute URL'],
    ['//evil.example', 'a protocol-relative URL'],
    ['', 'an empty value'],
  ])('ignores %s as a redirect (%s)', async (redirect) => {
    stubFetch(routes({ match: LOGIN, json: signedIn() }))
    const wrapper = await mountLogin(`/login?redirect=${encodeURIComponent(redirect)}`)

    await signIn(wrapper)

    expect(navigateTo).toHaveBeenCalledWith('/')
  })

  // The guard would bounce it straight back, and arriving at a half-empty screen
  // first is worse than not arriving at all.
  it('sends an unfinished account to onboarding, whatever the redirect asked for', async () => {
    stubFetch(routes({
      match: LOGIN,
      json: signedIn({ account: account({ onboarding_completed_at: null }) }),
    }))
    const wrapper = await mountLogin('/login?redirect=/profile')

    await signIn(wrapper)

    expect(navigateTo).toHaveBeenCalledWith('/onboarding')
  })

  // A bookmark or the back button; `ensure()` normally answers from the store.
  it('sends an already signed-in visitor away on arrival', async () => {
    stubFetch([{ match: SESSION, json: signedIn() }])

    await mountLogin('/login?redirect=/jobs')

    expect(navigateTo).toHaveBeenCalledWith('/jobs')
  })
})

describe('login page · refusals', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    navigateTo.mockClear()
    clearNuxtData()
    useSessionStore().$reset()
  })

  /** The API's answer for an unknown address and for a wrong password alike. */
  const REFUSED: Route = {
    match: LOGIN,
    status: 401,
    json: { error: 'invalid_credentials', detail: 'no account matches' },
  }

  it('says one sentence, and not which half was wrong', async () => {
    stubFetch(routes(REFUSED))
    const wrapper = await mountLogin()

    await signIn(wrapper, 'nobody@example.invalid', 'wrong-but-long-enough')

    const alert = wrapper.get('.auth-error')
    expect(alert.attributes('role')).toBe('alert')
    expect(alert.text()).toBe('That email address and password do not match an account.')
    // docs/AUTHENTICATION.md §Enumeration: the API is careful not to say whether the
    // address exists, and this screen is the place that could give it away.
    expect(alert.text()).not.toMatch(/no account matches|unknown|exists|registered/i)
    expect(navigateTo).not.toHaveBeenCalled()
  })

  // The value in the box is what was just rejected; leaving it invites the same
  // submission again, and a shared screen keeps it visible in the DOM.
  it('clears the password box but keeps the address', async () => {
    stubFetch(routes(REFUSED))
    const wrapper = await mountLogin()

    await signIn(wrapper, 'candidate@example.invalid', 'wrong-but-long-enough')

    expect(wrapper.get<HTMLInputElement>('#password').element.value).toBe('')
    expect(wrapper.get<HTMLInputElement>('#email').element.value)
      .toBe('candidate@example.invalid')
  })

  it('shows the lockout deadline in local time', async () => {
    const until = '2026-01-02T10:00:00Z'
    stubFetch(routes({
      match: LOGIN,
      status: 423,
      json: { error: 'account_locked', detail: 'locked', locked_until: until },
    }))
    const wrapper = await mountLogin()

    await signIn(wrapper)

    const expected = new Date(until)
      .toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    expect(wrapper.get('.auth-error').text()).toContain(expected)
  })

  it('describes an unreachable server as one', async () => {
    stubFetch(routes({ match: LOGIN, respond: () => Promise.reject(new TypeError('Failed to fetch')) }))
    const wrapper = await mountLogin()

    await signIn(wrapper)

    expect(wrapper.get('.auth-error').text())
      .toBe('The server could not be reached. Check your connection and try again.')
  })

  it('locks the form while the request is in flight', async () => {
    let answer: (response: Response) => void = () => {}
    stubFetch(routes({
      match: LOGIN,
      respond: () => new Promise<Response>((resolve) => { answer = resolve }),
    }))
    const wrapper = await mountLogin()

    await wrapper.get('#email').setValue('candidate@example.invalid')
    await wrapper.get('#password').setValue('a-long-enough-passphrase')
    await wrapper.get('form').trigger('submit')

    expect(wrapper.get('button[type="submit"]').text()).toBe('Signing in…')
    expect(wrapper.get('#email').attributes('disabled')).toBeDefined()
    expect(wrapper.get('#password').attributes('disabled')).toBeDefined()

    answer(new Response(JSON.stringify(signedIn()), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }))
    await flushPromises()

    expect(navigateTo).toHaveBeenCalledWith('/')
  })

  // Nothing typed here is kept anywhere but the inputs: the reply carries no token,
  // and the store has no field for one (stores/session.ts).
  it('leaves no credential in the session store', async () => {
    stubFetch(routes({ match: LOGIN, json: signedIn() }))
    const wrapper = await mountLogin()

    await signIn(wrapper, 'candidate@example.invalid', 'a-long-enough-passphrase')

    expect(JSON.stringify(useSessionStore().$state)).not.toContain('passphrase')
  })
})
