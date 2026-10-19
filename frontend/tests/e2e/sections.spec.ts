// A route-by-route layout regression matrix. APIs are synthetic; this does not
// certify a live provider or an employer submission.
import { expect, test } from '@playwright/test'
import { mockApi, type MockRoute } from './support/api'
import { ANALYTICS, SETTINGS, IDLE_RUN, overview, card } from './support/fixtures'
import * as f from '../nuxt/support/v2-fixtures'

const routes: MockRoute[] = [
  { match: '/api/v2/auth/session', json: f.signedIn() },
  { match: '/api/v2/onboarding', json: f.onboarding() },
  { match: '/api/v2/me/profile', json: f.profile() },
  { match: '/api/v2/me/search-profiles', json: f.searchList(f.search()) },
  { match: '/api/v2/me/evidence', json: f.evidenceList() },
  { match: '/api/v2/documents/', json: f.candidateDocument() },
  { match: '/api/v2/documents', json: f.documentList() },
  { match: '/api/v2/companies/', json: f.companyDetail() },
  { match: '/api/v2/companies', json: f.companyList() },
  { match: '/api/v2/geo/opportunities', json: f.opportunityGeoResponse() },
  { match: '/api/v2/settings/llm/connections', json: f.llmConnectionList() },
  { match: '/api/v2/me/morning-schedule', method: 'GET', json: { enabled: false, local_time: '08:00', cadence: 'daily' } },
  { match: '/api/v2/me/morning-runs/latest', method: 'GET', json: { runs: [] } },
  { match: '/api/v2/applications', json: f.applicationList() },
  { match: '/api/v2/chat/conversations', json: f.conversationList([]) },
  { match: '/api/v2/interview-sessions/history', json: f.interviewSummaryList([]) },
  { match: '/api/v2/interview-sessions', json: f.interviewSessionList([]) },
  { match: '/api/v2/career/analytics', json: f.careerAnalytics() },
  { match: '/api/v2/career/recommendations', json: f.careerRecommendationList() },
  { match: '/api/v2/career/strategy-proposals', json: f.strategyProposalList() },
  { match: '/api/v2/billing/plans', json: f.planList() },
  { match: '/api/v2/billing/subscription', json: f.subscriptionOverview() },
  { match: '/api/v2/billing/usage', json: f.usageSnapshot() },
  { match: '/api/overview', json: overview() },
  { match: '/api/runs/status', json: IDLE_RUN },
  { match: '/api/chat/history', json: { messages: [] } },
  { match: '/api/settings', json: { settings: SETTINGS, status: 'ok' } },
  { match: '/api/analytics', json: ANALYTICS },
  { match: '/api/jobs', json: { jobs: [card()] } },
]

const sections = ['/', '/jobs', '/analytics', '/settings', '/profile', '/evidence',
  '/documents', `/documents/${f.candidateDocument().id}`, '/providers', '/applications',
  '/companies', `/companies/${f.company().id}`, '/map', '/chat', '/interview',
  '/career', '/strategy', '/billing', '/terms', '/login', '/register', '/onboarding']

for (const locale of ['en', 'fr']) {
  for (const width of [320, 390, 768, 1440]) {
    for (const section of sections) {
      // One route per test makes a layout regression actionable: a single slow map
      // must not consume the timeout budget for every screen after it.
      test(`${locale} at ${width}px: ${section} renders without overflow or crashes`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      await page.context().addCookies([{ name: 'app_locale', value: locale, domain: 'localhost', path: '/' }])
      await page.route('**/tile.openstreetmap.org/**', route => route.fulfill({
        status: 200,
        contentType: 'image/png',
        body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'),
      }))
      const api = await mockApi(page, routes)
      const errors: string[] = []
      page.on('pageerror', error => errors.push(error.message))
      await page.goto(section)
      await expect(page.locator('h1').first()).toBeVisible()
      await expect(page.locator('html')).toHaveAttribute('lang', locale)
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
      expect(errors).toEqual([])
      expect(api.unmatched).toEqual([])
      })
    }
  }
}
