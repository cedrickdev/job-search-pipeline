// The applications screen in a browser: the surface reachable, the state on show, and
// the one next step it offers.
//
// The unit suite mounts this page against a stubbed `fetch`; two things only a browser
// shows are here — that the screen is reachable (the Applications nav link appears for
// a session) and that a reviewed application offers the single "Approve & submit"
// decision the review screen is for (§89).
//
// No backend runs. Every `/api/**` request is answered from the table below, so
// nothing reaches a live board or LLM (CLAUDE.md §Testing).
import { expect, test } from '@playwright/test'
import { mockApi } from './support/api'
import { IDLE_RUN, overview } from './support/fixtures'
import type { MockRoute } from './support/api'

const APPLICATIONS = '/api/v2/applications'
const APPLICATION_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'

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

const APPLICATION = {
  id: APPLICATION_ID,
  state: 'READY_FOR_REVIEW',
  channel: 'BROWSER',
  opportunity_id: '55555555-5555-4555-8555-555555555555',
  company_id: null,
  pinned_documents: [],
  attempt_count: 0,
  created_at: '2026-03-01T09:30:00Z',
  updated_at: '2026-03-01T09:30:00Z',
}

function routes(...first: MockRoute[]): MockRoute[] {
  return [
    ...first,
    { match: '/api/v2/auth/session', method: 'GET', json: { account: ACCOUNT, session: SESSION_WINDOW } },
    { match: APPLICATIONS, method: 'GET', json: { applications: [APPLICATION] } },
    { match: '/api/overview', json: overview() },
    { match: '/api/runs/status', json: IDLE_RUN },
    { match: '/api/chat/history', json: { messages: [] } },
  ]
}

test('reaches applications from the nav and shows the reviewed one', async ({ page }) => {
  const api = await mockApi(page, routes())
  await page.goto('/')

  await page.getByRole('link', { name: 'Applications' }).click()
  await expect(page).toHaveURL('/applications')
  await expect(page.getByRole('heading', { name: 'Applications' })).toBeVisible()
  await expect(page.getByText('READY_FOR_REVIEW')).toBeVisible()
  // A reviewed application offers the single approve-and-submit decision (§89).
  await expect(page.getByRole('button', { name: /Approve & submit/ })).toBeVisible()
  expect(api.unmatched).toEqual([])
})
