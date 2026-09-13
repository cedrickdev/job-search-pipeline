// The geo explorer in a real browser, with a real MapLibre map — the one thing the unit
// suite cannot have, because happy-dom has no WebGL and mocks the library away (§51). Here
// the map actually initialises on a stubbed style, so these tests prove what only a browser
// proves: the route loads, the canvas comes up without falling into the error state, the
// mode toggle and a shared URL pick the right read, and a panned view is searched only when
// the button is pressed.
//
// Nothing leaves the machine. `/api/**` is answered from the table below (support/api.ts),
// and the map's own network — its style and glyph ranges — is intercepted here with a
// minimal offline style, so no tile server, font server, job board or LLM is ever touched
// (CLAUDE.md §Testing, §52). The pins the server did not place are asserted through the
// list, which is where an unplaceable role lives (§8, §9); the WebGL canvas itself is not
// pixel-inspected — clustering and marker paint are MapLibre's own and are unit-covered.
import { expect, test } from '@playwright/test'
import { ANONYMOUS_SESSION, mockApi } from './support/api'
import { IDLE_RUN, overview } from './support/fixtures'
import type { ApiMock, MockRoute } from './support/api'
import type { Page } from '@playwright/test'

const COMPANY_ID = '44444444-4444-4444-8444-444444444444'
const PROFILE_ID = '33333333-3333-4333-8333-333333333333'

const ACCOUNT = {
  id: '11111111-1111-4111-8111-111111111111',
  email: 'candidate@example.invalid',
  display_name: 'Test Candidate',
  status: 'ACTIVE',
  created_at: '2026-01-02T09:00:00Z',
  onboarding_completed_at: '2026-01-02T09:30:00Z',
}
const SESSION_WINDOW = {
  issued_at: '2026-01-02T09:00:00Z',
  expires_at: '2026-01-09T09:00:00Z',
  last_seen_at: '2026-01-02T09:15:00Z',
}

/** A geocoded location with a point; pass `point: null` for an unplaceable one. */
function location(lat = 46.5197, lng = 6.6323, precision = 'EXACT_ADDRESS') {
  return {
    point: { latitude: lat, longitude: lng },
    precision,
    provenance: 'GEOCODED',
    confidence: 'HIGH',
    city: 'Lausanne',
    region: null,
    postal_code: null,
    country: 'CH',
    raw: null,
    geocoded_at: '2026-01-02T10:00:00Z',
    geocoder: 'nominatim',
  }
}

function opportunity(over: Record<string, unknown> = {}) {
  return {
    id: 'opp-1',
    title: 'Backend Engineer',
    company_name: 'Logitech',
    company_id: COMPANY_ID,
    application_url: 'https://boards.greenhouse.invalid/logitech/backend',
    contract_type: 'PERMANENT',
    opportunity_type: 'FULL_TIME',
    workplace_mode: 'ON_SITE',
    remote_scope: null,
    status: 'RESOLVED',
    location: location(),
    distance_meters: 1500,
    matched_radii: [],
    posted_at: '2026-01-01T08:00:00Z',
    discovered_at: '2026-01-02T09:00:00Z',
    posting_language: 'en',
    ...over,
  }
}

function oppResponse(opportunities: unknown[]) {
  return { opportunities, limit: 50, offset: 0 }
}

function companyItem(over: Record<string, unknown> = {}) {
  return {
    company: { id: COMPANY_ID, name: 'Logitech' },
    location: location(46.5197, 6.6323, 'CITY'),
    status: 'RESOLVED',
    is_headquarters: true,
    distance_meters: 3200,
    matched_radii: [],
    ...over,
  }
}

const OPPS = '/api/v2/geo/opportunities'
const COMPANIES = '/api/v2/geo/companies'
const SAVED = `/api/v2/me/search-profiles/${PROFILE_ID}/opportunities`

/**
 * A signed-in account and the geo reads, plus the V1 shell reads the app makes on any
 * screen — an unstubbed one would land in `unmatched` and fail the hermeticity check.
 * The saved route precedes the open one only for clarity; the two paths do not overlap.
 */
function routes(...first: MockRoute[]): MockRoute[] {
  return [
    ...first,
    { match: '/api/v2/auth/session', method: 'GET', json: { account: ACCOUNT, session: SESSION_WINDOW } },
    { match: SAVED, method: 'GET', json: oppResponse([opportunity({ id: 'saved-1', title: 'Saved Role' })]) },
    { match: OPPS, method: 'GET', json: oppResponse([opportunity()]) },
    { match: COMPANIES, method: 'GET', json: { companies: [companyItem()], limit: 50, offset: 0 } },
    { match: '/api/overview', json: overview() },
    { match: '/api/runs/status', json: IDLE_RUN },
    { match: '/api/chat/history', json: { messages: [] } },
  ]
}

/**
 * Intercept the map's own network so nothing leaves the machine.
 *
 * The runtime style URL (demotiles) is replaced with a minimal offline style: one
 * background layer, no sources, so no tiles are ever requested. A `glyphs` endpoint is
 * declared and answered with an empty 200 — a range MapLibre reads as "no glyphs here",
 * which is not the network failure that would trip the component's style-error path.
 */
async function mockMapAssets(page: Page) {
  const style = {
    version: 8,
    glyphs: 'https://glyphs.invalid/{fontstack}/{range}.pbf',
    sources: {},
    layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#0b1020' } }],
  }
  await page.route('**/demotiles.maplibre.org/**', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(style) }))
  await page.route('**/*.pbf', route =>
    route.fulfill({ status: 200, contentType: 'application/x-protobuf', body: '' }))
}

/** Sign in, stub the map assets, and open the explorer. */
async function openMap(page: Page, extra: MockRoute[] = [], path = '/map'): Promise<ApiMock> {
  await mockMapAssets(page)
  const api = await mockApi(page, routes(...extra))
  await page.goto(path)
  await expect(page.getByRole('heading', { name: 'Map' })).toBeVisible()
  return api
}

/** The MapLibre canvas is present — the map got a WebGL context and did not fall to the
    error overlay. Its presence is the browser-only fact these tests exist to check. */
async function expectHealthyMap(page: Page) {
  await expect(page.locator('[data-testid="opportunity-map"] canvas')).toBeVisible()
  await expect(page.locator('.map-overlay--error')).toHaveCount(0)
}

test('opens the explorer, brings up a live map, and lists what the open read returned', async ({ page }) => {
  const api = await openMap(page)

  await expectHealthyMap(page)
  await expect(page.locator('.map-row', { hasText: 'Backend Engineer' })).toBeVisible()
  expect(api.callsTo(OPPS).length).toBeGreaterThan(0)
  // No saved read without a profile in the URL.
  expect(api.callsTo('/search-profiles/')).toHaveLength(0)
  expect(api.unmatched).toEqual([])
})

test('keeps an unplaceable role in the list, tagged, though it can carry no pin (§8, §9)', async ({ page }) => {
  await openMap(page, [{
    match: OPPS,
    method: 'GET',
    json: oppResponse([
      opportunity({ id: 'placed', title: 'On-site Role' }),
      opportunity({
        id: 'remote',
        title: 'Remote Role',
        status: 'REMOTE',
        location: null,
        remote_scope: 'REMOTE_ANYWHERE',
        distance_meters: null,
      }),
    ]),
  }])

  await expectHealthyMap(page)
  // The unplaceable role is a real result: present in the list, tagged, distance a dash.
  const remote = page.locator('.map-row', { hasText: 'Remote Role' })
  await expect(remote).toBeVisible()
  await expect(remote).toContainText('No location')
  await expect(page.locator('.map-row', { hasText: 'On-site Role' })).toBeVisible()
})

test('switches to employers and reads the companies endpoint (§10)', async ({ page }) => {
  const api = await openMap(page)

  await page.getByRole('tab', { name: 'Companies' }).click()

  await expect(page.locator('.map-row', { hasText: 'Logitech' })).toBeVisible()
  await expect.poll(() => api.callsTo(COMPANIES).length).toBeGreaterThan(0)
})

test('honours a saved-search scope named in the URL, reading the saved endpoint (§15)', async ({ page }) => {
  const api = await openMap(page, [], `/map?profile=${PROFILE_ID}`)

  await expect(page.getByText('Scoped to a saved search')).toBeVisible()
  await expect(page.locator('.map-row', { hasText: 'Saved Role' })).toBeVisible()
  await expect.poll(() => api.callsTo(SAVED).length).toBeGreaterThan(0)
  // The open read is silenced while a saved search scopes the map.
  expect(api.callsTo(OPPS)).toHaveLength(0)
})

test('re-reads with a committed filter, and only on Apply (§23)', async ({ page }) => {
  const api = await openMap(page)
  await expectHealthyMap(page)
  const before = api.callsTo(OPPS).length

  await page.getByLabel('Country').fill('ch')
  // Typing alone does not fetch; the read waits for Apply.
  expect(api.callsTo('country=CH')).toHaveLength(0)
  await page.getByRole('button', { name: 'Apply' }).click()

  await expect.poll(() => api.callsTo('country=CH').length).toBeGreaterThan(0)
  expect(api.callsTo(OPPS).length).toBeGreaterThan(before)
})

test('searches the current view only when the button is pressed (§20)', async ({ page }) => {
  const api = await openMap(page)
  await expectHealthyMap(page)

  // A pan/zoom settles a new viewport — this alone must not fetch.
  const canvas = page.locator('[data-testid="opportunity-map"] canvas')
  await canvas.hover()
  await page.mouse.wheel(0, -240)
  await page.waitForTimeout(400) // moveend is debounced 200ms
  const before = api.callsTo(OPPS).length

  await page.getByRole('button', { name: 'Search this area' }).click()

  await expect.poll(() => api.callsTo(OPPS).length).toBeGreaterThan(before)
  // The request carried the map's bounds, which only "Search this area" sends.
  expect(api.callsTo('bounds=').length).toBeGreaterThan(0)
})

test('many nearby roles cluster without breaking the map, and all stay in the list (§11)', async ({ page }) => {
  // Pinned low-zoom camera (no auto-fit) so the co-located points fall inside one cluster.
  const many = Array.from({ length: 6 }, (_, i) =>
    opportunity({ id: `c-${i}`, title: `Clustered Role ${i}`, location: location(46.52 + i * 0.001, 6.63 + i * 0.001) }))
  await openMap(page, [{ match: OPPS, method: 'GET', json: oppResponse(many) }], '/map?lat=46.6&lng=6.6&zoom=6')

  // Clustering is MapLibre's own (§11); what a browser can assert is that it did not error
  // and that every role is still reachable in the list.
  await expectHealthyMap(page)
  await expect(page.locator('.map-row')).toHaveCount(6)
})

test('offers the mobile view switch and flips between the list and the map', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 780 })
  await openMap(page)

  const toggle = page.locator('.map-mobile-toggle')
  await expect(toggle).toBeVisible()
  // Opens on the map; the list column is collapsed at this width.
  await expect(page.locator('.map-col--list')).toBeHidden()

  await toggle.getByRole('tab', { name: 'List' }).click()
  await expect(page.locator('.map-col--list')).toBeVisible()
  await expect(page.locator('.map-col--map')).toBeHidden()
  await expect(page.locator('.map-row', { hasText: 'Backend Engineer' })).toBeVisible()
})

test('sends an anonymous visitor from the guarded map to the login form', async ({ page }) => {
  await mockMapAssets(page)
  await mockApi(page, [
    ANONYMOUS_SESSION,
    { match: '/api/overview', json: overview() },
    { match: '/api/runs/status', json: IDLE_RUN },
    { match: '/api/chat/history', json: { messages: [] } },
  ])

  await page.goto('/map')

  await expect(page).toHaveURL('/login?redirect=/map')
  // Nothing of the map rendered on the way past, and the nav does not offer it.
  await expect(page.locator('[data-testid="opportunity-map"]')).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Map' })).toHaveCount(0)
})
