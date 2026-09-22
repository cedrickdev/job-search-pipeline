// The applications screen: the list, its per-state actions and the audit trail.
//
// Asserted the way a user meets the engine's rules: a planned application offers
// "Prepare", a reviewed one offers the single "Approve & submit" decision (§89), and
// a submitted one offers neither — the UI shows the one next step the state allows and
// never a control that would 409.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import ApplicationsPage from '~/pages/applications.vue'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import { application, applicationList, signedIn } from '../support/v2-fixtures'
import type { Route } from '../support/http'
import type { ApplicationList } from '~/types/v2'

const APPLICATIONS = '/api/v2/applications'
const APPLICATION_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'

function routes(list: ApplicationList, extra: Route[] = []): Route[] {
  return [
    ...extra,
    { match: APPLICATIONS, method: 'GET', json: list },
    { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
  ]
}

async function mountWith(list: ApplicationList, extra: Route[] = []) {
  const http = stubFetch(routes(list, extra))
  await useSessionStore().ensure()
  const wrapper = await mountSuspended(ApplicationsPage, { route: '/applications' })
  await flushPromises()
  return { http, wrapper }
}

describe('applications page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('tells an empty account there is nothing yet', async () => {
    const { wrapper } = await mountWith(applicationList([]))
    expect(wrapper.text()).toContain('No applications yet')
  })

  it('offers Prepare for a planned application', async () => {
    const { wrapper } = await mountWith(
      applicationList([application({ state: 'PLANNED' })]))
    const labels = wrapper.findAll('button').map(b => b.text())
    expect(labels.some(t => t.includes('Prepare'))).toBe(true)
    expect(labels.some(t => t.includes('Approve'))).toBe(false)
  })

  it('offers Approve & submit for a reviewed application', async () => {
    const { wrapper } = await mountWith(
      applicationList([application({ state: 'READY_FOR_REVIEW' })]))
    const labels = wrapper.findAll('button').map(b => b.text())
    expect(labels.some(t => t.includes('Approve'))).toBe(true)
  })

  it('offers no lifecycle action once submitted', async () => {
    const { wrapper } = await mountWith(
      applicationList([application({ state: 'SUBMITTED' })]))
    const labels = wrapper.findAll('button').map(b => b.text())
    expect(labels.some(t => t.includes('Prepare'))).toBe(false)
    expect(labels.some(t => t.includes('Submit'))).toBe(false)
    expect(labels.some(t => t.includes('Cancel'))).toBe(false)
  })

  it('prepares a planned application on click', async () => {
    const prepared = application({ state: 'REQUIRES_HUMAN' })
    const { http, wrapper } = await mountWith(
      applicationList([application({ state: 'PLANNED' })]),
      [{ match: `${APPLICATIONS}/${APPLICATION_ID}/prepare`, method: 'POST',
         json: prepared }])
    const prepare = wrapper.findAll('button').find(b => b.text().includes('Prepare'))
    await prepare!.trigger('click')
    await flushPromises()
    expect(http.callsTo(`${APPLICATIONS}/${APPLICATION_ID}/prepare`)
      .filter(c => c.method === 'POST')).toHaveLength(1)
  })
})
