// One proposed action, as a confirm-gated card — where "prose has zero authority" meets
// the screen. The cases here are the ones the card's rule turns on: the label is derived
// from the *typed* action (never the model's prose), the summary is shown as plain text,
// Confirm/Dismiss are actionable only while PROPOSED, and a terminal proposal shows a
// status badge and no buttons — a stale card cannot act.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import ChatActionCard from '~/components/ChatActionCard.vue'
import { chatProposal } from '../support/v2-fixtures'
import type { VueWrapper } from '@vue/test-utils'

function buttonNamed(wrapper: VueWrapper, label: RegExp) {
  const found = wrapper.findAll('button').find(b => label.test(b.text()))
  if (!found) throw new Error(`no button matching ${label}`)
  return found
}

describe('ChatActionCard', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('labels the action from its typed fields, not from the summary prose', async () => {
    const wrapper = await mountSuspended(ChatActionCard, {
      props: {
        proposal: chatProposal({
          summary: 'Trust me, this just opens a harmless page.',
          action: { kind: 'SET_SEARCH_RADIUS', search_profile_id: 's1', radius_km: 30 },
        }),
      },
    })
    expect(wrapper.get('.action-card__what').text()).toBe('Set the search radius to 30 km')
    // The model's prose is shown, but only as the summary — never as the action's meaning.
    expect(wrapper.get('.action-card__summary').text()).toBe('Trust me, this just opens a harmless page.')
  })

  it('offers Confirm and Dismiss only while the proposal is open', async () => {
    const wrapper = await mountSuspended(ChatActionCard, {
      props: { proposal: chatProposal({ status: 'PROPOSED' }) },
    })
    expect(buttonNamed(wrapper, /Confirm/).exists()).toBe(true)
    expect(buttonNamed(wrapper, /Dismiss/).exists()).toBe(true)
  })

  it('emits confirm and dismiss to the page rather than acting itself', async () => {
    const wrapper = await mountSuspended(ChatActionCard, {
      props: { proposal: chatProposal() },
    })
    await buttonNamed(wrapper, /Confirm/).trigger('click')
    await buttonNamed(wrapper, /Dismiss/).trigger('click')
    expect(wrapper.emitted('confirm')).toHaveLength(1)
    expect(wrapper.emitted('dismiss')).toHaveLength(1)
  })

  // A terminal proposal is history: the buttons are gone and a badge shows where it
  // landed, so a stale card can never be confirmed a second time.
  it('shows a status badge and no buttons once a decision has been recorded', async () => {
    const wrapper = await mountSuspended(ChatActionCard, {
      props: { proposal: chatProposal({ status: 'EXECUTED' }) },
    })
    expect(wrapper.text()).toContain('EXECUTED')
    expect(wrapper.findAll('button').some(b => /Confirm|Dismiss/.test(b.text()))).toBe(false)
    expect(wrapper.get('.action-card__note').text()).toContain('Nothing changes without your confirmation')
  })

  it('disables the buttons while a decision is in flight', async () => {
    const wrapper = await mountSuspended(ChatActionCard, {
      props: { proposal: chatProposal(), busy: true },
    })
    expect(buttonNamed(wrapper, /Dismiss/).attributes('disabled')).toBeDefined()
  })
})
