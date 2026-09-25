// The career-chat screen: the control plane as a person operates it.
//
// The page's own responsibilities are what these cover — the store's transport is pinned
// in stores/chat.spec.ts, and this asserts only what the page adds on top: it opens the
// most recent thread on arrival, it renders each proposal as a confirm-gated card, and it
// performs its one side effect — following a *confirmed* NAVIGATE to a route it owns, and
// only then. A rejected confirm navigates nowhere: a proposal is a request, not a
// permission, and the page must not treat a click as an outcome.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mockNuxtImport, mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import ChatPage from '~/pages/chat.vue'
import { useChatStore } from '~/stores/chat'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import {
  chatExecution,
  chatMessageList,
  chatProposal,
  chatProposalList,
  conversation,
  conversationList,
  signedIn,
} from '../support/v2-fixtures'
import type { Route } from '../support/http'
import type { VueWrapper } from '@vue/test-utils'

const { navigateTo } = vi.hoisted(() => ({ navigateTo: vi.fn() }))
mockNuxtImport('navigateTo', () => navigateTo)

function chatRoutes(extra: Route[] = []): Route[] {
  return [
    ...extra,
    { match: '/messages', method: 'GET', json: chatMessageList() },
    { match: '/proposals', method: 'GET', json: chatProposalList() },
    { match: '/api/v2/chat/conversations', method: 'GET', json: conversationList() },
    { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
  ]
}

async function mountChat(extra: Route[] = []) {
  const http = stubFetch(chatRoutes(extra))
  await useSessionStore().ensure()
  const wrapper = await mountSuspended(ChatPage, { route: '/chat' })
  await flushPromises()
  return { http, wrapper }
}

function buttonNamed(wrapper: VueWrapper, label: RegExp) {
  return wrapper.findAll('button').find(b => label.test(b.text()))
}

describe('career-chat page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    navigateTo.mockClear()
    clearNuxtData()
    useSessionStore().$reset()
    useChatStore().$reset()
  })

  it('opens the most recent thread on arrival and shows its transcript', async () => {
    const { wrapper } = await mountChat()
    expect(wrapper.text()).toContain('Which roles near Lausanne should I look at?')
  })

  // The scope badge is display only — it makes the server's wall visible, it does not
  // grant anything. A GLOBAL thread spans the whole account and shows none.
  it('shows no scope badge for a GLOBAL thread', async () => {
    const { wrapper } = await mountChat()
    expect(wrapper.find('.chat__scope').exists()).toBe(false)
  })

  // An anchored thread names the resource it is bound to, so a person sees at a glance
  // that this thread is scoped and cannot reach beyond it.
  it('shows the scope badge for an anchored thread', async () => {
    const { wrapper } = await mountChat([
      { match: '/api/v2/chat/conversations', method: 'GET',
        json: conversationList([conversation({
          scope: 'APPLICATION',
          scope_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        })]) },
    ])
    expect(wrapper.get('.chat__scope').text()).toContain('This application')
  })

  it('lands a first-time visitor on the empty state', async () => {
    const { wrapper } = await mountChat([
      { match: '/api/v2/chat/conversations', method: 'GET', json: conversationList([]) },
    ])
    expect(wrapper.get('.chat__empty').text()).toContain('Start a conversation')
  })

  it('renders each proposal as a confirm-gated card', async () => {
    const { wrapper } = await mountChat()
    expect(wrapper.findAll('.action-card')).toHaveLength(1)
    expect(wrapper.get('.action-card__what').text()).toBe('Go to the opportunity map')
    expect(buttonNamed(wrapper, /Confirm/)).toBeTruthy()
  })

  // The page's only side effect: a confirmed NAVIGATE the server permitted is followed
  // to the route the client owns for that target (OPPORTUNITIES → /map).
  it('follows a confirmed NAVIGATE to the route it owns', async () => {
    const { wrapper } = await mountChat([
      { match: '/confirm', method: 'POST', json: chatExecution() },
    ])
    await buttonNamed(wrapper, /Confirm/)!.trigger('click')
    await flushPromises()
    expect(navigateTo).toHaveBeenCalledWith('/map')
  })

  // A proposal is a request, not a permission: a confirm the server refuses at the gate
  // navigates nowhere, and the card records where it landed.
  it('navigates nowhere when the confirm is rejected at the gate', async () => {
    const { wrapper } = await mountChat([
      { match: '/confirm', method: 'POST', json: chatExecution({ outcome: 'REJECTED', detail: 'not permitted' }) },
    ])
    await buttonNamed(wrapper, /Confirm/)!.trigger('click')
    await flushPromises()
    expect(navigateTo).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('REJECTED')
  })

  it('does not follow a confirmed action that names a target it has no route for', async () => {
    const { wrapper } = await mountChat([
      { match: '/proposals', method: 'GET',
        json: chatProposalList([chatProposal({ action: { kind: 'NAVIGATE', target: 'MATCHES', opportunity_id: null } })]) },
      { match: '/confirm', method: 'POST', json: chatExecution() },
    ])
    await buttonNamed(wrapper, /Confirm/)!.trigger('click')
    await flushPromises()
    expect(navigateTo).not.toHaveBeenCalled()
  })
})
