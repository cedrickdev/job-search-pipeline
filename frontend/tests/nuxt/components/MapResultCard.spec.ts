// The card says where a thing is and how to reach it — never how good a fit it is (§42), so
// the load-bearing negative assertion is that no match score is ever rendered. The rest are
// the phase's wording rules made visible: a company fallback says it is approximate and the
// pin is the office (§7), a company card links to the existing employer page (§45), and the
// posting opens in a new tab.
import { describe, expect, it } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import MapResultCard from '~/components/map/MapResultCard.vue'
import { companyGeoItem, geoLocation, opportunityGeoItem } from '../support/v2-fixtures'

const COMPANY_ID = '44444444-4444-4444-8444-444444444444'

describe('MapResultCard · opportunity', () => {
  it('states the facts and opens the posting in a new tab, no match score anywhere', async () => {
    const wrapper = await mountSuspended(MapResultCard, {
      props: { opportunity: opportunityGeoItem({ remote_scope: 'HYBRID' }) },
    })

    expect(wrapper.get('.map-card-title').text()).toBe('Backend Engineer')
    expect(wrapper.text()).toContain('Located')
    expect(wrapper.text()).toContain('Hybrid')
    // The employer name links to the existing company page, not a reinvented view (§45).
    expect(wrapper.get(`a[href="/companies/${COMPANY_ID}"]`).text()).toBe('Logitech')

    const apply = wrapper.get('.map-card-apply')
    expect(apply.attributes('href')).toBe('https://boards.greenhouse.invalid/logitech/backend')
    expect(apply.attributes('target')).toBe('_blank')
    expect(apply.attributes('rel')).toContain('noopener')

    // No fabricated fit: the screen shows location, never a score (§42).
    expect(wrapper.text()).not.toMatch(/match|score|% fit|fit score/i)
  })

  it('says a company fallback is approximate, in plain words (§7)', async () => {
    const wrapper = await mountSuspended(MapResultCard, {
      props: { opportunity: opportunityGeoItem({ status: 'COMPANY_FALLBACK', location: geoLocation() }) },
    })

    expect(wrapper.text()).toContain('Approximate — company office')
    expect(wrapper.get('.map-card-note').text())
      .toContain('the pin is the company office, not the role')
  })

  it('shows a dash for a remote role that has no distance (§9)', async () => {
    const wrapper = await mountSuspended(MapResultCard, {
      props: {
        opportunity: opportunityGeoItem({
          status: 'REMOTE',
          location: null,
          remote_scope: 'REMOTE_ANYWHERE',
          distance_meters: null,
        }),
      },
    })
    expect(wrapper.text()).toContain('Remote — anywhere')
    expect(wrapper.text()).toContain('—')
  })

  it('closes on request rather than owning its own visibility', async () => {
    const wrapper = await mountSuspended(MapResultCard, {
      props: { opportunity: opportunityGeoItem() },
    })
    await wrapper.get('.map-card-close').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})

describe('MapResultCard · company', () => {
  it('links to the employer page and offers a way through to it (§45)', async () => {
    const wrapper = await mountSuspended(MapResultCard, {
      props: { company: companyGeoItem() },
    })

    const links = wrapper.findAll(`a[href="/companies/${COMPANY_ID}"]`)
    expect(links.length).toBeGreaterThanOrEqual(1)
    expect(wrapper.text()).toContain('View company')
    expect(wrapper.text()).toContain('Headquarters')
    expect(wrapper.text()).not.toMatch(/match|score/i)
  })
})
