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
import { flushPromises } from '@vue/test-utils'
import MapFilters from '~/components/map/MapFilters.vue'
import { defaultMapFilterForm } from '~/types/map'

/** Selects an option from Nuxt UI's portal-rendered select menu. */
async function selectOption(wrapper: Awaited<ReturnType<typeof mountSuspended>>, label: string, value: string) {
  const trigger = wrapper.get(`[aria-label="${label}"]`)
  const contentId = trigger.attributes('aria-controls')
  await trigger.trigger('click')
  await flushPromises()
  const content = contentId ? document.getElementById(contentId) : null
  const option = [...(content?.querySelectorAll<HTMLElement>('[role="option"]') ?? [])]
    .find(item => item.textContent?.trim() === value)
  if (!option) throw new Error(`option ${value} was not rendered for ${label}`)
  option.dispatchEvent(new MouseEvent('pointerup', { bubbles: true, button: 0 }))
  await flushPromises()
}

describe('MapFilters', () => {
  async function openFilters(wrapper: Awaited<ReturnType<typeof mountSuspended>>) {
    await wrapper.get('.map-filters-toggle').trigger('click')
    await flushPromises()
  }

  it('offers role-shaped controls for opportunities', async () => {
    const wrapper = await mountSuspended(MapFilters, {
      props: { mode: 'opportunities', modelValue: defaultMapFilterForm() },
    })

    expect(wrapper.get('.map-filters-toggle').attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('#map-filters-panel').exists()).toBe(false)
    await openFilters(wrapper)
    expect(wrapper.find('[aria-label="Opportunity type"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="Workplace"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="Remote policy"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="Country"]').exists()).toBe(true)
    expect(wrapper.get('[aria-label="Country"]').element.tagName).toBe('SELECT')
  })

  it('hides the role controls in the companies view — an employer has no contract type', async () => {
    const wrapper = await mountSuspended(MapFilters, {
      props: { mode: 'companies', modelValue: defaultMapFilterForm() },
    })

    await openFilters(wrapper)
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
    await openFilters(off)
    expect(off.find('[aria-label="Radius in kilometres"]').exists()).toBe(false)

    const on = await mountSuspended(MapFilters, {
      props: {
        mode: 'opportunities',
        modelValue: { ...defaultMapFilterForm(), radiusEnabled: true },
      },
    })
    await openFilters(on)
    expect(on.find('[aria-label="Radius in kilometres"]').exists()).toBe(true)
  })

  it('writes selections back into the bound form', async () => {
    const form = reactive(defaultMapFilterForm())
    const wrapper = await mountSuspended(MapFilters, {
      props: { mode: 'opportunities', modelValue: form },
    })

    await openFilters(wrapper)
    await selectOption(wrapper, 'Opportunity type', 'Full-time')
    await wrapper.get('[aria-label="Country"]').setValue('CH')

    expect(form.opportunityType).toBe('FULL_TIME')
    expect(form.country).toBe('CH')
  })

  it('commits on Apply, not on every keystroke', async () => {
    const wrapper = await mountSuspended(MapFilters, {
      props: { mode: 'opportunities', modelValue: defaultMapFilterForm() },
    })

    await openFilters(wrapper)
    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('apply')).toHaveLength(1)
    expect(wrapper.get('.map-filters-toggle').attributes('aria-expanded')).toBe('false')
  })
})
