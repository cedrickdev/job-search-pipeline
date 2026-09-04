// Playwright configuration for the V2 frontend.
//
// `nuxt dev` is the server under test rather than a production preview: the
// app is `ssr: false`, so what the browser executes is the same client bundle
// either way, and dev needs no build step before the suite can run. The one
// production-only concern — that FastAPI can serve the bundle from `/assets/` —
// is a build-output property, checked by `npm run build`, not by a browser.
//
// No backend runs during these tests. Every `/api/**` request is intercepted in
// the browser by tests/e2e/support/api.ts, which answers from a route table and
// fails loudly on anything unstubbed. Nothing here may reach a live job board or
// a live LLM (CLAUDE.md §Testing), and with the dev proxy pointing at a FastAPI
// that is not running, a missed mock surfaces as a connection error rather than
// as a silent pass.
// The suite runs on its own port, not the app's default 3000, and never reuses a
// server it did not start. Both are the same lesson: 3000 is everybody's default,
// and an unrelated dev server already listening there was silently accepted as the
// app under test — every assertion then failed against a different product.
// Booting a private `nuxt dev` per run costs a few seconds and removes the whole
// class of failure; a busy port now aborts the run instead of faking it.
import { defineConfig, devices } from '@playwright/test'

const PORT = 3113
const BASE_URL = `http://localhost:${PORT}`

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? [['github'], ['list']] : [['list']],
  timeout: 30_000,
  expect: { timeout: 10_000 },

  use: {
    baseURL: BASE_URL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  // Chromium only. These tests assert application behaviour, not rendering
  // engines, and a parity migration has no cross-browser claim to defend.
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],

  webServer: {
    command: `npm run dev -- --port ${PORT}`,
    url: BASE_URL,
    reuseExistingServer: false,
    // A cold `nuxt dev` compiles the app on first request.
    timeout: 180_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
})
