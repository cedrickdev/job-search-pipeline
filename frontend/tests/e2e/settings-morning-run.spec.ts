import { expect, test } from '@playwright/test'
import { mockApi } from './support/api'
import { IDLE_RUN, SETTINGS } from './support/fixtures'
import { signedIn } from '../nuxt/support/v2-fixtures'

const profile = '33333333-3333-4333-8333-333333333333'
const run = '55555555-5555-4555-8555-555555555555'

for (const width of [320, 390, 768, 1280]) {
  test(`queued browser application remains visibly unsent at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 })
    const api = await mockApi(page, [
      { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
      { match: '/api/settings', method: 'GET', json: { settings: SETTINGS, status: 'ok' } },
      { match: '/api/runs/status', method: 'GET', json: IDLE_RUN },
      { match: '/api/v2/settings/llm/connections', method: 'GET', json: { connections: [] } },
      { match: '/api/v2/me/morning-schedule', method: 'GET', json: { enabled: false, local_time: '08:00', cadence: 'daily' } },
      { match: '/api/v2/me/morning-runs/latest', method: 'GET', json: { runs: [{
        run_id: run, search_profile_id: profile, status: 'SUCCEEDED', seen_count: 1,
        matched_count: 1, source_health: [], warning_codes: [],
        preparations: [{ opportunity_id: '11111111-1111-4111-8111-111111111111',
          stage: 'DOCUMENTS_RENDERED', updated_at: '2026-10-13T08:00:00Z',
          application_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
          application_state: 'APPROVED', submission_task_status: 'QUEUED' }],
      }] } },
    ])
    await page.goto('/settings')
    const section = page.getByTestId('v2-morning-run')
    await expect(section.getByRole('status').filter({ hasText: 'Queued for browser submission; not sent' })).toBeVisible()
    await expect(section.getByRole('link', { name: 'Application history' }))
      .toHaveAttribute('href', '/applications?application=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect(api.unmatched).toEqual([])
  })
}

for (const locale of ['en', 'fr'] as const) {
  test(`${locale}: prepared versions remain downloadable and distinct from submission`, async ({ page }) => {
    await page.context().addCookies([{ name: 'app_locale', value: locale, domain: 'localhost', path: '/' }])
    await page.setViewportSize({ width: 390, height: 780 })
    const api = await mockApi(page, [
      { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
      { match: '/api/settings', method: 'GET', json: { settings: SETTINGS, status: 'ok' } },
      { match: '/api/runs/status', method: 'GET', json: IDLE_RUN },
      { match: '/api/v2/settings/llm/connections', method: 'GET', json: { connections: [] } },
      { match: '/api/v2/me/morning-schedule', method: 'GET', json: { enabled: false, local_time: '08:00', cadence: 'daily' } },
      { match: '/api/v2/me/morning-runs/latest', method: 'GET', json: { runs: [{
        run_id: run, search_profile_id: profile, status: 'SUCCEEDED', seen_count: 1,
        matched_count: 1, source_health: [], warning_codes: [],
        preparations: [{ opportunity_id: '11111111-1111-4111-8111-111111111111',
          stage: 'DOCUMENTS_RENDERED', resume_version_id: '22222222-2222-4222-8222-222222222222',
          cover_letter_version_id: '44444444-4444-4444-8444-444444444444',
          updated_at: '2026-10-13T08:00:00Z' }],
      }] } },
    ])
    await page.goto('/settings')
    const section = page.getByTestId('v2-morning-run')
    await expect(section.getByText(locale === 'fr' ? 'CV et lettre prêts ; candidature non envoyée' : 'CV and letter ready; not submitted')).toBeVisible()
    await expect(section.getByRole('link', { name: locale === 'fr' ? 'Télécharger cette version du CV' : 'Download exact CV version' }))
      .toHaveAttribute('href', '/api/v2/documents/versions/22222222-2222-4222-8222-222222222222/download')
    await expect(section.getByRole('link', { name: locale === 'fr' ? 'Télécharger cette version de la lettre' : 'Download exact letter version' }))
      .toHaveAttribute('href', '/api/v2/documents/versions/44444444-4444-4444-8444-444444444444/download')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect(api.unmatched).toEqual([])
  })
}

test('mobile V2 discovery stays separate from legacy and shows a partial source failure', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 700 })
  const api = await mockApi(page, [
    { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
    { match: '/api/settings', method: 'GET', json: { settings: SETTINGS, status: 'ok' } },
    { match: '/api/runs/status', method: 'GET', json: IDLE_RUN },
    { match: '/api/v2/settings/llm/connections', method: 'GET', json: { connections: [] } },
    { match: '/api/v2/me/morning-schedule', method: 'GET', json: { enabled: false, local_time: '08:00', cadence: 'daily' } },
    { match: '/api/v2/me/morning-runs/latest', method: 'GET', json: { runs: [{
      run_id: run, search_profile_id: profile, status: 'PARTIAL', seen_count: 4, matched_count: 2,
      started_at: null, finished_at: null, source_health: [{ source_key: 'board_a', status: 'UNAVAILABLE', reason: 'SOURCE_TIMEOUT' }], warning_codes: ['SOURCE_TIMEOUT'],
    }] } },
    { match: '/api/v2/me/morning-runs', method: 'POST', status: 202, json: { runs: [{ run_id: run, search_profile_id: profile, status: 'QUEUED' }] } },
  ])

  await page.goto('/settings')
  const section = page.getByTestId('v2-morning-run')
  await expect(section.getByText('Partial discovery')).toBeVisible()
  await expect(section.getByText(/board_a.*timed out/)).toBeVisible()
  await expect(section.getByRole('link', { name: 'View opportunities on map' })).toHaveAttribute('href', `/map?profile=${profile}`)
  await section.getByRole('button', { name: 'Run V2 discovery' }).click()
  await expect(section.getByText('Queued')).toBeVisible()
  expect(api.callsTo('/api/runs/full').filter(call => call.method === 'POST')).toHaveLength(0)
  expect(api.callsTo('/api/v2/me/morning-runs').filter(call => call.method === 'POST')).toHaveLength(1)
  expect(api.unmatched).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

for (const locale of ['en', 'fr'] as const) {
  test(`${locale}: refused Swiss source is explained without pretending to submit`, async ({ page }) => {
    await page.context().addCookies([{ name: 'app_locale', value: locale, domain: 'localhost', path: '/' }])
    const api = await mockApi(page, [
      { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
      { match: '/api/settings', method: 'GET', json: { settings: SETTINGS, status: 'ok' } },
      { match: '/api/runs/status', method: 'GET', json: IDLE_RUN },
      { match: '/api/v2/settings/llm/connections', method: 'GET', json: { connections: [] } },
      { match: '/api/v2/me/morning-schedule', method: 'GET', json: { enabled: false, local_time: '08:00', cadence: 'daily' } },
      { match: '/api/v2/me/morning-runs/latest', method: 'GET', json: { runs: [] } },
      { match: '/api/v2/me/morning-runs', method: 'POST', status: 409, json: {
        error: 'discovery_source_unapproved', detail: 'source access not approved',
      } },
    ])
    await page.goto('/settings')
    const section = page.getByTestId('v2-morning-run')
    await section.getByRole('button', { name: locale === 'fr' ? 'Lancer la recherche V2' : 'Run V2 discovery' }).click()
    await expect(section.getByRole('alert')).toContainText(locale === 'fr' ? 'aucun board ATS suisse vérifié' : 'no reviewed Swiss ATS board')
    await expect(section.getByRole('status')).toContainText(locale === 'fr' ? 'Aucune recherche suisse active' : 'No active Swiss search')
    expect(await section.getByText(/candidature envoyée|application sent/i).count()).toBe(0)
    expect(api.unmatched).toEqual([])
  })
}

test('a completed discovery with zero offers says zero, not sent', async ({ page }) => {
  const api = await mockApi(page, [
    { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
    { match: '/api/settings', method: 'GET', json: { settings: SETTINGS, status: 'ok' } },
    { match: '/api/runs/status', method: 'GET', json: IDLE_RUN },
    { match: '/api/v2/settings/llm/connections', method: 'GET', json: { connections: [] } },
    { match: '/api/v2/me/morning-schedule', method: 'GET', json: { enabled: false, local_time: '08:00', cadence: 'daily' } },
    { match: '/api/v2/me/morning-runs/latest', method: 'GET', json: { runs: [{
      run_id: run, search_profile_id: profile, status: 'SUCCEEDED', seen_count: 0,
      matched_count: 0, source_health: [{ source_key: 'greenhouse', status: 'HEALTHY', reason: null }], warning_codes: [],
    }] } },
  ])
  await page.goto('/settings')
  const section = page.getByTestId('v2-morning-run')
  await expect(section.getByText('Discovery completed')).toBeVisible()
  await expect(section.getByText('0 matched offers')).toBeVisible()
  await expect(section.getByText(/greenhouse.*Available/)).toBeVisible()
  expect(await section.getByText(/application sent/i).count()).toBe(0)
  expect(api.unmatched).toEqual([])
})

for (const locale of ['en', 'fr'] as const) {
  test(`${locale}: a morning-run API outage is a visible error, not an empty successful search`, async ({ page }) => {
    await page.context().addCookies([{ name: 'app_locale', value: locale, domain: 'localhost', path: '/' }])
    const api = await mockApi(page, [
      { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
      { match: '/api/settings', method: 'GET', json: { settings: SETTINGS, status: 'ok' } },
      { match: '/api/runs/status', method: 'GET', json: IDLE_RUN },
      { match: '/api/v2/settings/llm/connections', method: 'GET', json: { connections: [] } },
      { match: '/api/v2/me/morning-schedule', method: 'GET', json: { enabled: false, local_time: '08:00', cadence: 'daily' } },
    ])
    await page.route('**/api/v2/me/morning-runs/latest', route => route.abort('failed'))
    await page.goto('/settings')
    const section = page.getByTestId('v2-morning-run')
    await expect(section.getByRole('alert')).toContainText(locale === 'fr' ? 'Serveur inaccessible' : 'server could not be reached')
    await expect(section.getByText(/Recherche terminée|Discovery completed/)).toHaveCount(0)
    expect(api.unmatched).toEqual([])
  })
}
