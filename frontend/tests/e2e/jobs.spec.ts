// The jobs route end to end: board, table, drawer, and one state change through
// the confirmation gate.
//
// This is the flow the unit specs can only cover in pieces — JobsBoard, JobsTable
// and JobDrawer each mount alone there, and nothing asserts that the page wires
// their `open` events to the drawer or that an action reaches the API from a real
// click. Completion criterion 4 (application actions work) is this file.
import { expect, test } from '@playwright/test'
import { mockApi } from './support/api'
import { IDLE_RUN, card, jobDetail, overview } from './support/fixtures'

const JOBS = {
  board: { 'Ready to apply': [card()], 'Applied': [card({ application_id: 2, job_id: 11, company: 'Beta', status: 'Applied' })] },
  items: [card(), card({ application_id: 2, job_id: 11, company: 'Beta', status: 'Applied' })],
}

// Most specific first: the action POST, then the detail GET, then the list GET —
// `/api/jobs?view=board` does not contain `/api/jobs/10`, so the two never cross.
const ROUTES = [
  { match: '/api/jobs/10/go', method: 'POST', status: 202, json: { ok: true } },
  { match: '/api/jobs/10', method: 'GET', json: jobDetail() },
  { match: '/api/jobs', method: 'GET', json: JOBS },
  { match: '/api/overview', json: overview() },
  { match: '/api/runs/status', json: IDLE_RUN },
  { match: '/api/chat/history', json: { messages: [] } },
]

test('opens a job from the board and approves it through the confirmation gate', async ({ page }) => {
  const api = await mockApi(page, ROUTES)
  await page.goto('/jobs')

  // Every lifecycle column renders, including the empty ones, so any status stays
  // reachable by drag.
  await expect(page.locator('.board-col')).toHaveCount(16)
  const ready = page.locator('[data-status="Ready to apply"]')
  await expect(ready.locator('.board-card')).toHaveCount(1)

  await ready.locator('.board-card').click()

  const drawer = page.getByRole('dialog', { name: 'Job detail' })
  await expect(drawer.getByRole('heading', { name: 'Alpha' })).toBeVisible()
  await expect(drawer).toContainText('Shift Lead')
  await expect(drawer).toContainText('Strong match')

  await drawer.getByRole('button', { name: 'Approve' }).click()
  const gate = page.getByRole('alertdialog', { name: 'Confirm action' })
  await expect(gate).toContainText('Approve Alpha')
  // Still nothing sent: the gate is a gate.
  expect(api.calls.filter(c => c.method === 'POST')).toEqual([])

  await gate.getByRole('button', { name: 'Confirm' }).click()
  await expect(gate).toHaveCount(0)
  await expect.poll(() => api.callsTo('/api/jobs/10/go').length).toBe(1)
  expect(api.unmatched).toEqual([])
})

test('switches to the table view and opens the same job', async ({ page }) => {
  await mockApi(page, ROUTES)
  await page.goto('/jobs')

  await page.getByRole('button', { name: 'Table' }).click()
  const rows = page.locator('tbody tr')
  await expect(rows).toHaveCount(2)

  await rows.first().click()
  await expect(page.getByRole('dialog', { name: 'Job detail' })).toBeVisible()
})

test('filters the board by track without a second request', async ({ page }) => {
  // The track filter is client-side so the board and the table share one cache
  // entry; a `track` query parameter would split it.
  const api = await mockApi(page, ROUTES)
  await page.goto('/jobs')

  // Count only once the board has rendered, so the first request is certainly in.
  await expect(page.locator('.board-col')).toHaveCount(16)
  const before = api.callsTo('/api/jobs').length
  await page.getByRole('tab', { name: /Travail/ }).click()

  // No card in the fixture is on the `travail` track, so the pipeline reads empty.
  await expect(page.getByText('Rien dans ce pipeline pour l\'instant.')).toBeVisible()
  expect(api.callsTo('/api/jobs')).toHaveLength(before)
  expect(api.calls.every(c => !c.url.includes('track'))).toBe(true)
})

test('closes the drawer from the backdrop', async ({ page }) => {
  await mockApi(page, ROUTES)
  await page.goto('/jobs')

  await page.locator('[data-status="Ready to apply"] .board-card').click()
  const drawer = page.getByRole('dialog', { name: 'Job detail' })
  await expect(drawer).toBeVisible()

  await page.locator('.drawer-backdrop').click({ position: { x: 5, y: 5 } })
  await expect(drawer).toHaveCount(0)
})
