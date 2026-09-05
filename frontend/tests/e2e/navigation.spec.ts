// Client-side routing across the four migrated routes.
//
// The unit page specs mock `navigateTo`, so nothing there proves the router is
// wired: a missing page file, a wrong filename or a broken layout slot would all
// still pass. This walks the real nav in a real browser and asserts each route
// renders its own surface, which is completion criterion 1 (every supported V1
// route has a Nuxt equivalent).
import { expect, test } from '@playwright/test'
import { ANONYMOUS_SESSION, mockApi } from './support/api'
import { ANALYTICS, IDLE_RUN, SETTINGS, card, overview } from './support/fixtures'

const ROUTES = [
  { match: '/api/overview', json: overview() },
  { match: '/api/analytics', json: ANALYTICS },
  { match: '/api/settings', method: 'GET', json: { settings: SETTINGS, status: 'ok' } },
  { match: '/api/runs/status', json: IDLE_RUN },
  { match: '/api/jobs', method: 'GET', json: { board: { 'Ready to apply': [card()] } } },
  { match: '/api/chat/history', json: { messages: [] } },
  ANONYMOUS_SESSION,
]

test('walks the sidebar through every migrated route', async ({ page }) => {
  const api = await mockApi(page, ROUTES)
  await page.goto('/')

  await expect(page.locator('.brand')).toHaveText('⌘ Command Center')
  // Scoped to the sidebar: Phase 4 put an account link in the topbar, and the nav
  // is what this test walks. That link is asserted in tests/e2e/auth.spec.ts.
  await expect(page.locator('.sidebar').getByRole('link'))
    .toHaveText(['Overview', 'Jobs', 'Analytics', 'Settings'])
  await expect(page.getByText('Today — do these first')).toBeVisible()

  await page.getByRole('link', { name: 'Jobs' }).click()
  await expect(page).toHaveURL('/jobs')
  await expect(page.getByRole('tab', { name: /Jobs étudiants/ })).toBeVisible()

  await page.getByRole('link', { name: 'Analytics' }).click()
  await expect(page).toHaveURL('/analytics')
  await expect(page.getByText('Applications per day')).toBeVisible()

  await page.getByRole('link', { name: 'Settings' }).click()
  await expect(page).toHaveURL('/settings')
  // By role: "Auto-apply" is also a substring of the toggle's own label text.
  await expect(page.getByRole('heading', { name: 'Auto-apply' })).toBeVisible()

  await page.getByRole('link', { name: 'Overview' }).click()
  await expect(page).toHaveURL('/')
  await expect(page.getByText('Today — do these first')).toBeVisible()

  expect(api.unmatched).toEqual([])
})

test('marks only the current route active, and does not keep Overview lit', async ({ page }) => {
  // The root link is `exact-active-class`; a plain active class would match every
  // route, since every path starts with "/". V1 got this from React Router's `end`.
  await mockApi(page, ROUTES)
  await page.goto('/')

  await expect(page.getByRole('link', { name: 'Overview' })).toHaveClass(/active/)

  await page.getByRole('link', { name: 'Jobs' }).click()
  await expect(page.getByRole('link', { name: 'Jobs' })).toHaveClass(/active/)
  await expect(page.getByRole('link', { name: 'Overview' })).not.toHaveClass(/active/)
})

test('survives a direct load of a deep route', async ({ page }) => {
  // The bundle is served by FastAPI with an index.html fallback, so /settings has
  // to boot the router from that path rather than 404.
  await mockApi(page, ROUTES)
  await page.goto('/settings')

  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
  await expect(page.getByLabel('Copilot backend')).toBeVisible()
})
