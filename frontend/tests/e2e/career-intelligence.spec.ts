import { expect, test } from '@playwright/test'
import { mockApi } from './support/api'
import type { MockRoute } from './support/api'
import * as f from '../nuxt/support/v2-fixtures'

const recommendation = f.careerRecommendation()
const createdProposal = f.strategyProposal({ source_recommendation_id: recommendation.id })

function routes(...first: MockRoute[]): MockRoute[] {
  return [
    ...first,
    { match: '/api/v2/auth/session', method: 'GET', json: f.signedIn() },
    { match: '/api/v2/me/search-profiles', method: 'GET', json: f.searchList(f.search({
      search: f.searchDraft({ title_keywords: ['backend'], excluded_keywords: ['senior'] }),
    })) },
    { match: '/api/v2/career/strategy-proposals/history', method: 'GET', json: f.strategyProposalList([]) },
    { match: '/api/v2/career/strategy-proposals', method: 'GET', json: f.strategyProposalList([]) },
    { match: '/api/v2/career/analytics', method: 'GET', json: f.careerAnalytics() },
    { match: '/api/v2/career/recommendations', method: 'GET', json: f.careerRecommendationList([recommendation]) },
  ]
}

test('turns advice into a reviewable proposal without applying it', async ({ page }) => {
  const api = await mockApi(page, routes(
    { match: '/api/v2/career/strategy-proposals', method: 'POST', status: 201, json: createdProposal },
  ))
  await page.goto('/career')

  await page.getByRole('link', { name: /Create a proposal|Créer une proposition/ }).click()
  await expect(page).toHaveURL(new RegExp(`/strategy\\?recommendation=${recommendation.id}`))
  await page.getByRole('textbox', { name: /Title keywords|Mots-clés du titre/ }).fill('backend\nplatform engineer')
  await page.getByRole('textbox', { name: /Review summary|Résumé à vérifier/ }).fill('Add platform roles to this search.')
  await page.getByRole('button', { name: /Create proposal|Créer la proposition/ }).click()

  await expect(page.locator('[data-test="proposal-created"]')).toBeVisible()
  const proposals = api.callsTo('/api/v2/career/strategy-proposals').filter(call => call.method === 'POST')
  expect(proposals).toHaveLength(1)
  expect(JSON.parse(proposals[0]!.body ?? '{}')).toMatchObject({
    source_recommendation_id: recommendation.id,
    change: { kind: 'SET_SEARCH_KEYWORDS' },
  })
  expect(api.callsTo('/approve')).toHaveLength(0)
  expect(api.unmatched).toEqual([])
})

test('explains an evidence-backed empty recommendation result', async ({ page }) => {
  await mockApi(page, routes(
    { match: '/api/v2/career/recommendations', method: 'POST', status: 201, json: f.careerRecommendationList([]) },
  ))
  await page.goto('/career')

  await page.getByRole('button', { name: /Generate recommendations|Générer des recommandations/ }).click()
  await expect(page.locator('[data-test="generation-result"]')).toContainText(
    /No recommendation is supported|Aucune recommandation n’est étayée/,
  )
})

test('keeps the strategy builder usable on a narrow phone', async ({ page }) => {
  await mockApi(page, routes())
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/strategy?recommendation=${recommendation.id}`)

  await expect(page.locator('[data-test="proposal-builder"]')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  const heights = await page.locator('[data-test="proposal-form"] button, [data-test="proposal-form"] input, [data-test="proposal-form"] textarea').evaluateAll(elements =>
    elements.filter(element => element.getClientRects().length > 0).map(element => element.getBoundingClientRect().height))
  expect(heights.length).toBeGreaterThan(0)
  expect(Math.min(...heights)).toBeGreaterThanOrEqual(44)
})
