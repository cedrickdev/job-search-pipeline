<!--
  LLM providers: the connections a task's prompt may be routed to.

  This screen configures the platform's plumbing, not the candidate's truth. A
  connection is a provider type, an endpoint and a model — with, for a hosted gateway,
  a credential kept encrypted and never shown back (docs/LLM_CONNECTIONS.md). It does
  not run anything: there is no "send a prompt" here. A task that needs a model
  (documents, matching, chat) routes through its own service, which chooses among the
  connections listed here.

  Three things are deliberate.

  **The credential is write-only.** A row never shows a key, only whether one is stored
  ("key set"). The form is where a key is set, rotated or removed; nothing round-trips
  it back onto the screen (§13).

  **Health is a probe, not a stored fact.** "Test" reaches the provider and shows what
  it answered — reachable, unavailable, auth required — as a line on the row. It changes
  nothing and is not remembered across a reload (§37).

  **Exactly one default.** Marking a connection default clears the flag on the rest in
  the same request, so the list always shows a single default a task falls back to when
  a user states no preference.
-->
<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import type { ConnectionFormPayload } from '~/composables/useLlmConnections'
import {
  useLlmConnectionActions,
  useLlmConnectionHealth,
  useLlmConnectionsQuery,
} from '~/composables/useLlmConnections'
import type {
  CreateLLMConnectionRequest,
  LLMConnection,
  LLMConnectionHealth,
  UpdateLLMConnectionRequest,
} from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'

definePageMeta({ middleware: 'auth' })

useHead({ title: 'Providers · Command Center' })

const { data, status, error } = useLlmConnectionsQuery()
const { create, update, remove, setEnabled, setDefault } = useLlmConnectionActions()
const health = useLlmConnectionHealth()

const loading = computed(() => status.value === 'pending' && !data.value)
const connections = computed(() => data.value?.connections ?? [])

// The row currently open for editing, by id; null when the add form is the only one.
const editingId = ref<string | null>(null)
// The row awaiting a delete confirmation, so a destructive click is never one-step.
const confirmingId = ref<string | null>(null)
// The last probe per connection, keyed by id — health is not stored server-side, so
// it lives here for the session only and is dropped on reload (§37).
const probes = reactive<Record<string, LLMConnectionHealth>>({})
// Which connection is being probed now, so only its "Test" button shows "Testing…".
const probingId = ref<string | null>(null)

const listError = computed(() => (error.value ? errorMessage(error.value) : null))

const PROVIDER_LABEL: Record<LLMConnection['provider_type'], string> = {
  CLAUDE_CODE: 'Claude Code',
  CODEX: 'Codex',
  OPENAI_COMPATIBLE: 'OpenAI-compatible',
  LOCAL_OPENAI_COMPATIBLE: 'Local OpenAI-compatible',
}

const HEALTH_LABEL: Record<LLMConnectionHealth['status'], string> = {
  UNKNOWN: 'Unknown',
  HEALTHY: 'Reachable',
  DEGRADED: 'Degraded',
  UNAVAILABLE: 'Unavailable',
  AUTH_REQUIRED: 'Needs credentials',
  MISCONFIGURED: 'Misconfigured',
}

/** Whether a probe result is a clean bill of health, for styling the line. */
function isHealthy(result: LLMConnectionHealth): boolean {
  return result.status === 'HEALTHY'
}

async function onCreate(payload: ConnectionFormPayload) {
  // A create reads the whole shape; `remove_api_key` has no meaning on a new row.
  const body: CreateLLMConnectionRequest = {
    provider_type: payload.provider_type,
    display_name: payload.display_name,
    base_url: payload.base_url,
    model: payload.model,
    api_key: payload.api_key,
    custom_headers: payload.custom_headers,
    enabled: payload.enabled,
    is_default: payload.is_default,
    priority: payload.priority,
  }
  try {
    await create.mutateAsync(body)
  }
  catch {
    // Left as typed, the error rendered under the form.
  }
}

async function onUpdate(connectionId: string, payload: ConnectionFormPayload) {
  // A PATCH cannot change provider_type, enabled or is_default — those are their own
  // endpoints — so the edit sends only the mutable fields and the three-state key.
  const body: UpdateLLMConnectionRequest = {
    display_name: payload.display_name,
    base_url: payload.base_url,
    model: payload.model,
    api_key: payload.api_key,
    remove_api_key: payload.remove_api_key,
    custom_headers: payload.custom_headers,
    priority: payload.priority,
  }
  try {
    await update.mutateAsync({ connectionId, changes: body })
    editingId.value = null
  }
  catch {
    // Left open with the error shown, so the user can correct and retry.
  }
}

async function onDelete(connectionId: string) {
  try {
    await remove.mutateAsync(connectionId)
    confirmingId.value = null
    delete probes[connectionId]
  }
  catch {
    confirmingId.value = null
  }
}

async function onProbe(connectionId: string) {
  probingId.value = connectionId
  try {
    probes[connectionId] = await health.mutateAsync(connectionId)
  }
  catch (probeError) {
    // A misconfigured connection rejects rather than answering; show it like a status.
    probes[connectionId] = {
      status: 'MISCONFIGURED',
      detail: errorMessage(probeError),
      latency_ms: null,
    }
  }
  finally {
    probingId.value = null
  }
}

function toggleEdit(connectionId: string) {
  editingId.value = editingId.value === connectionId ? null : connectionId
  confirmingId.value = null
}
</script>

<template>
  <div class="acct-page">
    <header class="acct-head">
      <h1>Providers</h1>
    </header>

    <p class="settings-hint">
      The LLM connections a task may use — a local CLI, a hosted gateway, or a local
      server. A key is stored encrypted and never shown again; only whether one is set
      is reported back. Nothing here runs a prompt.
    </p>

    <p v-if="loading" class="ov-state">
      Loading connections…
    </p>
    <p v-else-if="listError" class="ov-state ov-error">
      {{ listError }}
    </p>

    <template v-else>
      <section class="settings-card">
        <h2>Connections</h2>
        <p v-if="!connections.length" class="ov-empty">
          No connections yet. Add one below.
        </p>
        <ul v-else class="acct-searches">
          <li v-for="conn in connections" :key="conn.id" class="acct-search">
            <div class="acct-search-row">
              <div class="acct-search-main">
                <span class="acct-search-name">
                  {{ conn.display_name }}
                  <span v-if="conn.is_default" class="acct-live">· default</span>
                </span>
                <span class="acct-search-meta">
                  {{ PROVIDER_LABEL[conn.provider_type] }}
                  <template v-if="conn.base_url">
                    · <code>{{ conn.base_url }}</code>
                  </template>
                  <template v-if="conn.model">
                    · {{ conn.model }}
                  </template>
                  · priority {{ conn.priority }}
                  <template v-if="conn.has_api_key">
                    · key set
                  </template>
                </span>
              </div>
              <span :class="conn.enabled ? 'acct-live' : 'acct-paused'">
                {{ conn.enabled ? 'enabled' : 'disabled' }}
              </span>
            </div>

            <p
              v-if="probes[conn.id]"
              :class="isHealthy(probes[conn.id]!) ? 'settings-ok' : 'settings-err'"
              role="status"
            >
              {{ HEALTH_LABEL[probes[conn.id]!.status] }}
              <template v-if="probes[conn.id]!.detail">
                — {{ probes[conn.id]!.detail }}
              </template>
              <template v-if="probes[conn.id]!.latency_ms !== null">
                ({{ probes[conn.id]!.latency_ms }} ms)
              </template>
            </p>

            <div class="acct-actions">
              <button
                class="btn-ghost"
                type="button"
                :disabled="probingId === conn.id"
                @click="onProbe(conn.id)"
              >
                {{ probingId === conn.id ? 'Testing…' : 'Test' }}
              </button>
              <button
                v-if="!conn.is_default"
                class="btn-ghost"
                type="button"
                :disabled="setDefault.isPending"
                @click="setDefault.mutate(conn.id)"
              >
                Make default
              </button>
              <button
                class="btn-ghost"
                type="button"
                :disabled="setEnabled.isPending"
                @click="setEnabled.mutate({ connectionId: conn.id, enabled: !conn.enabled })"
              >
                {{ conn.enabled ? 'Disable' : 'Enable' }}
              </button>
              <button class="btn-ghost" type="button" @click="toggleEdit(conn.id)">
                {{ editingId === conn.id ? 'Cancel' : 'Edit' }}
              </button>
              <template v-if="confirmingId === conn.id">
                <span class="settings-err">Delete this connection?</span>
                <button
                  class="btn-ghost"
                  type="button"
                  :disabled="remove.isPending"
                  @click="onDelete(conn.id)"
                >
                  Confirm
                </button>
                <button class="btn-ghost" type="button" @click="confirmingId = null">
                  Keep
                </button>
              </template>
              <button
                v-else
                class="btn-ghost"
                type="button"
                @click="confirmingId = conn.id"
              >
                Delete
              </button>
            </div>

            <LlmConnectionForm
              v-if="editingId === conn.id"
              :connection="conn"
              :pending="update.isPending"
              :error="update.error"
              submit-label="Save changes"
              @submit="payload => onUpdate(conn.id, payload)"
            />
          </li>
        </ul>
      </section>

      <section class="settings-card">
        <h2>Add a connection</h2>
        <LlmConnectionForm
          :connection="null"
          :pending="create.isPending"
          :error="create.error"
          submit-label="Add connection"
          @submit="onCreate"
        />
        <p v-if="create.isSuccess && !create.isPending" class="settings-ok">
          Connection added.
        </p>
      </section>
    </template>
  </div>
</template>
