// The shell in a real browser: theme initialisation and the copilot dock.
//
// These are the cases tests/nuxt/layouts/default.spec.ts explains it cannot make.
// @nuxtjs/color-mode decides the starting mode from an inline script that runs
// before paint, and under `import.meta.test` its client plugin substitutes a
// hardcoded light stub when that script has not run — so "dark by default" and
// "reads the persisted choice" are only answerable where the script executes.
// That is here.
import { expect, test } from '@playwright/test'
import { mockApi } from './support/api'
import { overview } from './support/fixtures'
import { signedIn } from '../nuxt/support/v2-fixtures'

declare global {
  interface Window {
    __themeAtDomReady?: string | null
  }
}

/** Everything the authenticated shell and the Copilot dock ask for. */
const ROUTES = [
  { match: '/api/v2/auth/session', json: signedIn() },
  { match: '/api/overview', json: overview() },
  { match: '/api/chat/history', json: { messages: [] } },
]

test('applies dark before the app hydrates', async ({ page }) => {
  await mockApi(page, ROUTES)
  // Captured at DOMContentLoaded, i.e. before Vue has painted anything: if the
  // attribute were written by the app instead of the pre-paint script, this would
  // be null and a V1 user would see a light flash on every load.
  await page.addInitScript(() => {
    document.addEventListener(
      'DOMContentLoaded',
      () => {
        window.__themeAtDomReady = document.documentElement.getAttribute('data-theme')
      },
      { once: true },
    )
  })

  await page.goto('/')

  await expect(page.getByLabel('Toggle theme')).toHaveText('Dark')
  expect(await page.evaluate(() => window.__themeAtDomReady)).toBe('dark')
  // Both hooks, from one preference: `data-theme` drives the ported V1 tokens and
  // `.dark` drives Nuxt UI and Tailwind.
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect(page.locator('html')).toHaveClass(/dark/)
})

test('reads a V1 user\'s persisted choice from the "theme" key', async ({ page }) => {
  await mockApi(page, ROUTES)
  // The key V1 wrote, not the module's default "nuxt-color-mode": a saved choice
  // has to survive the migration.
  await page.addInitScript(() => window.localStorage.setItem('theme', 'light'))

  await page.goto('/')

  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await expect(page.getByLabel('Toggle theme')).toHaveText('Light')
})

test('persists a toggle across a reload', async ({ page }) => {
  await mockApi(page, ROUTES)
  await page.goto('/')

  await page.getByLabel('Toggle theme').click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  expect(await page.evaluate(() => localStorage.getItem('theme'))).toBe('light')

  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await expect(page.getByLabel('Toggle theme')).toHaveText('Light')
})

test('persists the French locale and keeps the overview within a phone viewport', async ({ page }) => {
  await mockApi(page, ROUTES)
  await page.setViewportSize({ width: 320, height: 720 })
  await page.goto('/')

  await page.getByLabel('Language').click()
  await page.getByRole('option', { name: 'FR' }).click()
  await expect(page.locator('html')).toHaveAttribute('lang', 'fr')
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Carte')
  expect(await page.evaluate(() => document.documentElement.scrollWidth))
    .toBeLessThanOrEqual(320)

  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('lang', 'fr')
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Carte')
})

test('uses a contained navigation drawer instead of a full-width sidebar on a phone', async ({ page }) => {
  await mockApi(page, ROUTES)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')

  const drawer = page.locator('.sidebar')
  expect((await drawer.boundingBox())?.x).toBeLessThan(0)

  const trigger = page.locator('.mobile-nav-toggle')
  await trigger.click()
  await expect(trigger).toHaveAttribute('aria-expanded', 'true')
  await expect(page.locator('.nav-backdrop')).toBeVisible()
  expect((await drawer.boundingBox())?.width).toBeLessThan(390)
  await expect.poll(async () => (await drawer.boundingBox())?.x ?? -999)
    .toBeGreaterThanOrEqual(0)

  // Click in the exposed strip beside the drawer, not the drawer's own surface.
  await page.mouse.click(380, 420)
  await expect(trigger).toHaveAttribute('aria-expanded', 'false')
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
})

test('keeps the tablet topbar on one compact row and presents the profile as an avatar', async ({ page }) => {
  await mockApi(page, [
    ...ROUTES,
  ])
  await page.setViewportSize({ width: 900, height: 720 })
  await page.goto('/')

  await expect(page.getByRole('link', { name: 'Your account: Test Candidate' })).toBeVisible()
  await expect(page.locator('.account-avatar')).toHaveText('TC')
  expect((await page.locator('.topbar').boundingBox())?.height).toBeLessThanOrEqual(64)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(900)
})

test('keeps desktop utility controls compact without wrapping the search label', async ({ page }) => {
  await mockApi(page, [
    ...ROUTES,
  ])
  await page.setViewportSize({ width: 1100, height: 720 })
  await page.goto('/')

  const topbar = page.locator('.topbar')
  const searchLabel = page.locator('.command-trigger span:not([aria-hidden="true"])')
  await expect(searchLabel).toHaveCSS('white-space', 'nowrap')
  expect((await topbar.boundingBox())?.height).toBeLessThanOrEqual(64)
  expect((await page.locator('.theme-btn').first().boundingBox())?.width).toBeLessThanOrEqual(48)
  await expect(page.locator('.account-avatar')).toHaveText('TC')
})

test('opens and closes the global copilot dock', async ({ page }) => {
  const api = await mockApi(page, ROUTES)
  await page.goto('/')

  const panel = page.locator('section[aria-label="Copilot"]')
  await expect(panel).toHaveCount(0)

  await page.getByRole('button', { name: /copilot/i }).click()
  await expect(panel).toBeVisible()
  // Opening the dock hydrates the global thread rather than starting blank.
  expect(api.callsTo('/api/chat/history')).toHaveLength(1)

  await page.locator('.copilot-dock-head button').click()
  await expect(panel).toHaveCount(0)
  expect(api.unmatched).toEqual([])
})
