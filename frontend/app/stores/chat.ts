// The career-chat control plane's client state — the one place a thread's transcript
// and its proposals live while the panel is open.
//
// It follows stores/session.ts's shape (options API: typed state, getters, async
// actions), and it holds server data on purpose for the same reason the map explorer
// does: a chat turn *streams*, so its prose has to accumulate somewhere a component can
// render live, and the proposals a turn produces have to survive a confirm/dismiss that
// re-reads none of the transcript. Everything here is derived from the server — the
// store never decides an outcome, it records the one the server returned.
//
// The phase's rule is visible in what the actions do NOT do. `sendMessage` streams
// prose and appends inert proposals; it changes nothing. `confirmProposal` is the only
// action that can cause a mutation, and even it only *asks*: the server re-runs every
// gate and answers with an audited execution, and the card's new status is read off
// that outcome (`OUTCOME_STATUS`), never assumed from the click. Prose has zero
// authority; a proposal is a request, not a permission (docs/CAREER_CHAT.md).
import { defineStore } from 'pinia'
import type {
  ChatActionExecution,
  ChatActionExecutionOutcome,
  ChatActionProposal,
  ChatActionProposalList,
  ChatActionProposalStatus,
  ChatMessageList,
  Conversation,
  ConversationList,
} from '~/types/v2'
import { apiGet, apiPost } from '~/utils/api-client'
import {
  chatProposal as chatProposalPath,
  conversation as conversationPath,
  V2_ENDPOINTS,
} from '~/utils/endpoints'
import { streamChatTurn } from '~/utils/v2-chat'
import { errorMessage } from '~/utils/v2-errors'

/** One rendered turn in the transcript: prose and who said it. */
export interface ChatTurn {
  role: 'USER' | 'ASSISTANT'
  content: string
}

/**
 * How a confirmed proposal's execution outcome maps to the proposal's resulting status.
 *
 * This is the backend's own contract (`ChatActionProposalStatus`): a `SUCCEEDED`
 * execution leaves the proposal `EXECUTED`, a `REJECTED` one (refused at validation)
 * `REJECTED`, a `FAILED` one (permitted but the service raised) `FAILED`. Reading the
 * status off the outcome is why the store needs no extra round trip after a confirm.
 */
const OUTCOME_STATUS: Record<ChatActionExecutionOutcome, ChatActionProposalStatus> = {
  SUCCEEDED: 'EXECUTED',
  REJECTED: 'REJECTED',
  FAILED: 'FAILED',
}

interface ChatState {
  /** This account's threads, most recent activity first. */
  conversations: Conversation[]
  /** The thread currently open, or null before one is selected. */
  activeId: string | null
  /** The active thread's transcript, oldest first. */
  turns: ChatTurn[]
  /** The active thread's proposals — inert until confirmed. */
  proposals: ChatActionProposal[]
  /** Assistant prose as it streams; committed to `turns` on COMPLETED, then cleared. */
  preview: string
  /** A turn is mid-stream. */
  streaming: boolean
  /** The active thread's transcript is loading. */
  loading: boolean
  /** The last failure, as a sentence a component can show. */
  error: string | null
  /** The most recent confirm's audited outcome, for the feedback line. */
  lastExecution: ChatActionExecution | null
}

export const useChatStore = defineStore('chat', {
  state: (): ChatState => ({
    conversations: [],
    activeId: null,
    turns: [],
    proposals: [],
    preview: '',
    streaming: false,
    loading: false,
    error: null,
    lastExecution: null,
  }),

  getters: {
    activeConversation: (state): Conversation | null =>
      state.conversations.find(c => c.id === state.activeId) ?? null,
    /** The cards still awaiting a decision — the only ones a user may act on. */
    openProposals: (state): ChatActionProposal[] =>
      state.proposals.filter(p => p.status === 'PROPOSED'),
  },

  actions: {
    /** Load this account's threads (most recent first). */
    async loadConversations(): Promise<void> {
      this.error = null
      try {
        const list = await apiGet<ConversationList>(V2_ENDPOINTS.chatConversations)
        this.conversations = list.conversations
      }
      catch (caught) {
        this.error = errorMessage(caught)
      }
    },

    /** Open a new thread, put it at the top of the list and make it active. */
    async startConversation(title?: string): Promise<string | null> {
      this.error = null
      try {
        const created = await apiPost<Conversation>(
          V2_ENDPOINTS.chatConversations, { title: title ?? null })
        this.conversations = [created, ...this.conversations]
        this.activeId = created.id
        this.turns = []
        this.proposals = []
        this.lastExecution = null
        return created.id
      }
      catch (caught) {
        this.error = errorMessage(caught)
        return null
      }
    },

    /** Make `conversationId` active and load its transcript and proposals. */
    async select(conversationId: string): Promise<void> {
      this.activeId = conversationId
      this.turns = []
      this.proposals = []
      this.preview = ''
      this.error = null
      this.lastExecution = null
      this.loading = true
      try {
        const [messages, proposals] = await Promise.all([
          apiGet<ChatMessageList>(
            conversationPath(V2_ENDPOINTS.chatMessages, conversationId)),
          apiGet<ChatActionProposalList>(
            conversationPath(V2_ENDPOINTS.chatProposals, conversationId)),
        ])
        this.turns = messages.messages.map(m => ({ role: m.role, content: m.content }))
        this.proposals = proposals.proposals
      }
      catch (caught) {
        this.error = errorMessage(caught)
      }
      finally {
        this.loading = false
      }
    },

    /**
     * Stream one user turn. The user's line shows at once; the assistant's prose
     * accumulates in `preview` as it arrives and is committed to the transcript by the
     * terminal COMPLETED event, which also carries the (inert, PROPOSED) proposals.
     * Nothing here mutates anything on the server — the turn is prose plus proposals.
     */
    async sendMessage(text: string): Promise<void> {
      const message = text.trim()
      if (!message || this.streaming || this.activeId === null) return
      const path = conversationPath(V2_ENDPOINTS.chatMessages, this.activeId)
      this.turns = [...this.turns, { role: 'USER', content: message }]
      this.preview = ''
      this.error = null
      this.lastExecution = null
      this.streaming = true
      try {
        await streamChatTurn(path, message, {
          onEvent: (event) => {
            if (event.type === 'TOKEN') {
              this.preview += event.text ?? ''
            }
            else if (event.type === 'COMPLETED') {
              if (event.message) {
                this.turns = [
                  ...this.turns,
                  { role: 'ASSISTANT', content: event.message.content },
                ]
              }
              this.proposals = [...this.proposals, ...event.proposals]
              this.preview = ''
            }
            else if (event.type === 'ERROR') {
              this.error = event.error_detail
                ?? 'The assistant could not complete that turn.'
            }
          },
        })
      }
      catch (caught) {
        this.error = errorMessage(caught)
      }
      finally {
        this.streaming = false
        this.preview = ''
      }
    },

    /**
     * Confirm a proposal — the last gate. The server re-runs every check the action
     * would face on its own route and answers with an audited execution; the card's new
     * status follows from that outcome, never from the click. Idempotent by id: a second
     * confirm returns the recorded execution rather than acting again.
     */
    async confirmProposal(proposalId: string): Promise<ChatActionExecution | null> {
      this.error = null
      try {
        const execution = await apiPost<ChatActionExecution>(
          chatProposalPath(V2_ENDPOINTS.chatProposalConfirm, proposalId))
        this.lastExecution = execution
        this.patchProposal(execution.proposal_id, OUTCOME_STATUS[execution.outcome])
        return execution
      }
      catch (caught) {
        this.error = errorMessage(caught)
        return null
      }
    },

    /** Decline a proposal. It leaves the open set without ever reaching a service. */
    async dismissProposal(proposalId: string): Promise<void> {
      this.error = null
      try {
        const updated = await apiPost<ChatActionProposal>(
          chatProposalPath(V2_ENDPOINTS.chatProposalDismiss, proposalId))
        this.replaceProposal(updated)
      }
      catch (caught) {
        this.error = errorMessage(caught)
      }
    },

    /** Set one proposal's status in place — the local half of a confirm's outcome. */
    patchProposal(proposalId: string, status: ChatActionProposalStatus): void {
      this.proposals = this.proposals.map(p =>
        (p.id === proposalId ? { ...p, status } : p))
    },

    /** Replace one proposal wholesale — for a dismiss, whose response is the new row. */
    replaceProposal(proposal: ChatActionProposal): void {
      this.proposals = this.proposals.map(p => (p.id === proposal.id ? proposal : p))
    },
  },
})

