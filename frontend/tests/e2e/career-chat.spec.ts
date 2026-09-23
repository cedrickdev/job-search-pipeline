// The career-chat control plane in a real browser: reachable from the nav, the thread and
// its proposals on show, and the page's one side effect proven where only a browser can —
// a *confirmed* NAVIGATE actually changes the route.
//
// The unit suite (tests/nuxt/pages/chat.spec.ts) mounts this page against a stubbed
// `fetch` and asserts `navigateTo` was called; here the navigation is real, so this is the
// proof that a confirmed action moves the app and — the phase's whole point — that prose
// and unconfirmed proposals do not. Nothing leaves the machine: every `/api/**` request is
// answered from the table below, so no live board or LLM is reached (CLAUDE.md §Testing).
import { expect, test } from '@playwright/test'
import { mockApi } from './support/api'
import { IDLE_RUN, overview } from './support/fixtures'
import type { MockRoute } from './support/api'

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

const CONVERSATION_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const MESSAGE_ID = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
const PROPOSAL_ID = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'

const CONVERSATION = {
  id: CONVERSATION_ID,
  title: 'Backend roles in Lausanne',
  is_archived: false,
  last_message_at: '2026-03-01T10:05:00Z',
  created_at: '2026-03-01T10:00:00Z',
  updated_at: '2026-03-01T10:05:00Z',
}
const USER_MESSAGE = {
  id: MESSAGE_ID,
  conversation_id: CONVERSATION_ID,
  role: 'USER',
  content: 'Where should I focus my applications?',
  sequence: 1,
  llm_run_id: null,
  provider_key: null,
  created_at: '2026-03-01T10:00:00Z',
}

/** A proposal whose typed action navigates to a route the client owns. */
function navigateProposal(target: string) {
  return {
    id: PROPOSAL_ID,
    conversation_id: CONVERSATION_ID,
    message_id: MESSAGE_ID,
    ordinal: 0,
    status: 'PROPOSED',
    summary: 'This just opens a page for you.',
    action: { kind: 'NAVIGATE', target, opportunity_id: null },
    created_at: '2026-03-01T10:05:00Z',
    updated_at: '2026-03-01T10:05:00Z',
  }
}

const EXECUTION_SUCCEEDED = {
  id: 'ffffffff-ffff-4fff-8fff-ffffffffffff',
  proposal_id: PROPOSAL_ID,
  outcome: 'SUCCEEDED',
  detail: null,
  result_ref: null,
  created_at: '2026-03-01T10:06:00Z',
}

/**
 * A signed-in account, the chat reads for one thread, and the V1 shell reads the app makes
 * on any screen — an unstubbed one would land in `unmatched` and fail the hermeticity
 * check. The message/proposal reads precede the bare conversations route because every URL
 * under `/conversations` contains that substring; first match wins.
 */
function routes(...first: MockRoute[]): MockRoute[] {
  return [
    ...first,
    { match: '/api/v2/auth/session', method: 'GET', json: { account: ACCOUNT, session: SESSION_WINDOW } },
    { match: '/messages', method: 'GET', json: { messages: [USER_MESSAGE] } },
    { match: '/proposals', method: 'GET', json: { proposals: [navigateProposal('APPLICATIONS')] } },
    { match: '/api/v2/chat/conversations', method: 'GET', json: { conversations: [CONVERSATION] } },
    { match: '/api/overview', json: overview() },
    { match: '/api/runs/status', json: IDLE_RUN },
    { match: '/api/chat/history', json: { messages: [] } },
  ]
}

test('reaches career chat from the nav and shows the thread and its proposal', async ({ page }) => {
  const api = await mockApi(page, routes())
  await page.goto('/')

  await page.getByRole('link', { name: 'Career Chat' }).click()
  await expect(page).toHaveURL('/chat')
  await expect(page.getByRole('heading', { name: 'Career chat' })).toBeVisible()
  await expect(page.getByText('Where should I focus my applications?')).toBeVisible()
  // The card's label is derived from the typed action, not from the model's summary prose.
  await expect(page.getByText('Go to your applications')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Confirm' })).toBeVisible()
  expect(api.unmatched).toEqual([])
})

test('follows a confirmed NAVIGATE to the route it owns', async ({ page }) => {
  await mockApi(page, routes(
    { match: '/confirm', method: 'POST', json: EXECUTION_SUCCEEDED },
    // The destination the confirmed NAVIGATE lands on — its own screen's read.
    { match: '/api/v2/applications', method: 'GET', json: { applications: [] } },
  ))
  await page.goto('/chat')

  await page.getByRole('button', { name: 'Confirm' }).click()

  // The page's one side effect, in a real browser: the confirmed action moves the app.
  await expect(page).toHaveURL('/applications')
  await expect(page.getByRole('heading', { name: 'Applications' })).toBeVisible()
})
