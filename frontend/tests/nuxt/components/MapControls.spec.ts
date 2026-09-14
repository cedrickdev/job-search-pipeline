// The mode switch is a two-way binding on one store field, and that is the whole test: a
// click asks the parent to change `mode`, and the control reflects whatever comes back
// (§10, §12). It holds no state of its own, so there is nothing else to pin.
import { describe, expect, it } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import MapControls from '~/components/map/MapControls.vue'

describe('MapControls', () => {
  it('marks the active view for assistive tech', async () => {
    const wrapper = await mountSuspended(MapControls, { props: { modelValue: 'opportunities' } })
    const tabs = wrapper.findAll('[role="tab"]')

    expect(tabs.map(t => t.text())).toEqual(['Opportunities', 'Companies'])
    expect(tabs[0]!.attributes('aria-selected')).toBe('true')
    expect(tabs[1]!.attributes('aria-selected')).toBe('false')
  })

  it('asks the parent to switch — it does not decide for itself', async () => {
    const wrapper = await mountSuspended(MapControls, { props: { modelValue: 'opportunities' } })

    await wrapper.findAll('[role="tab"]')[1]!.trigger('click')

    expect(wrapper.emitted('update:modelValue')).toEqual([['companies']])
  })
})
