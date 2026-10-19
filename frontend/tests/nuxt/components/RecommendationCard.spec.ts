import { describe, expect, it } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import RecommendationCard from '~/components/RecommendationCard.vue'
import { careerRecommendation } from '../support/v2-fixtures'

describe('RecommendationCard', () => {
  it('offers a separate review step instead of applying advice directly', async () => {
    const recommendation = careerRecommendation()
    const wrapper = await mountSuspended(RecommendationCard, {
      props: { recommendation },
    })

    const action = wrapper.get('[data-test="create-proposal"]')
    expect(action.attributes('href')).toBe(`/strategy?recommendation=${recommendation.id}`)
    expect(wrapper.find('button').exists()).toBe(false)
  })

  it('translates card chrome into French', async () => {
    document.cookie = 'app_locale=fr; path=/'
    const wrapper = await mountSuspended(RecommendationCard, {
      props: { recommendation: careerRecommendation() },
    })

    expect(wrapper.text()).toContain('confiance élevée')
    expect(wrapper.text()).toContain('Créer une proposition')
  })
})
