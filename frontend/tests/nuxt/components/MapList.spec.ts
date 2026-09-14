// The list is the complete answer; the map is only its placeable subset (§8, §9). So the
// case that matters is the one the map cannot show: a remote-only role has no point, and it
// must still be a row — tagged "No location", distance "—", never dropped and never faked to
// a coordinate. The rest pins the one-selection contract: a row is a real button, clicking
// it emits an id and nothing else, and the selected row is marked for assistive tech (§31).
import { describe, expect, it } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import MapList from '~/components/map/MapList.vue'
import { companyGeoItem, geoLocation, opportunityGeoItem } from '../support/v2-fixtures'

describe('MapList · opportunities', () => {
  const placed = opportunityGeoItem({ id: 'placed', title: 'Backend Engineer', distance_meters: 1500 })
  const remote = opportunityGeoItem({
    id: 'remote',
    title: 'Remote Engineer',
    status: 'REMOTE',
    location: null,
    remote_scope: 'REMOTE_ANYWHERE',
    distance_meters: null,
  })

  it('lists an unplaceable role too, tagged and with no distance', async () => {
    const wrapper = await mountSuspended(MapList, {
      props: { mode: 'opportunities', opportunities: [placed, remote] },
    })
    const rows = wrapper.findAll('.map-row')

    expect(rows).toHaveLength(2)
    const remoteRow = wrapper.get('[data-id="remote"]')
    expect(remoteRow.text()).toContain('No location')
    expect(remoteRow.text()).toContain('—')
    // The placed one reads a real distance, not a dash.
    expect(wrapper.get('[data-id="placed"]').text()).toContain('1.5 km')
  })

  it('emits just the id when a row is chosen — the store holds the selection', async () => {
    const wrapper = await mountSuspended(MapList, {
      props: { mode: 'opportunities', opportunities: [placed] },
    })

    await wrapper.get('[data-id="placed"]').trigger('click')

    expect(wrapper.emitted('select')).toEqual([['placed']])
  })

  it('marks the selected row with aria-current', async () => {
    const wrapper = await mountSuspended(MapList, {
      props: { mode: 'opportunities', opportunities: [placed, remote], selectedId: 'placed' },
    })

    expect(wrapper.get('[data-id="placed"]').attributes('aria-current')).toBe('true')
    expect(wrapper.get('[data-id="remote"]').attributes('aria-current')).toBeUndefined()
  })

  it('rows are real buttons — reachable from the keyboard', async () => {
    const wrapper = await mountSuspended(MapList, {
      props: { mode: 'opportunities', opportunities: [placed] },
    })
    expect(wrapper.get('[data-id="placed"]').element.tagName).toBe('BUTTON')
  })
})

describe('MapList · companies', () => {
  it('keys rows by company id and emits it, HQ marked', async () => {
    const hq = companyGeoItem({ is_headquarters: true })
    const wrapper = await mountSuspended(MapList, {
      props: { mode: 'companies', companies: [hq] },
    })

    const row = wrapper.get(`[data-id="${hq.company.id}"]`)
    expect(row.text()).toContain('Logitech')
    expect(row.text()).toContain('HQ')

    await row.trigger('click')
    expect(wrapper.emitted('select')).toEqual([[hq.company.id]])
  })

  it('tags an employer with no point as unplaced', async () => {
    const nowhere = companyGeoItem({ location: geoLocation({ point: null }), distance_meters: null })
    const wrapper = await mountSuspended(MapList, {
      props: { mode: 'companies', companies: [nowhere] },
    })

    expect(wrapper.get(`[data-id="${nowhere.company.id}"]`).text()).toContain('No location')
  })
})
