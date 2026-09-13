// The map never re-queries as the user pans — this button is the explicit request to fetch
// the current view (§20). It carries no state: the page decides when it shows and what
// "search" does; here it only emits, and shows a pending label while a search is in flight.
import { describe, expect, it } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import MapSearchAreaButton from '~/components/map/MapSearchAreaButton.vue'

describe('MapSearchAreaButton', () => {
  it('emits an explicit search request on click', async () => {
    const wrapper = await mountSuspended(MapSearchAreaButton)

    expect(wrapper.text()).toContain('Search this area')
    await wrapper.get('button').trigger('click')

    expect(wrapper.emitted('search')).toHaveLength(1)
  })

  it('shows a pending label and disables itself while searching', async () => {
    const wrapper = await mountSuspended(MapSearchAreaButton, { props: { loading: true } })

    expect(wrapper.text()).toContain('Searching…')
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
  })
})
