<!--
  Career chat: the control plane, as a person operates it.

  One screen, two halves. The left is this account's threads; the right is the open
  thread — its transcript, the proposals its turns produced, and a composer. The whole
  screen is built around one rule, and it is visible in the layout: prose and proposals
  are shown in *different* places and treated differently. Assistant prose renders as
  Markdown and can say anything; it changes nothing. A proposal is a separate,
  confirm-gated card, and confirming it asks the server — which re-runs every gate the
  action would face on its own route — rather than trusting the chat.

  So the only side effect this page performs on its own is navigation, and only after a
  `NAVIGATE` proposal was *confirmed* and the server returned `SUCCEEDED`: the client
  then follows the target to a route it owns, ignoring any it does not recognise
  (`navigationRoute`). Everything else is the store asking the server and rendering the
  answer (docs/CAREER_CHAT.md).
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useChatStore } from '~/stores/chat'
import type { ChatActionProposal } from '~/types/v2'
import { renderMarkdown } from '~/utils/markdown'
import { navigationRoute } from '~/utils/v2-chat'

definePageMeta({ middleware: 'auth' })

useHead({ title: 'Career chat · Command Center' })

const store = useChatStore()
const draft = ref('')
// The proposal mid-decision, so only its card shows a pending state.
const busyProposalId = ref<string | null>(null)

// Open the most recent thread on arrival, if there is one; a first-time visitor lands
// on the empty state and starts one with their first message.
onMounted(async () => {
  await store.loadConversations()
  const first = store.conversations[0]
  if (first) await store.select(first.id)
})

// The last confirm's audited outcome, as a one-line notice: a plain "done" for a
// SUCCEEDED action, and the server's own secret-free detail for a REJECTED (refused at
// the gate) or FAILED one — never invented, only what the execution carried.
const executionNotice = computed(() => {
  const execution = store.lastExecution
  if (!execution) return null
  if (execution.outcome === 'SUCCEEDED') {
    return { tone: 'success' as const, text: execution.detail ?? 'Done.' }
  }
  const lead = execution.outcome === 'REJECTED'
    ? 'This action was not permitted.'
    : 'This action could not be completed.'
  return {
    tone: 'error' as const,
    text: execution.detail ? `${lead} ${execution.detail}` : lead,
  }
})

// Send the composed turn. When there is no open thread yet, one is opened first, so a
// first-time visitor's first message just works.
async function onSubmit(): Promise<void> {
  const text = draft.value.trim()
  if (!text || store.streaming) return
  if (store.activeId === null && (await store.startConversation()) === null) return
  draft.value = ''
  await store.sendMessage(text)
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void onSubmit()
  }
}

// Confirm a proposal, then — only if it was a NAVIGATE the server actually permitted —
// follow it to the route it names. A rejected or failed confirm navigates nowhere.
async function confirmProposal(proposal: ChatActionProposal): Promise<void> {
  busyProposalId.value = proposal.id
  try {
    const execution = await store.confirmProposal(proposal.id)
    if (execution?.outcome === 'SUCCEEDED' && proposal.action.kind === 'NAVIGATE') {
      const route = navigationRoute(proposal.action.target)
      if (route) await navigateTo(route)
    }
  }
  finally {
    busyProposalId.value = null
  }
}

async function dismissProposal(proposal: ChatActionProposal): Promise<void> {
  busyProposalId.value = proposal.id
  try {
    await store.dismissProposal(proposal.id)
  }
  finally {
    busyProposalId.value = null
  }
}</script>

<template>
  <section class="chat">
    <aside class="chat__threads">
      <div class="chat__threads-head">
        <h1>Career chat</h1>
        <UButton
          size="xs"
          icon="i-heroicons-plus"
          aria-label="New conversation"
          @click="() => store.startConversation()"
        >
          New
        </UButton>
      </div>
      <p v-if="store.conversations.length === 0" class="chat__muted">
        No conversations yet.
      </p>
      <ul v-else class="chat__thread-list">
        <li v-for="c in store.conversations" :key="c.id">
          <button
            type="button"
            class="chat__thread"
            :class="{ 'chat__thread--active': c.id === store.activeId }"
            @click="() => store.select(c.id)"
          >
            {{ c.title }}
          </button>
        </li>
      </ul>
    </aside>

    <div class="chat__panel">
      <p v-if="store.error" class="chat__error" role="alert">
        {{ store.error }}
      </p>

      <div v-if="store.activeId === null" class="chat__empty">
        Start a conversation to ask the assistant about your search, your applications
        or your documents. It can suggest actions; nothing happens until you confirm.
      </div>

      <template v-else>
        <div class="chat__log">
          <div
            v-for="(turn, i) in store.turns"
            :key="i"
            class="chat__turn"
            :class="turn.role.toLowerCase()"
          >
            <!-- eslint-disable-next-line vue/no-v-html -->
            <div v-if="turn.role === 'ASSISTANT'" class="md" v-html="renderMarkdown(turn.content)" />
            <template v-else>
              {{ turn.content }}
            </template>
          </div>

          <div v-if="store.streaming && store.preview" class="chat__turn assistant preview">
            <!-- eslint-disable-next-line vue/no-v-html -->
            <div class="md" v-html="renderMarkdown(store.preview)" />
          </div>
          <div v-else-if="store.streaming" class="chat__turn assistant preview">
            …
          </div>

          <div
            v-if="executionNotice"
            class="chat__notice"
            :class="executionNotice.tone"
            role="status"
          >
            {{ executionNotice.text }}
          </div>

          <ChatActionCard
            v-for="p in store.proposals"
            :key="p.id"
            :proposal="p"
            :busy="busyProposalId === p.id"
            @confirm="() => confirmProposal(p)"
            @dismiss="() => dismissProposal(p)"
          />
        </div>

        <form class="chat__compose" @submit.prevent="onSubmit">
          <textarea
            v-model="draft"
            class="chat__input"
            aria-label="Message the assistant"
            placeholder="Ask about your search, applications or documents…"
            @keydown="onKeydown"
          />
          <UButton
            type="submit"
            :loading="store.streaming"
            :disabled="store.streaming || !draft.trim()"
          >
            Send
          </UButton>
        </form>
      </template>
    </div>
  </section>
</template>

<style scoped>
.chat { display: grid; grid-template-columns: 16rem 1fr; gap: 1rem; height: calc(100vh - 8rem); min-height: 24rem; }
.chat__threads { display: flex; flex-direction: column; gap: 0.75rem; border-right: 1px solid var(--ui-border, #e5e7eb); padding-right: 1rem; overflow-y: auto; }
.chat__threads-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.chat__threads-head h1 { font-size: 1.1rem; font-weight: 600; margin: 0; }
.chat__muted { font-size: 0.85rem; color: var(--ui-text-muted, #6b7280); margin: 0; }
.chat__thread-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.25rem; }
.chat__thread { width: 100%; text-align: left; padding: 0.4rem 0.6rem; border-radius: 0.375rem; background: transparent; border: none; cursor: pointer; font: inherit; color: inherit; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chat__thread:hover { background: var(--ui-bg-elevated, #f3f4f6); }
.chat__thread--active { background: var(--ui-bg-elevated, #eef2ff); font-weight: 600; }
.chat__panel { display: flex; flex-direction: column; gap: 0.75rem; min-height: 0; }
.chat__error { color: var(--ui-error, #dc2626); font-size: 0.9rem; margin: 0; }
.chat__empty { color: var(--ui-text-muted, #6b7280); max-width: 32rem; }
.chat__log { flex: 1; display: flex; flex-direction: column; gap: 0.75rem; overflow-y: auto; padding-right: 0.25rem; }
.chat__turn { max-width: 42rem; padding: 0.5rem 0.75rem; border-radius: 0.5rem; white-space: pre-wrap; word-break: break-word; }
.chat__turn.user { align-self: flex-end; background: var(--ui-bg-elevated, #eef2ff); }
.chat__turn.assistant { align-self: flex-start; background: var(--ui-bg-elevated, #f3f4f6); }
.chat__turn.preview { opacity: 0.75; }
.chat__notice { font-size: 0.85rem; padding: 0.4rem 0.6rem; border-radius: 0.375rem; }
.chat__notice.success { background: #ecfdf5; color: #047857; }
.chat__notice.error { background: #fef2f2; color: #b91c1c; }
.chat__compose { display: flex; gap: 0.5rem; align-items: flex-end; }
.chat__input { flex: 1; resize: vertical; min-height: 3rem; max-height: 12rem; padding: 0.5rem 0.75rem; border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.5rem; font: inherit; }
.md :deep(p) { margin: 0 0 0.5rem; }
.md :deep(p:last-child) { margin-bottom: 0; }
.md :deep(pre) { overflow-x: auto; padding: 0.5rem; border-radius: 0.375rem; background: var(--ui-bg-elevated, #f3f4f6); }
</style>