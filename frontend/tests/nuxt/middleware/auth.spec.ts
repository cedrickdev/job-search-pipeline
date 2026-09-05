// The named route guard: which screen an unauthenticated or half-set-up visitor
// gets instead of the one they asked for.
//
// The middleware is called directly rather than through a mounted page, because a
// route middleware *is* a function of the route — `defineNuxtRouteMiddleware`
// returns it unchanged — and mounting a page to observe a redirect would put the
// component's own `onMounted` between the assertion and the thing being asserted.
//
// `navigateTo` is mocked for the same reason: its return value is what the guard
// hands back to the router, and in a test there is no router to hand it to. The
// assertion is on the call, which is the middleware's whole output.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mockNuxtImport } from '@nuxt/test-utils/runtime'
import auth from '~/middleware/auth'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import { account, signedIn } from '../support/v2-fixtures'
import type { RouteLocationNormalized } from 'vue-router'

const SESSION = '/api/v2/auth/session'

const { navigateTo } = vi.hoisted(() => ({ navigateTo: vi.fn() }))
mockNuxtImport('navigateTo', () => navigateTo)

/** Only the two fields the guard reads; the router builds the rest. */
function route(fullPath: string): RouteLocationNormalized {
  const path = fullPath.split('?')[0]!
  return { path, fullPath } as RouteLocationNormalized
}

/** The guard takes (to, from); `from` is unused, so the same route serves. */
function guard(fullPath: string) {
  const to = route(fullPath)
  return auth(to, to)
}

describe('auth middleware', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    navigateTo.mockClear()
    clearNuxtData()
    useSessionStore().$reset()
  })

  it('sends a visitor with no session to the login form', async () => {
    stubFetch([{ match: SESSION, status: 401, json: { error: 'not_authenticated' } }])

    await guard('/profile')

    expect(navigateTo).toHaveBeenCalledWith({
      path: '/login',
      query: { redirect: '/profile' },
    })
  })

  // `fullPath`, so a query string survives the round trip through the login form.
  it('carries the whole path, query string included', async () => {
    stubFetch([{ match: SESSION, status: 401, json: { error: 'not_authenticated' } }])

    await guard('/profile?tab=searches')

    const target = navigateTo.mock.calls[0]?.[0] as { query: { redirect: string } }
    expect(target.query.redirect).toBe('/profile?tab=searches')
    // A guard that forwarded an absolute URL would be an open redirect. `fullPath` is
    // always a path of this app, and login.vue refuses anything else anyway.
    expect(target.query.redirect.startsWith('/')).toBe(true)
    expect(target.query.redirect.startsWith('//')).toBe(false)
  })

  it('lets a signed-in, finished account through', async () => {
    stubFetch([{ match: SESSION, json: signedIn() }])

    await expect(guard('/profile')).resolves.toBeUndefined()
    expect(navigateTo).not.toHaveBeenCalled()
  })

  // Every screen behind the guard reads `/me`, and an account that has never
  // completed onboarding has no profile and no saved search to read.
  it('sends an unfinished account to onboarding', async () => {
    stubFetch([{
      match: SESSION,
      json: signedIn({ account: account({ onboarding_completed_at: null }) }),
    }])

    await guard('/profile')

    expect(navigateTo).toHaveBeenCalledWith('/onboarding')
  })

  it('exempts /onboarding itself, or the redirect would loop', async () => {
    stubFetch([{
      match: SESSION,
      json: signedIn({ account: account({ onboarding_completed_at: null }) }),
    }])

    await expect(guard('/onboarding')).resolves.toBeUndefined()
    expect(navigateTo).not.toHaveBeenCalled()
  })

  it('resolves the session once for several navigations', async () => {
    const http = stubFetch([{ match: SESSION, json: signedIn() }])

    await guard('/profile')
    await guard('/onboarding')
    await guard('/profile')

    expect(http.callsTo(SESSION)).toHaveLength(1)
  })

  /**
   * A session that cannot be read is not a session, so the guard still refuses the
   * page — but it must not *record* the visitor as signed out. `status` stays
   * `unknown`, so the login form's own `ensure()` asks again and sends them straight
   * back if the database was merely briefly unreachable.
   */
  it('refuses the page when the session cannot be read, without concluding anything', async () => {
    const http = stubFetch([{
      match: SESSION,
      status: 503,
      json: { error: 'database_unavailable' },
    }])

    await guard('/profile')

    expect(navigateTo).toHaveBeenCalledWith({
      path: '/login',
      query: { redirect: '/profile' },
    })
    expect(useSessionStore().status).toBe('unknown')

    await guard('/profile')
    expect(http.callsTo(SESSION)).toHaveLength(2)
  })
})
