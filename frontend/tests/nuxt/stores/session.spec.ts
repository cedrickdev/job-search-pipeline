// The session store: hydration, the three-state status, and cache isolation.
//
// Written against the real `fetch` stub rather than a mocked store, because the
// behaviours worth pinning here are all about what happens *around* the request —
// how many are sent, which failures are ordinary, and what is dropped from the
// query cache when the account changes.
//
// The cross-user isolation cases are the ones with a requirement behind them
// (docs/ENGINEERING_STANDARDS.md §Security). The API scopes every read to the
// session's user, and these assert the client cannot undo that by serving the
// previous account's cached payload to the next one.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import { account, signedIn } from '../support/v2-fixtures'
import type { Route } from '../support/http'

const SESSION = '/api/v2/auth/session'
const LOGIN = '/api/v2/auth/login'
const REGISTER = '/api/v2/auth/register'
const LOGOUT = '/api/v2/auth/logout'

/** A 401 body in the shape the API sends it. */
const UNAUTHENTICATED: Route = {
  match: SESSION,
  status: 401,
  json: { error: 'not_authenticated', detail: 'no session' },
}

function store() {
  return useSessionStore()
}

describe('session store', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    store().$reset()
  })

  it('hydrates from the session endpoint', async () => {
    stubFetch([{ match: SESSION, json: signedIn() }])
    const session = store()

    await expect(session.ensure()).resolves.toBe(true)

    expect(session.isAuthenticated).toBe(true)
    expect(session.account?.email).toBe('candidate@example.invalid')
    expect(session.session?.expires_at).toBe('2026-01-09T09:00:00Z')
    expect(session.label).toBe('Test Candidate')
  })

  it('falls back to the address when the account has no display name', async () => {
    stubFetch([{ match: SESSION, json: signedIn({ account: account({ display_name: null }) }) }])
    await store().ensure()
    expect(store().label).toBe('candidate@example.invalid')
  })

  // The middleware and the shell both call `ensure()` on a cold load. Without the
  // shared in-flight promise that is two identical requests per navigation.
  it('sends one request for concurrent callers', async () => {
    const http = stubFetch([{ match: SESSION, json: signedIn() }])
    const session = store()

    const [a, b, c] = await Promise.all([session.ensure(), session.ensure(), session.ensure()])

    expect([a, b, c]).toEqual([true, true, true])
    expect(http.callsTo(SESSION)).toHaveLength(1)
  })

  it('answers from memory once the status is known', async () => {
    const http = stubFetch([{ match: SESSION, json: signedIn() }])
    const session = store()

    await session.ensure()
    await session.ensure()

    expect(http.callsTo(SESSION)).toHaveLength(1)
  })

  it('asks again when told to refresh', async () => {
    const http = stubFetch([{ match: SESSION, json: signedIn() }])
    const session = store()

    await session.ensure()
    await session.refresh()

    expect(http.callsTo(SESSION)).toHaveLength(2)
  })

  // 401 is the expected answer for a visitor with no cookie, not a failure to
  // report: it means anonymous, and nothing is recorded in `error`.
  it('treats a 401 as anonymous rather than an error', async () => {
    stubFetch([UNAUTHENTICATED])
    const session = store()

    await expect(session.ensure()).resolves.toBe(false)

    expect(session.status).toBe('anonymous')
    expect(session.error).toBeNull()
    expect(session.account).toBeNull()
  })

  /**
   * A 503 must not be read as "signed out".
   *
   * `status` stays `unknown`, so the next navigation asks again instead of showing a
   * login form on the strength of an unreachable database.
   */
  it('keeps the status unknown when the session cannot be read', async () => {
    const http = stubFetch([{
      match: SESSION,
      status: 503,
      json: { error: 'database_unavailable', detail: 'no database' },
    }])
    const session = store()

    await expect(session.ensure()).resolves.toBe(false)

    expect(session.status).toBe('unknown')
    expect(session.error).not.toBeNull()

    await session.ensure()
    expect(http.callsTo(SESSION)).toHaveLength(2)
  })

  it('signs in and keeps the account it was given', async () => {
    const http = stubFetch([{ match: LOGIN, json: signedIn() }])
    const session = store()

    const signedInAccount = await session.signIn({
      email: 'candidate@example.invalid',
      password: 'a-long-enough-passphrase',
    })

    expect(signedInAccount.id).toBe(account().id)
    expect(session.isAuthenticated).toBe(true)
    expect(http.callsTo(LOGIN)[0]?.method).toBe('POST')
    // Nothing about the request body is stored: the store holds no credential.
    expect(JSON.stringify(session.$state)).not.toContain('passphrase')
  })

  it('lets a refused sign-in reject and stays anonymous about it', async () => {
    stubFetch([{
      match: LOGIN,
      status: 401,
      json: { error: 'invalid_credentials', detail: 'no' },
    }])
    const session = store()

    await expect(session.signIn({ email: 'a@b.invalid', password: 'x' }))
      .rejects.toMatchObject({ code: 'invalid_credentials' })

    expect(session.isAuthenticated).toBe(false)
    expect(session.account).toBeNull()
  })

  it('signs up the same way it signs in', async () => {
    const http = stubFetch([{
      match: REGISTER,
      status: 201,
      json: signedIn({ account: account({ onboarding_completed_at: null }) }),
    }])
    const session = store()

    await session.signUp({ email: 'new@example.invalid', password: 'a-long-enough-one' })

    expect(http.callsTo(REGISTER)[0]?.method).toBe('POST')
    expect(session.isAuthenticated).toBe(true)
    // A brand-new account has no profile and no search, so the guard sends it to
    // /onboarding.
    expect(session.needsOnboarding).toBe(true)
  })

  it('reports a finished setup as not needing onboarding', async () => {
    stubFetch([{ match: SESSION, json: signedIn() }])
    await store().ensure()
    expect(store().needsOnboarding).toBe(false)
  })

  it('is not "needs onboarding" while nobody is signed in', () => {
    expect(store().needsOnboarding).toBe(false)
  })

  it('adopts the account a completed onboarding returns', () => {
    const session = store()
    session.setAccount(account({ onboarding_completed_at: '2026-01-03T10:00:00Z' }))

    expect(session.isAuthenticated).toBe(true)
    expect(session.needsOnboarding).toBe(false)
  })
})

/**
 * Signing out revokes the session server-side, and cannot be prevented by the
 * server failing to answer.
 */
describe('session store · signing out', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    store().$reset()
  })

  async function signedInStore() {
    stubFetch([{ match: SESSION, json: signedIn() }])
    const session = store()
    await session.ensure()
    return session
  }

  it('revokes the session on the server and forgets it here', async () => {
    const session = await signedInStore()
    const http = stubFetch([{ match: LOGOUT, status: 204, text: '' }])

    await session.signOut()

    expect(http.callsTo(LOGOUT)[0]?.method).toBe('POST')
    expect(session.status).toBe('anonymous')
    expect(session.account).toBeNull()
    expect(session.session).toBeNull()
  })

  // The revocation request is what makes a logout real, but if it does not answer
  // the useful thing is still to end the session in this browser — not to leave the
  // user signed in beside an error.
  it('ends the session locally even when the server does not answer', async () => {
    const session = await signedInStore()
    const http = stubFetch([{
      match: LOGOUT,
      status: 503,
      json: { error: 'database_unavailable', detail: 'no database' },
    }])

    await expect(session.signOut()).resolves.toBeUndefined()

    expect(http.called(LOGOUT)).toBe(true)
    expect(session.status).toBe('anonymous')
    expect(session.account).toBeNull()
  })

  it('is not an error to sign out of an already expired session', async () => {
    const session = await signedInStore()
    stubFetch([{ match: LOGOUT, status: 401, json: { error: 'not_authenticated' } }])

    await expect(session.signOut()).resolves.toBeUndefined()
    expect(session.status).toBe('anonymous')
  })
})

/**
 * Cross-user isolation in the client cache (docs/ENGINEERING_STANDARDS.md §Security).
 *
 * The API scopes every `/me` read to the session's user, so the only way this app
 * can show one account another's data is by keeping a payload past the session that
 * fetched it. `clearCached('me', 'onboarding')` is what prevents that, and these
 * assert it in both directions — signing in must not inherit, signing out must not
 * leave anything behind.
 *
 * The cache is written to directly rather than through a mounted page: `useNuxtData`
 * reads and writes the same `payload.data` entries `useApiQuery` caches into, and
 * mounting four components to populate them would test those components instead.
 */
describe('session store · cross-user cache isolation', () => {
  /** Every account-scoped key the app actually caches (composables/useAccount.ts). */
  const ACCOUNT_KEYS = ['me:profile', 'me:searches:all', 'me:searches:active', 'onboarding']

  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    store().$reset()
  })

  function primeCache() {
    for (const key of ACCOUNT_KEYS) {
      useNuxtData(key).data.value = { belongs_to: 'the previous account' }
    }
    // Not account-scoped: the V1 pages have no accounts in this phase, and dropping
    // their cache on every session change would be a needless refetch.
    useNuxtData('jobs').data.value = { items: [] }
  }

  function cached(key: string): unknown {
    return useNuxtData(key).data.value
  }

  it('drops the previous account\'s cached payloads when someone signs in', async () => {
    stubFetch([{ match: LOGIN, json: signedIn({ account: account({ id: '9999', email: 'other@example.invalid' }) }) }])
    primeCache()

    await store().signIn({ email: 'other@example.invalid', password: 'a-long-enough-one' })

    for (const key of ACCOUNT_KEYS) expect(cached(key), key).toBeUndefined()
    expect(cached('jobs')).toEqual({ items: [] })
  })

  it('drops them on registration too', async () => {
    stubFetch([{ match: REGISTER, status: 201, json: signedIn() }])
    primeCache()

    await store().signUp({ email: 'new@example.invalid', password: 'a-long-enough-one' })

    for (const key of ACCOUNT_KEYS) expect(cached(key), key).toBeUndefined()
  })

  it('leaves nothing cached for whoever sits down after a sign-out', async () => {
    stubFetch([{ match: LOGOUT, status: 204, text: '' }])
    store().setAccount(account())
    primeCache()

    await store().signOut()

    for (const key of ACCOUNT_KEYS) expect(cached(key), key).toBeUndefined()
    expect(cached('jobs')).toEqual({ items: [] })
  })

  // The same purge has to happen on the path where the *server* ended the session:
  // a 401 from `GET /auth/session` means the cookie is no longer good, and anything
  // fetched with it is now someone else's or nobody's.
  it('drops them when the session turns out to be gone', async () => {
    stubFetch([UNAUTHENTICATED])
    primeCache()

    await store().ensure()

    for (const key of ACCOUNT_KEYS) expect(cached(key), key).toBeUndefined()
  })

  // A 503 is not a session change: the cookie may still be perfectly good, and
  // throwing the cache away would log the user out of their own data over a blip.
  it('keeps them when the session simply could not be read', async () => {
    stubFetch([{ match: SESSION, status: 503, json: { error: 'database_unavailable' } }])
    primeCache()

    await store().ensure()

    for (const key of ACCOUNT_KEYS) expect(cached(key), key).not.toBeUndefined()
  })
})
