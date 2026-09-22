// The LLM providers settings in a browser: the surface reachable, the write carrying
// its CSRF header, and the credential that never comes back.
//
// The unit suite mounts this page against a stubbed `fetch`, so what it proves is how
// it renders a list and shapes a request. Three things only a browser shows:
//
//   * that the screen is reachable — the Providers nav link appears for a session, and
//     the guard sends an anonymous visitor to /login instead;
//   * that adding a connection — an unsafe write against user-owned data — leaves with
//     the `X-CSRF-Token` header the API demands, and that a CLI form carries no key
//     field to begin with (§1);
//   * that a stored credential is only ever "key set" on screen, never its value (§13).
//
// No backend runs. Every `/api/**` request is answered from the table below, so nothing
// reaches a live LLM or provider (CLAUDE.md §Testing), and a "connection" is a stubbed
// payload rather than a real one.
import { expect, test } from '@playwright/test'
import { ANONYMOUS_SESSION, mockApi } from './support/api'
import { IDLE_RUN, overview } from './support/fixtures'
import type { MockRoute } from './support/api'
import type { Page } from '@playwright/test'

const CSRF_COOKIE = 'jobsearch_csrf'
const CSRF_VALUE = 'a-csrf-token-from-the-session'
const CONNECTION_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const CONNECTIONS = '/api/v2/settings/llm/connections'

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

/** A hosted gateway with a stored key — the response says so, never the value (§13). */
const CONNECTION = {
  id: CONNECTION_ID,
  provider_type: 'OPENAI_COMPATIBLE',
  display_name: 'Acme Gateway',
  base_url: 'https://gateway.example.invalid/v1',
  model: 'gpt-4o-mini',
  has_api_key: true,
  custom_headers: {},
  enabled: true,
  is_default: true,
  priority: 100,
  created_at: '2026-01-02T09:00:00Z',
  updated_at: '2026-01-02T09:00:00Z',
}

const CONNECTION_LIST = { connections: [CONNECTION] }

/** The created connection, echoed back with an id and no key value. */
const CREATED = {
  ...CONNECTION,
  id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
  provider_type: 'CLAUDE_CODE',
  display_name: 'My Claude',
  base_url: null,
  model: null,
  has_api_key: false,
  is_default: false,
}

/** A signed-in account, the Phase 11 surface, and the V1 screens the shell wraps. */
function routes(...first: MockRoute[]): MockRoute[] {
  return [
    ...first,
    { match: '/api/v2/auth/session', method: 'GET', json: { account: ACCOUNT, session: SESSION_WINDOW } },
    { match: CONNECTIONS, method: 'POST', status: 201, json: CREATED },
    { match: CONNECTIONS, method: 'GET', json: CONNECTION_LIST },
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

test('reaches providers from the nav and lists a connection', async ({ page }) => {
  const api = await mockApi(page, routes())
  await page.goto('/')

  // The link is session-gated, so its presence is the session having resolved.
  await page.getByRole('link', { name: 'Providers' }).click()
  await expect(page).toHaveURL('/providers')
  await expect(page.getByRole('heading', { name: 'Providers' })).toBeVisible()
  await expect(page.getByText('Acme Gateway')).toBeVisible()
  // A stored credential shows as "key set", never a value.
  await expect(page.getByText('key set')).toBeVisible()
  expect(api.unmatched).toEqual([])
})

test('a CLI create carries no key field and sends the CSRF header', async ({ page }) => {
  const api = await mockApi(page, routes())
  const headers: (string | undefined)[] = []
  page.on('request', (request) => {
    if (request.url().includes(CONNECTIONS) && request.method() === 'POST') {
      headers.push(request.headers()['x-csrf-token'])
    }
  })
  await grantCsrfCookie(page)
  await page.goto('/providers')

  // The create form defaults to a CLI type: no key field, because the CLI manages its
  // own auth and the platform never stores a key for it (§1).
  await expect(page.getByLabel('API key')).toHaveCount(0)

  await page.getByLabel('Display name').fill('My Claude')
  await page.getByRole('button', { name: 'Add connection' }).click()

  const posts = api.callsTo(CONNECTIONS).filter(c => c.method === 'POST')
  expect(posts).toHaveLength(1)
  expect(headers).toEqual([CSRF_VALUE])
  // The body carries no credential for a CLI connection.
  expect(JSON.parse(posts[0]!.body ?? '{}')).toMatchObject({
    provider_type: 'CLAUDE_CODE',
    base_url: null,
    api_key: null,
  })
  expect(api.unmatched).toEqual([])
})

test('sends an anonymous visitor from providers to the login form', async ({ page }) => {
  await mockApi(page, [
    ANONYMOUS_SESSION,
    { match: '/api/overview', json: overview() },
    { match: '/api/runs/status', json: IDLE_RUN },
    { match: '/api/chat/history', json: { messages: [] } },
  ])

  await page.goto('/providers')

  await expect(page).toHaveURL('/login?redirect=/providers')
  await expect(page.getByRole('link', { name: 'Providers' })).toHaveCount(0)
})
