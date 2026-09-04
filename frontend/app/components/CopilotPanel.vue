<!--
  Copilot chat panel. Ported from webapp/src/components/CopilotPanel.tsx.

  Streams the turn over POST /api/chat, shows an ephemeral token preview, then
  replaces it with the mandate-sanitized `done` text — the preview is never kept,
  because the tokens are pre-gate prose. Copilot-proposed actions render as
  confirm-gated cards: nothing changes state until the user clicks Confirm, which
  POSTs to the typed endpoint (re-validated server-side). A reply that fails the
  mandate gate is shown with a warning, never silently surfaced as trustworthy.

  Assistant prose goes through `renderMarkdown` (markdown-it, `html: false`) into
  `v-html`. V1's react-markdown could not emit raw HTML by construction; with a
  string sink that guarantee has to come from the renderer, so it does — see
  app/utils/markdown.ts.

  The scope watcher resets every transient bit of state before rehydrating: the
  drawer mounts this panel without a `key`, so switching jobs reuses the same
  instance and the previous job's thread would otherwise bleed through. The
  cancelled flag and the `messages.length === 0` guard together keep a reply the
  user started before history resolved.
-->
<script setup lang="ts">
import { ref, watch } from 'vue'
import { apiPost } from '~/utils/api-client'
import {
  actionEndpoint,
  fetchChatHistory,
  streamChat,
  type ActionProposal,
  type ChatDone,
  type ChatEvent,
  type ChatScope,
} from '~/utils/chat'
import { invalidate } from '~/composables/useApiQuery'
import { renderMarkdown } from '~/utils/markdown'

const props = withDefaults(defineProps<{ scope?: ChatScope, scopeId?: number }>(), {
  scope: 'global',
  scopeId: 0,
})

interface Msg {
  role: 'user' | 'assistant'
  text: string
}

const input = ref('')
const messages = ref<Msg[]>([])
const preview = ref('')
const proposals = ref<ActionProposal[]>([])
const warning = ref<string[] | null>(null)
const error = ref<string | null>(null)
const busy = ref(false)
const editing = ref<{ idx: number, text: string } | null>(null)

watch(
  () => [props.scope, props.scopeId] as const,
  ([scope, scopeId], _old, onCleanup) => {
    let cancelled = false
    onCleanup(() => {
      cancelled = true
    })
    messages.value = []
    proposals.value = []
    preview.value = ''
    warning.value = null
    error.value = null
    fetchChatHistory(scope, scopeId)
      .then((history) => {
        if (cancelled || history.length === 0) return
        if (messages.value.length === 0) {
          messages.value = history.map(m => ({ role: m.role, text: m.text }))
        }
      })
      .catch(() => {
        /* no history / fetch failed: start empty */
      })
  },
  { immediate: true },
)

async function send() {
  const message = input.value.trim()
  if (!message || busy.value) return
  messages.value = [...messages.value, { role: 'user', text: message }]
  input.value = ''
  preview.value = ''
  warning.value = null
  error.value = null
  busy.value = true
  try {
    await streamChat(
      { message, scope: props.scope, scope_id: props.scopeId },
      {
        onEvent: (ev: ChatEvent) => {
          if (ev.event === 'token') {
            preview.value += String(ev.data ?? '')
          } else if (ev.event === 'action_proposal') {
            proposals.value = [...proposals.value, ev.data as ActionProposal]
          } else if (ev.event === 'done') {
            const d = ev.data as ChatDone
            messages.value = [...messages.value, { role: 'assistant', text: d.text }]
            preview.value = ''
            if (!d.mandate_ok) warning.value = d.flags ?? []
          } else if (ev.event === 'error') {
            error.value = String(ev.data)
          }
        },
      },
    )
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'chat failed'
  } finally {
    busy.value = false
  }
}

async function confirmAction(idx: number, p: ActionProposal) {
  // Fold in any inline edits to the args before resolving the endpoint.
  let effective = p
  const edit = editing.value
  if (edit && edit.idx === idx) {
    try {
      effective = { ...p, args: JSON.parse(edit.text) }
    } catch {
      /* invalid JSON: fall back to the original args */
    }
  }
  const ep = actionEndpoint(effective)
  if (!ep) return
  await apiPost(ep.path, ep.body)
  proposals.value = proposals.value.filter((_, i) => i !== idx)
  editing.value = null
  // Confirm feedback at the trigger point: a regen only queues a request the
  // keyless background run renders later, so say so honestly; other actions
  // apply immediately.
  const what = effective.label || effective.type
  // Echo the inferred boldness level so the user sees the copilot understood
  // "be bold" / "play it safe" before the keyless run renders the CV.
  const creativity = effective.args?.creativity
  const how
    = effective.type === 'regen' && typeof creativity === 'string' ? ` (${creativity})` : ''
  const note
    = effective.type === 'regen'
      ? `✓ ${what}${how} — queued. The new CV will render on the next run.`
      : `✓ ${what} — done.`
  messages.value = [...messages.value, { role: 'assistant', text: note }]
  await invalidate('jobs', 'overview', `job:${effective.job_id}`, `prep:${effective.job_id}`)
}

function dismiss(idx: number) {
  proposals.value = proposals.value.filter((_, i) => i !== idx)
  if (editing.value?.idx === idx) editing.value = null
}

function toggleEdit(idx: number, p: ActionProposal) {
  editing.value
    = editing.value?.idx === idx ? null : { idx, text: JSON.stringify(p.args ?? {}, null, 2) }
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void send()
  }
}

function appendTranscript(text: string) {
  input.value = (input.value ? `${input.value} ` : '') + text
}
</script>

<template>
  <section class="copilot" aria-label="Copilot">
    <div class="copilot-log">
      <!-- Assistant prose is Markdown (bold, lists, code); the user's own text stays literal. -->
      <div v-for="(m, i) in messages" :key="i" class="copilot-msg" :class="m.role">
        <!-- eslint-disable-next-line vue/no-v-html -->
        <div v-if="m.role === 'assistant'" class="md" v-html="renderMarkdown(m.text)" />
        <template v-else>
          {{ m.text }}
        </template>
      </div>

      <div v-if="busy && preview" class="copilot-msg assistant preview">
        <!-- eslint-disable-next-line vue/no-v-html -->
        <div class="md" v-html="renderMarkdown(preview)" />
      </div>
      <div v-else-if="busy" class="copilot-msg assistant preview">
        …
      </div>

      <div v-if="warning" class="copilot-warning" role="alert">
        ⚠ Reply did not clear the safety gate — not saved.
        <template v-if="warning.length > 0">
          Flags: {{ warning.join(', ') }}.
        </template>
      </div>
      <div v-if="error" class="copilot-warning" role="alert">
        ⚠ {{ error }}
      </div>

      <div
        v-for="(p, idx) in proposals"
        :key="idx"
        class="copilot-action"
        role="group"
        aria-label="Proposed action"
      >
        <div class="copilot-action-label">
          {{ p.label || `${p.type} · job ${p.job_id}` }}
        </div>
        <textarea
          v-if="editing?.idx === idx"
          v-model="editing.text"
          class="copilot-action-edit"
          aria-label="Edit action"
        />
        <div v-if="actionEndpoint(p) === null" class="copilot-action-note">
          Not available yet.
        </div>
        <div class="copilot-action-btns">
          <button
            class="btn-primary"
            :disabled="actionEndpoint(p) === null"
            @click="confirmAction(idx, p)"
          >
            Confirm
          </button>
          <button class="btn-ghost" @click="toggleEdit(idx, p)">
            Edit
          </button>
          <button class="btn-ghost" @click="dismiss(idx)">
            Dismiss
          </button>
        </div>
      </div>
    </div>

    <div class="copilot-compose">
      <textarea
        v-model="input"
        aria-label="Message copilot"
        class="copilot-input"
        placeholder="Ask the copilot…"
        @keydown="onKeydown"
      />
      <MicButton @transcript="appendTranscript" />
      <button class="btn-primary" :disabled="busy || !input.trim()" @click="send">
        Ask
      </button>
    </div>
  </section>
</template>
