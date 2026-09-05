// The copilot end to end: a real SSE turn and a confirm-gated action.
//
// tests/nuxt/components/CopilotPanel.spec.ts mocks `streamChat`, so the transport
// itself — POST, `ReadableStream`, `TextDecoder`, `parseSseBuffer` — is never
// exercised there. Here the frames come off a real HTTP response in a real
// browser, which is completion criterion 3 (chat/streaming works). No LLM is
// involved: Playwright fulfils the request with canned frames.
//
// The frames are byte-for-byte what sse_starlette emits — CRLF separators and a
// `json.dumps`-ed payload per event (server/routes/chat.py) — so the parser's
// CRLF normalisation is covered by the same pass.
import { expect, test } from '@playwright/test'
import { ANONYMOUS_SESSION, mockApi } from './support/api'
import { IDLE_RUN, card, jobDetail, overview } from './support/fixtures'

function frame(event: string, data: unknown): string {
  return `event: ${event}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`
}

const PROPOSAL = {
  type: 'set_status',
  job_id: 7,
  args: { status: 'applied' },
  label: 'Mark Alpha as applied',
}

/** One complete turn: a session id, two tokens, an action, then the final text. */
const TURN = [
  frame('session', 'sess-1'),
  frame('token', 'Alpha '),
  frame('token', 'looks best.'),
  frame('action_proposal', PROPOSAL),
  frame('done', {
    text: '**Alpha** looks best.\n\n- 91 score\n- 88% screen',
    session_id: 'sess-1',
    mandate_ok: true,
    flags: [],
  }),
]

function routes(sse: string[] = TURN) {
  return [
    { match: '/api/chat/history', method: 'GET', json: { messages: [] } },
    { match: '/api/chat', method: 'POST', sse },
    { match: '/api/jobs/7/status', method: 'POST', status: 202, json: { ok: true } },
    { match: '/api/jobs/10', method: 'GET', json: jobDetail() },
    { match: '/api/jobs', method: 'GET', json: { board: { 'Ready to apply': [card()] } } },
    { match: '/api/overview', json: overview() },
    { match: '/api/runs/status', json: IDLE_RUN },
    ANONYMOUS_SESSION,
  ]
}

test('streams a turn into the global dock and renders it as Markdown', async ({ page }) => {
  const api = await mockApi(page, routes())
  await page.goto('/')

  await page.getByRole('button', { name: /copilot/i }).click()
  const panel = page.locator('section[aria-label="Copilot"]')

  await panel.getByLabel('Message copilot').fill('Which job first?')
  await panel.getByRole('button', { name: 'Ask' }).click()

  // The `done` text replaces the token preview — the tokens are pre-gate prose and
  // must never be what is left on screen.
  const reply = panel.locator('.copilot-msg.assistant').first()
  await expect(reply.locator('strong')).toHaveText('Alpha')
  await expect(reply.locator('li')).toHaveText(['91 score', '88% screen'])
  await expect(panel.locator('.copilot-msg.preview')).toHaveCount(0)
  // Markdown was rendered, not printed.
  await expect(panel).not.toContainText('**')

  const posts = api.calls.filter(c => c.method === 'POST' && c.url.startsWith('/api/chat'))
  expect(posts).toHaveLength(1)
  expect(JSON.parse(posts[0]!.body!)).toEqual({
    message: 'Which job first?',
    scope: 'global',
    scope_id: 0,
  })
  expect(api.unmatched).toEqual([])
})

test('confirms a proposed action and posts it to the typed endpoint', async ({ page }) => {
  const api = await mockApi(page, routes())
  await page.goto('/')

  await page.getByRole('button', { name: /copilot/i }).click()
  const panel = page.locator('section[aria-label="Copilot"]')
  await panel.getByLabel('Message copilot').fill('Alpha is done, mark it')
  await panel.getByRole('button', { name: 'Ask' }).click()

  const action = panel.getByRole('group', { name: 'Proposed action' })
  await expect(action).toContainText('Mark Alpha as applied')
  // The proposal is a suggestion: nothing has changed state yet.
  expect(api.callsTo('/api/jobs/7/status')).toEqual([])

  await action.getByRole('button', { name: 'Confirm' }).click()

  await expect.poll(() => api.callsTo('/api/jobs/7/status').length).toBe(1)
  expect(JSON.parse(api.callsTo('/api/jobs/7/status')[0]!.body!)).toEqual({ status: 'applied' })
  await expect(action).toHaveCount(0)
  await expect(panel).toContainText('✓ Mark Alpha as applied — done.')
  expect(api.unmatched).toEqual([])
})

test('scopes the drawer copilot to the open job', async ({ page }) => {
  // The drawer passes scope="job" + scope-id, so both the history GET and the turn
  // POST have to carry the job id — a global thread leaking into a job tab (or the
  // reverse) is the bug this guards.
  const api = await mockApi(page, routes())
  await page.goto('/jobs')

  await page.locator('[data-status="Ready to apply"] .board-card').click()
  const drawer = page.getByRole('dialog', { name: 'Job detail' })
  await drawer.getByRole('button', { name: 'Copilot' }).click()

  await expect.poll(() => api.callsTo('/api/chat/history').map(c => c.url)).toEqual([
    '/api/chat/history?scope=job&scope_id=10',
  ])

  await drawer.getByLabel('Message copilot').fill('Anything missing?')
  await drawer.getByRole('button', { name: 'Ask' }).click()

  await expect(drawer.locator('.copilot-msg.assistant').first()).toContainText('Alpha looks best.')
  const post = api.calls.find(c => c.method === 'POST' && c.url.startsWith('/api/chat'))!
  expect(JSON.parse(post.body!)).toEqual({
    message: 'Anything missing?',
    scope: 'job',
    scope_id: 10,
  })
  expect(api.unmatched).toEqual([])
})
