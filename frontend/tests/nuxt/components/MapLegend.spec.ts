// The legend explains the colours and, in the one editorial line, that remote-only and
// unresolved roles are real results in the list rather than omissions (§8, §9). It must
// never imply a fit — no scores here or anywhere on the screen (§42). The wording of the
// "approximate" swatch differs by mode: a company office versus a coarse area.
import { describe, expect, it } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import MapLegend from '~/components/map/MapLegend.vue'

describe('MapLegend', () => {
  it('explains the pins and points to the list for the ones without a pin', async () => {
    const wrapper = await mountSuspended(MapLegend, { props: { mode: 'opportunities' } })

    expect(wrapper.text()).toContain('Located')
    expect(wrapper.text()).toContain('Approximate — company office')
    expect(wrapper.text()).toContain('Cluster')
    expect(wrapper.text()).toContain('Remote-only and unresolved roles have no pin')
    // Never a fit score (§42).
    expect(wrapper.text()).not.toMatch(/match|score/i)
  })

  it('drops the roles note and rewords "approximate" for employers', async () => {
    const wrapper = await mountSuspended(MapLegend, { props: { mode: 'companies' } })

    expect(wrapper.text()).toContain('Approximate area')
    expect(wrapper.text()).not.toContain('have no pin')
  })
})
