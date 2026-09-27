// One application's outcome timeline: the "observe" link, and the separation it enforces.
//
// The load-bearing assertion is what this component cannot do. A recruiter's "no" is
// recorded as a REJECTED outcome and must never reach into the Phase 12 execution
// lifecycle — so the timeline never calls prepare/submit/cancel, and a superseded or
// retracted row stays visible rather than being deleted. The rest pins the three writes:
// record opens a milestone, correct supersedes one, retract flips one, and only an
// effective outcome offers the last two.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import OutcomeTimeline from '~/components/OutcomeTimeline.vue'
import { stubFetch } from '../support/http'
import { applicationOutcome, applicationOutcomeList } from '../support/v2-fixtures'
import type { Route } from '../support/http'
import type { ApplicationOutcomeList } from '~/types/v2'

const APPLICATION_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const OUTCOME_ID = '6f6f6f6f-6f6f-4f6f-8f6f-6f6f6f6f6f6f'
const OUTCOMES = `/api/v2/applications/${APPLICATION_ID}/outcomes`

async function mountWith(list: ApplicationOutcomeList, extra: Route[] = []) {
  const http = stubFetch([
    ...extra,
    { match: OUTCOMES, method: 'GET', json: list },
  ])
  const wrapper = await mountSuspended(OutcomeTimeline, {
    props: { applicationId: APPLICATION_ID },
  })
  await flushPromises()
  return { http, wrapper }
}

describe('OutcomeTimeline', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('tells an application with no outcomes that none are recorded', async () => {
    const { wrapper } = await mountWith(applicationOutcomeList([]))
    expect(wrapper.text()).toContain('No outcomes yet')
  })

  it('shows a rejection as an outcome and never touches the lifecycle', async () => {
    const rejected = applicationOutcome({ kind: 'REJECTED', detail: 'Position filled internally.' })
    const { http, wrapper } = await mountWith(applicationOutcomeList([rejected]))
    expect(wrapper.text().toLowerCase()).toContain('rejected')
    // The separation invariant: recording an outcome moves no execution state.
    expect(http.called('/prepare')).toBe(false)
    expect(http.called('/submit')).toBe(false)
    expect(http.called('/cancel')).toBe(false)
  })

  it('records a new milestone with the manual source', async () => {
    const { http, wrapper } = await mountWith(applicationOutcomeList([]), [
      { match: OUTCOMES, method: 'POST', json: applicationOutcome({ kind: 'SCREEN' }) },
    ])
    await wrapper.get('[data-test="record-outcome"]').trigger('submit')
    await flushPromises()

    const posts = http.callsTo(OUTCOMES).filter(c => c.method === 'POST')
    expect(posts).toHaveLength(1)
    const body = JSON.parse(posts[0]!.init!.body as string)
    expect(body.source).toBe('MANUAL_USER')
    expect(body.kind).toBe('ACKNOWLEDGED')
  })

  it('supersedes an effective outcome through the correction form', async () => {
    const { http, wrapper } = await mountWith(applicationOutcomeList([applicationOutcome()]), [
      {
        match: `/outcomes/${OUTCOME_ID}/correct`,
        method: 'POST',
        json: applicationOutcome({ id: 'new', is_correction: true }),
      },
    ])
    await wrapper.get('[data-test="correct"]').trigger('click')
    await wrapper.get('[data-test="correct-form"]').trigger('submit')
    await flushPromises()

    expect(http.callsTo(`/outcomes/${OUTCOME_ID}/correct`).filter(c => c.method === 'POST'))
      .toHaveLength(1)
  })

  it('retracts an effective outcome on click', async () => {
    const { http, wrapper } = await mountWith(applicationOutcomeList([applicationOutcome()]), [
      {
        match: `/outcomes/${OUTCOME_ID}/retract`,
        method: 'POST',
        json: applicationOutcome({ status: 'RETRACTED', is_effective: false }),
      },
    ])
    await wrapper.get('[data-test="retract"]').trigger('click')
    await flushPromises()

    expect(http.callsTo(`/outcomes/${OUTCOME_ID}/retract`).filter(c => c.method === 'POST'))
      .toHaveLength(1)
  })

  it('keeps superseded and retracted rows visible but not actionable', async () => {
    const { wrapper } = await mountWith(applicationOutcomeList([
      applicationOutcome({ id: OUTCOME_ID, status: 'SUPERSEDED', is_effective: false }),
      applicationOutcome({ id: 'retracted-one', status: 'RETRACTED', is_effective: false }),
    ]))
    expect(wrapper.findAll('[data-status="SUPERSEDED"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-status="RETRACTED"]')).toHaveLength(1)
    // A non-effective row offers neither correct nor retract.
    expect(wrapper.find('[data-test="correct"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="retract"]').exists()).toBe(false)
  })
})
