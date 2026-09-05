// Authentication end to end: the guard, the CSRF header and the walk a new
// account actually takes.
//
// The unit suite mocks `navigateTo`, so what it asserts is *where a page decides to
// go*. Nothing there proves the router obeys, that the middleware runs at all on a
// real navigation, or that the header the API demands leaves the browser. Those are
// the cases here, and they are the ones a broken session would break silently.
//
// Two things are only observable in a browser:
//
//   * `X-CSRF-Token`. `app/utils/api-client.ts` reads it from `document.cookie` and
//     copies it onto every unsafe request; here the cookie is a real cookie, set on
//     a real jar, and the assertion is on the header Playwright saw arrive.
//   * The guard's redirect. `middleware/auth.ts` returns a `navigateTo` — whether
//     the address bar ends up at `/login?redirect=/profile` is the router's half of
//     that contract.
//
// No backend runs: every `/api/**` request is answered from the table below. The
// cookie is set directly for the same reason — the browser cannot be handed a
// `Set-Cookie` from a server that is not there — and it carries the bare
// `jobsearch_csrf` name, which is the one the explicit local-HTTP deployment issues
// (`AuthSettings.for_local_http`); the `__Host-` spelling needs the HTTPS
// configuration this dev server does not have.
import { expect, test } from '@playwright/test'
import { ANONYMOUS_SESSION, mockApi } from './support/api'
import { IDLE_RUN, overview } from './support/fixtures'
import type { MockRoute } from './support/api'
import type { Page } from '@playwright/test'

const CSRF_COOKIE = 'jobsearch_csrf'
const CSRF_VALUE = 'a-csrf-token-from-the-session'
const PASSPHRASE = 'a-passphrase-of-a-few-words'

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

const SEARCH_DRAFT = {
  name: 'Backend roles',
  is_active: true,
  areas: [{ kind: 'COUNTRY', country: 'CH', label: null }],
  queries: [],
  title_keywords: [],
  excluded_keywords: [],
  opportunity_types: [],
  contract_types: [],
  workplace_modes: [],
  posting_languages: [],
  source_keys: [],
  workload: null,
}

const PROFILE = {
  id: '22222222-2222-4222-8222-222222222222',
  user_id: ACCOUNT.id,
  profile: {
    display_name: 'Test Candidate',
    headline: null,
    base_location: null,
    languages: [],
    work_authorizations: [],
    availability: null,
  },
  updated_at: '2026-01-02T09:20:00Z',
}

const SEARCH = {
  id: '33333333-3333-4333-8333-333333333333',
  user_id: ACCOUNT.id,
  search: SEARCH_DRAFT,
  created_at: '2026-01-02T09:25:00Z',
  updated_at: '2026-01-02T09:25:00Z',
}

/** `GET /api/v2/onboarding`, with the derived flags derived as the backend does. */
function onboarding(counts: {
  has_profile: boolean
  search_profiles: number
  active_search_profiles: number
  completed_at?: string | null
}): MockRoute {
  const completed = counts.completed_at ?? null
  return {
    match: '/api/v2/onboarding',
    method: 'GET',
    json: {
      ...counts,
      completed_at: completed,
      is_complete: completed !== null,
      may_complete: counts.has_profile && counts.active_search_profiles > 0,
    },
  }
}

const NO_PROFILE: MockRoute = {
  match: '/api/v2/me/profile',
  method: 'GET',
  status: 404,
  json: { error: 'candidate_profile_not_found', detail: 'no profile' },
}

/** A signed-in account whose setup is finished, and the screens it can open. */
function routes(...first: MockRoute[]): MockRoute[] {
  return [
    ...first,
    { match: '/api/v2/auth/session', method: 'GET', json: { account: ACCOUNT, session: SESSION_WINDOW } },
    { match: '/api/v2/me/profile', method: 'GET', json: PROFILE },
    { match: '/api/v2/me/search-profiles', method: 'GET', json: { search_profiles: [SEARCH] } },
    onboarding({ has_profile: true, search_profiles: 1, active_search_profiles: 1, completed_at: ACCOUNT.onboarding_completed_at }),
    { match: '/api/overview', json: overview() },
    { match: '/api/runs/status', json: IDLE_RUN },
    { match: '/api/chat/history', json: { messages: [] } },
  ]
}

/**
 * Change what the server says from here on.
 *
 * `mockApi` resolves each request against the array it was given, first match
 * wins, so putting a route in front of the table replaces the answer for every
 * later request — which is how a write that the app then refetches is modelled
 * without a database.
 */
function serverNow(table: MockRoute[], ...added: MockRoute[]) {
  table.unshift(...added)
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

test('sends an anonymous visitor from a guarded page to the login form', async ({ page }) => {
  await mockApi(page, [ANONYMOUS_SESSION, { match: '/api/overview', json: overview() }])

  await page.goto('/profile')

  // The whole point of the query string: the address they asked for is not lost.
  await expect(page).toHaveURL('/login?redirect=/profile')
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible()
  // Nothing of the guarded screen rendered on the way past.
  await expect(page.getByRole('heading', { name: 'Your account' })).toHaveCount(0)
})

test('signs in and returns to the page the guard interrupted', async ({ page }) => {
  // Anonymous in front of the table, or the login page would bounce a visitor who
  // already has a session away before the form could be filled.
  const table = [ANONYMOUS_SESSION, ...routes()]
  const api = await mockApi(page, table)
  await page.goto('/login?redirect=/profile')

  await page.locator('#email').fill(ACCOUNT.email)
  await page.locator('#password').fill(PASSPHRASE)
  serverNow(table, { match: '/api/v2/auth/login', method: 'POST', json: { account: ACCOUNT, session: SESSION_WINDOW } })
  await page.getByRole('button', { name: 'Sign in' }).click()

  await expect(page).toHaveURL('/profile')
  await expect(page.getByRole('heading', { name: 'Your account' })).toBeVisible()
  await expect(page.getByText(ACCOUNT.email)).toBeVisible()
  const login = api.callsTo('/api/v2/auth/login')
  expect(login).toHaveLength(1)
  // The password went in the body and nowhere else — not the URL, not a header.
  expect(login[0]!.url).toBe('/api/v2/auth/login')
  expect(JSON.parse(login[0]!.body!)).toEqual({ email: ACCOUNT.email, password: PASSPHRASE })
})

// `?redirect=https://evil.example` in a mailed link, refused by the page rather
// than by the browser: the sign-in succeeds and lands on this app's own overview.
test('refuses an off-site redirect after signing in', async ({ page }) => {
  const table = [
    ANONYMOUS_SESSION,
    ...routes({ match: '/api/v2/auth/login', method: 'POST', json: { account: ACCOUNT, session: SESSION_WINDOW } }),
  ]
  await mockApi(page, table)
  await page.goto(`/login?redirect=${encodeURIComponent('https://evil.example')}`)

  await page.locator('#email').fill(ACCOUNT.email)
  await page.locator('#password').fill(PASSPHRASE)
  await page.getByRole('button', { name: 'Sign in' }).click()

  await expect(page).toHaveURL('/')
})

test('sends an unfinished account to onboarding instead of the page it asked for', async ({ page }) => {
  const table = routes()
  serverNow(
    table,
    { match: '/api/v2/auth/session', method: 'GET', json: { account: { ...ACCOUNT, onboarding_completed_at: null }, session: SESSION_WINDOW } },
    onboarding({ has_profile: true, search_profiles: 1, active_search_profiles: 1 }),
  )
  await mockApi(page, table)

  await page.goto('/profile')

  await expect(page).toHaveURL('/onboarding')
  await expect(page.getByRole('heading', { name: 'Ready' })).toBeVisible()
})

/**
 * The header the API demands, on a request a browser made.
 *
 * The cookie is readable by script by design and the session cookie is not; this
 * is the double-submit pair, and the assertion is that the unsafe request carries
 * the header while the reads around it do not need to.
 */
test('echoes the CSRF cookie into the header on an unsafe request', async ({ page }) => {
  const table = routes()
  const api = await mockApi(page, table)
  const headers: Record<string, string | undefined>[] = []
  page.on('request', (request) => {
    if (request.url().includes('/api/v2/me/search-profiles') && request.method() === 'PUT') {
      headers.push({ csrf: request.headers()['x-csrf-token'] })
    }
  })
  await grantCsrfCookie(page)
  await page.goto('/profile')

  await expect(page.getByText('Backend roles')).toBeVisible()
  const paused = { ...SEARCH, search: { ...SEARCH_DRAFT, is_active: false } }
  serverNow(
    table,
    { match: '/api/v2/me/search-profiles/', method: 'PUT', json: paused },
    { match: '/api/v2/me/search-profiles', method: 'GET', json: { search_profiles: [paused] } },
  )
  await page.getByRole('button', { name: 'Pause Backend roles' }).click()

  await expect(page.locator('.acct-paused')).toHaveText('paused')
  expect(headers).toEqual([{ csrf: CSRF_VALUE }])
  // The pause is a wholesale replacement, as the page's comment says it must be.
  const put = api.callsTo('/api/v2/me/search-profiles/').filter(c => c.method === 'PUT')
  expect(JSON.parse(put[0]!.body!)).toEqual({ ...SEARCH_DRAFT, is_active: false })
})

/** Registration through to the command center: the walk a new account takes. */
test('registers, sets the account up and lands on the command center', async ({ page }) => {
  const table = routes()
  serverNow(
    table,
    { match: '/api/v2/auth/register', method: 'POST', status: 201, json: { account: { ...ACCOUNT, onboarding_completed_at: null }, session: SESSION_WINDOW } },
    { match: '/api/v2/auth/session', method: 'GET', status: 401, json: { error: 'not_authenticated', detail: 'none' } },
    NO_PROFILE,
    { match: '/api/v2/me/search-profiles', method: 'GET', json: { search_profiles: [] } },
    onboarding({ has_profile: false, search_profiles: 0, active_search_profiles: 0 }),
  )
  const api = await mockApi(page, table)
  await grantCsrfCookie(page)
  await page.goto('/register')

  await page.locator('#display-name').fill('Test Candidate')
  await page.locator('#email').fill(ACCOUNT.email)
  await page.locator('#password').fill(PASSPHRASE)
  await page.getByRole('button', { name: 'Create account' }).click()

  // A new account has nothing set up, so there is one place for it to go.
  await expect(page).toHaveURL('/onboarding')
  await expect(page.getByRole('heading', { name: 'Who you are' })).toBeVisible()

  // Step 1 — the profile. The step that follows is the server's answer, so the
  // write and the state it changes are stubbed together.
  serverNow(
    table,
    { match: '/api/v2/me/profile', method: 'PUT', json: PROFILE },
    { match: '/api/v2/me/profile', method: 'GET', json: PROFILE },
    onboarding({ has_profile: true, search_profiles: 0, active_search_profiles: 0 }),
  )
  await page.locator('#profile-display-name').fill('Test Candidate')
  await page.getByRole('button', { name: 'Save and continue' }).click()

  await expect(page.getByRole('heading', { name: 'What you are looking for' })).toBeVisible()

  // Step 2 — the first search.
  serverNow(
    table,
    { match: '/api/v2/me/search-profiles', method: 'POST', status: 201, json: SEARCH },
    { match: '/api/v2/me/search-profiles', method: 'GET', json: { search_profiles: [SEARCH] } },
    onboarding({ has_profile: true, search_profiles: 1, active_search_profiles: 1 }),
  )
  await page.locator('#search-name').fill('Backend roles')
  await page.getByLabel('Area 1 country').fill('CH')
  await page.getByRole('button', { name: 'Save and continue' }).click()

  await expect(page.getByRole('heading', { name: 'Ready' })).toBeVisible()

  // Step 3 — the stamp, and out.
  serverNow(table, { match: '/api/v2/onboarding/complete', method: 'POST', json: ACCOUNT })
  await page.getByRole('button', { name: 'Finish setup' }).click()

  await expect(page).toHaveURL('/')
  // The shell's account affordance, which only appears for a session it has read.
  await expect(page.locator('.topbar-account')).toHaveText('Test Candidate')
  expect(api.callsTo('/api/v2/onboarding/complete')).toHaveLength(1)
  expect(api.unmatched).toEqual([])
})

test('signs out, and the guard closes behind it', async ({ page }) => {
  const table = routes({ match: '/api/v2/auth/logout', method: 'POST', status: 204, json: null })
  const api = await mockApi(page, table)
  await grantCsrfCookie(page)
  await page.goto('/profile')

  await expect(page.getByRole('heading', { name: 'Your account' })).toBeVisible()
  // The session is gone server-side from here on, which is what a revoked session
  // looks like to the next read.
  serverNow(table, ANONYMOUS_SESSION)
  await page.getByRole('button', { name: 'Sign out' }).click()

  await expect(page).toHaveURL('/login')
  expect(api.callsTo('/api/v2/auth/logout')).toHaveLength(1)

  // And the page it left is guarded again — no cached account behind it.
  await page.goto('/profile')
  await expect(page).toHaveURL('/login?redirect=/profile')
  expect(api.unmatched).toEqual([])
})
