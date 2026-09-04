// Ported from webapp/src/api/client.test.ts.
//
// The first three cases are V1's, unchanged in meaning. The last three cover the
// quirks api-client.ts's header claims it reproduces — an empty body, a non-JSON
// body, and an error body with no `detail` key. V1 relied on those behaviours in
// its components (a 204 from a POST action, FastAPI's validation-error shape)
// without ever asserting them; a rewrite of the HTTP layer is the moment to.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiGet, apiPost } from '~/utils/api-client'
import { stubFetch } from '../support/http'

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
