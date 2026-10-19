// The candidate's safe local path in one real browser.  Feature specs cover the
// individual pages; this one proves their hand-off: a Swiss collection yields a
// visible opportunity, an ATS document can be prepared from that opportunity, and
// the local autopilot stops at human review without submitting anything.
//
// Every HTTP answer is a deterministic fixture and all non-local hosts are aborted.
// This is deliberately not an integration test against job boards, an LLM, or a
// browser worker: it proves the user-facing local workflow and its safety boundary.
import { expect, test } from '@playwright/test'
import { mockApi } from './support/api'
import { IDLE_RUN, overview } from './support/fixtures'
import type { MockRoute } from './support/api'
import type { Page } from '@playwright/test'

const ACCOUNT_ID = '11111111-1111-4111-8111-111111111111'
const PROFILE_ID = '33333333-3333-4333-8333-333333333333'
const OPPORTUNITY_ID = '55555555-5555-4555-8555-555555555555'
const DOCUMENT_ID = '88888888-8888-4888-888888888888'
const APPLICATION_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const RUN_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'

const ACCOUNT = {
  id: ACCOUNT_ID,
  email: 'candidate@example.invalid',
  display_name: 'Test Candidate',
  status: 'ACTIVE',
  created_at: '2026-10-01T09:00:00Z',
  onboarding_completed_at: '2026-10-01T09:30:00Z',
}
const SESSION_WINDOW = {
  issued_at: '2026-10-01T09:00:00Z',
  expires_at: '2026-10-08T09:00:00Z',
  last_seen_at: '2026-10-01T09:15:00Z',
}
const SEARCH = {
  id: PROFILE_ID,
  user_id: ACCOUNT_ID,
  search: {
    name: 'Swiss backend roles', is_active: true,
    areas: [{ kind: 'COUNTRY', country: 'CH', label: null }],
    queries: [], title_keywords: ['backend'], excluded_keywords: [],
    opportunity_types: [], contract_types: [], workplace_modes: [],
    posting_languages: [], source_keys: [], workload: null,
  },
  created_at: '2026-10-01T09:20:00Z', updated_at: '2026-10-01T09:20:00Z',
}
const OPPORTUNITY = {
  id: OPPORTUNITY_ID,
  title: 'Backend Engineer', company_name: 'Swiss Example SA', company_id: null,
  application_url: 'https://boards.example.invalid/role',
  contract_type: 'PERMANENT', opportunity_type: 'FULL_TIME', workplace_mode: 'HYBRID',
  remote_scope: null, status: 'RESOLVED', distance_meters: 1200, matched_radii: [],
  posted_at: '2026-10-01T08:00:00Z', discovered_at: '2026-10-01T09:00:00Z', posting_language: 'en',
  location: {
    point: { latitude: 46.5197, longitude: 6.6323 }, precision: 'CITY',
    provenance: 'GEOCODED', confidence: 'HIGH', city: 'Lausanne', region: 'VD',
    postal_code: null, country: 'CH', raw: 'Lausanne, VD, CH',
    geocoded_at: '2026-10-01T09:00:00Z', geocoder: 'fixture',
  },
}
const DOCUMENT = {
  id: DOCUMENT_ID, candidate_profile_id: '22222222-2222-4222-8222-222222222222',
  opportunity_id: OPPORTUNITY_ID, document_type: 'RESUME', latest_usable_version: 1,
  created_at: '2026-10-01T09:00:00Z', updated_at: '2026-10-01T09:00:00Z',
  versions: [{
    id: '99999999-9999-4999-8999-999999999999', version: 1, status: 'USABLE', language: 'en',
    content: {
      kind: 'RESUME', full_name: 'Test Candidate', headline: 'Backend Engineer',
      summary: { text: 'Evidence-grounded Swiss backend profile.', evidence_ids: [] },
      experience: [], education: [], skill_groups: [], languages: [],
    },
    guard_report: { ok: true, violations: [] },
    artifact: { filename: 'resume.pdf', content_type: 'application/pdf', size_bytes: 1024 },
    generator_key: 'fixture/1', created_at: '2026-10-01T09:00:00Z',
  }],
}
const APPLICATION = {
  id: APPLICATION_ID, state: 'REQUIRES_HUMAN', channel: 'BROWSER', opportunity_id: OPPORTUNITY_ID,
  company_id: null, pinned_documents: [DOCUMENT_ID], attempt_count: 0,
  created_at: '2026-10-01T09:01:00Z', updated_at: '2026-10-01T09:01:00Z',
}

function routes(): MockRoute[] {
  return [
    { match: '/api/v2/auth/session', method: 'GET', json: { account: ACCOUNT, session: SESSION_WINDOW } },
    { match: `/api/v2/me/search-profiles/${PROFILE_ID}/opportunities`, method: 'GET', json: { opportunities: [OPPORTUNITY], limit: 50, offset: 0 } },
    { match: `/api/v2/me/search-profiles/${PROFILE_ID}/discovery-runs/latest`, method: 'GET', status: 404, json: { error: 'discovery_not_found' } },
    { match: `/api/v2/me/search-profiles/${PROFILE_ID}/discovery-runs`, method: 'POST', status: 202, json: { run_id: RUN_ID, status: 'QUEUED' } },
    { match: `/api/v2/me/discovery-runs/${RUN_ID}`, method: 'GET', json: {
      run_id: RUN_ID, search_profile_id: PROFILE_ID, country: 'CH', trigger: 'MANUAL',
      status: 'SUCCEEDED', started_at: '2026-10-01T09:00:00Z', finished_at: '2026-10-01T09:00:01Z',
      seen_count: 1, matched_count: 1, source_health: [], warning_codes: [],
    } },
    { match: '/api/v2/me/search-profiles', method: 'GET', json: { search_profiles: [SEARCH] } },
    { match: `/api/v2/opportunities/${OPPORTUNITY_ID}/resume`, method: 'POST', status: 201, json: DOCUMENT },
    { match: `/api/v2/documents/${DOCUMENT_ID}`, method: 'GET', json: DOCUMENT },
    { match: `/api/v2/me/opportunities/${OPPORTUNITY_ID}/local-autopilot`, method: 'POST', json: {
      next_action: 'STOP_FOR_HUMAN_REVIEW', stop_reason: 'HUMAN_REQUIRED', application: APPLICATION,
    } },
    { match: '/api/v2/applications', method: 'GET', json: { applications: [APPLICATION] } },
    { match: '/api/overview', method: 'GET', json: overview() },
    { match: '/api/runs/status', method: 'GET', json: IDLE_RUN },
    { match: '/api/chat/history', method: 'GET', json: { messages: [] } },
  ]
}

async function mockMapAssets(page: Page) {
  const transparentPng = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
    'base64',
  )
  await page.route('**/tile.openstreetmap.org/**', route =>
    route.fulfill({ status: 200, contentType: 'image/png', body: transparentPng }))
  await page.route('**/*.pbf', route =>
    route.fulfill({ status: 200, contentType: 'application/x-protobuf', body: '' }))
}

test('keeps the complete Swiss local path review-only after document generation', async ({ page }) => {
  const unexpectedHosts: string[] = []
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if ((url.protocol === 'http:' || url.protocol === 'https:') && !['localhost', '127.0.0.1'].includes(url.hostname)) {
      unexpectedHosts.push(url.hostname)
      await route.abort()
      return
    }
    await route.continue()
  })
  await mockMapAssets(page)
  const api = await mockApi(page, routes())

  await page.goto(`/map?profile=${PROFILE_ID}`)
  await expect(page.locator('[data-testid="opportunity-map"] canvas')).toBeVisible()
  await expect(page.locator('.map-row', { hasText: 'Backend Engineer' })).toBeVisible()

  await page.getByRole('button', { name: /^(Actualiser les offres|Refresh opportunities)$/ }).click()
  await expect.poll(() => api.callsTo(`/api/v2/me/search-profiles/${PROFILE_ID}/discovery-runs`).filter(call => call.method === 'POST').length).toBe(1)

  await page.locator('.map-row', { hasText: 'Backend Engineer' }).click()
  await expect(page.getByTestId('map-card')).toBeVisible()
  await page.getByRole('button', { name: /^(CV adapté|Resume)$/ }).click()
  await expect(page).toHaveURL(`/documents/${DOCUMENT_ID}`)
  // The map's debounced viewport URL write must not pull us back after navigation.
  await page.waitForTimeout(400)
  await expect(page).toHaveURL(`/documents/${DOCUMENT_ID}`)
  await expect(page.getByText('USABLE')).toBeVisible()

  await page.goBack()
  await page.locator('.map-row', { hasText: 'Backend Engineer' }).click()
  await page.getByRole('button', { name: /^(Évaluer et préparer|Evaluate and prepare)$/ }).click()
  await expect(page.locator('.map-card-autopilot-status')).toContainText(/(vérifiée par vous|your review)/i)
  await page.getByRole('link', { name: /^(Vérifier la candidature|Review application)/ }).click()
  await expect(page).toHaveURL('/applications')
  await expect(page.getByRole('listitem').filter({ hasText: 'Human review required' })).toBeVisible()
  await expect(page.getByRole('button', { name: /^(Submit|Envoyer|Approve & submit|Approuver et envoyer)$/ })).toHaveCount(0)

  expect(api.callsTo(`/api/v2/applications/${APPLICATION_ID}/submit`).filter(call => call.method === 'POST')).toHaveLength(0)
  expect(api.unmatched).toEqual([])
  expect(unexpectedHosts).toEqual([])
})
