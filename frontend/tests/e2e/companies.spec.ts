// The company directory in a browser: the guard, the filter and the provenance.
//
// The unit suite mounts these two pages against a stubbed `fetch`, so what it proves
// is how they render an answer. Three things only a browser can show:
//
//   * that the directory is reachable at all — the nav link appears for a session,
//     and the guard sends an anonymous visitor to /login instead;
//   * that a filtered navigation survives a real round trip, including the one filter
//     the phase exists for (`has_opportunities=false`, employers with nothing posted);
//   * that a discovery pass — an unsafe request against shared data — leaves with the
//     `X-CSRF-Token` header the API demands.
//
// No backend runs. Every `/api/**` request is answered from the table below, so
// nothing here reaches a live ATS (CLAUDE.md §Testing), and the discovery "pass" is a
// stubbed report rather than a crawl.
import { expect, test } from '@playwright/test'
import { ANONYMOUS_SESSION, mockApi } from './support/api'
import { IDLE_RUN, overview } from './support/fixtures'
import type { MockRoute } from './support/api'
import type { Page } from '@playwright/test'

const CSRF_COOKIE = 'jobsearch_csrf'
const CSRF_VALUE = 'a-csrf-token-from-the-session'
const COMPANY_ID = '44444444-4444-4444-8444-444444444444'

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

/** An employer nobody is currently hiring for — the case Phase 6 exists to hold. */
const QUIET_COMPANY = {
  id: COMPANY_ID,
  name: 'Quiet Employer SA',
  normalized_name: 'quiet employer',
  website: 'https://quiet.invalid',
  careers_url: 'https://boards.greenhouse.invalid/quiet',
  country: 'CH',
  identity_status: 'VERIFIED',
  detected_ats: {
    platform: 'GREENHOUSE',
    organization_id: 'quiet',
    status: 'CONFIRMED',
    detected_by: 'configured_ats',
    evidence: [{
      code: 'ATS_HOST_MATCH',
      detail: 'the careers URL is on the platform host',
      source_url: 'https://boards.greenhouse.invalid/quiet',
      observed_at: '2026-01-02T10:00:00Z',
    }],
  },
  spontaneous_application: null,
  accepts_spontaneous_applications: null,
  locations: [{
    city: 'Lausanne',
    region: null,
    postal_code: null,
    country: 'CH',
    raw: null,
    is_headquarters: true,
  }],
}

const COMPANY_DETAIL = {
  company: QUIET_COMPANY,
  aliases: [{
    alias: 'Quiet Employer Europe S.A.',
    normalized_alias: 'quiet employer europe',
    source_key: 'stored_opportunities',
    first_seen_at: '2026-01-02T10:00:00Z',
    last_seen_at: '2026-01-03T10:00:00Z',
  }],
  career_sites: [{
    url: 'https://boards.greenhouse.invalid/quiet',
    kind: 'ATS_BOARD',
    platform: 'GREENHOUSE',
    source_key: 'configured_ats',
    verification_status: 'CONFIRMED',
    discovered_at: '2026-01-02T10:00:00Z',
    last_checked_at: null,
  }],
  discoveries: [{
    provider_key: 'configured_ats',
    external_id: 'greenhouse:quiet',
    seed_kind: 'ATS_ORGANIZATION',
    company_name: 'Quiet Employer',
    source_url: 'https://boards.greenhouse.invalid/quiet',
    discovered_at: '2026-01-02T10:00:00Z',
    confidence: 'CONFIRMED',
  }],
  discovered_by: ['configured_ats'],
}

const DISCOVERY_RUN = {
  country: null,
  started_at: '2026-01-02T10:00:00Z',
  duration_ms: 42,
  providers: ['configured_ats', 'stored_opportunities'],
  unusable_providers: [],
  is_complete: true,
  created: 2,
  matched: 5,
  ambiguous: 1,
  company_ids: [COMPANY_ID],
  health: [],
  warnings: [],
  links: { examined: 8, linked: 6, ambiguous: 1, unresolved: 1 },
}

/**
 * A signed-in account, the company surface, and the V1 screens the shell wraps.
 *
 * The detail route precedes the list route because `mockApi` answers from the first
 * match and `/companies/{id}` contains `/companies`. The V1 reads are here because the
 * nav walk starts on the overview, and an unstubbed request would land in `unmatched`.
 */
function routes(...first: MockRoute[]): MockRoute[] {
  return [
    ...first,
    { match: '/api/v2/auth/session', method: 'GET', json: { account: ACCOUNT, session: SESSION_WINDOW } },
    { match: '/api/v2/company-discovery/run', method: 'POST', json: DISCOVERY_RUN },
    { match: `/api/v2/companies/${COMPANY_ID}`, method: 'GET', json: COMPANY_DETAIL },
    { match: '/api/v2/companies', method: 'GET', json: { companies: [QUIET_COMPANY], total: 1, limit: 20, offset: 0 } },
    { match: '/api/overview', json: overview() },
    { match: '/api/runs/status', json: IDLE_RUN },
    { match: '/api/chat/history', json: { messages: [] } },
  ]
}

/** Give the browser the CSRF cookie a real session would have set. */
async function grantCsrfCookie(page: Page) {
  await page.context().addCookies([{
    name: CSRF_COOKIE,
    value: CSRF_VALUE,
    domain: 'localhost',
    path: '/',
    sameSite: 'Lax',
  }])
}

test('reaches the directory from the nav and finds an employer with no open role', async ({ page }) => {
  const api = await mockApi(page, routes())
  await page.goto('/')

  // The link is session-gated, so its presence is also the session having resolved.
  await page.getByRole('link', { name: 'Companies' }).click()
  await expect(page).toHaveURL('/companies')
  await expect(page.getByRole('heading', { name: 'Companies' })).toBeVisible()

  await page.getByLabel('Open roles').selectOption('false')
  await page.getByRole('button', { name: 'Filter' }).click()

  await expect(page.getByRole('link', { name: 'Quiet Employer SA' })).toBeVisible()
  const filtered = api.callsTo('has_opportunities=false')
  expect(filtered.length).toBeGreaterThan(0)
  expect(api.unmatched).toEqual([])
})

test('opens one employer and shows what each claim rests on', async ({ page }) => {
  const api = await mockApi(page, routes())
  await page.goto('/companies')

  await page.getByRole('link', { name: 'Quiet Employer SA' }).click()

  await expect(page).toHaveURL(`/companies/${COMPANY_ID}`)
  await expect(page.getByRole('heading', { name: 'Quiet Employer SA' })).toBeVisible()
  // The platform never appears without how sure the backend is about it.
  await expect(page.getByRole('heading', { name: 'Applicant tracking system' })).toBeVisible()
  await expect(page.getByText('ATS_HOST_MATCH')).toBeVisible()
  await expect(page.getByText('Quiet Employer Europe S.A.')).toBeVisible()
  await expect(page.getByText('greenhouse:quiet')).toBeVisible()
  // Undecided stays undecided: nobody checked for a spontaneous-application channel.
  await expect(page.getByText('Unknown — nobody has checked')).toBeVisible()
  expect(api.unmatched).toEqual([])
})

test('runs a discovery pass with the CSRF header and reports what it did', async ({ page }) => {
  const api = await mockApi(page, routes())
  const headers: (string | undefined)[] = []
  page.on('request', (request) => {
    if (request.url().includes('/api/v2/company-discovery/run')) {
      headers.push(request.headers()['x-csrf-token'])
    }
  })
  await grantCsrfCookie(page)
  await page.goto('/companies')

  await page.getByRole('button', { name: 'Run discovery' }).click()

  // Ambiguity is reported, not hidden: it is what "we refused to merge on a guess"
  // looks like from outside.
  await expect(page.locator('.settings-ok'))
    .toContainText('2 created, 5 matched, 1 left ambiguous, 6 postings linked')
  const posts = api.callsTo('/api/v2/company-discovery/run')
  expect(posts).toHaveLength(1)
  // No seeds: a company is shared data, so the pass takes no body at all.
  expect(posts[0]!.body).toBeNull()
  expect(headers).toEqual([CSRF_VALUE])
  expect(api.unmatched).toEqual([])
})

test('sends an anonymous visitor from the directory to the login form', async ({ page }) => {
  await mockApi(page, [
    ANONYMOUS_SESSION,
    { match: '/api/overview', json: overview() },
    { match: '/api/runs/status', json: IDLE_RUN },
    { match: '/api/chat/history', json: { messages: [] } },
  ])

  await page.goto(`/companies/${COMPANY_ID}`)

  await expect(page).toHaveURL(`/login?redirect=/companies/${COMPANY_ID}`)
  // Nothing of the company screen rendered on the way past — and the nav does not
  // offer it either.
  await expect(page.getByRole('heading', { name: 'Quiet Employer SA' })).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Companies' })).toHaveCount(0)
})
