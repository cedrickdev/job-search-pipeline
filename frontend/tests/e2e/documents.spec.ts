// The evidence store and generated documents in a browser: the guard, the write,
// and the auditable refusal.
//
// The unit suite mounts these pages against a stubbed `fetch`, so what it proves is
// how they render an answer and shape a request. Three things only a browser shows:
//
//   * that the two screens are reachable at all — the nav links appear for a session,
//     and the guard sends an anonymous visitor to /login instead;
//   * that recording a fact — an unsafe write against user-owned data — leaves with
//     the `X-CSRF-Token` header the API demands;
//   * that a rejected version survives to the screen with the rule it broke and the
//     line that broke it, because the whole point of the guard is auditable (§45): a
//     refusal the reader can see, not a silent rewrite.
//
// No backend runs. Every `/api/**` request is answered from the table below, so
// nothing here reaches a live LLM or renderer (CLAUDE.md §Testing), and a "generated"
// document is a stubbed payload rather than a real generation.
import { expect, test } from '@playwright/test'
import { ANONYMOUS_SESSION, mockApi } from './support/api'
import { IDLE_RUN, overview } from './support/fixtures'
import type { MockRoute } from './support/api'
import type { Page } from '@playwright/test'

const CSRF_COOKIE = 'jobsearch_csrf'
const CSRF_VALUE = 'a-csrf-token-from-the-session'
const EVIDENCE_ID = '66666666-6666-4666-8666-666666666666'
const DOCUMENT_ID = '88888888-8888-4888-8888-888888888888'

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

const EVIDENCE = {
  id: EVIDENCE_ID,
  kind: 'CV_SUMMARY',
  provenance: 'BASE_CV',
  summary: 'Backend engineer with 8 years of experience',
  reference_key: null,
  detail: null,
  issued_on: null,
  valid_until: null,
  source_document: null,
  recorded_at: '2026-01-02T09:40:00Z',
}

const CLAIM = {
  id: '77777777-7777-4777-8777-777777777777',
  claim_type: 'EXPERIENCE',
  label: 'Senior Backend Engineer',
  detail: 'Acme, 2018–2026',
  evidence_ids: [EVIDENCE_ID],
}

const EVIDENCE_LIST = { evidence: [EVIDENCE], claims: [CLAIM] }

/** A version the guard refused, kept with the reason it was rejected (§45). */
const REJECTED_DOCUMENT = {
  id: DOCUMENT_ID,
  candidate_profile_id: '22222222-2222-4222-8222-222222222222',
  opportunity_id: '55555555-5555-4555-8555-555555555555',
  document_type: 'RESUME',
  latest_usable_version: null,
  created_at: '2026-01-02T10:05:00Z',
  updated_at: '2026-01-02T10:05:00Z',
  versions: [{
    id: '99999999-9999-4999-8999-999999999999',
    version: 1,
    status: 'REJECTED',
    language: 'en',
    content: {
      kind: 'RESUME',
      full_name: 'Test Candidate',
      headline: 'Backend engineer',
      summary: { text: 'Backend engineer with 8 years of experience', evidence_ids: [EVIDENCE_ID] },
      experience: [],
      education: [],
      skill_groups: [],
      languages: [],
    },
    guard_report: {
      ok: false,
      violations: [{
        code: 'INVENTED_NUMBER',
        detail: 'a metric no evidence supports',
        evidence_ids: [],
        offending_text: 'grew revenue 300%',
      }],
    },
    artifact: null,
    generator_key: 'deterministic-reference/1',
    created_at: '2026-01-02T10:05:00Z',
  }],
}

const DOCUMENT_LIST = { documents: [REJECTED_DOCUMENT] }

/** A signed-in account, the Phase 10 surface, and the V1 screens the shell wraps. */
function routes(...first: MockRoute[]): MockRoute[] {
  return [
    ...first,
    { match: '/api/v2/auth/session', method: 'GET', json: { account: ACCOUNT, session: SESSION_WINDOW } },
    { match: '/api/v2/me/claims', method: 'POST', status: 201, json: CLAIM },
    { match: '/api/v2/me/evidence', method: 'POST', status: 201, json: EVIDENCE },
    { match: '/api/v2/me/evidence', method: 'GET', json: EVIDENCE_LIST },
    { match: `/api/v2/documents/${DOCUMENT_ID}`, method: 'GET', json: REJECTED_DOCUMENT },
    { match: '/api/v2/documents', method: 'GET', json: DOCUMENT_LIST },
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

test('reaches evidence and documents from the nav', async ({ page }) => {
  const api = await mockApi(page, routes())
  await page.goto('/')

  // The links are session-gated, so their presence is the session having resolved.
  await page.getByRole('link', { name: 'Evidence' }).click()
  await expect(page).toHaveURL('/evidence')
  await expect(page.getByRole('heading', { name: 'Evidence', exact: true })).toBeVisible()
  // The one summary appears in the record, its claim and the picker; assert the record.
  await expect(page.locator('.acct-search-name')
    .filter({ hasText: 'Backend engineer with 8 years of experience' }).first()).toBeVisible()

  // The nav link, not the "Documents →" cross-link the evidence page also carries.
  await page.getByRole('link', { name: 'Documents', exact: true }).click()
  await expect(page).toHaveURL('/documents')
  await expect(page.getByRole('heading', { name: 'Documents' })).toBeVisible()
  expect(api.unmatched).toEqual([])
})

test('records a fact with the CSRF header the write demands', async ({ page }) => {
  const api = await mockApi(page, routes())
  const headers: (string | undefined)[] = []
  page.on('request', (request) => {
    if (request.url().includes('/api/v2/me/evidence') && request.method() === 'POST') {
      headers.push(request.headers()['x-csrf-token'])
    }
  })
  await grantCsrfCookie(page)
  await page.goto('/evidence')

  await page.getByLabel('Evidence summary').fill('Shipped the billing rewrite')
  await page.getByRole('button', { name: 'Record evidence' }).click()

  const posts = api.callsTo('/api/v2/me/evidence').filter(c => c.method === 'POST')
  expect(posts).toHaveLength(1)
  expect(headers).toEqual([CSRF_VALUE])
  expect(api.unmatched).toEqual([])
})

test('keeps a rejected version on screen with the rule it broke (§45)', async ({ page }) => {
  const api = await mockApi(page, routes())
  await page.goto('/documents')

  await page.getByRole('link', { name: 'Résumé' }).click()
  await expect(page).toHaveURL(`/documents/${DOCUMENT_ID}`)

  // The refusal is visible, named, and quoted — not hidden behind a rewrite.
  await expect(page.getByText('REJECTED')).toBeVisible()
  await expect(page.getByText('INVENTED_NUMBER')).toBeVisible()
  await expect(page.getByText('grew revenue 300%')).toBeVisible()
  // Nothing to download, because nothing cleared the guard.
  await expect(page.getByRole('button', { name: 'Download PDF' })).toBeDisabled()
  expect(api.unmatched).toEqual([])
})

test('sends an anonymous visitor from documents to the login form', async ({ page }) => {
  await mockApi(page, [
    ANONYMOUS_SESSION,
    { match: '/api/overview', json: overview() },
    { match: '/api/runs/status', json: IDLE_RUN },
    { match: '/api/chat/history', json: { messages: [] } },
  ])

  await page.goto('/documents')

  await expect(page).toHaveURL('/login?redirect=/documents')
  await expect(page.getByRole('link', { name: 'Documents' })).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Evidence' })).toHaveCount(0)
})
