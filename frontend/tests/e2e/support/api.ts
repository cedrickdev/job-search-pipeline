// Browser-side API stub for the E2E suite.
//
// Same contract as tests/nuxt/support/http.ts, one layer out: a route table, first
// match wins, and an unstubbed URL answered with 501 and recorded — so "the app
// asked for something nobody mocked" fails the test instead of quietly rendering
// an error state. `page.route` intercepts in the browser, before the request
// reaches Nitro's dev proxy, which is what keeps the suite free of any live
// backend, job board or LLM.
import type { Page } from '@playwright/test'

export interface MockRoute {
  /** Matched against path + query: substring, or regex for anything finer. */
  match: string | RegExp
  method?: string
  status?: number
  json?: unknown
  /**
   * Serve these frames as `text/event-stream`.
   *
   * Playwright fulfils a route with one complete body, so the frames arrive
   * together rather than paced out. That still exercises the real path the unit
   * tests fake — `fetch` → `ReadableStream` → `TextDecoder` → `parseSseBuffer` in
   * a real browser — but it does not prove incremental rendering; the token
   * preview is asserted in tests/nuxt/components/CopilotPanel.spec.ts.
   */
  sse?: string[]
}

export interface RecordedCall {
  url: string
  method: string
  body: string | null
}

export interface ApiMock {
  calls: RecordedCall[]
  /** Requests that matched no route — assert this stays empty. */
  unmatched: RecordedCall[]
  callsTo: (fragment: string) => RecordedCall[]
}

/**
 * No session, which is the shell's normal case in these tests.
 *
 * `app/layouts/default.vue` calls `session.ensure()` on mount, so every screen
 * inside the shell asks for the session once. The V1-ported screens are not
 * guarded — an anonymous visitor is a supported state there and gets a "Sign in"
 * link — so 401 is the answer that leaves those tests testing what they were
 * written to test. Without it the request would land in `unmatched`.
 */
export const ANONYMOUS_SESSION: MockRoute = {
  match: '/api/v2/auth/session',
  method: 'GET',
  status: 401,
  json: { error: 'not_authenticated', detail: 'no session cookie' },
}

/**
 * Reads made by the authenticated landing route before a test navigates away.
 *
 * The V2 root deliberately resolves to `/map`; it therefore hydrates the search
 * picker and the open catalogue.  These are shell bootstrap reads, not hidden
 * live-network fallbacks.  Keeping their deterministic empty answers here lets
 * feature specs focus on the page they are exercising while map specs still put
 * their richer routes first and assert the exact request themselves.
 */
const SHELL_BOOTSTRAP_ROUTES: MockRoute[] = [
  {
    match: '/api/v2/country-packs',
    method: 'GET',
    json: {
      packs: [{
        country: 'CH',
        display_name: 'Switzerland',
        default_locale: 'fr-CH',
        locales: ['fr-CH', 'de-CH', 'it-CH', 'en-CH'],
        currency: 'CHF',
        languages: ['fr', 'de', 'it', 'en'],
        timezone: 'Europe/Zurich',
        map_viewport: {
          center: { latitude: 46.8182, longitude: 8.2275 },
          zoom: 6.2,
          bounds: { north: 47.8085, south: 45.8179, east: 10.4921, west: 5.9559 },
        },
        opportunity_types: ['FULL_TIME', 'INTERNSHIP'],
        enabled_source_count: 11,
      }],
    },
  },
  { match: '/api/v2/me/search-profiles?active_only=true', method: 'GET', json: { search_profiles: [] } },
  { match: '/api/v2/geo/opportunities', method: 'GET', json: { opportunities: [], limit: 50, offset: 0 } },
]

/** Install the route table on `page`. Call before `page.goto`. */
export async function mockApi(page: Page, routes: MockRoute[]): Promise<ApiMock> {
  const calls: RecordedCall[] = []
  const unmatched: RecordedCall[] = []

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const parsed = new URL(request.url())
    const url = parsed.pathname + parsed.search
    const method = request.method()
    const call: RecordedCall = { url, method, body: request.postData() }
    calls.push(call)

    const hit = [...routes, ...SHELL_BOOTSTRAP_ROUTES].find((r) => {
      if (r.method && r.method.toUpperCase() !== method) return false
      return typeof r.match === 'string' ? url.includes(r.match) : r.match.test(url)
    })

    if (!hit) {
      unmatched.push(call)
      await route.fulfill({
        status: 501,
        contentType: 'application/json',
        body: JSON.stringify({ detail: `no route stubbed for ${method} ${url}` }),
      })
      return
    }

    if (hit.sse) {
      await route.fulfill({
        status: hit.status ?? 200,
        contentType: 'text/event-stream',
        body: hit.sse.join(''),
      })
      return
    }

    await route.fulfill({
      status: hit.status ?? 200,
      contentType: 'application/json',
      body: JSON.stringify(hit.json ?? null),
    })
  })

  return {
    calls,
    unmatched,
    callsTo: fragment => calls.filter(c => c.url.includes(fragment)),
  }
}
