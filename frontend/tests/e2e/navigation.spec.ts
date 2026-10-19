// Client-side routing through the authenticated V2 product navigation.
//
// The unit page specs mock `navigateTo`, so nothing there proves the router is
// wired: a missing page file, a wrong filename or a broken layout slot would all
// still pass. This walks the real nav in a real browser and asserts each route
// renders its own surface, which is completion criterion 1 (every supported V1
// route has a Nuxt equivalent).
import { expect, test } from '@playwright/test'
import { mockApi } from './support/api'
import { IDLE_RUN, overview } from './support/fixtures'
import { signedIn } from '../nuxt/support/v2-fixtures'

const ROUTES = [
  { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
  { match: '/api/v2/companies', method: 'GET', json: { companies: [], limit: 50, offset: 0 } },
  { match: '/api/v2/documents', method: 'GET', json: { documents: [] } },
  { match: '/api/v2/me/evidence', method: 'GET', json: { evidence: [] } },
  { match: '/api/v2/applications', method: 'GET', json: { applications: [] } },
  { match: '/api/v2/settings/llm/connections', method: 'GET', json: { connections: [] } },
  { match: '/api/v2/me/morning-schedule', method: 'GET', json: { enabled: false, local_time: '08:00', cadence: 'daily' } },
  { match: '/api/v2/me/morning-runs/latest', method: 'GET', json: { runs: [] } },
  { match: '/api/settings', method: 'GET', json: { settings: {
    auto_apply: false, auto_apply_min_score: 85, auto_apply_daily_cap: 5,
    tailor_creativity: 'balanced', llm_backend: 'claude_cli', llm_base_url: '', llm_model: '',
    schedule_enabled: false, schedule_time: '08:00', schedule_cadence: 'daily',
  }, status: 'ok' } },
  { match: '/api/overview', json: overview() },
  { match: '/api/runs/status', json: IDLE_RUN },
  { match: '/api/chat/history', json: { messages: [] } },
]

test('walks the primary V2 navigation', async ({ page }) => {
  const api = await mockApi(page, ROUTES)
  await page.goto('/')

  await expect(page.locator('.brand')).toHaveText('⌘ Command Center')
  await expect(page.locator('.sidebar').getByRole('link'))
    .toContainText(['Companies', 'Map', 'Documents', 'Evidence', 'Applications', 'Interview', 'Strategy', 'Billing', 'Settings'])
  await expect(page.getByRole('heading', { name: 'Map' })).toBeVisible()

  await page.getByRole('link', { name: 'Companies' }).click()
  await expect(page).toHaveURL('/companies')
  await expect(page.getByRole('heading', { name: 'Companies' })).toBeVisible()

  await page.getByRole('link', { name: 'Documents' }).click()
  await expect(page).toHaveURL('/documents')
  await expect(page.getByRole('heading', { name: 'Documents' })).toBeVisible()

  await page.getByRole('link', { name: 'Applications' }).click()
  await expect(page).toHaveURL('/applications')
  await expect(page.getByRole('heading', { name: 'Applications' })).toBeVisible()

  await page.getByRole('link', { name: 'Settings' }).click()
  await expect(page).toHaveURL('/settings')
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

  expect(api.unmatched).toEqual([])
})

test('marks only the current V2 route active', async ({ page }) => {
  await mockApi(page, ROUTES)
  await page.goto('/')

  await expect(page.getByRole('link', { name: 'Map' })).toHaveClass(/active/)

  await page.getByRole('link', { name: 'Companies' }).click()
  await expect(page.getByRole('link', { name: 'Companies' })).toHaveClass(/active/)
  await expect(page.getByRole('link', { name: 'Map' })).not.toHaveClass(/active/)
})

test('survives a direct load of a deep V2 route', async ({ page }) => {
  // The bundle is served by FastAPI with an index.html fallback, so /providers has
  // to boot the router from that path rather than 404.
  await mockApi(page, ROUTES)
  await page.goto('/providers')

  await expect(page.getByRole('heading', { name: 'Providers' })).toBeVisible()
})
