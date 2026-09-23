// The career-chat store: the client half of "prose has zero authority".
//
// Written against the real `fetch` stub, not a mocked transport, because the behaviours
// worth pinning are all about what the store does with what the server returned: a turn
// streams prose into `preview` and commits it on COMPLETED, the proposals it carries stay
// inert, and a confirm's card status is read off the audited outcome — never assumed from
// the click. The three outcomes (SUCCEEDED→EXECUTED, REJECTED→REJECTED, FAILED→FAILED)
// are the store's whole contract with the executor, so each is asserted directly.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useChatStore } from '~/stores/chat'
import type { ChatStreamEvent } from '~/types/v2'
import { stubFetch } from '../support/http'
import {
  chatExecution,
  chatMessage,
  chatMessageList,
  chatProposal,
  chatProposalList,
  conversation,
  conversationList,
} from '../support/v2-fixtures'
import type { Route } from '../support/http'

const CONVERSATIONS = '/api/v2/chat/conversations'
const CONVERSATION_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const PROPOSAL_ID = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'

function store() {
  return useChatStore()
}

/** One `data:`-framed SSE event, exactly as the route serializes it. */
function frame(event: Partial<ChatStreamEvent> & { type: ChatStreamEvent['type'] }): string {
  const full: ChatStreamEvent = {
    type: event.type,
    text: event.text ?? null,
    message: event.message ?? null,
    proposals: event.proposals ?? [],
    error_code: event.error_code ?? null,
    error_detail: event.error_detail ?? null,
  }
  return `data: ${JSON.stringify(full)}\n\n`
}

/**
 * The chat routes, most specific first so `stubFetch`'s first-match wins the collisions
 * (every URL under `/conversations` contains that substring). `sse` is the turn stream;
 * pass it to shape one turn's events.
 */
function chatRoutes(sse: string[] = [frame({ type: 'COMPLETED' })]): Route[] {
  return [
    { match: '/messages', method: 'POST', sse },
    { match: '/messages', method: 'GET', json: chatMessageList() },
    { match: '/confirm', method: 'POST', json: chatExecution() },
    { match: '/dismiss', method: 'POST', json: chatProposal({ status: 'DISMISSED' }) },
    { match: '/proposals', method: 'GET', json: chatProposalList() },
    { match: CONVERSATIONS, method: 'POST', json: conversation() },
    { match: CONVERSATIONS, method: 'GET', json: conversationList() },
  ]
}

describe('chat store', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    store().$reset()
  })

  it('loads this account\'s threads, most recent first', async () => {
    stubFetch(chatRoutes())
    const s = store()
    await s.loadConversations()
    expect(s.conversations.map(c => c.id)).toEqual([CONVERSATION_ID])
  })

  it('records the refusal sentence when loading threads fails', async () => {
    stubFetch([{ match: CONVERSATIONS, method: 'GET', status: 503, json: { error: 'database_unavailable' } }])
    const s = store()
    await s.loadConversations()
    expect(s.error).toBe('The service is temporarily unavailable. Try again in a moment.')
  })

  it('opens a new thread at the top and makes it active with an empty transcript', async () => {
    stubFetch(chatRoutes())
    const s = store()
    s.turns = [{ role: 'USER', content: 'stale' }]
    const id = await s.startConversation()
    expect(id).toBe(CONVERSATION_ID)
    expect(s.activeId).toBe(CONVERSATION_ID)
    expect(s.conversations[0]?.id).toBe(CONVERSATION_ID)
    expect(s.turns).toEqual([])
    expect(s.proposals).toEqual([])
  })

  it('loads a thread\'s transcript and proposals when selected', async () => {
    stubFetch(chatRoutes())
    const s = store()
    await s.select(CONVERSATION_ID)
    expect(s.activeId).toBe(CONVERSATION_ID)
    expect(s.turns).toEqual([{ role: 'USER', content: chatMessage().content }])
    expect(s.proposals.map(p => p.id)).toEqual([PROPOSAL_ID])
  })

  it('streams a turn: user line at once, prose into preview, committed on COMPLETED', async () => {
    const assistant = chatMessage({ role: 'ASSISTANT', content: 'Here is what I found.', sequence: 2 })
    stubFetch(chatRoutes([
      frame({ type: 'TOKEN', text: 'Here is ' }),
      frame({ type: 'TOKEN', text: 'what I found.' }),
      frame({ type: 'COMPLETED', message: assistant, proposals: [chatProposal()] }),
    ]))
    const s = store()
    s.activeId = CONVERSATION_ID

    await s.sendMessage('what should I do?')

    expect(s.turns).toEqual([
      { role: 'USER', content: 'what should I do?' },
      { role: 'ASSISTANT', content: 'Here is what I found.' },
    ])
    // The proposal is appended inert and PROPOSED — the turn changed nothing on its own.
    expect(s.proposals.map(p => p.status)).toEqual(['PROPOSED'])
    expect(s.preview).toBe('')
    expect(s.streaming).toBe(false)
  })

  it('records a mid-turn ERROR event as the store\'s error', async () => {
    stubFetch(chatRoutes([
      frame({ type: 'ERROR', error_code: 'provider_timeout', error_detail: 'the provider timed out' }),
    ]))
    const s = store()
    s.activeId = CONVERSATION_ID
    await s.sendMessage('hello')
    expect(s.error).toBe('the provider timed out')
    expect(s.streaming).toBe(false)
  })

  it('does not send an empty turn or one with no active thread', async () => {
    const http = stubFetch(chatRoutes())
    const s = store()
    await s.sendMessage('   ')
    s.activeId = null
    await s.sendMessage('real text')
    expect(http.callsTo('/messages')).toHaveLength(0)
  })

  // The three outcomes are the executor's contract: the card's next status is read off
  // the audited outcome, never assumed from the confirm click.
  it('confirms a proposal and reads its status off the SUCCEEDED outcome', async () => {
    stubFetch(chatRoutes())
    const s = store()
    s.proposals = [chatProposal()]
    const execution = await s.confirmProposal(PROPOSAL_ID)
    expect(execution?.outcome).toBe('SUCCEEDED')
    expect(s.lastExecution?.outcome).toBe('SUCCEEDED')
    expect(s.proposals[0]?.status).toBe('EXECUTED')
  })

  it('marks a rejected confirm REJECTED — a proposal is not a permission', async () => {
    stubFetch([
      { match: '/confirm', method: 'POST', json: chatExecution({ outcome: 'REJECTED', detail: 'not permitted' }) },
      ...chatRoutes(),
    ])
    const s = store()
    s.proposals = [chatProposal()]
    await s.confirmProposal(PROPOSAL_ID)
    expect(s.proposals[0]?.status).toBe('REJECTED')
  })

  it('marks a permitted-but-failed confirm FAILED', async () => {
    stubFetch([
      { match: '/confirm', method: 'POST', json: chatExecution({ outcome: 'FAILED', detail: 'the service raised' }) },
      ...chatRoutes(),
    ])
    const s = store()
    s.proposals = [chatProposal()]
    await s.confirmProposal(PROPOSAL_ID)
    expect(s.proposals[0]?.status).toBe('FAILED')
  })

  it('replaces a dismissed proposal with the server\'s new row', async () => {
    stubFetch(chatRoutes())
    const s = store()
    s.proposals = [chatProposal()]
    await s.dismissProposal(PROPOSAL_ID)
    expect(s.proposals[0]?.status).toBe('DISMISSED')
  })
})
