// Ported from webapp/src/components/Board.test.tsx (V1's `Board` is JobsBoard).
//
// All four V1 cases carry over unchanged in intent. They are the reason the
// component's header lists three load-bearing behaviours: every lifecycle column
// must exist as a drop target, a drop on the card's own column must not POST, and
// `dragover` must preventDefault or the browser never fires `drop`.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import JobsBoard from '~/components/JobsBoard.vue'
import { card } from '../support/fixtures'
import { stubFetch } from '../support/http'

const BOARD = {
  board: {
    'Ready to apply': [card({ application_id: 1, job_id: 10, company: 'Alpha', title: 'ML' })],
    'Applied': [card({
      application_id: 2,
      job_id: 11,
      company: 'Beta',
      title: 'Sales',
      status: 'Applied',
      score: 70,
      phone_screen_pct: null,
    })],
  },
}

function routes() {
  return [
    { match: '/api/jobs/', method: 'POST', json: { ok: true } },
    { match: '/api/jobs', method: 'GET', json: BOARD },
  ]
}

describe('JobsBoard', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    // The payload cache is shared by every mount in the process; without this the
    // second test in the file would render the first one's data and skip the GET.
    clearNuxtData()
  })

  it('renders columns with cards', async () => {
    stubFetch(routes())
    const wrapper = await mountSuspended(JobsBoard)
    await flushPromises()

    expect(wrapper.text()).toContain('Alpha')
    expect(wrapper.text()).toContain('Beta')
    // Column headers present.
    expect(wrapper.findAll('h3').map(h => h.text())).toContain('Ready to apply')
  })

  it('changes status by dragging a card onto another column', async () => {
    const http = stubFetch(routes())
    const wrapper = await mountSuspended(JobsBoard)
    await flushPromises()

    const card = wrapper.findAll('.board-card').find(c => c.text().includes('Alpha'))!
    await card.trigger('dragstart')
    await wrapper.get('[data-status="Applied"]').trigger('dragover')
    await wrapper.get('[data-status="Applied"]').trigger('drop')
    await flushPromises()

    const posts = http.calls.filter(c => c.method === 'POST')
    expect(posts.map(c => c.url)).toEqual(['/api/jobs/10/status'])
    expect(http.bodyOf('/api/jobs/10/status')).toEqual({ status: 'Applied' })
  })

  it('does not POST when a card is dropped on its own column', async () => {
    const http = stubFetch(routes())
    const wrapper = await mountSuspended(JobsBoard)
    await flushPromises()

    const card = wrapper.findAll('.board-card').find(c => c.text().includes('Alpha'))!
    await card.trigger('dragstart')
    await wrapper.get('[data-status="Ready to apply"]').trigger('drop')
    await flushPromises()

    expect(http.calls.some(c => c.method === 'POST')).toBe(false)
  })

  it('renders every lifecycle status as a drop target, even empty ones', async () => {
    stubFetch(routes())
    const wrapper = await mountSuspended(JobsBoard)
    await flushPromises()

    // "Rejected" has no cards in the fixture but must still be a droppable column.
    expect(wrapper.find('[data-status="Rejected"]').exists()).toBe(true)
  })

  it('marks the hovered column and cancels the dragover default', async () => {
    // Not a V1 case: V1's jsdom fireEvent ignored the return of preventDefault, so
    // the browser requirement the component header calls out was never pinned.
    stubFetch(routes())
    const wrapper = await mountSuspended(JobsBoard)
    await flushPromises()

    const card = wrapper.findAll('.board-card').find(c => c.text().includes('Alpha'))!
    await card.trigger('dragstart')

    const column = wrapper.get('[data-status="Applied"]')
    const event = new Event('dragover', { cancelable: true })
    column.element.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)

    await flushPromises()
    expect(column.classes()).toContain('drag-over')
  })
})
