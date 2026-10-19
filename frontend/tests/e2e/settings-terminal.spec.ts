import { expect, test } from '@playwright/test'
import { mockApi } from './support/api'
import { IDLE_RUN, SETTINGS } from './support/fixtures'
import { signedIn } from '../nuxt/support/v2-fixtures'

const session = {
  id: '11111111-1111-4111-8111-111111111111',
  state: 'ACTIVE',
  created_at: '2026-10-08T20:00:00Z',
  expires_at: '2026-10-08T21:00:00Z',
  ended_at: null,
  safe_error_code: null,
  stream_url: '/api/v2/settings/claude-terminal/sessions/11111111-1111-4111-8111-111111111111/stream',
}

test('starts and stops the responsive settings terminal without a host launcher', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.routeWebSocket('**/api/v2/settings/claude-terminal/sessions/*/stream', ws => {
    ws.onMessage(() => {})
  })
  const api = await mockApi(page, [
    { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
    { match: '/api/settings', method: 'GET', json: { settings: SETTINGS, status: 'ok' } },
    { match: '/api/runs/status', method: 'GET', json: IDLE_RUN },
    { match: '/api/v2/settings/llm/connections', method: 'GET', json: { connections: [] } },
    { match: '/api/v2/me/morning-schedule', method: 'GET', json: { enabled: false, local_time: '08:00', cadence: 'daily' } },
    { match: '/api/v2/me/morning-runs/latest', method: 'GET', json: { runs: [] } },
    { match: '/api/v2/settings/claude-terminal/sessions', method: 'POST', json: session },
    { match: '/api/v2/settings/claude-terminal/sessions/', method: 'DELETE', json: { ...session, state: 'TERMINATED', ended_at: '2026-10-08T20:01:00Z' } },
  ])

  await page.goto('/settings')
  await page.getByRole('button', { name: 'Open Claude Code terminal' }).click()

  await expect(page.getByRole('region', { name: 'Claude Code terminal' })).toBeVisible()
  await page.getByRole('button', { name: 'Stop terminal' }).click()
  await expect(page.getByRole('region', { name: 'Claude Code terminal' })).toHaveCount(0)
  expect(api.callsTo('/api/v2/settings/llm/claude-code/terminal')).toHaveLength(0)
  expect(api.unmatched).toEqual([])
})
