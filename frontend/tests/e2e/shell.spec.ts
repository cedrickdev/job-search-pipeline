// The shell in a real browser: theme initialisation and the copilot dock.
//
// These are the cases tests/nuxt/layouts/default.spec.ts explains it cannot make.
// @nuxtjs/color-mode decides the starting mode from an inline script that runs
// before paint, and under `import.meta.test` its client plugin substitutes a
// hardcoded light stub when that script has not run — so "dark by default" and
// "reads the persisted choice" are only answerable where the script executes.
// That is here.
import { expect, test } from '@playwright/test'
import { ANONYMOUS_SESSION, mockApi } from './support/api'
import { overview } from './support/fixtures'

declare global {
  interface Window {
    __themeAtDomReady?: string | null
  }
}

/** Everything the overview route and the copilot dock ask for. */
const ROUTES = [
  { match: '/api/overview', json: overview() },
  { match: '/api/chat/history', json: { messages: [] } },
  ANONYMOUS_SESSION,
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

  await expect(page.getByLabel('Toggle theme')).toHaveText('☾ Dark')
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
  await expect(page.getByLabel('Toggle theme')).toHaveText('☀ Light')
})

test('persists a toggle across a reload', async ({ page }) => {
  await mockApi(page, ROUTES)
  await page.goto('/')

  await page.getByLabel('Toggle theme').click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  expect(await page.evaluate(() => localStorage.getItem('theme'))).toBe('light')

  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await expect(page.getByLabel('Toggle theme')).toHaveText('☀ Light')
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
