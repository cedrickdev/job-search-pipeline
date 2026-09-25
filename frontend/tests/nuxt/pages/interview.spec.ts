// The interview-practice screen: what the page adds on top of the store.
//
// The store's transport is pinned in stores/interview.spec.ts; this asserts only the page's
// own work — it opens the most recent session on arrival, renders the current question and
// the readiness as a coaching signal (never a forecast), builds the opportunity picker from
// the account's applications, and lets a typed answer earn coaching in place. The phase's
// rule is checked where a person would see it broken: the panel shows dimensions, strengths
// and improvements, and nowhere a probability or a verdict.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import InterviewPage from '~/pages/interview.vue'
import { useInterviewStore } from '~/stores/interview'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import {
  applicationList,
  interviewDetail,
  interviewOutcome,
  interviewSessionList,
  interviewSummaryList,
  profile,
  sessionReadiness,
  signedIn,
} from '../support/v2-fixtures'
import type { Route } from '../support/http'
import type { VueWrapper } from '@vue/test-utils'

const LINKED_OPPORTUNITY_ID = '66666666-6666-4666-8666-666666666666'

/**
 * The page's reads, most specific first so `stubFetch`'s first-match wins the collisions:
 * every URL under `/interview-sessions` contains that substring, so `/history`, `/detail`,
 * `/readiness` and `/answers` all precede the bare list. `extra` is prepended to shape a case.
 */
function interviewRoutes(extra: Route[] = []): Route[] {
  return [
    ...extra,
    { match: '/me/profile', method: 'GET', json: profile() },
    { match: '/interview-sessions/history', method: 'GET', json: interviewSummaryList() },
    { match: '/detail', method: 'GET', json: interviewDetail() },
    { match: '/readiness', method: 'GET', json: sessionReadiness() },
    { match: '/answers', method: 'POST', json: interviewOutcome() },
    { match: '/api/v2/applications', method: 'GET', json: applicationList() },
    { match: '/api/v2/interview-sessions', method: 'GET', json: interviewSessionList() },
    { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
  ]
}

// The page seeds a shared (Pinia) store on mount; a page left mounted from an earlier test
// would react to the next test's store writes and fire ghost reads. Unmount after each.
const mounted: VueWrapper[] = []

async function mountInterview(extra: Route[] = [], route = '/interview') {
  const http = stubFetch(interviewRoutes(extra))
  await useSessionStore().ensure()
  const wrapper = await mountSuspended(InterviewPage, { route })
  mounted.push(wrapper)
  await flushPromises()
  return { http, wrapper }
}

describe('interview page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    useSessionStore().$reset()
    useInterviewStore().$reset()
  })

  afterEach(() => {
    mounted.forEach(wrapper => wrapper.unmount())
    mounted.length = 0
  })

  it('opens the most recent session on arrival and shows its current question', async () => {
    const { wrapper } = await mountInterview()
    expect(wrapper.text()).toContain('Tell me about a time you took ownership')
    // The head badge reflects the detail the panel loaded, not the list summary.
    expect(wrapper.get('.sim__head .sim__badge').text()).toContain('IN_PROGRESS')
  })

  it('shows readiness as a coaching signal, never a hiring forecast', async () => {
    const { wrapper } = await mountInterview()
    expect(wrapper.get('.sim__note').text()).toContain('not a hiring forecast')
    expect(wrapper.text()).not.toMatch(/probability|verdict|likelihood|% chance/i)
  })

  it('lands a first-time visitor on the empty state', async () => {
    // A first visit has neither sessions nor a readiness trend. The history override
    // precedes the bare list so it is not shadowed — its URL contains `/interview-sessions`.
    const { wrapper } = await mountInterview([
      { match: '/interview-sessions/history', method: 'GET', json: interviewSummaryList([]) },
      { match: '/api/v2/interview-sessions', method: 'GET', json: interviewSessionList([]) },
    ])
    expect(wrapper.get('.sim__empty').text()).toContain('Start a session')
  })

  it('builds the opportunity picker from the account\'s applications', async () => {
    const { wrapper } = await mountInterview()
    const options = wrapper.findAll('[aria-label="Opportunity to practice for"] option')
    expect(options.map(o => o.text())).toContain('PLANNED · 55555555')
  })

  it('preselects the opportunity a ?opportunity= link names', async () => {
    const { wrapper } = await mountInterview(
      [{ match: '/api/v2/applications', method: 'GET', json: applicationList([]) }],
      `/interview?opportunity=${LINKED_OPPORTUNITY_ID}`)
    const options = wrapper.findAll('[aria-label="Opportunity to practice for"] option')
    expect(options.map(o => o.text())).toContain(`Linked · ${LINKED_OPPORTUNITY_ID.slice(0, 8)}`)
  })

  it('submits a typed answer and shows its coaching in place', async () => {
    const { wrapper } = await mountInterview()
    await wrapper.get('textarea').setValue('I owned the flaky-deploy fix end to end.')
    await wrapper.get('form.sim__compose').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('Coaching')
    expect(wrapper.text()).toContain('Owned a concrete outcome end to end.')
    // The question is answered, so the composer is gone and the loop waits on the next ask.
    expect(wrapper.find('form.sim__compose').exists()).toBe(false)
  })
})
