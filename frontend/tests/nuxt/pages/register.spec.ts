// The registration screen. Two things beyond the login page's cases: the password
// rule this form states, and the fact that a new account is signed in the moment it
// exists — 201 carries the same body a login does, so there is no second step.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mockNuxtImport, mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import RegisterPage from '~/pages/register.vue'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import { account, signedIn } from '../support/v2-fixtures'
import type { Route } from '../support/http'
import type { VueWrapper } from '@vue/test-utils'

const SESSION = '/api/v2/auth/session'
const REGISTER = '/api/v2/auth/register'

/** Matches `MINIMUM_PASSWORD_LENGTH` in backend/app/core/passwords.py. */
const LONG_ENOUGH = 'a-passphrase-of-a-few-words'

const { navigateTo } = vi.hoisted(() => ({ navigateTo: vi.fn() }))
mockNuxtImport('navigateTo', () => navigateTo)

const ANONYMOUS: Route = { match: SESSION, status: 401, json: { error: 'not_authenticated' } }

function routes(...extra: Route[]): Route[] {
  return [...extra, ANONYMOUS]
}

/** A fresh account, as `POST /auth/register` answers: 201, and nothing set up yet. */
function created(): Route {
  return {
    match: REGISTER,
    status: 201,
    json: signedIn({ account: account({ onboarding_completed_at: null }) }),
  }
}

async function mountRegister() {
  const wrapper = await mountSuspended(RegisterPage, { route: '/register' })
  await flushPromises()
  return wrapper
}

async function register(wrapper: VueWrapper, fields: {
  name?: string
  email?: string
  password?: string
} = {}) {
  if (fields.name !== undefined) await wrapper.get('#display-name').setValue(fields.name)
  await wrapper.get('#email').setValue(fields.email ?? 'new@example.invalid')
  await wrapper.get('#password').setValue(fields.password ?? LONG_ENOUGH)
  await wrapper.get('form').trigger('submit')
  await flushPromises()
}

describe('register page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    navigateTo.mockClear()
    clearNuxtData()
    useSessionStore().$reset()
  })

  it('states the password rule the backend actually enforces', async () => {
    stubFetch(routes())
    const wrapper = await mountRegister()

    const password = wrapper.get('#password')
    expect(password.attributes('minlength')).toBe('12')
    expect(password.attributes('autocomplete')).toBe('new-password')
    expect(password.attributes('aria-describedby')).toBe('password-hint')
    expect(wrapper.get('#password-hint').text()).toContain('At least 12 characters')
    // Length only — no composition rule, and no second box to retype it into.
    expect(wrapper.text()).not.toMatch(/uppercase|symbol|confirm/i)
  })

  it('names the account and signs it in, then goes to onboarding', async () => {
    const http = stubFetch(routes(created()))
    const wrapper = await mountRegister()

    await register(wrapper, { name: 'Test Candidate' })

    expect(http.bodyOf(REGISTER)).toEqual({
      email: 'new@example.invalid',
      password: LONG_ENOUGH,
      display_name: 'Test Candidate',
    })
    expect(useSessionStore().isAuthenticated).toBe(true)
    // There is nothing else a brand-new account can usefully do.
    expect(navigateTo).toHaveBeenCalledWith('/onboarding')
  })

  // The field is optional server-side, and `null` is how "not given" is spelled
  // there; `""` would fail its `min_length=1`.
  it('sends no name rather than an empty one', async () => {
    const http = stubFetch(routes(created()))
    const wrapper = await mountRegister()

    await register(wrapper)

    expect(http.bodyOf(REGISTER)).toMatchObject({ display_name: null })
  })

  it('will not submit a password shorter than the rule', async () => {
    stubFetch(routes(created()))
    const wrapper = await mountRegister()

    await wrapper.get('#email').setValue('new@example.invalid')
    await wrapper.get('#password').setValue('too-short')

    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.auth-field-error').text()).toBe('12 characters minimum.')
  })

  it('says nothing about length before anything is typed', async () => {
    stubFetch(routes())
    const wrapper = await mountRegister()
    expect(wrapper.find('.auth-field-error').exists()).toBe(false)
  })

  it('sends an already signed-in visitor to their overview', async () => {
    stubFetch([{ match: SESSION, json: signedIn() }])

    await mountRegister()

    expect(navigateTo).toHaveBeenCalledWith('/')
  })

  it('sends an already signed-in, unfinished account to onboarding', async () => {
    stubFetch([{
      match: SESSION,
      json: signedIn({ account: account({ onboarding_completed_at: null }) }),
    }])

    await mountRegister()

    expect(navigateTo).toHaveBeenCalledWith('/onboarding')
  })
})

describe('register page · refusals', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    navigateTo.mockClear()
    clearNuxtData()
    useSessionStore().$reset()
  })

  it('says an account already exists for that address', async () => {
    stubFetch(routes({
      match: REGISTER,
      status: 409,
      json: { error: 'email_already_registered', detail: 'taken' },
    }))
    const wrapper = await mountRegister()

    await register(wrapper)

    expect(wrapper.get('.auth-error').text())
      .toBe('An account already exists for that email address.')
    expect(navigateTo).not.toHaveBeenCalled()
  })

  it('puts a rejected field beside the field it names', async () => {
    stubFetch(routes({
      match: REGISTER,
      status: 422,
      json: {
        error: 'validation_failed',
        detail: 'invalid',
        errors: [{ loc: ['body', 'email'], msg: 'not a valid email address' }],
      },
    }))
    const wrapper = await mountRegister()

    await register(wrapper, { email: 'not-an-address' })

    const messages = wrapper.findAll('.auth-field-error').map(p => p.text())
    expect(messages).toContain('not a valid email address')
  })

  /**
   * A rejected password is never echoed back.
   *
   * The 422 body is stripped of `input` before it leaves the API, and `fieldErrors`
   * builds its list from `msg` alone — so even a body that carried one could not put
   * it on screen. Asserted here because this is the page where a password is typed.
   */
  it('never shows the password that was refused', async () => {
    stubFetch(routes({
      match: REGISTER,
      status: 422,
      json: {
        error: 'validation_failed',
        detail: 'invalid',
        errors: [{ loc: ['body', 'password'], msg: 'too weak', input: LONG_ENOUGH }],
      },
    }))
    const wrapper = await mountRegister()

    await register(wrapper)

    expect(wrapper.get('.auth-field-error').text()).toBe('too weak')
    expect(wrapper.html()).not.toContain(LONG_ENOUGH)
    expect(wrapper.get<HTMLInputElement>('#password').element.value).toBe('')
  })
})
