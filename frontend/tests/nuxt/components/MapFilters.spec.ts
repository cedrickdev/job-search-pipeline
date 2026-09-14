// Every control here is a parameter the geo API already takes (§23); the form never filters
// client-side (§34). So the tests are about which controls the mode offers and that the
// form commits on Apply rather than on every keystroke — not about any filtering, because
// there is none to do here.
//
// Role-shaped controls (type, workplace, remote policy) belong to opportunities only; a
// radius is offered in both but its distance box appears only once the radius is switched on.
import { describe, expect, it } from 'vitest'
import { reactive } from 'vue'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import MapFilters from '~/components/map/MapFilters.vue'
import { defaultMapFilterForm } from '~/types/map'

describe('MapFilters', () => {
  it('offers role-shaped controls for opportunities', async () => {
    const wrapper = await mountSuspended(MapFilters, {
      props: { mode: 'opportunities', modelValue: defaultMapFilterForm() },
    })

    expect(wrapper.find('[aria-label="Opportunity type"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="Workplace"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="Remote policy"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="Country"]').exists()).toBe(true)
  })

  it('hides the role controls in the companies view — an employer has no contract type', async () => {
    const wrapper = await mountSuspended(MapFilters, {
      props: { mode: 'companies', modelValue: defaultMapFilterForm() },
    })

    expect(wrapper.find('[aria-label="Opportunity type"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="Workplace"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="Remote policy"]').exists()).toBe(false)
    // The country and radius are shared, so they stay.
    expect(wrapper.find('[aria-label="Country"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="Limit to radius"]').exists()).toBe(true)
  })

  it('reveals the distance box only once a radius is switched on', async () => {
    const off = await mountSuspended(MapFilters, {
      props: { mode: 'opportunities', modelValue: defaultMapFilterForm() },
    })
    expect(off.find('[aria-label="Radius in kilometres"]').exists()).toBe(false)

    const on = await mountSuspended(MapFilters, {
      props: {
        mode: 'opportunities',
        modelValue: { ...defaultMapFilterForm(), radiusEnabled: true },
      },
    })
    expect(on.find('[aria-label="Radius in kilometres"]').exists()).toBe(true)
  })

  it('writes selections back into the bound form', async () => {
    const form = reactive(defaultMapFilterForm())
    const wrapper = await mountSuspended(MapFilters, {
      props: { mode: 'opportunities', modelValue: form },
    })

    await wrapper.get('[aria-label="Opportunity type"]').setValue('FULL_TIME')
    await wrapper.get('[aria-label="Country"]').setValue('ch')

    expect(form.opportunityType).toBe('FULL_TIME')
    expect(form.country).toBe('ch')
  })

  it('commits on Apply, not on every keystroke', async () => {
    const wrapper = await mountSuspended(MapFilters, {
      props: { mode: 'opportunities', modelValue: defaultMapFilterForm() },
    })

    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('apply')).toHaveLength(1)
  })
})
