// Ported from webapp/src/api/client.test.ts.
//
// The first three cases are V1's, unchanged in meaning. The next three cover the
// quirks api-client.ts's header claims it reproduces — an empty body, a non-JSON
// body, and an error body with no `detail` key. V1 relied on those behaviours in
// its components (a 204 from a POST action, FastAPI's validation-error shape)
// without ever asserting them; a rewrite of the HTTP layer is the moment to.
//
// The last group is Phase 4's additions: the CSRF header, `ApiError.code`, and
// `apiDelete`. They are asserted at this level because that is where they are
// implemented — every V2 write in the app inherits them from here, and a test per
// call site would only re-test `fetch`.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, CSRF_HEADER, apiDelete, apiGet, apiPost, apiPut, csrfToken } from '~/utils/api-client'
import { stubFetch } from '../support/http'

/**
 * Put a cookie in the jar happy-dom keeps for this document.
 *
 * `__Host-` names are written with `Secure; Path=/` because the prefix is only
 * valid alongside them — a browser drops the cookie otherwise, and happy-dom
 * enforces the same rule. That constraint is exactly why the prefixed name is the
 * one worth trusting.
 */
function setCookie(name: string, value: string) {
  const required = name.startsWith('__Host-') ? '; Secure; Path=/' : ''
  document.cookie = `${name}=${value}${required}`
}

function clearCookies() {
  for (const entry of document.cookie.split(';')) {
    const name = entry.split('=')[0]?.trim()
    if (!name) continue
    const required = name.startsWith('__Host-') ? '; Secure; Path=/' : ''
    document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT${required}`
  }
}

function headerOf(init: RequestInit | undefined, name: string): string | undefined {
  const headers = init?.headers as Record<string, string> | undefined
  return headers?.[name]
}

describe('api client', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('GET returns parsed json', async () => {
    stubFetch([{ match: '/api/x', json: { ok: true } }])
    expect(await apiGet<{ ok: boolean }>('/api/x')).toEqual({ ok: true })
  })

  it('POST sends json body and parses response', async () => {
    const http = stubFetch([{ match: '/api/y', json: { id: 1 } }])
    const out = await apiPost<{ id: number }>('/api/y', { a: 1 })
    expect(out).toEqual({ id: 1 })
    expect(http.callsTo('/api/y')[0]?.method).toBe('POST')
    expect(http.bodyOf('/api/y')).toEqual({ a: 1 })
  })

  it('throws ApiError with status and detail on non-2xx', async () => {
    stubFetch([{ match: '/api/z', status: 409, json: { detail: 'nope' } }])
    await expect(apiPost('/api/z', {})).rejects.toMatchObject({
      status: 409,
      detail: 'nope',
    })
  })

  it('resolves an empty body to null rather than a parse error', async () => {
    stubFetch([{ match: '/api/empty', status: 200, text: '' }])
    await expect(apiPost('/api/empty', {})).resolves.toBeNull()
  })

  it('resolves a non-JSON body to its raw text', async () => {
    stubFetch([{ match: '/api/plain', text: 'not json' }])
    await expect(apiGet('/api/plain')).resolves.toBe('not json')
  })

  it('falls back to the whole body when an error carries no detail', async () => {
    stubFetch([{ match: '/api/weird', status: 500, json: { oops: true } }])
    const error = await apiGet('/api/weird').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).detail).toEqual({ oops: true })
    // A non-string detail must still produce a readable message.
    expect((error as ApiError).message).toBe('HTTP 500')
  })
})

describe('api client · sessions and CSRF (Phase 4)', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearCookies()
  })
  afterEach(clearCookies)

  it('sends the CSRF cookie back in the header on unsafe methods', async () => {
    setCookie('__Host-jobsearch_csrf', 'tok-123')
    const http = stubFetch([{ match: '/api/v2/', json: {} }])

    await apiPost('/api/v2/auth/logout')
    await apiPut('/api/v2/me/profile', { a: 1 })
    await apiDelete('/api/v2/me/search-profiles/1')

    for (const call of http.callsTo('/api/v2/')) {
      expect(headerOf(call.init, CSRF_HEADER)).toBe('tok-123')
    }
    expect(http.callsTo('/api/v2/').map(c => c.method))
      .toEqual(['POST', 'PUT', 'DELETE'])
  })

  // GET is exempt on the backend too (`SAFE_METHODS`), and sending it anyway would
  // suggest the token is what authenticates the request. It is not — the session
  // cookie is.
  it('sends no CSRF header on a GET', async () => {
    setCookie('__Host-jobsearch_csrf', 'tok-123')
    const http = stubFetch([{ match: '/api/v2/auth/session', json: {} }])

    await apiGet('/api/v2/auth/session')

    expect(headerOf(http.calls[0]?.init, CSRF_HEADER)).toBeUndefined()
  })

  // A sibling subdomain can set `jobsearch_csrf` but not `__Host-jobsearch_csrf`,
  // so when both are present the prefixed one is the only one worth trusting.
  it('prefers the __Host- cookie over a bare one', () => {
    setCookie('jobsearch_csrf', 'planted')
    setCookie('__Host-jobsearch_csrf', 'real')
    expect(csrfToken()).toBe('real')
  })

  it('reads the bare cookie when that is the only one issued', () => {
    setCookie('jobsearch_csrf', 'local-http')
    expect(csrfToken()).toBe('local-http')
  })

  it('omits the header entirely when no CSRF cookie exists', async () => {
    const http = stubFetch([{ match: '/api/v2/auth/login', json: {} }])

    await apiPost('/api/v2/auth/login', { email: 'a@b.invalid', password: 'x' })

    expect(csrfToken()).toBeNull()
    expect(headerOf(http.calls[0]?.init, CSRF_HEADER)).toBeUndefined()
  })

  it('exposes the V2 error slug as `code` and keeps the whole body', async () => {
    stubFetch([{
      match: '/api/v2/auth/login',
      status: 423,
      json: {
        error: 'account_locked',
        detail: 'too many attempts',
        locked_until: '2026-01-02T10:00:00Z',
      },
    }])

    const error = await apiPost('/api/v2/auth/login', {}).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('account_locked')
    expect((error as ApiError).status).toBe(423)
    expect((error as ApiError).detail).toBe('too many attempts')
    expect((error as ApiError).body).toMatchObject({ locked_until: '2026-01-02T10:00:00Z' })
  })

  // V1's 28 paths answer `{detail: …}` and must keep behaving exactly as they did.
  it('leaves `code` null for a V1 error body', async () => {
    stubFetch([{ match: '/api/jobs', status: 404, json: { detail: 'nope' } }])
    const error = await apiGet('/api/jobs/1').catch((e: unknown) => e)
    expect((error as ApiError).code).toBeNull()
    expect((error as ApiError).detail).toBe('nope')
  })

  it('resolves a 204 from DELETE to null', async () => {
    stubFetch([{ match: '/api/v2/me/search-profiles/', status: 204, text: '' }])
    await expect(apiDelete('/api/v2/me/search-profiles/abc')).resolves.toBeNull()
  })

  it('sends the session cookie with every request', async () => {
    const http = stubFetch([{ match: '/api/', json: {} }])
    await apiGet('/api/v2/auth/session')
    await apiPost('/api/v2/auth/logout')
    for (const call of http.calls) {
      expect(call.init?.credentials).toBe('same-origin')
    }
  })
})
