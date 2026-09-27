// The strategy screen: the spine's only mutation, gated behind a human.
//
// The load-bearing assertion is the second gate. A change that loosens a safety brake is
// refused by the server with `sensitive_confirmation_required` on a plain approval; the
// card must catch exactly that and ask for a deliberate second "yes" that carries
// `confirm_sensitive`. This is the acceptance rule "the system never silently expands the
// user's application policy", proven end to end: a first approval sent without the flag, a
// refusal, then a second approval that carries it. The rest pins the queue's shape — an
// open proposal offers approve and dismiss, an empty queue points at the career surface,
// and settled proposals fall to the history.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import StrategyPage from '~/pages/strategy.vue'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import {
  signedIn,
  strategyExecution,
  strategyProposal,
  strategyProposalList,
} from '../support/v2-fixtures'
import type { Route } from '../support/http'
import type { StrategyChangeProposalList } from '~/types/v2'

const PENDING = '/api/v2/career/strategy-proposals'
const HISTORY = '/api/v2/career/strategy-proposals/history'

interface StrategyOpts {
  pending?: StrategyChangeProposalList
  history?: StrategyChangeProposalList
  extra?: Route[]
}

function routes(opts: StrategyOpts = {}): Route[] {
  return [
    ...(opts.extra ?? []),
    // History precedes pending: the history URL contains the pending path as a
    // substring, and stubFetch takes the first route that matches.
    { match: HISTORY, method: 'GET', json: opts.history ?? strategyProposalList([]) },
    { match: PENDING, method: 'GET', json: opts.pending ?? strategyProposalList([]) },
    { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
  ]
}

async function mountWith(opts: StrategyOpts = {}) {
  const http = stubFetch(routes(opts))
  await useSessionStore().ensure()
  const wrapper = await mountSuspended(StrategyPage, { route: '/strategy' })
  await flushPromises()
  return { http, wrapper }
}

describe('strategy page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('lists an open proposal with its two verbs', async () => {
    const { wrapper } = await mountWith({ pending: strategyProposalList([strategyProposal()]) })
    expect(wrapper.get('[data-test="pending"]').text()).toContain('platform engineer')
    expect(wrapper.find('[data-test="approve"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="dismiss"]').exists()).toBe(true)
  })

  it('tells an empty queue there is nothing to review and points at the career surface', async () => {
    const { wrapper } = await mountWith()
    expect(wrapper.get('[data-test="pending"]').text()).toContain('Nothing to review')
    const link = wrapper.findAll('a').find(a => a.attributes('href') === '/career')
    expect(link).toBeTruthy()
  })

  it('shows settled proposals in the history', async () => {
    const dismissed = strategyProposal({ status: 'DISMISSED', is_open: false })
    const { wrapper } = await mountWith({ history: strategyProposalList([dismissed]) })
    expect(wrapper.find('[data-test="history"]').exists()).toBe(true)
    expect(wrapper.get('[data-test="history"]').text().toLowerCase()).toContain('dismissed')
  })

  it('approves a non-sensitive proposal without a second gate', async () => {
    const { http, wrapper } = await mountWith({
      pending: strategyProposalList([strategyProposal()]),
      extra: [{ match: '/approve', method: 'POST', json: strategyExecution() }],
    })
    await wrapper.get('[data-test="approve"]').trigger('click')
    await flushPromises()

    const approvals = http.callsTo('/approve').filter(c => c.method === 'POST')
    expect(approvals).toHaveLength(1)
    expect(JSON.parse(approvals[0]!.init!.body as string).confirm_sensitive).toBe(false)
    expect(wrapper.find('[data-test="confirm-sensitive"]').exists()).toBe(false)
  })

  it('requires a deliberate second confirmation to loosen a safety limit', async () => {
    const sensitive = strategyProposal({
      is_sensitive: true,
      change_kind: 'SET_MINIMUM_SCORE',
      target: 'APPLICATION_POLICY',
      summary: 'Lower the minimum score from 0.7 to 0.6.',
    })
    const { http, wrapper } = await mountWith({
      pending: strategyProposalList([sensitive]),
      extra: [{
        match: '/approve',
        method: 'POST',
        respond: (_url, init) => {
          const body = JSON.parse((init?.body as string) ?? '{}')
          if (body.confirm_sensitive) {
            return new Response(JSON.stringify(strategyExecution()), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            })
          }
          return new Response(
            JSON.stringify({ error: 'sensitive_confirmation_required', detail: 'Confirm again.' }),
            { status: 409, headers: { 'Content-Type': 'application/json' } },
          )
        },
      }],
    })

    // A plain approval is refused, and the card asks a second time.
    await wrapper.get('[data-test="approve"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="confirm-sensitive"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('loosens a safety limit')

    // Only the deliberate second yes carries confirm_sensitive.
    await wrapper.get('[data-test="confirm-sensitive"]').trigger('click')
    await flushPromises()

    const approvals = http.callsTo('/approve').filter(c => c.method === 'POST')
    expect(approvals).toHaveLength(2)
    expect(JSON.parse(approvals[0]!.init!.body as string).confirm_sensitive).toBe(false)
    expect(JSON.parse(approvals[1]!.init!.body as string).confirm_sensitive).toBe(true)
  })

  it('dismisses a proposal on click', async () => {
    const { http, wrapper } = await mountWith({
      pending: strategyProposalList([strategyProposal()]),
      extra: [{
        match: '/dismiss',
        method: 'POST',
        json: strategyProposal({ status: 'DISMISSED', is_open: false }),
      }],
    })
    await wrapper.get('[data-test="dismiss"]').trigger('click')
    await flushPromises()
    expect(http.callsTo('/dismiss').filter(c => c.method === 'POST')).toHaveLength(1)
  })
})
